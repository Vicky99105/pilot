from uuid import UUID

from app.services.intake.contract import WorkflowAlreadyStarted
from app.shared.types import RequestType


class FakeWorkflowStarter:
    """Records starts and refuses a repeated workflow ID, like Temporal does."""

    def __init__(self, *, unavailable: bool = False) -> None:
        self.started: dict[str, tuple[UUID, RequestType]] = {}
        self.unavailable = unavailable

    async def start_claim(
        self, *, workflow_id: str, submission_id: UUID, request_type: RequestType
    ) -> None:
        if self.unavailable:
            raise ConnectionError("temporal unreachable")
        if workflow_id in self.started:
            raise WorkflowAlreadyStarted(workflow_id)
        self.started[workflow_id] = (submission_id, request_type)
