import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict
from fastapi import FastAPI
from sqlalchemy import text

from app.database import Base, engine
from app.routers import customers, drafts, orders, products

logger = logging.getLogger("app.startup")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifespan context manager.

    - Initializes database tables on startup.
    - Runs a safe, non-destructive migration to upgrade any legacy 'PENDING' order statuses
      to 'CONFIRMED' so existing databases load seamlessly without enum errors.
    - Ensures schema columns like 'confirmed_order_id' exist.
    """
    # 1. Create any missing tables (products, customers, orders, order_items, draft_orders, draft_order_items)
    Base.metadata.create_all(bind=engine)

    # 2. Non-destructive migration for existing SQLite database files:
    # Safely migrate legacy 'PENDING' status to 'CONFIRMED' without dropping tables or losing data.
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE orders SET status = 'CONFIRMED' WHERE status = 'PENDING'"))
    except Exception as exc:
        logger.debug("Legacy order status migration skipped or not needed: %s", exc)

    # 3. Non-destructive migration for Day 4:
    # Safely add confirmed_order_id column to draft_orders table if not already present.
    try:
        with engine.begin() as conn:
            result = conn.execute(text("PRAGMA table_info(draft_orders)")).fetchall()
            existing_cols = [row[1] for row in result]
            if "confirmed_order_id" not in existing_cols:
                conn.execute(
                    text("ALTER TABLE draft_orders ADD COLUMN confirmed_order_id INTEGER REFERENCES orders(id)")
                )
                logger.info("Migrated draft_orders schema: added confirmed_order_id column.")
    except Exception as exc:
        logger.warning("Draft order schema inspection or migration encountered notice: %s", exc)

    yield


app = FastAPI(
    title="Inventory and Order Management API",
    description="A modular REST API for managing products, inventory, customers, orders, and AI drafts.",
    version="1.0.0",
    lifespan=lifespan,
)

# Register route modules with clear URL prefixes and OpenAPI tags
app.include_router(products.router, prefix="/products", tags=["Products"])
app.include_router(customers.router, prefix="/customers", tags=["Customers"])
app.include_router(orders.router, prefix="/orders", tags=["Orders"])
app.include_router(drafts.router, prefix="/draft-orders", tags=["Draft Orders (AI)"])


@app.get(
    "/",
    tags=["Health"],
    summary="Health check endpoint",
)
def read_root() -> Dict[str, str]:
    """
    Simple health check endpoint returning server status.
    """
    return {
        "status": "ok",
        "message": "Inventory and Order Management API is running",
    }
