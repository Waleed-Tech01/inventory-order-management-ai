from typing import Generator
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

# SQLite requires check_same_thread=False when used across multiple threads in FastAPI.
# Other database engines (e.g., PostgreSQL) do not use this argument.
connect_args = (
    {"check_same_thread": False}
    if settings.database_url.startswith("sqlite")
    else {}
)

# 1. Create the SQLAlchemy Engine
# The engine manages database connections and communication with the database file.
engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    echo=False,  # Set to True if you want to inspect raw SQL statements in the console
)


# Enable SQLite Foreign Key constraint enforcement
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    if settings.database_url.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


# 2. Create the Session Factory
# SessionLocal is a factory class; each call to SessionLocal() creates a new database session.
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# 3. Define the Declarative Base class
# All database models will inherit from this Base class so SQLAlchemy knows about their tables.
class Base(DeclarativeBase):
    pass


# 4. FastAPI Dependency for database sessions
def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency that provides a transactional database session per request.
    Yields the session to the route handler and guarantees it is closed once the request finishes.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

