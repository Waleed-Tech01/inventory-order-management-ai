"""
Product Catalog & Inventory Router (Milestone 1).

Handles product lifecycle operations including creation, search/filtering,
updates, and soft-deactivation. Enforces SKU uniqueness and inventory integrity.
"""
from typing import Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import DraftOrderItem, OrderItem, Product
from app.schemas import ProductCreate, ProductResponse, ProductUpdate

router = APIRouter()


@router.post(
    "/",
    response_model=ProductResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new product",
)
def create_product(
    product_in: ProductCreate,
    db: Session = Depends(get_db),
) -> Product:
    """
    Create a new product in the catalog.

    - Validates that the SKU is unique across all products.
    - Validates that price and stock quantity are non-negative.
    """
    # Enforce SKU uniqueness before attempting insert
    existing_product = db.query(Product).filter(Product.sku == product_in.sku).first()
    if existing_product:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Product with SKU '{product_in.sku}' already exists.",
        )

    db_product = Product(
        name=product_in.name,
        sku=product_in.sku,
        category=product_in.category,
        price=product_in.price,
        stock_quantity=product_in.stock_quantity,
        is_active=product_in.is_active,
    )
    db.add(db_product)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Product with SKU '{product_in.sku}' already exists.",
        )
    db.refresh(db_product)
    return db_product


@router.get(
    "/",
    response_model=List[ProductResponse],
    summary="List products with search, filter, and pagination",
)
def list_products(
    search: Optional[str] = Query(
        None,
        description="Search substring matched against product name or SKU",
    ),
    category: Optional[str] = Query(
        None,
        description="Exact or case-insensitive category filter",
    ),
    is_active: Optional[bool] = Query(
        None,
        description="Filter products by active status (true or false)",
    ),
    low_stock_threshold: Optional[int] = Query(
        None,
        ge=0,
        description="Filter products with stock quantity less than or equal to this threshold",
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
) -> List[Product]:
    """
    Retrieve a paginated list of products.
    Supports optional search by name or SKU, and filters for category and active status.
    """
    # Normalize default Query objects if invoked directly in unit tests
    search_val = search if isinstance(search, str) else None
    cat_val = category if isinstance(category, str) else None
    active_val = is_active if isinstance(is_active, bool) else None
    thresh_val = low_stock_threshold if isinstance(low_stock_threshold, int) else None
    skip_val = skip if isinstance(skip, int) else 0
    limit_val = limit if isinstance(limit, int) else 20

    query = db.query(Product)

    # Search filter (name or SKU)
    if search_val:
        search_pattern = f"%{search_val.strip()}%"
        query = query.filter(
            or_(
                Product.name.ilike(search_pattern),
                Product.sku.ilike(search_pattern),
            )
        )

    # Category filter
    if cat_val:
        query = query.filter(Product.category.ilike(cat_val.strip()))

    # Active status filter
    if active_val is not None:
        query = query.filter(Product.is_active == active_val)

    # Low stock threshold filter
    if thresh_val is not None:
        query = query.filter(Product.stock_quantity <= thresh_val)

    # Apply pagination and return ordered list
    products = query.order_by(Product.id.asc()).offset(skip_val).limit(limit_val).all()
    return products


@router.get(
    "/{product_id}",
    response_model=ProductResponse,
    summary="Get product by ID",
)
def get_product(
    product_id: int,
    db: Session = Depends(get_db),
) -> Product:
    """
    Retrieve details for a single product by its primary key ID.
    """
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product with ID {product_id} not found.",
        )
    return product


@router.put(
    "/{product_id}",
    response_model=ProductResponse,
    summary="Update an existing product",
)
def update_product(
    product_id: int,
    product_in: ProductUpdate,
    db: Session = Depends(get_db),
) -> Product:
    """
    Update all fields of an existing product.

    - Verifies the product exists.
    - Prevents collision if SKU is changed to one already owned by another product.
    """
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product with ID {product_id} not found.",
        )

    # Check for SKU collision with other products
    conflicting_product = (
        db.query(Product)
        .filter(Product.sku == product_in.sku, Product.id != product_id)
        .first()
    )
    if conflicting_product:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Product with SKU '{product_in.sku}' already exists.",
        )

    product.name = product_in.name
    product.sku = product_in.sku
    product.category = product_in.category
    product.price = product_in.price
    product.stock_quantity = product_in.stock_quantity
    product.is_active = product_in.is_active

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Product with SKU '{product_in.sku}' already exists.",
        )
    db.refresh(product)
    return product


@router.patch(
    "/{product_id}/deactivate",
    response_model=ProductResponse,
    summary="Deactivate a product",
)
def deactivate_product(
    product_id: int,
    db: Session = Depends(get_db),
) -> Product:
    """
    Deactivate a product without permanently deleting it from the database (soft delete).
    """
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product with ID {product_id} not found.",
        )

    product.is_active = False
    db.commit()
    db.refresh(product)
    return product


@router.delete(
    "/{product_id}",
    status_code=status.HTTP_200_OK,
    summary="Delete a product (only if unreferenced in orders or drafts)",
)
def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
) -> Dict[str, str]:
    """
    Permanently delete a product if it has never been referenced in orders or drafts.

    If the product has historical order items or draft order items, deletion is blocked
    to preserve audit trails and relational integrity. Use PATCH /{product_id}/deactivate
    to soft-deactivate instead.
    """
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product with ID {product_id} not found.",
        )

    # Relational integrity: block deletion if product exists in historical orders
    order_item_count = db.query(OrderItem).filter(OrderItem.product_id == product_id).count()
    if order_item_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot delete product '{product.name}' (ID {product_id}): it is referenced in "
                f"{order_item_count} historical order item(s). Use PATCH /products/{product_id}/deactivate "
                "to soft-deactivate instead."
            ),
        )

    # Guard against deleting products actively referenced in draft orders
    draft_item_count = (
        db.query(DraftOrderItem)
        .filter(DraftOrderItem.product_id == product_id)
        .count()
    )
    if draft_item_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot delete product '{product.name}' (ID {product_id}): it is referenced in "
                f"{draft_item_count} draft order item(s). Use PATCH /products/{product_id}/deactivate "
                "to soft-deactivate instead."
            ),
        )

    db.delete(product)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete product {product_id} due to database integrity constraints.",
        )

    return {"message": f"Product '{product.name}' (ID {product_id}) successfully deleted."}
