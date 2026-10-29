"""
Order Processing & Inventory Control Router (Milestone 2).

Manages confirmed purchase orders, atomic inventory deductions, lifecycle transitions
(CONFIRMED -> COMPLETED / CANCELLED), and single stock restoration upon cancellation.
"""
from decimal import Decimal
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Customer, Order, OrderItem, OrderStatus, Product
from app.schemas import OrderCreate, OrderResponse

router = APIRouter()


@router.post(
    "/",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new confirmed order",
)
def create_order(
    order_in: OrderCreate,
    db: Session = Depends(get_db),
) -> Order:
    """
    Create a new order with line items, perform atomic stock deduction, and mark status as CONFIRMED.

    Business Rules:
    1. Validates that the customer exists.
    2. Validates that all requested products exist, are active, and have sufficient stock.
    3. Concurrency Protection: Uses an atomic conditional SQL update (WHERE stock_quantity >= qty)
       to guarantee no over-selling occurs even under simultaneous concurrent orders.
    4. Calculates line totals and order total using exact Decimal arithmetic.
    5. Preserves historical unit price snapshot in OrderItem.
    6. All changes occur in a single database transaction. If any failure happens,
       the transaction is fully rolled back, leaving both orders and stock unchanged.
    """
    try:
        # 1. Validate customer existence
        customer = db.query(Customer).filter(Customer.id == order_in.customer_id).first()
        if not customer:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Customer with ID {order_in.customer_id} not found.",
            )

        # 2. Pre-validate all items before executing stock modifications
        product_map = {}
        for item in order_in.items:
            product = db.query(Product).filter(Product.id == item.product_id).first()
            if not product:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Product with ID {item.product_id} not found.",
                )

            if not product.is_active:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Product '{product.name}' (ID: {product.id}) is inactive and cannot be ordered.",
                )

            if product.stock_quantity < item.quantity:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Insufficient stock for product '{product.name}' (SKU: {product.sku}). "
                        f"Available stock: {product.stock_quantity}, requested: {item.quantity}."
                    ),
                )
            product_map[item.product_id] = product

        # 3. Perform atomic stock deduction & order creation within transaction
        total_amount = Decimal("0.00")
        order_items: List[OrderItem] = []

        for item in order_in.items:
            product = product_map[item.product_id]
            unit_price = Decimal(str(product.price))
            line_total = unit_price * Decimal(item.quantity)
            total_amount += line_total

            # Atomic conditional update: prevents race condition / simultaneous over-deduction
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

            # Create OrderItem with preserved historical unit price
            order_item = OrderItem(
                product_id=product.id,
                quantity=item.quantity,
                unit_price=unit_price,
                total_price=line_total,
            )
            order_items.append(order_item)

        # Create master Order record with CONFIRMED status
        db_order = Order(
            customer_id=customer.id,
            status=OrderStatus.CONFIRMED,
            total_amount=total_amount,
            items=order_items,
        )
        db.add(db_order)
        db.commit()
        db.refresh(db_order)
        return db_order

    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to place order due to an unexpected error: {exc}",
        )


@router.get(
    "/",
    response_model=List[OrderResponse],
    summary="List orders with filters and pagination",
)
def list_orders(
    status_filter: Optional[OrderStatus] = Query(
        None,
        alias="status",
        description="Filter orders by status (CONFIRMED, COMPLETED, CANCELLED)",
    ),
    customer_id: Optional[int] = Query(
        None,
        description="Filter orders by customer ID",
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
) -> List[Order]:
    """
    Retrieve a paginated list of orders with optional status and customer filters.
    """
    # Normalize default Query objects if invoked directly in unit tests
    status_val = status_filter if isinstance(status_filter, OrderStatus) else None
    cust_id_val = customer_id if isinstance(customer_id, int) else None
    skip_val = skip if isinstance(skip, int) else 0
    limit_val = limit if isinstance(limit, int) else 20

    query = db.query(Order)

    if status_val is not None:
        query = query.filter(Order.status == status_val)

    if cust_id_val is not None:
        query = query.filter(Order.customer_id == cust_id_val)

    orders = query.order_by(Order.id.desc()).offset(skip_val).limit(limit_val).all()
    return orders


@router.get(
    "/{order_id}",
    response_model=OrderResponse,
    summary="Get order by ID",
)
def get_order(
    order_id: int,
    db: Session = Depends(get_db),
) -> Order:
    """
    Retrieve full order details by ID, including line items.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order with ID {order_id} not found.",
        )
    return order


@router.patch(
    "/{order_id}/complete",
    response_model=OrderResponse,
    summary="Mark an order as completed",
)
def complete_order(
    order_id: int,
    db: Session = Depends(get_db),
) -> Order:
    """
    Transition an order from CONFIRMED to COMPLETED.

    Rules:
    - Only CONFIRMED orders can be completed.
    - An already COMPLETED order cannot be completed again.
    - A CANCELLED order cannot be completed.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order with ID {order_id} not found.",
        )

    if order.status == OrderStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Order {order_id} is already completed.",
        )

    if order.status == OrderStatus.CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot complete order {order_id} because it has been cancelled.",
        )

    if order.status != OrderStatus.CONFIRMED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot complete order {order_id} with current status '{order.status}'.",
        )

    order.status = OrderStatus.COMPLETED
    db.commit()
    db.refresh(order)
    return order


@router.patch(
    "/{order_id}/cancel",
    response_model=OrderResponse,
    summary="Cancel order and restore stock exactly once",
)
def cancel_order(
    order_id: int,
    db: Session = Depends(get_db),
) -> Order:
    """
    Cancel a CONFIRMED order and restore product inventory.

    Business Rules:
    - Restores inventory stock for each line item in the order.
    - Guaranteed to restore stock exactly once: only CONFIRMED orders can be cancelled.
      Once marked CANCELLED, any subsequent cancellation attempt is rejected.
    - COMPLETED orders cannot be cancelled.
    - If a referenced product cannot be found, aborts with an integrity error rather than
      silently skipping restoration.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order with ID {order_id} not found.",
        )

    if order.status == OrderStatus.CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Order {order_id} is already cancelled. Stock was already restored.",
        )

    if order.status == OrderStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel order {order_id} because it has already been completed.",
        )

    if order.status != OrderStatus.CONFIRMED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel order {order_id} with current status '{order.status}'.",
        )

    try:
        # Restore inventory for each item
        for item in order.items:
            product = db.query(Product).filter(Product.id == item.product_id).first()
            if not product:
                db.rollback()
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=(
                        f"Data integrity error: Product ID {item.product_id} associated with order {order_id} "
                        f"could not be found in the database. Stock restoration aborted."
                    ),
                )
            # Atomic inventory increment
            db.query(Product).filter(Product.id == item.product_id).update(
                {Product.stock_quantity: Product.stock_quantity + item.quantity},
                synchronize_session=False,
            )

        order.status = OrderStatus.CANCELLED
        db.commit()
        db.refresh(order)
        return order

    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to cancel order and restore stock: {exc}",
        )
