"""
Customer Management Router (Milestone 1).

Handles customer registration, profile lookups, updates, and guarded deletion.
Enforces RFC-compliant email normalization and relational audit integrity.
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Customer
from app.schemas import CustomerCreate, CustomerResponse, CustomerUpdate

router = APIRouter()


@router.post(
    "/",
    response_model=CustomerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new customer",
)
def create_customer(
    customer_in: CustomerCreate,
    db: Session = Depends(get_db),
) -> Customer:
    """
    Register a new customer.

    - Validates email format and normalizes it (lowercase, stripped).
    - Ensures the email address is unique.
    """
    # Check if a customer with the same email already exists
    existing_customer = (
        db.query(Customer).filter(Customer.email == customer_in.email).first()
    )
    if existing_customer:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Customer with email '{customer_in.email}' already exists.",
        )

    db_customer = Customer(
        name=customer_in.name,
        email=customer_in.email,
    )
    db.add(db_customer)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Customer with email '{customer_in.email}' already exists.",
        )
    db.refresh(db_customer)
    return db_customer


@router.get(
    "/",
    response_model=List[CustomerResponse],
    summary="List customers with search and pagination",
)
def list_customers(
    search: Optional[str] = Query(
        None,
        description="Search term matched against customer name or email",
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
) -> List[Customer]:
    """
    Retrieve a paginated list of customers with optional name or email search.
    """
    # Normalize default Query objects if invoked directly in unit tests
    search_val = search if isinstance(search, str) else None
    skip_val = skip if isinstance(skip, int) else 0
    limit_val = limit if isinstance(limit, int) else 20

    query = db.query(Customer)

    if search_val:
        search_pattern = f"%{search_val.strip()}%"
        query = query.filter(
            or_(
                Customer.name.ilike(search_pattern),
                Customer.email.ilike(search_pattern),
            )
        )

    customers = query.order_by(Customer.id.asc()).offset(skip_val).limit(limit_val).all()
    return customers


@router.get(
    "/{customer_id}",
    response_model=CustomerResponse,
    summary="Get customer by ID",
)
def get_customer(
    customer_id: int,
    db: Session = Depends(get_db),
) -> Customer:
    """
    Retrieve a single customer's profile by their unique ID.
    """
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Customer with ID {customer_id} not found.",
        )
    return customer


@router.put(
    "/{customer_id}",
    response_model=CustomerResponse,
    summary="Update customer profile",
)
def update_customer(
    customer_id: int,
    customer_in: CustomerUpdate,
    db: Session = Depends(get_db),
) -> Customer:
    """
    Update an existing customer's name and email address.

    - Verifies customer exists.
    - Ensures updated email does not conflict with another customer's email.
    """
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Customer with ID {customer_id} not found.",
        )

    # Check for email collision with a different customer
    conflicting_customer = (
        db.query(Customer)
        .filter(Customer.email == customer_in.email, Customer.id != customer_id)
        .first()
    )
    if conflicting_customer:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Customer with email '{customer_in.email}' already exists.",
        )

    customer.name = customer_in.name
    customer.email = customer_in.email

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Customer with email '{customer_in.email}' already exists.",
        )
    db.refresh(customer)
    return customer


@router.delete(
    "/{customer_id}",
    status_code=status.HTTP_200_OK,
    summary="Delete a customer",
)
def delete_customer(
    customer_id: int,
    db: Session = Depends(get_db),
):
    """
    Delete a customer record.

    Integrity Protection:
    - Rejects deletion if the customer has associated purchase orders or draft orders.
    - Preserves audit trails and financial history.
    """
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Customer with ID {customer_id} not found.",
        )

    # Guard against deleting customers with existing purchase orders
    if customer.orders:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot delete customer {customer_id} because they have {len(customer.orders)} "
                f"associated order(s). Order records must be preserved for accounting and audit integrity."
            ),
        )

    # Guard against deleting customers with existing draft orders
    if customer.draft_orders:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot delete customer {customer_id} because they have {len(customer.draft_orders)} "
                f"associated draft order(s)."
            ),
        )

    db.delete(customer)
    db.commit()
    return {"message": f"Customer {customer_id} successfully deleted."}

