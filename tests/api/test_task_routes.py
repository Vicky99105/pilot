from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.services.monitoring.contract import ClaimStatusRow
from app.services.users.contract import User
from app.shared.types import Role, Stage
from tests.fakes import FakeHealthService
from tests.fakes.monitoring import FakeMonitoring
from tests.fakes.tasks import FakeTaskService
from tests.fakes.users import FakeUserService

UNREACHABLE = Settings(
    database_url="postgresql+psycopg://user:pass@127.0.0.1:1/none",
    mock_core_database_url="postgresql+psycopg://user:pass@127.0.0.1:1/none",
    temporal_address="127.0.0.1:1",
)
OPS = User(id=uuid4(), name="Ops", kind="staff", roles=[Role.CLAIMS_OPERATOR])
FINANCE = User(id=uuid4(), name="Finance", kind="staff", roles=[Role.FINANCE])
MEMBER = User(id=uuid4(), name="Member", kind="member", roles=[Role.MEMBER])
NOW = datetime(2026, 11, 19, 6, 0, tzinfo=UTC)


class Api:
    def __init__(
        self, client: httpx.AsyncClient, tasks: FakeTaskService, monitoring: FakeMonitoring
    ) -> None:
        self.client = client
        self.tasks = tasks
        self.monitoring = monitoring

    def as_user(self, user: User) -> dict[str, str]:
        return {"X-User-Id": str(user.id)}


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    tasks = FakeTaskService(roles={u.id: set(u.roles) for u in (OPS, FINANCE, MEMBER)})
    row = ClaimStatusRow(
        claim_id=uuid4(),
        claim_number="CLM-1",
        state="registration",
        entered_at=NOW,
        dwell_seconds=60,
        deadline=None,
    )
    monitoring = FakeMonitoring([row])
    app = create_app(
        UNREACHABLE,
        health_service=FakeHealthService(),
        task_service=tasks,
        monitoring=monitoring,
        user_service=FakeUserService([OPS, FINANCE, MEMBER]),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield Api(client, tasks, monitoring)


async def _open(api: Api, kind: str, role: Role, hours: int = 1) -> str:
    task = await api.tasks.open_task(
        claim_id=uuid4(),
        workflow_id=f"wf-{kind}",
        kind=kind,
        role=role,
        title=kind,
        payload={},
        deadline=NOW + timedelta(hours=hours),
    )
    return str(task.id)


@pytest.mark.parametrize("headers", [{}, {"X-User-Id": "not-a-uuid"}, {"X-User-Id": str(uuid4())}])
async def test_requests_without_a_known_user_get_401(api: Api, headers: dict[str, str]) -> None:
    response = await api.client.get("/api/tasks/queue", headers=headers)

    assert response.status_code == 401


async def test_queue_lists_tasks_for_the_users_roles(api: Api) -> None:
    await _open(api, "confirm_registration", Role.CLAIMS_OPERATOR)
    await _open(api, "release_payment", Role.FINANCE)

    response = await api.client.get("/api/tasks/queue", headers=api.as_user(OPS))

    assert response.status_code == 200
    assert [t["kind"] for t in response.json()] == ["confirm_registration"]


async def test_next_takes_a_task_or_returns_204_when_none(api: Api) -> None:
    await _open(api, "confirm_registration", Role.CLAIMS_OPERATOR)

    taken = await api.client.post("/api/tasks/next", headers=api.as_user(OPS))
    empty = await api.client.post("/api/tasks/next", headers=api.as_user(FINANCE))

    assert taken.status_code == 200
    assert (taken.json()["status"], taken.json()["assignee"]) == ("taken", str(OPS.id))
    assert empty.status_code == 204


async def test_get_task_returns_it_or_404(api: Api) -> None:
    task_id = await _open(api, "confirm_registration", Role.CLAIMS_OPERATOR)

    found = await api.client.get(f"/api/tasks/{task_id}", headers=api.as_user(OPS))
    hidden = await api.client.get(f"/api/tasks/{task_id}", headers=api.as_user(FINANCE))

    assert found.status_code == 200 and found.json()["id"] == task_id
    assert hidden.status_code == 404


async def test_complete_signals_and_maps_errors(api: Api) -> None:
    task_id = await _open(api, "confirm_registration", Role.CLAIMS_OPERATOR)
    body = {"outcome": {"action": "confirm"}}

    forbidden = await api.client.post(
        f"/api/tasks/{task_id}/complete", json=body, headers=api.as_user(FINANCE)
    )
    done = await api.client.post(
        f"/api/tasks/{task_id}/complete", json=body, headers=api.as_user(OPS)
    )
    again = await api.client.post(
        f"/api/tasks/{task_id}/complete", json=body, headers=api.as_user(OPS)
    )
    missing = await api.client.post(
        f"/api/tasks/{uuid4()}/complete", json=body, headers=api.as_user(OPS)
    )

    assert (forbidden.status_code, done.status_code, again.status_code) == (403, 200, 409)
    assert missing.status_code == 404
    assert done.json()["outcome"] == {"action": "confirm"}
    assert len(api.tasks.signaller.signals) == 1


async def test_monitoring_claims_is_for_staff_only(api: Api) -> None:
    staff = await api.client.get("/api/monitoring/claims", headers=api.as_user(OPS))
    member = await api.client.get("/api/monitoring/claims", headers=api.as_user(MEMBER))

    assert staff.status_code == 200
    assert staff.json()[0]["claim_number"] == "CLM-1"
    assert member.status_code == 403


async def test_a_claims_event_history_is_for_staff_only(api: Api) -> None:
    claim_id = uuid4()
    await api.monitoring.record_event(
        claim_id=claim_id, stage=Stage.INTAKE, action="stage_completed", actor="system"
    )
    path = f"/api/claims/{claim_id}/events"

    staff = await api.client.get(path, headers=api.as_user(OPS))
    member = await api.client.get(path, headers=api.as_user(MEMBER))

    assert staff.status_code == 200
    assert [(e["stage"], e["action"]) for e in staff.json()] == [("intake", "stage_completed")]
    assert member.status_code == 403


async def test_me_returns_the_selected_user_with_roles(api: Api) -> None:
    response = await api.client.get("/api/me", headers=api.as_user(OPS))

    assert response.status_code == 200
    assert response.json()["roles"] == ["claims_operator"]
    assert response.json()["kind"] == "staff"
    assert (await api.client.get("/api/me")).status_code == 401
