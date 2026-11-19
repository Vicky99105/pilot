"""Review-area routes for P2-6: the supervisor view, hold and resume, reassignment, the
override log, and the claimant's reconsideration, on SQLite with the seeded users."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ClaimRow
from app.database.repositories.overrides import OverrideRepository
from app.database.repositories.stage_records import StageRecordRepository
from app.database.seed import seed_claims_db
from app.main import create_app
from app.services.claims.service import DefaultClaimQueryService
from app.services.intake.service import DefaultIntakeService
from app.services.overrides.contract import OverrideEntry
from app.services.overrides.service import DefaultOverrideLog
from app.services.reconsideration.service import DefaultReconsiderationService
from app.services.supervision.service import DefaultSupervisionService
from app.services.tasks.service import DefaultTaskService
from app.services.users.service import DefaultUserService
from app.shared.seed_ids import (
    DOCTOR_USER_ID,
    MEMBER_POLICY_NUMBER,
    MEMBER_USER_ID,
    OPS_USER_ID,
    SENIOR_USER_ID,
    SUPERVISOR_USER_ID,
)
from app.shared.types import Role, Stage
from tests.api.test_task_routes import UNREACHABLE
from tests.docs import make_pdf
from tests.fakes import FakeHealthService
from tests.fakes.core import FakePermissionService
from tests.fakes.file_store import FakeFileStore
from tests.fakes.intake import FakeWorkflowStarter
from tests.fakes.model_gateway import FakeModelGateway
from tests.fakes.monitoring import FakeMonitoring
from tests.fakes.tasks import FakeWorkflowSignaller
from tests.services.claim_builders import ClaimSpec, add_digitised_claim
from tests.services.test_supervision import FakeClaimSignaller


class Api:
    def __init__(
        self,
        client: httpx.AsyncClient,
        sessions: async_sessionmaker[AsyncSession],
        tasks: DefaultTaskService,
        signaller: FakeClaimSignaller,
        starter: FakeWorkflowStarter,
    ) -> None:
        self.client, self.sessions, self.tasks = client, sessions, tasks
        self.signaller, self.starter = signaller, starter

    async def get(self, user: UUID, path: str) -> httpx.Response:
        return await self.client.get(path, headers={"X-User-Id": str(user)})

    async def post(self, user: UUID, path: str, body: dict[str, Any]) -> httpx.Response:
        return await self.client.post(path, headers={"X-User-Id": str(user)}, json=body)

    async def claim(self, status: str = "registration") -> UUID:
        claim_id = await add_digitised_claim(self.sessions, ClaimSpec(submitted_by=MEMBER_USER_ID))
        async with self.sessions.begin() as session:
            row = await session.get(ClaimRow, claim_id)
            assert row is not None
            row.status, row.workflow_id = status, str(claim_id)
            if status == "closed":
                row.closed_at = datetime.now(UTC)
        return claim_id


@pytest.fixture
async def api(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[Api]:
    await seed_claims_db(sessions)
    files, starter = FakeFileStore(), FakeWorkflowStarter()
    tasks = DefaultTaskService(sessions, FakeWorkflowSignaller())
    monitoring, signaller = FakeMonitoring(), FakeClaimSignaller()
    intake = DefaultIntakeService(sessions, files, starter, signaller=starter)
    app = create_app(
        UNREACHABLE,
        health_service=FakeHealthService(),
        task_service=tasks,
        monitoring=monitoring,
        user_service=DefaultUserService(sessions),
        intake_service=intake,
        claim_queries=DefaultClaimQueryService(sessions),
        file_store=files,
        permission_service=FakePermissionService(
            policies={MEMBER_USER_ID: {MEMBER_POLICY_NUMBER}}, hospitals={}
        ),
        model_gateway=FakeModelGateway({}),
        supervision_service=DefaultSupervisionService(sessions, tasks, monitoring, signaller),
        override_log=DefaultOverrideLog(sessions),
        reconsideration_service=DefaultReconsiderationService(sessions, intake, monitoring),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield Api(client, sessions, tasks, signaller, starter)


async def test_the_supervisor_view_is_for_supervisors_only(api: Api) -> None:
    ok = await api.get(SUPERVISOR_USER_ID, "/api/supervisor/overview")
    refused = await api.get(OPS_USER_ID, "/api/supervisor/overview")

    assert ok.status_code == 200
    assert {q["role"] for q in ok.json()["queues"]} >= {"claims_operator", "qc_auditor"}
    assert refused.status_code == 403


async def test_a_supervisor_holds_and_resumes_a_claim(api: Api) -> None:
    claim_id = await api.claim()
    closed = await api.claim(status="closed")

    held = await api.post(SUPERVISOR_USER_ID, f"/api/claims/{claim_id}/hold", {"reason": "tip-off"})
    again = await api.post(SUPERVISOR_USER_ID, f"/api/claims/{claim_id}/hold", {"reason": "x"})
    by_ops = await api.post(OPS_USER_ID, f"/api/claims/{claim_id}/resume", {"reason": "x"})
    resumed = await api.post(
        SUPERVISOR_USER_ID, f"/api/claims/{claim_id}/resume", {"reason": "verified"}
    )
    on_closed = await api.post(SUPERVISOR_USER_ID, f"/api/claims/{closed}/hold", {"reason": "x"})

    assert held.status_code == 200 and held.json()["reason"] == "tip-off"
    assert (again.status_code, by_ops.status_code, resumed.status_code) == (409, 403, 204)
    assert on_closed.status_code == 409
    assert api.signaller.sent == [("hold", str(claim_id)), ("resume", str(claim_id))]


async def test_a_supervisor_reassigns_a_task_to_an_eligible_user(api: Api) -> None:
    claim_id = await api.claim()
    task = await api.tasks.open_task(
        claim_id=claim_id, workflow_id=str(claim_id), kind="approve", role=Role.APPROVER,
        title="Approve", payload={}, deadline=datetime.now(UTC) + timedelta(hours=1),
    )  # fmt: skip

    eligible = await api.get(SUPERVISOR_USER_ID, f"/api/tasks/{task.id}/assignees")
    moved = await api.post(
        SUPERVISOR_USER_ID,
        f"/api/tasks/{task.id}/reassign",
        {"to_user_id": str(SENIOR_USER_ID), "reason": "Doctor is on leave"},
    )
    refused = await api.post(
        SUPERVISOR_USER_ID,
        f"/api/tasks/{task.id}/reassign",
        {"to_user_id": str(OPS_USER_ID), "reason": "ops cannot approve"},
    )

    assert {a["user_id"] for a in eligible.json()} == {str(DOCTOR_USER_ID), str(SENIOR_USER_ID)}
    assert moved.status_code == 200 and moved.json()["assignee"] == str(SENIOR_USER_ID)
    assert refused.status_code == 403
    assert (await api.get(OPS_USER_ID, f"/api/tasks/{task.id}/assignees")).status_code == 403


async def test_the_override_log_lists_changes_for_staff(api: Api) -> None:
    claim_id = await api.claim()
    await OverrideRepository(api.sessions).append(
        [
            OverrideEntry(
                claim_id=claim_id, stage=Stage.BILL_REVIEW, kind="deduction_edit",
                field="line 7 admissible", before="15000.00", after="5000.00",
                reason="One CT only", actor=f"user:{OPS_USER_ID}",
            )
        ]
    )  # fmt: skip

    log = await api.get(SUPERVISOR_USER_ID, f"/api/overrides?claim_id={claim_id}")
    mine = await api.get(SUPERVISOR_USER_ID, f"/api/overrides?user_id={OPS_USER_ID}")
    member = await api.get(MEMBER_USER_ID, "/api/overrides")

    assert [(o["field"], o["before"], o["after"]) for o in log.json()] == [
        ("line 7 admissible", "15000.00", "5000.00")
    ]
    assert len(mine.json()) == 1
    assert member.status_code == 403


async def test_the_claimant_asks_for_a_closed_claim_to_be_reconsidered(api: Api) -> None:
    closed = await api.claim(status="closed")
    still_open = await api.claim()
    await StageRecordRepository(api.sessions).append(
        closed, Stage.DECISION, {"final_decision": "partial", "final_amount": "100.00"},
        created_by="test",
    )  # fmt: skip

    async def ask(claim_id: UUID, user: UUID) -> httpx.Response:
        return await api.client.post(
            f"/api/claims/{claim_id}/reconsideration",
            headers={"X-User-Id": str(user)},
            data={"form": json.dumps({"grounds": "The ICU stay was medically needed"})},
            files=[("other", ("note.pdf", make_pdf(["NOTE"]), "application/pdf"))],
        )

    done = await ask(closed, MEMBER_USER_ID)
    refused = await ask(still_open, MEMBER_USER_ID)
    stranger = await ask(closed, uuid4())

    assert done.status_code == 201, done.text
    assert (done.json()["number"], done.json()["previous_decision"]) == (1, "partial")
    assert refused.status_code == 409
    assert stranger.status_code == 401
    detail = await api.get(MEMBER_USER_ID, f"/api/claims/{closed}")
    assert [r["number"] for r in detail.json()["reconsiderations"]] == [1]
    assert detail.json()["status"] == "reconsideration"
