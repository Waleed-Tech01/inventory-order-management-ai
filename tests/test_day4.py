"""
Day 4 Automated Tests: Draft Order Confirmation Workflow.

Tests:
1. Confirming a PENDING_CONFIRMATION draft converts it to a confirmed Order and deducts inventory stock.
2. Idempotent double-confirmation protection: cannot confirm an already confirmed draft.
3. Rejecting a PENDING_CONFIRMATION draft sets status to REJECTED with zero stock changes.
4. Rejecting a REQUIRES_CLARIFICATION draft sets status to REJECTED with zero stock changes.
5. Rejection idempotency and cannot reject an already CONFIRMED draft.
6. Cannot confirm a draft that REQUIRES_CLARIFICATION.
7. Insufficient stock at confirmation time triggers HTTP 400 and full atomic rollback.
8. Inactive product at confirmation time triggers HTTP 400 and full atomic rollback.
9. Price change detection: requires renewed human approval before creating order or deducting stock.
10. Confirming or rejecting non-existent draft orders returns HTTP 404.

Run with:
    python -m unittest tests/test_day4.py
"""

from decimal import Decimal
import unittest
from fastapi import HTTPException

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
from app.routers.drafts import confirm_draft_order, reject_draft_order


class TestDay4DraftConfirmation(unittest.TestCase):
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
        self.customer = Customer(name="Alice Johnson", email="alice@example.com")
        self.db.add(self.customer)

        # Seed standard products
        self.p_mouse = Product(
            name="Ergonomic Mouse",
            sku="MOU-ERG",
            category="Peripherals",
            price=Decimal("29.99"),
            stock_quantity=15,
            is_active=True,
        )
        self.p_keyboard = Product(
            name="Mechanical Keyboard",
            sku="KB-MECH",
            category="Peripherals",
            price=Decimal("89.99"),
            stock_quantity=10,
            is_active=True,
        )
        self.p_limited = Product(
            name="Limited Edition Pad",
            sku="PAD-LTD",
            category="Accessories",
            price=Decimal("15.00"),
            stock_quantity=1,
            is_active=True,
        )
        self.db.add_all([self.p_mouse, self.p_keyboard, self.p_limited])
        self.db.commit()
        self.db.refresh(self.customer)
        self.db.refresh(self.p_mouse)
        self.db.refresh(self.p_keyboard)
        self.db.refresh(self.p_limited)

    def tearDown(self):
        self.db.close()

    def _create_sample_draft(
        self,
        status: DraftOrderStatus = DraftOrderStatus.PENDING_CONFIRMATION,
        quantity: int = 2,
        price: Decimal = Decimal("29.99"),
        product: Product = None,
        match_status: DraftItemMatchStatus = DraftItemMatchStatus.MATCHED,
    ) -> DraftOrder:
        """Helper to create a draft order in the database for testing."""
        target_product = product or self.p_mouse
        prod_id = target_product.id if match_status == DraftItemMatchStatus.MATCHED else None
        prod_name = target_product.name if match_status == DraftItemMatchStatus.MATCHED else None

        draft = DraftOrder(
            customer_id=self.customer.id,
            raw_message="I want items",
            status=status,
            estimated_total=price * Decimal(quantity) if prod_id else Decimal("0.00"),
        )
        self.db.add(draft)
        self.db.flush()

        item = DraftOrderItem(
            draft_order_id=draft.id,
            raw_product_text=target_product.name,
            product_id=prod_id,
            matched_product_name=prod_name,
            quantity=quantity,
            unit_price=price if prod_id else None,
            total_price=(price * Decimal(quantity)) if prod_id else None,
            match_status=match_status,
        )
        self.db.add(item)
        self.db.commit()
        self.db.refresh(draft)
        return draft

    def test_01_confirm_pending_draft_success(self):
        """Confirming a PENDING_CONFIRMATION draft creates an Order and deducts stock."""
        draft = self._create_sample_draft(quantity=2)
        initial_stock = self.p_mouse.stock_quantity  # 15

        res = confirm_draft_order(draft_id=draft.id, db=self.db)

        # 1. Verify DraftOrder updated
        self.db.refresh(draft)
        self.assertEqual(draft.status, DraftOrderStatus.CONFIRMED)
        self.assertIsNotNone(draft.confirmed_order_id)

        # 2. Verify confirmed Order created
        order = self.db.query(Order).filter(Order.id == draft.confirmed_order_id).first()
        self.assertIsNotNone(order)
        self.assertEqual(order.customer_id, self.customer.id)
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        self.assertEqual(order.total_amount, Decimal("59.98"))
        self.assertEqual(len(order.items), 1)
        self.assertEqual(order.items[0].product_id, self.p_mouse.id)
        self.assertEqual(order.items[0].quantity, 2)
        self.assertEqual(order.items[0].unit_price, Decimal("29.99"))

        # 3. Verify stock deducted
        self.db.refresh(self.p_mouse)
        self.assertEqual(self.p_mouse.stock_quantity, initial_stock - 2)

    def test_02_idempotent_double_confirmation_prevented(self):
        """Attempting to confirm an already confirmed draft raises HTTP 400 and creates no duplicates."""
        draft = self._create_sample_draft(quantity=2)
        confirm_draft_order(draft_id=draft.id, db=self.db)

        self.db.refresh(self.p_mouse)
        stock_after_first = self.p_mouse.stock_quantity
        order_count_after_first = self.db.query(Order).count()

        # Second confirmation call must be rejected
        with self.assertRaises(HTTPException) as ctx:
            confirm_draft_order(draft_id=draft.id, db=self.db)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("already confirmed", ctx.exception.detail.lower())

        # Verify no duplicate orders created and no duplicate stock deduction
        self.assertEqual(self.db.query(Order).count(), order_count_after_first)
        self.db.refresh(self.p_mouse)
        self.assertEqual(self.p_mouse.stock_quantity, stock_after_first)

    def test_03_reject_pending_confirmation_draft(self):
        """Rejecting a PENDING_CONFIRMATION draft sets status to REJECTED with zero stock changes."""
        draft = self._create_sample_draft(quantity=3)
        initial_stock = self.p_mouse.stock_quantity

        rejected_draft = reject_draft_order(draft_id=draft.id, db=self.db)

        self.assertEqual(rejected_draft.status, DraftOrderStatus.REJECTED)
        self.assertIsNone(rejected_draft.confirmed_order_id)

        # Zero orders created
        self.assertEqual(self.db.query(Order).count(), 0)

        # Stock is unchanged
        self.db.refresh(self.p_mouse)
        self.assertEqual(self.p_mouse.stock_quantity, initial_stock)

    def test_04_reject_requires_clarification_draft(self):
        """Rejecting a REQUIRES_CLARIFICATION draft sets status to REJECTED without side effects."""
        draft = self._create_sample_draft(
            status=DraftOrderStatus.REQUIRES_CLARIFICATION,
            match_status=DraftItemMatchStatus.UNKNOWN_PRODUCT,
        )

        rejected_draft = reject_draft_order(draft_id=draft.id, db=self.db)
        self.assertEqual(rejected_draft.status, DraftOrderStatus.REJECTED)
        self.assertEqual(self.db.query(Order).count(), 0)

    def test_05_rejection_idempotency_and_cannot_reject_confirmed(self):
        """Rejecting an already rejected draft raises 400; rejecting a confirmed draft raises 400."""
        # Test 5A: Rejecting already REJECTED draft
        draft_a = self._create_sample_draft()
        reject_draft_order(draft_id=draft_a.id, db=self.db)
        with self.assertRaises(HTTPException) as ctx_a:
            reject_draft_order(draft_id=draft_a.id, db=self.db)
        self.assertEqual(ctx_a.exception.status_code, 400)
        self.assertIn("already rejected", ctx_a.exception.detail.lower())

        # Test 5B: Rejecting already CONFIRMED draft
        draft_b = self._create_sample_draft()
        confirm_draft_order(draft_id=draft_b.id, db=self.db)
        with self.assertRaises(HTTPException) as ctx_b:
            reject_draft_order(draft_id=draft_b.id, db=self.db)
        self.assertEqual(ctx_b.exception.status_code, 400)
        self.assertIn("already been confirmed", ctx_b.exception.detail.lower())

    def test_06_cannot_confirm_draft_requiring_clarification(self):
        """Cannot confirm a draft with status REQUIRES_CLARIFICATION."""
        draft = self._create_sample_draft(
            status=DraftOrderStatus.REQUIRES_CLARIFICATION,
            match_status=DraftItemMatchStatus.UNKNOWN_PRODUCT,
        )

        with self.assertRaises(HTTPException) as ctx:
            confirm_draft_order(draft_id=draft.id, db=self.db)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("only drafts in 'pending_confirmation'", ctx.exception.detail.lower())
        self.assertEqual(self.db.query(Order).count(), 0)

    def test_07_insufficient_stock_fails_and_rolls_back_atomically(self):
        """Insufficient stock aborts confirmation with HTTP 400 and rolls back all stock deductions."""
        # Create a draft with 2 items: 1 Keyboard (in stock: 10) and 5 Limited Pads (in stock: only 1!)
        draft = DraftOrder(
            customer_id=self.customer.id,
            raw_message="1 keyboard and 5 pads",
            status=DraftOrderStatus.PENDING_CONFIRMATION,
            estimated_total=Decimal("164.99"),
        )
        self.db.add(draft)
        self.db.flush()

        item1 = DraftOrderItem(
            draft_order_id=draft.id,
            raw_product_text="Mechanical Keyboard",
            product_id=self.p_keyboard.id,
            matched_product_name=self.p_keyboard.name,
            quantity=1,
            unit_price=Decimal("89.99"),
            total_price=Decimal("89.99"),
            match_status=DraftItemMatchStatus.MATCHED,
        )
        item2 = DraftOrderItem(
            draft_order_id=draft.id,
            raw_product_text="Limited Edition Pad",
            product_id=self.p_limited.id,
            matched_product_name=self.p_limited.name,
            quantity=5,  # Exceeds available stock of 1
            unit_price=Decimal("15.00"),
            total_price=Decimal("75.00"),
            match_status=DraftItemMatchStatus.MATCHED,
        )
        self.db.add_all([item1, item2])
        self.db.commit()

        initial_kb_stock = self.p_keyboard.stock_quantity
        initial_pad_stock = self.p_limited.stock_quantity

        with self.assertRaises(HTTPException) as ctx:
            confirm_draft_order(draft_id=draft.id, db=self.db)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("insufficient stock", ctx.exception.detail.lower())

        # Atomic rollback verification: Neither keyboard nor pad stock changed
        self.db.refresh(self.p_keyboard)
        self.db.refresh(self.p_limited)
        self.assertEqual(self.p_keyboard.stock_quantity, initial_kb_stock)
        self.assertEqual(self.p_limited.stock_quantity, initial_pad_stock)

        # Zero orders created and draft status unchanged
        self.assertEqual(self.db.query(Order).count(), 0)
        self.db.refresh(draft)
        self.assertEqual(draft.status, DraftOrderStatus.PENDING_CONFIRMATION)

    def test_08_inactive_product_at_confirmation_fails_and_rolls_back(self):
        """Inactive product at confirmation time triggers HTTP 400 and full rollback."""
        draft = self._create_sample_draft(quantity=1)

        # Deactivate product after draft was created
        self.p_mouse.is_active = False
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            confirm_draft_order(draft_id=draft.id, db=self.db)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("inactive", ctx.exception.detail.lower())
        self.assertEqual(self.db.query(Order).count(), 0)

    def test_09_price_change_detection_requires_renewed_approval(self):
        """
        Price change since draft creation requires renewed approval (confirm_price_change=True),
        while historical prices for already existing orders remain unchanged.
        """
        # Create a prior confirmed order before the catalog price changes
        prior_order = Order(
            customer_id=self.customer.id,
            status=OrderStatus.CONFIRMED,
            total_amount=Decimal("29.99"),
            items=[
                OrderItem(
                    product_id=self.p_mouse.id,
                    quantity=1,
                    unit_price=Decimal("29.99"),
                    total_price=Decimal("29.99"),
                )
            ],
        )
        self.db.add(prior_order)
        self.db.commit()

        draft = self._create_sample_draft(quantity=2, price=Decimal("29.99"))
        initial_stock = self.p_mouse.stock_quantity

        # Product price increases in catalog from $29.99 to $39.99
        self.p_mouse.price = Decimal("39.99")
        self.db.commit()

        # 1. Verify prior order's historical line item price is preserved at $29.99
        self.db.refresh(prior_order)
        self.assertEqual(prior_order.items[0].unit_price, Decimal("29.99"))
        self.assertEqual(prior_order.total_amount, Decimal("29.99"))

        # 2. Confirmation without approval must be rejected
        with self.assertRaises(HTTPException) as ctx:
            confirm_draft_order(draft_id=draft.id, confirm_price_change=False, db=self.db)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("price change detected", ctx.exception.detail.lower())
        self.assertIn("renewed human approval is required", ctx.exception.detail.lower())

        # Verify stock untouched
        self.db.refresh(self.p_mouse)
        self.assertEqual(self.p_mouse.stock_quantity, initial_stock)

        # 3. Confirmation WITH approval succeeds with updated pricing ($39.99)
        res = confirm_draft_order(draft_id=draft.id, confirm_price_change=True, db=self.db)
        self.assertIsNotNone(res.order)
        self.assertEqual(res.order.total_amount, Decimal("79.98"))  # 2 * $39.99

        # Verify stock deducted
        self.db.refresh(self.p_mouse)
        self.assertEqual(self.p_mouse.stock_quantity, initial_stock - 2)

    def test_10_non_existent_draft_order_returns_404(self):
        """Confirming or rejecting a non-existent draft ID returns HTTP 404."""
        with self.assertRaises(HTTPException) as ctx_confirm:
            confirm_draft_order(draft_id=99999, db=self.db)
        self.assertEqual(ctx_confirm.exception.status_code, 404)

        with self.assertRaises(HTTPException) as ctx_reject:
            reject_draft_order(draft_id=99999, db=self.db)
        self.assertEqual(ctx_reject.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
