"""Request dependencies. Routes get contracts from here, never implementations."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request

from app.integrations.core.contract import PermissionService
from app.services.claims.contract import ClaimQueryService
from app.services.file_store.contract import FileStore
from app.services.intake.contract import IntakeService
from app.services.monitoring.contract import Monitoring
from app.services.tasks.contract import TaskService
from app.services.users.contract import STAFF_ROLES, User, UserService


def get_task_service(request: Request) -> TaskService:
    service: TaskService = request.app.state.task_service
    return service


def get_monitoring(request: Request) -> Monitoring:
    service: Monitoring = request.app.state.monitoring
    return service


def get_user_service(request: Request) -> UserService:
    service: UserService = request.app.state.user_service
    return service


def get_intake_service(request: Request) -> IntakeService:
    service: IntakeService = request.app.state.intake_service
    return service


def get_claim_queries(request: Request) -> ClaimQueryService:
    service: ClaimQueryService = request.app.state.claim_queries
    return service


def get_file_store(request: Request) -> FileStore:
    service: FileStore = request.app.state.file_store
    return service


def get_permission_service(request: Request) -> PermissionService:
    service: PermissionService = request.app.state.permission_service
    return service


async def current_user(
    users: Annotated[UserService, Depends(get_user_service)],
    x_user_id: Annotated[str | None, Header()] = None,
) -> User:
    """Phase 1 auth: the X-User-Id header names a seeded user. Real auth comes later."""
    try:
        user_id = UUID(x_user_id or "")
    except ValueError:
        raise HTTPException(401, "X-User-Id header must be a seeded user's ID") from None
    user = await users.get(user_id)
    if user is None:
        raise HTTPException(401, "unknown user")
    return user


async def staff_user(user: Annotated[User, Depends(current_user)]) -> User:
    if not STAFF_ROLES.intersection(user.roles):
        raise HTTPException(403, "staff only")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
StaffUser = Annotated[User, Depends(staff_user)]
Tasks = Annotated[TaskService, Depends(get_task_service)]
MonitoringDep = Annotated[Monitoring, Depends(get_monitoring)]
Intake = Annotated[IntakeService, Depends(get_intake_service)]
Claims = Annotated[ClaimQueryService, Depends(get_claim_queries)]
Files = Annotated[FileStore, Depends(get_file_store)]
Permissions = Annotated[PermissionService, Depends(get_permission_service)]
