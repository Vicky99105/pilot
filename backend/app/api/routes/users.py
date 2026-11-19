from fastapi import APIRouter

from app.api.deps import CurrentUser
from app.services.users.contract import User

router = APIRouter(prefix="/api", tags=["users"])


@router.get("/me", response_model=User)
async def me(user: CurrentUser) -> User:
    """The user the X-User-Id header names, with roles: the UI picks the areas it shows."""
    return user
