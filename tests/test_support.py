"""
Test Support Module: In-Memory SQLite Isolation and Dependency Overrides.

Ensures all automated unit tests execute against an isolated in-memory
SQLite database (`sqlite:///:memory:`) using `StaticPool`.
Tests will NEVER touch or mutate the development database `inventory.db`.
"""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app

# Isolated In-Memory Engine for Testing
test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)


@event.listens_for(test_engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

# Create schema in the in-memory database
Base.metadata.create_all(bind=test_engine)


def get_test_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


# Override FastAPI application dependency for API tests
app.dependency_overrides[get_db] = get_test_db

# Convenience aliases matching app.database
engine = test_engine
SessionLocal = TestSessionLocal
