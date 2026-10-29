import enum
from decimal import Decimal
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import relationship

from app.database import Base


# ==========================================
# Milestone 1: Products & Customers
# ==========================================

class Product(Base):
    """
    SQLAlchemy database model for the products table.

    Represents physical or digital catalog items sold by the business.
    """
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, index=True)
    sku = Column(String(100), unique=True, index=True, nullable=False)
    category = Column(String(100), nullable=False, index=True)
    price = Column(Numeric(precision=10, scale=2, asdecimal=True), nullable=False)
    stock_quantity = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    order_items = relationship("OrderItem", back_populates="product")
    draft_order_items = relationship("DraftOrderItem", back_populates="product")

    __table_args__ = (
        CheckConstraint("price >= 0", name="check_product_price_non_negative"),
        CheckConstraint("stock_quantity >= 0", name="check_product_stock_non_negative"),
    )

    def __repr__(self) -> str:
        return f"<Product(id={self.id}, sku='{self.sku}', name='{self.name}', price={self.price}, stock={self.stock_quantity})>"


class Customer(Base):
    """
    SQLAlchemy database model for the customers table.

    Represents buyers who place orders in the system.
    """
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships (historical orders and drafts must never be automatically cascaded/orphaned)
    orders = relationship("Order", back_populates="customer")
    draft_orders = relationship("DraftOrder", back_populates="customer")

    def __repr__(self) -> str:
        return f"<Customer(id={self.id}, name='{self.name}', email='{self.email}')>"


# ==========================================
# Milestone 2: Orders & Line Items
# ==========================================

class OrderStatus(str, enum.Enum):
    """
    Enumeration of allowed order lifecycle states.
    - CONFIRMED: Initial status when an order is placed and inventory is committed.
    - COMPLETED: Order has been fulfilled and closed.
    - CANCELLED: Order was aborted and inventory was returned to stock.
    """
    CONFIRMED = "CONFIRMED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class Order(Base):
    """
    SQLAlchemy database model for the orders table.

    Tracks a purchase transaction placed by a customer.
    Initial status is CONFIRMED upon creation.
    """
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False, index=True)
    status = Column(
        SQLEnum(OrderStatus),
        nullable=False,
        default=OrderStatus.CONFIRMED,
        index=True,
    )
    total_amount = Column(
        Numeric(precision=10, scale=2, asdecimal=True),
        nullable=False,
        default=Decimal("0.00"),
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    customer = relationship("Customer", back_populates="orders")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("total_amount >= 0", name="check_order_total_amount_non_negative"),
    )

    def __repr__(self) -> str:
        return f"<Order(id={self.id}, customer_id={self.customer_id}, status='{self.status}', total={self.total_amount})>"


class OrderItem(Base):
    """
    SQLAlchemy database model for individual line items within a confirmed order.

    Preserves the historical unit price at the time the order was placed.
    """
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Numeric(precision=10, scale=2, asdecimal=True), nullable=False)
    total_price = Column(Numeric(precision=10, scale=2, asdecimal=True), nullable=False)

    # Relationships
    order = relationship("Order", back_populates="items")
    product = relationship("Product", back_populates="order_items")

    __table_args__ = (
        CheckConstraint("quantity > 0", name="check_order_item_quantity_positive"),
        CheckConstraint("unit_price >= 0", name="check_order_item_unit_price_non_negative"),
        CheckConstraint("total_price >= 0", name="check_order_item_total_price_non_negative"),
    )

    def __repr__(self) -> str:
        return (
            f"<OrderItem(id={self.id}, order_id={self.order_id}, product_id={self.product_id}, "
            f"qty={self.quantity}, unit_price={self.unit_price}, total={self.total_price})>"
        )


# ==========================================
# Milestones 3 & 4: AI Draft Orders & Confirmation
# ==========================================

class DraftOrderStatus(str, enum.Enum):
    """
    Status of an AI-generated draft order.
    - PENDING_CONFIRMATION: All items matched successfully; ready for human confirmation.
    - REQUIRES_CLARIFICATION: One or more items are unknown, ambiguous, or missing quantity.
    - CONFIRMED: Converted into an official confirmed order by human approval.
    - REJECTED: Discarded by human operator or customer.
    """
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    REQUIRES_CLARIFICATION = "REQUIRES_CLARIFICATION"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class DraftItemMatchStatus(str, enum.Enum):
    """
    Status of an individual product match within an AI draft.
    - MATCHED: Exactly one active product in the catalog matched the customer request.
    - UNKNOWN_PRODUCT: No matching product was found in the database catalog.
    - AMBIGUOUS: Multiple catalog products match the customer request (needs human selection).
    - MISSING_QUANTITY: Customer did not specify a valid quantity.
    - INACTIVE: Matched product is marked inactive.
    """
    MATCHED = "MATCHED"
    UNKNOWN_PRODUCT = "UNKNOWN_PRODUCT"
    AMBIGUOUS = "AMBIGUOUS"
    MISSING_QUANTITY = "MISSING_QUANTITY"
    INACTIVE = "INACTIVE"


class DraftOrder(Base):
    """
    SQLAlchemy database model for AI-generated draft orders.

    Stores the raw customer message, parsed items, and clarification flags.
    NOTE: Draft orders NEVER deduct stock or create real orders until human confirmation.
    """
    __tablename__ = "draft_orders"

    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False, index=True)
    raw_message = Column(Text, nullable=False)
    status = Column(
        SQLEnum(DraftOrderStatus),
        nullable=False,
        default=DraftOrderStatus.PENDING_CONFIRMATION,
        index=True,
    )
    notes = Column(Text, nullable=True)
    estimated_total = Column(
        Numeric(precision=10, scale=2, asdecimal=True),
        nullable=False,
        default=Decimal("0.00"),
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    # Day 4: Link to confirmed order when converted
    confirmed_order_id = Column(Integer, ForeignKey("orders.id"), nullable=True, index=True)

    # Relationships
    customer = relationship("Customer", back_populates="draft_orders")
    items = relationship("DraftOrderItem", back_populates="draft_order", cascade="all, delete-orphan")
    confirmed_order = relationship("Order", foreign_keys=[confirmed_order_id])

    def __repr__(self) -> str:
        return (
            f"<DraftOrder(id={self.id}, customer_id={self.customer_id}, status='{self.status}', "
            f"est_total={self.estimated_total}, confirmed_order_id={self.confirmed_order_id})>"
        )


class DraftOrderItem(Base):
    """
    SQLAlchemy database model for individual items within an AI-generated draft order.
    May contain unresolved products, missing quantities, or ambiguous candidate matches.
    """
    __tablename__ = "draft_order_items"

    id = Column(Integer, primary_key=True, index=True)
    draft_order_id = Column(Integer, ForeignKey("draft_orders.id"), nullable=False, index=True)
    raw_product_text = Column(String(255), nullable=False)
    # product_id is nullable because unknown or ambiguous items do not point to a single product yet
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True, index=True)
    matched_product_name = Column(String(255), nullable=True)
    quantity = Column(Integer, nullable=True)
    unit_price = Column(Numeric(precision=10, scale=2, asdecimal=True), nullable=True)
    total_price = Column(Numeric(precision=10, scale=2, asdecimal=True), nullable=True)
    match_status = Column(
        SQLEnum(DraftItemMatchStatus),
        nullable=False,
        default=DraftItemMatchStatus.MATCHED,
        index=True,
    )
    possible_matches = Column(Text, nullable=True)  # Comma-separated or JSON string for ambiguous candidates

    # Relationships
    draft_order = relationship("DraftOrder", back_populates="items")
    product = relationship("Product", back_populates="draft_order_items")

    def __repr__(self) -> str:
        return (
            f"<DraftOrderItem(id={self.id}, raw='{self.raw_product_text}', status='{self.match_status}', "
            f"prod_id={self.product_id}, qty={self.quantity})>"
        )
