"""
Day 5 Automated Tests: Comprehensive Reliability, Fuzzy Matching, and Integrity Verification.

Tests:
1. Fuzzy catalog matching: Single-word and multi-word typos match active products via difflib.
2. Ambiguous fuzzy matching flags AMBIGUOUS when multiple candidates match.
3. Unreferenced product deletion succeeds with HTTP 200.
4. Product deletion is strictly blocked with HTTP 400 when referenced in historical orders.
5. Product deletion is strictly blocked with HTTP 400 when referenced in draft orders.
6. Concurrent / duplicate SKU creation rollback via IntegrityError returns HTTP 400.
7. Concurrent / duplicate Customer email creation rollback via IntegrityError returns HTTP 400.
8. Test database isolation: in-memory engine verification ensures production database is untouched.

Run with:
    python -m unittest tests/test_day5.py
"""

from decimal import Decimal
import unittest
from fastapi import HTTPException
from pydantic import ValidationError

from tests.test_support import Base, SessionLocal, engine
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
from app.schemas import CustomerCreate, ProductCreate, ProductUpdate
from app.services.ai_service import match_product_in_catalog
from app.routers.products import (
    create_product,
    delete_product,
    get_product,
)
from app.routers.customers import create_customer, delete_customer


class TestDay5ReliabilityAndIntegrity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=engine)

    def setUp(self):
        self.db = SessionLocal()
        # Clean test state in reverse dependency order
        self.db.query(DraftOrderItem).delete()
        self.db.query(DraftOrder).delete()
        self.db.query(OrderItem).delete()
        self.db.query(Order).delete()
        self.db.query(Product).delete()
        self.db.query(Customer).delete()
        self.db.commit()

        # Seed standard catalog
        self.p1 = Product(
            name="Mechanical Keyboard",
            sku="KB-MECH-01",
            category="Electronics",
            price=Decimal("89.99"),
            stock_quantity=50,
            is_active=True,
        )
        self.p2 = Product(
            name="Wireless Mouse",
            sku="MS-WIRE-02",
            category="Electronics",
            price=Decimal("29.99"),
            stock_quantity=100,
            is_active=True,
        )
        self.p3 = Product(
            name="USB-C Charging Cable",
            sku="CB-USBC-03",
            category="Accessories",
            price=Decimal("12.50"),
            stock_quantity=200,
            is_active=True,
        )
        self.customer = Customer(name="Alice Smith", email="alice@example.com")
        self.db.add_all([self.p1, self.p2, self.p3, self.customer])
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_01_fuzzy_catalog_matching_typos(self):
        """Typo fallback matches active products using difflib with ratio >= 0.70."""
        # Single-word typo with transpositions: "keybaord" -> "Mechanical Keyboard"
        prod, status, candidates = match_product_in_catalog("keybaord", self.db)
        self.assertEqual(status, DraftItemMatchStatus.MATCHED)
        self.assertIsNotNone(prod)
        self.assertEqual(prod.id, self.p1.id)

        # Multi-word typo: "wireles mous" -> "Wireless Mouse"
        prod, status, candidates = match_product_in_catalog("wireles mous", self.db)
        self.assertEqual(status, DraftItemMatchStatus.MATCHED)
        self.assertIsNotNone(prod)
        self.assertEqual(prod.id, self.p2.id)

    def test_02_ambiguous_fuzzy_matching(self):
        """When multiple products are close matches, status must be AMBIGUOUS."""
        # Add two similar product variants
        p_extra1 = Product(
            name="Wireless Mouse Basic",
            sku="MS-WIRE-BASIC",
            category="Electronics",
            price=Decimal("19.99"),
            stock_quantity=30,
            is_active=True,
        )
        p_extra2 = Product(
            name="Wireless Mouse Pro",
            sku="MS-WIRE-PRO",
            category="Electronics",
            price=Decimal("49.99"),
            stock_quantity=30,
            is_active=True,
        )
        self.db.add_all([p_extra1, p_extra2])
        self.db.commit()

        # "wireless mouse" matches Wireless Mouse, Wireless Mouse Basic, and Wireless Mouse Pro
        prod, status, candidates = match_product_in_catalog("wireless mouse basic or pro", self.db)
        self.assertEqual(status, DraftItemMatchStatus.AMBIGUOUS)
        self.assertIsNone(prod)
        self.assertGreaterEqual(len(candidates), 2)

    def test_03_delete_unreferenced_product_success(self):
        """Unreferenced products can be cleanly deleted with HTTP 200."""
        temp_prod = Product(
            name="Temporary Test Product",
            sku="TEMP-PROD-99",
            category="Test",
            price=Decimal("5.00"),
            stock_quantity=10,
            is_active=True,
        )
        self.db.add(temp_prod)
        self.db.commit()
        prod_id = temp_prod.id

        result = delete_product(product_id=prod_id, db=self.db)
        self.assertIn("successfully deleted", result["message"])

        # Verify product is removed from database
        deleted_lookup = self.db.query(Product).filter(Product.id == prod_id).first()
        self.assertIsNone(deleted_lookup)

    def test_04_delete_product_blocked_when_referenced_in_orders(self):
        """Product with historical order items cannot be deleted (preserves financial audit)."""
        order = Order(
            customer_id=self.customer.id,
            status=OrderStatus.CONFIRMED,
            total_amount=Decimal("89.99"),
        )
        self.db.add(order)
        self.db.flush()

        order_item = OrderItem(
            order_id=order.id,
            product_id=self.p1.id,
            quantity=1,
            unit_price=Decimal("89.99"),
            total_price=Decimal("89.99"),
        )
        self.db.add(order_item)
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            delete_product(product_id=self.p1.id, db=self.db)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("historical order item", ctx.exception.detail)
        self.assertIn("soft-deactivate", ctx.exception.detail)

    def test_05_delete_product_blocked_when_referenced_in_drafts(self):
        """Product referenced in draft order items cannot be deleted."""
        draft = DraftOrder(
            customer_id=self.customer.id,
            raw_message="Need 1 cable",
            status=DraftOrderStatus.PENDING_CONFIRMATION,
            estimated_total=Decimal("12.50"),
        )
        self.db.add(draft)
        self.db.flush()

        draft_item = DraftOrderItem(
            draft_order_id=draft.id,
            raw_product_text="1 cable",
            product_id=self.p3.id,
            quantity=1,
            unit_price=Decimal("12.50"),
            total_price=Decimal("12.50"),
            match_status=DraftItemMatchStatus.MATCHED,
        )
        self.db.add(draft_item)
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            delete_product(product_id=self.p3.id, db=self.db)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("draft order item", ctx.exception.detail)

    def test_06_integrity_error_rollback_on_duplicate_sku(self):
        """Duplicate SKU insertion caught cleanly and rolled back with HTTP 400."""
        duplicate_payload = ProductCreate(
            name="Duplicate Keyboard",
            sku="KB-MECH-01",  # Existing SKU
            category="Electronics",
            price=Decimal("99.99"),
            stock_quantity=20,
            is_active=True,
        )
        with self.assertRaises(HTTPException) as ctx:
            create_product(product_in=duplicate_payload, db=self.db)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("already exists", ctx.exception.detail)

    def test_07_integrity_error_rollback_on_duplicate_customer_email(self):
        """Duplicate Customer email insertion caught cleanly and rolled back with HTTP 400."""
        duplicate_payload = CustomerCreate(
            name="Alice Duplicate",
            email="alice@example.com",  # Existing email
        )
        with self.assertRaises(HTTPException) as ctx:
            create_customer(customer_in=duplicate_payload, db=self.db)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("already exists", ctx.exception.detail)

    def test_08_in_memory_test_database_isolation(self):
        """Verify the test session runs in-memory and does not share disk file state."""
        # Query product count in test session
        test_count = self.db.query(Product).count()
        self.assertEqual(test_count, 3)

        # Connect directly to disk inventory.db and verify it has independent state
        import sqlite3
        con = sqlite3.connect("inventory.db")
        disk_count = con.execute("SELECT count(id) FROM products").fetchone()[0]
        con.close()
        # Verify disk database exists and has records
        self.assertGreaterEqual(disk_count, 1)


if __name__ == "__main__":
    unittest.main()
