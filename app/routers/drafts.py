"""
AI Draft Orders & Confirmation Workflow Router (Milestones 3 & 4).

Coordinates natural-language draft order generation, catalog resolution,
clarification feedback, and human confirmation / rejection workflows.
"""
from collections import defaultdict
from decimal import Decimal
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    Customer,
    DraftItemMatchStatus,
    DraftOrder,
    DraftOrderItem,
    DraftOrderStatus,
    Order,
    OrderItem,
    OrderStatus,
    Product,
)
from app.schemas import (
    DraftOrderConfirmResponse,
    DraftOrderCreate,
    DraftOrderResponse,
    OrderResponse,
)
from app.services.ai_service import (
    build_draft_order_from_parsed,
    parse_customer_message_with_llm,
)

router = APIRouter()


@router.post(
    "/",
    response_model=DraftOrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate an AI draft order from a customer message",
)
def create_draft_order(
    draft_in: DraftOrderCreate,
    db: Session = Depends(get_db),
) -> DraftOrder:
    """
    Parse an unstructured customer message using AI, match requested items against catalog,
    and persist an AI-generated draft order.

    Safety & Reliability Rules:
    1. Validates that the customer exists.
    2. Calls the LLM to extract product references and quantities.
    3. Handles unknown products, ambiguous matches, and missing quantities by flagging
       items and setting draft status to REQUIRES_CLARIFICATION.
    4. IMMUTABLE INVENTORY: Draft generation NEVER deducts stock or creates a confirmed order.
    """
    # 1. Validate customer existence
    customer = db.query(Customer).filter(Customer.id == draft_in.customer_id).first()
    if not customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Customer with ID {draft_in.customer_id} not found.",
        )

    # 2. Extract structured order intent using AI service (handles timeouts and malformed responses)
    parsed_request = parse_customer_message_with_llm(draft_in.message)

    # 3. Match items against actual catalog and build DraftOrder model
    draft_order = build_draft_order_from_parsed(
        customer_id=customer.id,
        raw_message=draft_in.message,
        parsed=parsed_request,
        db=db,
    )

    # 4. Persist draft without modifying product stock or confirmed orders
    db.add(draft_order)
    db.commit()
    db.refresh(draft_order)
    return draft_order


@router.get(
    "/",
    response_model=List[DraftOrderResponse],
    summary="List draft orders with filters and pagination",
)
def list_draft_orders(
    status_filter: Optional[DraftOrderStatus] = Query(
        None,
        alias="status",
        description="Filter drafts by status (PENDING_CONFIRMATION, REQUIRES_CLARIFICATION, etc.)",
    ),
    customer_id: Optional[int] = Query(
        None,
        description="Filter drafts by customer ID",
    ),
    skip: int = Query(
        0,
        ge=0,
        description="Number of records to skip (offset)",
    ),
    limit: int = Query(
        20,
        ge=1,
        le=100,
        description="Maximum number of records to return (1-100)",
    ),
    db: Session = Depends(get_db),
) -> List[DraftOrder]:
    """
    Retrieve a paginated list of draft orders with optional status and customer filters.
    """
    # Normalize default Query objects if invoked directly in unit tests
    status_val = status_filter if isinstance(status_filter, DraftOrderStatus) else None
    cust_id_val = customer_id if isinstance(customer_id, int) else None
    skip_val = skip if isinstance(skip, int) else 0
    limit_val = limit if isinstance(limit, int) else 20

    query = db.query(DraftOrder)

    if status_val is not None:
        query = query.filter(DraftOrder.status == status_val)

    if cust_id_val is not None:
        query = query.filter(DraftOrder.customer_id == cust_id_val)

    drafts = query.order_by(DraftOrder.id.desc()).offset(skip_val).limit(limit_val).all()
    return drafts


@router.get(
    "/{draft_id}",
    response_model=DraftOrderResponse,
    summary="Get draft order by ID",
)
def get_draft_order(
    draft_id: int,
    db: Session = Depends(get_db),
) -> DraftOrder:
    """
    Retrieve details for a single draft order, including all extracted items,
    match statuses, candidate suggestions, and clarification notes.
    """
    draft = db.query(DraftOrder).filter(DraftOrder.id == draft_id).first()
    if not draft:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Draft order with ID {draft_id} not found.",
        )
    return draft


@router.post(
    "/{draft_id}/confirm",
    response_model=DraftOrderConfirmResponse,
    status_code=status.HTTP_200_OK,
    summary="Confirm an AI draft order and convert to a confirmed order",
)
def confirm_draft_order(
    draft_id: int,
    confirm_price_change: bool = Query(
        False,
        description="Explicit human approval to proceed if product prices have changed since draft creation.",
    ),
    db: Session = Depends(get_db),
) -> DraftOrderConfirmResponse:
    """
    Confirm an AI-generated draft order and convert it into a confirmed purchase order.

    Business Rules:
    1. Validates that the draft order exists (404).
    2. Validates idempotency: if already confirmed or linked to an order, rejects with 400.
    3. Validates that draft status is PENDING_CONFIRMATION (rejects REJECTED or REQUIRES_CLARIFICATION with 400).
    4. Validates customer existence (404).
    5. Validates draft items: must be non-empty, all items must have MATCHED status, valid product_id, and positive quantity.
    6. Validates product availability: all referenced products must exist and be active (is_active=True).
    7. Validates current pricing: compares item.unit_price against current catalog price.
       If price has changed since draft creation, requires renewed human approval (confirm_price_change=True).
       Without approval, raises 400 Bad Request and does not deduct stock or create order.
    8. Validates total stock sufficiency: verifies enough stock is available for the requested quantities.
    9. Atomic Execution: stock deduction, Order creation, OrderItem creation, and DraftOrder status update
       occur in a single transaction. If any failure happens, all changes are rolled back.
    """
    # 1. Fetch draft order
    draft = db.query(DraftOrder).filter(DraftOrder.id == draft_id).first()
    if not draft:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Draft order with ID {draft_id} not found.",
        )

    # 2. Check draft status & idempotency
    if draft.status == DraftOrderStatus.CONFIRMED or draft.confirmed_order_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Draft order {draft_id} is already confirmed (Linked Order ID: {draft.confirmed_order_id}).",
        )

    if draft.status == DraftOrderStatus.REJECTED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot confirm draft order {draft_id} because it has been rejected.",
        )

    if draft.status != DraftOrderStatus.PENDING_CONFIRMATION:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot confirm draft order {draft_id} with status '{draft.status}'. "
                f"Only drafts in 'PENDING_CONFIRMATION' status can be confirmed."
            ),
        )

    # 3. Validate customer existence
    customer = db.query(Customer).filter(Customer.id == draft.customer_id).first()
    if not customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Customer with ID {draft.customer_id} associated with draft {draft_id} not found.",
        )

    # 4. Validate items non-empty
    if not draft.items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Draft order {draft_id} contains no items to confirm.",
        )

    # 5. Pre-validate all items before executing stock modifications
    product_map = {}
    price_changes = []
    total_qty_needed = defaultdict(int)

    for item in draft.items:
        if item.match_status != DraftItemMatchStatus.MATCHED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Cannot confirm draft order {draft_id}: item '{item.raw_product_text}' "
                    f"has unresolved match status '{item.match_status}'."
                ),
            )

        if item.product_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot confirm draft order {draft_id}: item '{item.raw_product_text}' is missing a product ID.",
            )

        if item.quantity is None or item.quantity <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot confirm draft order {draft_id}: item '{item.raw_product_text}' has an invalid quantity ({item.quantity}).",
            )

        product = db.query(Product).filter(Product.id == item.product_id).first()
        if not product:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Product with ID {item.product_id} ('{item.raw_product_text}') not found.",
            )

        if not product.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Product '{product.name}' (SKU: {product.sku}) is inactive and cannot be ordered.",
            )

        # Check for price changes since draft creation
        current_price = Decimal(str(product.price))
        draft_unit_price = Decimal(str(item.unit_price)) if item.unit_price is not None else None

        if draft_unit_price is not None and draft_unit_price != current_price:
            price_changes.append(
                f"'{product.name}' (SKU: {product.sku}): draft price was ${draft_unit_price:.2f}, current price is ${current_price:.2f}"
            )

        product_map[item.product_id] = product
        total_qty_needed[item.product_id] += item.quantity

    # 6. Requirement 5: If prices have changed, require renewed human approval before creating order or deducting stock
    if price_changes and not confirm_price_change:
        changes_str = "; ".join(price_changes)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Price change detected since draft creation: {changes_str}. "
                f"Renewed human approval is required before creating the order or deducting stock. "
                f"Pass confirm_price_change=true to approve new pricing and proceed."
            ),
        )

    # 7. Check stock sufficiency across aggregated product quantities
    for prod_id, qty_needed in total_qty_needed.items():
        product = product_map[prod_id]
        if product.stock_quantity < qty_needed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Insufficient stock for product '{product.name}' (SKU: {product.sku}). "
                    f"Available stock: {product.stock_quantity}, requested: {qty_needed}."
                ),
            )

    # 8. Single Atomic Transaction: Deduct stock, create Order & OrderItems, update DraftOrder
    try:
        total_amount = Decimal("0.00")
        order_items: List[OrderItem] = []

        for item in draft.items:
            product = product_map[item.product_id]
            unit_price = Decimal(str(product.price))
            line_total = unit_price * Decimal(item.quantity)
            total_amount += line_total

            # Atomic conditional stock deduction
            rows_affected = (
                db.query(Product)
                .filter(
                    Product.id == item.product_id,
                    Product.is_active.is_(True),
                    Product.stock_quantity >= item.quantity,
                )
                .update(
                    {Product.stock_quantity: Product.stock_quantity - item.quantity},
                    synchronize_session=False,
                )
            )

            if rows_affected == 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Insufficient stock for product '{product.name}' (SKU: {product.sku}) "
                        f"due to a simultaneous concurrent order. Requested: {item.quantity}."
                    ),
                )

            order_item = OrderItem(
                product_id=product.id,
                quantity=item.quantity,
                unit_price=unit_price,
                total_price=line_total,
            )
            order_items.append(order_item)

            # Synchronize draft item unit price & line total to confirmed values
            item.unit_price = unit_price
            item.total_price = line_total

        # Create master Order with CONFIRMED status
        new_order = Order(
            customer_id=customer.id,
            status=OrderStatus.CONFIRMED,
            total_amount=total_amount,
            items=order_items,
        )
        db.add(new_order)
        db.flush()

        # Update DraftOrder status, confirmed_order_id, and estimated_total
        draft.status = DraftOrderStatus.CONFIRMED
        draft.confirmed_order_id = new_order.id
        draft.estimated_total = total_amount

        db.commit()
        db.refresh(new_order)
        db.refresh(draft)

        return DraftOrderConfirmResponse(
            message=f"Draft order {draft.id} successfully confirmed and converted to Order {new_order.id}.",
            draft_order=DraftOrderResponse.model_validate(draft),
            order=OrderResponse.model_validate(new_order),
        )

    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to confirm draft order due to an unexpected error: {exc}",
        )


@router.post(
    "/{draft_id}/reject",
    response_model=DraftOrderResponse,
    status_code=status.HTTP_200_OK,
    summary="Reject an AI draft order",
)
def reject_draft_order(
    draft_id: int,
    db: Session = Depends(get_db),
) -> DraftOrder:
    """
    Reject and discard an AI-generated draft order.

    Rules:
    - Only unconfirmed drafts (PENDING_CONFIRMATION or REQUIRES_CLARIFICATION) can be rejected.
    - If already REJECTED, rejects with HTTP 400 Bad Request (idempotent / safe against repeated calls).
    - If already CONFIRMED, rejects with HTTP 400 Bad Request (cannot reject a confirmed draft).
    - Rejection NEVER creates orders, NEVER modifies stock quantities, and has zero inventory side-effects.
    """
    draft = db.query(DraftOrder).filter(DraftOrder.id == draft_id).first()
    if not draft:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Draft order with ID {draft_id} not found.",
        )

    if draft.status == DraftOrderStatus.CONFIRMED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot reject draft order {draft_id} because it has already been confirmed (Order ID: {draft.confirmed_order_id}).",
        )

    if draft.status == DraftOrderStatus.REJECTED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Draft order {draft_id} is already rejected.",
        )

    draft.status = DraftOrderStatus.REJECTED
    db.commit()
    db.refresh(draft)
    return draft
