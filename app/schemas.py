from datetime import datetime
from decimal import Decimal
import enum
from typing import Annotated, List, Optional
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


# ==========================================
# Milestone 1: Product Schemas
# ==========================================

class ProductBase(BaseModel):
    """
    Shared attributes for product request and response models.
    """
    name: Annotated[
        str,
        Field(min_length=1, max_length=255, description="Product title or label")
    ]
    sku: Annotated[
        str,
        Field(
            min_length=1,
            max_length=100,
            description="Stock Keeping Unit (must be unique)"
        )
    ]
    category: Annotated[
        str,
        Field(min_length=1, max_length=100, description="Product category group")
    ]
    price: Annotated[
        Decimal,
        Field(
            ge=0,
            decimal_places=2,
            description="Unit price (must be greater than or equal to 0.00)"
        )
    ]
    stock_quantity: Annotated[
        int,
        Field(
            default=0,
            ge=0,
            description="Inventory stock level (must be greater than or equal to 0)"
        )
    ]
    is_active: Annotated[
        bool,
        Field(
            default=True,
            description="Whether the product is available for purchase"
        )
    ]

    @field_validator("name", "category")
    @classmethod
    def validate_non_empty_strings(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("Field cannot be empty or contain only whitespace.")
        return trimmed

    @field_validator("sku")
    @classmethod
    def normalize_sku(cls, value: str) -> str:
        trimmed = value.strip().upper()
        if not trimmed:
            raise ValueError("SKU cannot be empty or contain only whitespace.")
        return trimmed


class ProductCreate(ProductBase):
    """
    Schema for creating a new product.
    Auto-generated fields (id, created_at, updated_at) are excluded to prevent client tampering.
    """
    pass


class ProductUpdate(ProductBase):
    """
    Schema for updating an existing product via PUT.
    Enforces that all required fields are validated.
    """
    pass


class ProductResponse(ProductBase):
    """
    Schema for returning product data to the API client.
    Enables reading directly from SQLAlchemy ORM instances via from_attributes=True.
    """
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ==========================================
# Milestone 1: Customer Schemas
# ==========================================

class CustomerBase(BaseModel):
    """
    Shared attributes for customer request and response models.
    """
    name: Annotated[
        str,
        Field(min_length=1, max_length=255, description="Full customer name")
    ]
    email: Annotated[
        EmailStr,
        Field(description="Valid unique customer email address")
    ]

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("Customer name cannot be empty or contain only whitespace.")
        return trimmed

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        # Standardize email addresses to lowercase and remove outer whitespace
        return str(value).strip().lower()


class CustomerCreate(CustomerBase):
    """
    Schema for registering a new customer.
    Auto-generated fields (id, created_at, updated_at) are excluded.
    """
    pass


class CustomerUpdate(CustomerBase):
    """
    Schema for updating an existing customer record via PUT.
    """
    pass


class CustomerResponse(CustomerBase):
    """
    Schema for returning customer information to the API client.
    Enables reading directly from SQLAlchemy ORM instances via from_attributes=True.
    """
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ==========================================
# Milestone 2: Confirmed Order Schemas
# ==========================================

class OrderStatusEnum(str, enum.Enum):
    """
    Order statuses mirrored in Pydantic for API contract validation.
    Matches models.py: CONFIRMED, COMPLETED, CANCELLED.
    """
    CONFIRMED = "CONFIRMED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class OrderItemCreate(BaseModel):
    """
    Schema for an item submitted as part of a new order request.
    """
    product_id: Annotated[int, Field(gt=0, description="Unique ID of the product")]
    quantity: Annotated[int, Field(gt=0, description="Quantity to purchase (must be greater than 0)")]


class OrderItemResponse(BaseModel):
    """
    Schema for returning line item details in an order response.
    """
    id: int
    product_id: int
    quantity: int
    unit_price: Decimal
    total_price: Decimal

    model_config = ConfigDict(from_attributes=True)


class OrderCreate(BaseModel):
    """
    Schema for creating a new order.
    Requires an existing customer and at least one line item.
    """
    customer_id: Annotated[int, Field(gt=0, description="Unique ID of the purchasing customer")]
    items: Annotated[
        List[OrderItemCreate],
        Field(min_length=1, description="List of items to order (at least one item required)")
    ]

    @field_validator("items")
    @classmethod
    def validate_unique_products(cls, items: List[OrderItemCreate]) -> List[OrderItemCreate]:
        """
        Ensure no product is repeated across multiple line items in the same order request.
        """
        product_ids = [item.product_id for item in items]
        if len(product_ids) != len(set(product_ids)):
            raise ValueError(
                "Duplicate product IDs found in order items. Consolidate quantities into a single line item."
            )
        return items


class OrderResponse(BaseModel):
    """
    Schema for returning complete order details, including line items.
    """
    id: int
    customer_id: int
    status: OrderStatusEnum
    total_amount: Decimal
    created_at: datetime
    updated_at: datetime
    items: List[OrderItemResponse]

    model_config = ConfigDict(from_attributes=True)


# ==========================================
# Milestone 3: AI Draft Order Schemas
# ==========================================

class DraftOrderStatusEnum(str, enum.Enum):
    """
    Draft order lifecycle statuses mirrored in Pydantic.
    Matches models.DraftOrderStatus exact values.
    """
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    REQUIRES_CLARIFICATION = "REQUIRES_CLARIFICATION"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class DraftItemMatchStatusEnum(str, enum.Enum):
    """
    Draft item matching statuses indicating how a customer query was resolved.
    Matches models.DraftItemMatchStatus exact values.
    """
    MATCHED = "MATCHED"
    UNKNOWN_PRODUCT = "UNKNOWN_PRODUCT"
    AMBIGUOUS = "AMBIGUOUS"
    MISSING_QUANTITY = "MISSING_QUANTITY"
    INACTIVE = "INACTIVE"


# Intermediate extraction schemas used by the AI parsing service
class ExtractedOrderItem(BaseModel):
    """
    An individual product/quantity pair extracted from raw customer text by the LLM.
    """
    product_name: str = Field(..., description="Name or reference of product mentioned by customer")
    quantity: Optional[int] = Field(None, description="Requested quantity, or null if unspecified")


class ParsedOrderRequest(BaseModel):
    """
    Structured extraction payload returned from the LLM parsing step.
    """
    items: List[ExtractedOrderItem] = Field(default_factory=list, description="Extracted products and quantities")
    intent: Optional[str] = Field(None, description="Brief note on customer intent or missing details")


# Request schema for generating a draft from a natural-language message
class DraftOrderCreate(BaseModel):
    """
    Request schema to create an AI-generated draft order from a customer's message.
    """
    customer_id: Annotated[int, Field(gt=0, description="ID of the customer who sent the message")]
    message: Annotated[str, Field(min_length=1, max_length=2000, description="Natural language customer order message")]

    @field_validator("message")
    @classmethod
    def validate_message(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("Message cannot be empty or blank whitespace.")
        return trimmed


# Request schema for an individual item inside a draft order
class DraftOrderItemCreate(BaseModel):
    """
    Request schema for adding or specifying an item within a draft order.
    """
    raw_product_text: Annotated[
        str,
        Field(min_length=1, max_length=255, description="Product reference text from customer")
    ]
    product_id: Annotated[
        Optional[int],
        Field(default=None, gt=0, description="Matched catalog product ID if resolved")
    ]
    quantity: Annotated[
        Optional[int],
        Field(default=None, gt=0, description="Requested quantity if specified")
    ]


# Response schema for draft line items
class DraftOrderItemResponse(BaseModel):
    """
    Response schema for an individual line item inside an AI-generated draft order.
    Matches field names from models.DraftOrderItem.
    """
    id: int
    draft_order_id: int
    raw_product_text: str
    product_id: Optional[int] = None
    matched_product_name: Optional[str] = None
    quantity: Optional[int] = None
    unit_price: Optional[Decimal] = None
    total_price: Optional[Decimal] = None
    match_status: DraftItemMatchStatusEnum
    possible_matches: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# Response schema for full draft orders
class DraftOrderResponse(BaseModel):
    """
    Response schema for a complete AI-generated draft order.
    Matches field names from models.DraftOrder.
    """
    id: int
    customer_id: int
    raw_message: str
    status: DraftOrderStatusEnum
    notes: Optional[str] = None
    estimated_total: Decimal
    created_at: datetime
    updated_at: datetime
    confirmed_order_id: Optional[int] = None
    items: List[DraftOrderItemResponse]

    model_config = ConfigDict(from_attributes=True)


# ==========================================
# Milestone 4: Draft Confirmation Schemas
# ==========================================

class DraftOrderConfirmResponse(BaseModel):
    """
    Response schema returned upon confirming an AI draft order.
    Returns the confirmation status message, the updated draft order, and the newly created confirmed order.
    """
    message: str = Field(
        default="Draft order successfully confirmed and converted to confirmed order.",
        description="Confirmation status message"
    )
    draft_order: DraftOrderResponse
    order: OrderResponse

    model_config = ConfigDict(from_attributes=True)

