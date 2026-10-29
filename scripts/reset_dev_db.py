"""
Development-only database reset and seed script.

Usage:
    python scripts/reset_dev_db.py

This script:
1. Creates a safe backup of inventory.db (inventory.db.bak) if it exists.
2. Recreates all database tables fresh.
3. Seeds initial sample products and customers for Swagger UI testing.
"""

from decimal import Decimal
import os
import shutil
import sys

# Ensure root folder is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.database import Base, engine, SessionLocal
from app.models import Customer, Product


def reset_and_seed():
    db_file = "inventory.db"
    backup_file = "inventory.db.bak"

    if os.path.exists(db_file):
        shutil.copyfile(db_file, backup_file)
        print(f"Safety backup created at '{backup_file}'")

    print("Dropping existing tables and recreating schema...")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    print("Seeding starter products and customers for testing...")
    db = SessionLocal()
    try:
        # Sample customers
        c1 = Customer(name="Alice Walker", email="alice@example.com")
        c2 = Customer(name="Bob Smith", email="bob@example.com")
        db.add_all([c1, c2])
        db.flush()

        # Sample products
        p1 = Product(name="Wireless Ergonomic Mouse", sku="MOU-001", category="Peripherals", price=Decimal("29.99"), stock_quantity=25, is_active=True)
        p2 = Product(name="Gaming Mouse RGB", sku="MOU-RGB", category="Peripherals", price=Decimal("59.99"), stock_quantity=15, is_active=True)
        p3 = Product(name="Mechanical Keyboard", sku="KB-001", category="Peripherals", price=Decimal("89.99"), stock_quantity=20, is_active=True)
        p4 = Product(name="USB-C Charging Cable", sku="CAB-001", category="Accessories", price=Decimal("9.99"), stock_quantity=50, is_active=True)
        p5 = Product(name="Discontinued Webcam", sku="CAM-OLD", category="Peripherals", price=Decimal("19.99"), stock_quantity=5, is_active=False)

        db.add_all([p1, p2, p3, p4, p5])
        db.commit()

        print("Database reset and seeded successfully!")
        print(f"- Seeded {db.query(Customer).count()} customers.")
        print(f"- Seeded {db.query(Product).count()} products.")
    except Exception as e:
        db.rollback()
        print(f"Error seeding database: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    reset_and_seed()
