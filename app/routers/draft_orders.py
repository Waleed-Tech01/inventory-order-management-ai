"""
Draft Orders Router Alias.

Re-exports route handlers and the APIRouter instance from `app.routers.drafts`.
Maintained for backwards-compatible imports across external clients and tests.
"""
from app.routers.drafts import (
    confirm_draft_order,
    create_draft_order,
    get_draft_order,
    list_draft_orders,
    reject_draft_order,
    router,
)

__all__ = [
    "router",
    "create_draft_order",
    "list_draft_orders",
    "get_draft_order",
    "confirm_draft_order",
    "reject_draft_order",
]
