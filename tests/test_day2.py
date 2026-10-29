"""
Day 2 Automated Tests: Orders and Inventory Management.

Tests:
1. Order schema validation (positive quantity, non-empty items, duplicate product rejection).
2. Successful order creation with atomic stock deduction and Decimal total calculation.
3. Order creation rejection for non-existent customer, non-existent product, and inactive product.
4. Insufficient stock rejection with atomic rollback (zero stock deducted, zero orders created).
5. Order retrieval by ID and listing with status and customer filters.
6. Order completion lifecycle (CONFIRMED -> COMPLETED, guards against re-completion or cancelled orders).
7. Order cancellation and single stock restoration (CONFIRMED -> CANCELLED, restores stock exactly once).
8. Customer deletion integrity protection (cannot delete customer with existing orders).

Run with:
    python -m unittest tests/test_day2.py
"""

from decimal import Decimal
import unittest
from fastapi import HTTPException
from pydantic import ValidationError

from tests.test_support import Base, SessionLocal, engine
from app.models import Customer, DraftOrder, DraftOrderItem, Order, OrderItem, OrderStatus, Product
from app.schemas import OrderCreate, OrderItemCreate
from app.routers.orders import (
    cancel_order,
    complete_order,
    create_order,
    get_order,
    list_orders,
)
from app.routers.customers import delete_customer


class TestDay2OrdersAndInventory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=engine)

    def setUp(self):
        self.db = SessionLocal()
        # Clean existing test data in reverse-dependency order
        self.db.query(DraftOrderItem).delete()
        self.db.query(DraftOrder).delete()
        self.db.query(OrderItem).delete()
        self.db.query(Order).delete()
        self.db.query(Product).delete()
        self.db.query(Customer).delete()
        self.db.commit()

        # Seed standard customer
        self.customer = Customer(name="Alice Walker", email="alice@example.com")
        self.db.add(self.customer)

        # Seed standard products
        self.p_mouse = Product(
            name="Gaming Mouse",
            sku="GM-001",
            category="Peripherals",
            price=Decimal("49.99"),
            stock_quantity=10,
            is_active=True,
        )
        self.p_cable = Product(
            name="USB-C Cable",
            sku="USB-001",
            category="Accessories",
            price=Decimal("9.99"),
            stock_quantity=20,
            is_active=True,
        )
        self.p_inactive = Product(
            name="Legacy Webcam",
            sku="CAM-OLD",
            category="Peripherals",
            price=Decimal("19.99"),
            stock_quantity=5,
            is_active=False,
        )
        self.db.add_all([self.p_mouse, self.p_cable, self.p_inactive])
        self.db.commit()
        self.db.refresh(self.customer)
        self.db.refresh(self.p_mouse)
        self.db.refresh(self.p_cable)
        self.db.refresh(self.p_inactive)

    def tearDown(self):
        self.db.close()

    def test_01_order_schema_validation(self):
        """Test OrderCreate and OrderItemCreate validation rules."""
        # Quantity must be > 0
        with self.assertRaises(ValidationError):
            OrderItemCreate(product_id=self.p_mouse.id, quantity=0)

        with self.assertRaises(ValidationError):
            OrderItemCreate(product_id=self.p_mouse.id, quantity=-2)

        # Empty items list rejected
        with self.assertRaises(ValidationError):
            OrderCreate(customer_id=self.customer.id, items=[])

        # Duplicate product IDs in items list rejected
        with self.assertRaises(ValidationError):
            OrderCreate(
                customer_id=self.customer.id,
                items=[
                    OrderItemCreate(product_id=self.p_mouse.id, quantity=1),
                    OrderItemCreate(product_id=self.p_mouse.id, quantity=2),
                ],
            )

    def test_02_create_order_success(self):
        """Test successful order creation with atomic stock deduction and Decimal total calculation."""
        initial_mouse_stock = self.p_mouse.stock_quantity  # 10
        initial_cable_stock = self.p_cable.stock_quantity  # 20

        req = OrderCreate(
            customer_id=self.customer.id,
            items=[
                OrderItemCreate(product_id=self.p_mouse.id, quantity=2),
                OrderItemCreate(product_id=self.p_cable.id, quantity=3),
            ],
        )

        order = create_order(order_in=req, db=self.db)

        # 1. Order status is CONFIRMED
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        self.assertEqual(order.customer_id, self.customer.id)

        # 2. Total amount calculation: (2 * 49.99) + (3 * 9.99) = 99.98 + 29.97 = 129.95
        self.assertEqual(order.total_amount, Decimal("129.95"))

        # 3. Line items recorded with historical unit price
        self.assertEqual(len(order.items), 2)
        items_by_pid = {item.product_id: item for item in order.items}
        self.assertEqual(items_by_pid[self.p_mouse.id].quantity, 2)
        self.assertEqual(items_by_pid[self.p_mouse.id].unit_price, Decimal("49.99"))
        self.assertEqual(items_by_pid[self.p_mouse.id].total_price, Decimal("99.98"))

        self.assertEqual(items_by_pid[self.p_cable.id].quantity, 3)
        self.assertEqual(items_by_pid[self.p_cable.id].unit_price, Decimal("9.99"))
        self.assertEqual(items_by_pid[self.p_cable.id].total_price, Decimal("29.97"))

        # 4. Stock deducted
        self.db.refresh(self.p_mouse)
        self.db.refresh(self.p_cable)
        self.assertEqual(self.p_mouse.stock_quantity, initial_mouse_stock - 2)
        self.assertEqual(self.p_cable.stock_quantity, initial_cable_stock - 3)

    def test_03_create_order_rejections(self):
        """Test order rejections for non-existent customer, non-existent product, and inactive product."""
        # Non-existent customer
        with self.assertRaises(HTTPException) as ctx_cust:
            create_order(
                order_in=OrderCreate(
                    customer_id=99999,
                    items=[OrderItemCreate(product_id=self.p_mouse.id, quantity=1)],
                ),
                db=self.db,
            )
        self.assertEqual(ctx_cust.exception.status_code, 404)

        # Non-existent product
        with self.assertRaises(HTTPException) as ctx_prod:
            create_order(
                order_in=OrderCreate(
                    customer_id=self.customer.id,
                    items=[OrderItemCreate(product_id=99999, quantity=1)],
                ),
                db=self.db,
            )
        self.assertEqual(ctx_prod.exception.status_code, 404)

        # Inactive product
        with self.assertRaises(HTTPException) as ctx_inact:
            create_order(
                order_in=OrderCreate(
                    customer_id=self.customer.id,
                    items=[OrderItemCreate(product_id=self.p_inactive.id, quantity=1)],
                ),
                db=self.db,
            )
        self.assertEqual(ctx_inact.exception.status_code, 400)
        self.assertIn("inactive", ctx_inact.exception.detail.lower())

    def test_04_insufficient_stock_rollback(self):
        """Test insufficient stock rejection and verify full transaction rollback."""
        initial_mouse_stock = self.p_mouse.stock_quantity  # 10
        initial_cable_stock = self.p_cable.stock_quantity  # 20

        # Request 2 mice (in stock) and 25 cables (only 20 available!)
        req = OrderCreate(
            customer_id=self.customer.id,
            items=[
                OrderItemCreate(product_id=self.p_mouse.id, quantity=2),
                OrderItemCreate(product_id=self.p_cable.id, quantity=25),
            ],
        )

        with self.assertRaises(HTTPException) as ctx:
            create_order(order_in=req, db=self.db)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("insufficient stock", ctx.exception.detail.lower())

        # Verify atomic rollback: Neither mouse nor cable stock was deducted
        self.db.refresh(self.p_mouse)
        self.db.refresh(self.p_cable)
        self.assertEqual(self.p_mouse.stock_quantity, initial_mouse_stock)
        self.assertEqual(self.p_cable.stock_quantity, initial_cable_stock)
        self.assertEqual(self.db.query(Order).count(), 0)

    def test_05_get_and_list_orders(self):
        """Test retrieving orders by ID and listing with status and customer filters."""
        req = OrderCreate(
            customer_id=self.customer.id,
            items=[OrderItemCreate(product_id=self.p_mouse.id, quantity=1)],
        )
        created_order = create_order(order_in=req, db=self.db)

        # Get order by ID
        fetched = get_order(order_id=created_order.id, db=self.db)
        self.assertEqual(fetched.id, created_order.id)
        self.assertEqual(len(fetched.items), 1)

        # Get non-existent order returns 404
        with self.assertRaises(HTTPException) as ctx:
            get_order(order_id=99999, db=self.db)
        self.assertEqual(ctx.exception.status_code, 404)

        # List orders by status
        orders_confirmed = list_orders(status_filter=OrderStatus.CONFIRMED, db=self.db)
        self.assertEqual(len(orders_confirmed), 1)

        orders_completed = list_orders(status_filter=OrderStatus.COMPLETED, db=self.db)
        self.assertEqual(len(orders_completed), 0)

    def test_06_complete_order_lifecycle(self):
        """Test transitioning an order from CONFIRMED to COMPLETED."""
        req = OrderCreate(
            customer_id=self.customer.id,
            items=[OrderItemCreate(product_id=self.p_mouse.id, quantity=1)],
        )
        order = create_order(order_in=req, db=self.db)

        # Complete order
        completed = complete_order(order_id=order.id, db=self.db)
        self.assertEqual(completed.status, OrderStatus.COMPLETED)

        # Re-completing must be rejected
        with self.assertRaises(HTTPException) as ctx_recomplete:
            complete_order(order_id=order.id, db=self.db)
        self.assertEqual(ctx_recomplete.exception.status_code, 400)
        self.assertIn("already completed", ctx_recomplete.exception.detail.lower())

    def test_07_cancel_order_restores_stock_once(self):
        """Test cancelling a CONFIRMED order restores stock exactly once."""
        initial_stock = self.p_mouse.stock_quantity  # 10
        req = OrderCreate(
            customer_id=self.customer.id,
            items=[OrderItemCreate(product_id=self.p_mouse.id, quantity=3)],
        )
        order = create_order(order_in=req, db=self.db)

        # Stock is now 7
        self.db.refresh(self.p_mouse)
        self.assertEqual(self.p_mouse.stock_quantity, initial_stock - 3)

        # Cancel order
        cancelled = cancel_order(order_id=order.id, db=self.db)
        self.assertEqual(cancelled.status, OrderStatus.CANCELLED)

        # Stock is restored to 10
        self.db.refresh(self.p_mouse)
        self.assertEqual(self.p_mouse.stock_quantity, initial_stock)

        # Second cancellation must be rejected (idempotency guard against double restoration)
        with self.assertRaises(HTTPException) as ctx_recancel:
            cancel_order(order_id=order.id, db=self.db)
        self.assertEqual(ctx_recancel.exception.status_code, 400)
        self.assertIn("already cancelled", ctx_recancel.exception.detail.lower())

        # Stock remains 10 (not restored again!)
        self.db.refresh(self.p_mouse)
        self.assertEqual(self.p_mouse.stock_quantity, initial_stock)

    def test_08_customer_deletion_integrity_protection(self):
        """Customer with existing orders cannot be deleted (relational integrity)."""
        req = OrderCreate(
            customer_id=self.customer.id,
            items=[OrderItemCreate(product_id=self.p_mouse.id, quantity=1)],
        )
        create_order(order_in=req, db=self.db)

        # Attempt to delete customer must be rejected with HTTP 400
        with self.assertRaises(HTTPException) as ctx:
            delete_customer(customer_id=self.customer.id, db=self.db)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("cannot delete customer", ctx.exception.detail.lower())


if __name__ == "__main__":
    unittest.main()
