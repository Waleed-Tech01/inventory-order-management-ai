"""
Day 1 Automated Tests: Product & Customer Management.

Tests:
1. Product schema validations (non-negative price, non-negative stock, SKU normalization).
2. Customer schema validations (email format, normalization, non-empty name).
3. Product creation and duplicate SKU rejection.
4. Product listing with search, category filter, active filter, low-stock threshold, and pagination.
5. Product retrieval by ID and update with SKU collision prevention.
6. Product soft-deactivation.
7. Customer CRUD operations and safe deletion when no orders exist.

Run with:
    python -m unittest tests/test_day1.py
"""

from decimal import Decimal
import unittest
from fastapi import HTTPException
from pydantic import ValidationError

from tests.test_support import Base, SessionLocal, engine
from app.models import Customer, DraftOrder, DraftOrderItem, Order, OrderItem, Product
from app.schemas import CustomerCreate, CustomerUpdate, ProductCreate, ProductUpdate
from app.routers.products import (
    create_product,
    deactivate_product,
    get_product,
    list_products,
    update_product,
)
from app.routers.customers import (
    create_customer,
    delete_customer,
    get_customer,
    list_customers,
    update_customer,
)


class TestDay1ProductsAndCustomers(unittest.TestCase):
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

    def tearDown(self):
        self.db.close()

    def test_01_product_schema_validation(self):
        """Test ProductCreate schema validation rules."""
        # 1. Valid product creation
        p = ProductCreate(
            name="Mechanical Keyboard",
            sku="  kb-mech  ",
            category="Peripherals",
            price=Decimal("89.99"),
            stock_quantity=20,
        )
        self.assertEqual(p.sku, "KB-MECH")  # SKU normalized to uppercase & stripped

        # 2. Negative price rejected
        with self.assertRaises(ValidationError):
            ProductCreate(
                name="Keyboard",
                sku="KB-001",
                category="Peripherals",
                price=Decimal("-10.00"),
                stock_quantity=5,
            )

        # 3. Negative stock rejected
        with self.assertRaises(ValidationError):
            ProductCreate(
                name="Keyboard",
                sku="KB-001",
                category="Peripherals",
                price=Decimal("49.99"),
                stock_quantity=-1,
            )

        # 4. Empty or whitespace-only name rejected
        with self.assertRaises(ValidationError):
            ProductCreate(
                name="   ",
                sku="KB-001",
                category="Peripherals",
                price=Decimal("49.99"),
                stock_quantity=5,
            )

    def test_02_customer_schema_validation(self):
        """Test CustomerCreate schema validation rules."""
        # 1. Valid customer creation and normalization
        c = CustomerCreate(name="  Bob Smith  ", email="  BOB@Example.COM  ")
        self.assertEqual(c.name, "Bob Smith")
        self.assertEqual(c.email, "bob@example.com")

        # 2. Invalid email format rejected
        with self.assertRaises(ValidationError):
            CustomerCreate(name="Alice", email="invalid-email-address")

        # 3. Blank name rejected
        with self.assertRaises(ValidationError):
            CustomerCreate(name="   ", email="alice@example.com")

    def test_03_create_product_endpoint(self):
        """Test creating a product and rejecting duplicate SKUs."""
        prod_in = ProductCreate(
            name="Ergonomic Mouse",
            sku="MOU-ERG",
            category="Peripherals",
            price=Decimal("29.99"),
            stock_quantity=15,
        )
        created = create_product(product_in=prod_in, db=self.db)
        self.assertEqual(created.sku, "MOU-ERG")
        self.assertEqual(created.price, Decimal("29.99"))

        # Duplicate SKU must be rejected with HTTP 400
        duplicate_in = ProductCreate(
            name="Another Mouse",
            sku="mou-erg",  # Will normalize to MOU-ERG
            category="Peripherals",
            price=Decimal("39.99"),
            stock_quantity=10,
        )
        with self.assertRaises(HTTPException) as ctx:
            create_product(product_in=duplicate_in, db=self.db)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("already exists", ctx.exception.detail.lower())

    def test_04_list_products_filtering_search_pagination(self):
        """Test listing products with search, category, active, low-stock, and pagination filters."""
        p1 = Product(name="Gaming Mouse RGB", sku="MOU-RGB", category="Peripherals", price=Decimal("59.99"), stock_quantity=10, is_active=True)
        p2 = Product(name="Office Mouse", sku="MOU-OFF", category="Peripherals", price=Decimal("19.99"), stock_quantity=2, is_active=True)
        p3 = Product(name="Mechanical Keyboard", sku="KB-MECH", category="Peripherals", price=Decimal("89.99"), stock_quantity=20, is_active=True)
        p4 = Product(name="USB-C Cable", sku="CAB-001", category="Accessories", price=Decimal("9.99"), stock_quantity=1, is_active=False)
        self.db.add_all([p1, p2, p3, p4])
        self.db.commit()

        # 1. Search by name substring
        res_search = list_products(search="mouse", db=self.db)
        self.assertEqual(len(res_search), 2)

        # 2. Filter by category
        res_cat = list_products(category="Accessories", db=self.db)
        self.assertEqual(len(res_cat), 1)
        self.assertEqual(res_cat[0].sku, "CAB-001")

        # 3. Filter by active status
        res_active = list_products(is_active=True, db=self.db)
        self.assertEqual(len(res_active), 3)

        # 4. Filter by low-stock threshold (<= 2)
        res_low_stock = list_products(low_stock_threshold=2, db=self.db)
        self.assertEqual(len(res_low_stock), 2)
        low_skus = {p.sku for p in res_low_stock}
        self.assertEqual(low_skus, {"MOU-OFF", "CAB-001"})

        # 5. Pagination (skip=1, limit=2)
        res_page = list_products(skip=1, limit=2, db=self.db)
        self.assertEqual(len(res_page), 2)

    def test_05_get_and_update_product(self):
        """Test retrieving by ID and updating product details."""
        prod = Product(name="Test Monitor", sku="MON-001", category="Displays", price=Decimal("199.99"), stock_quantity=5, is_active=True)
        self.db.add(prod)
        self.db.commit()

        # Get existing product
        fetched = get_product(product_id=prod.id, db=self.db)
        self.assertEqual(fetched.name, "Test Monitor")

        # Get non-existent product returns 404
        with self.assertRaises(HTTPException) as ctx:
            get_product(product_id=99999, db=self.db)
        self.assertEqual(ctx.exception.status_code, 404)

        # Update product
        update_in = ProductUpdate(
            name="Ultra Monitor 4K",
            sku="MON-001",
            category="Displays",
            price=Decimal("249.99"),
            stock_quantity=8,
            is_active=True,
        )
        updated = update_product(product_id=prod.id, product_in=update_in, db=self.db)
        self.assertEqual(updated.name, "Ultra Monitor 4K")
        self.assertEqual(updated.price, Decimal("249.99"))

    def test_06_deactivate_product(self):
        """Test soft-deactivation of a product."""
        prod = Product(name="Discontinued Item", sku="OLD-001", category="Misc", price=Decimal("10.00"), stock_quantity=3, is_active=True)
        self.db.add(prod)
        self.db.commit()

        deactivated = deactivate_product(product_id=prod.id, db=self.db)
        self.assertFalse(deactivated.is_active)

        # Deactivating non-existent product returns 404
        with self.assertRaises(HTTPException) as ctx:
            deactivate_product(product_id=99999, db=self.db)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_07_customer_crud_and_delete(self):
        """Test customer creation, retrieval, listing, updating, and deletion."""
        # 1. Create customer
        cust_in = CustomerCreate(name="Alice Walker", email="alice@example.com")
        c1 = create_customer(customer_in=cust_in, db=self.db)
        self.assertEqual(c1.email, "alice@example.com")

        # 2. Reject duplicate email
        with self.assertRaises(HTTPException) as ctx:
            create_customer(customer_in=CustomerCreate(name="Alice Duplicate", email="alice@example.com"), db=self.db)
        self.assertEqual(ctx.exception.status_code, 400)

        # 3. Get customer
        fetched = get_customer(customer_id=c1.id, db=self.db)
        self.assertEqual(fetched.id, c1.id)

        # 4. List customers with search
        res = list_customers(search="alice", db=self.db)
        self.assertEqual(len(res), 1)

        # 5. Update customer
        upd = update_customer(customer_id=c1.id, customer_in=CustomerUpdate(name="Alice W.", email="alice.new@example.com"), db=self.db)
        self.assertEqual(upd.name, "Alice W.")
        self.assertEqual(upd.email, "alice.new@example.com")

        # 6. Delete customer with no orders succeeds
        del_res = delete_customer(customer_id=c1.id, db=self.db)
        self.assertIn("successfully deleted", del_res["message"].lower())

        # Verify customer is deleted
        with self.assertRaises(HTTPException) as ctx:
            get_customer(customer_id=c1.id, db=self.db)
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
