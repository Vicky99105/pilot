"""The acceptance driver stops when a checkpoint comes back refused by a guardrail, instead
of sending the same outcome again until the run times out."""

from pathlib import Path
from typing import Any

from app.shared.seed_ids import FINANCE_USER_ID, MEMBER_POLICY_NUMBER
from tools.acceptance.run_tracer import Driver, submit

CLAIM = "claim-1"
REFUSAL = "PermanentError: PayeeCheckFailed: payee checks failed: cheque_matches_claim_form"


class FakeApi:
    """Serves the reopened release_payment task: its payload carries the refusal."""

    def __init__(self) -> None:
        self.completed: list[str] = []
        self._next = 0

    def _task(self) -> dict[str, Any]:
        return {"id": f"task-{self._next}", "claim_id": CLAIM, "kind": "release_payment"}

    def get(self, path: str, user: Any) -> Any:
        if path == "/api/tasks/queue":
            return [self._task()] if user == FINANCE_USER_ID else []
        if path.startswith("/api/tasks/"):
            return {**self._task(), "payload": {"error": REFUSAL}}
        if path == f"/api/claims/{CLAIM}":
            return {"open_query": None}
        if path == f"/api/claims/{CLAIM}/events":
            return [{"stage": "settlement", "action": "outcome_refused"}] * (self._next + 1)
        raise AssertionError(path)

    def post(self, path: str, user: Any, **kwargs: Any) -> Any:
        self.completed.append(path)
        self._next += 1
        return None


def test_driver_stops_at_a_checkpoint_reopened_after_a_refusal(tmp_path: Path) -> None:
    api = FakeApi()
    driver = Driver(api, {}, tmp_path, budget=3.0)  # type: ignore[arg-type]

    outcome = driver.run(CLAIM, tmp_path / "receipt.pdf", timeout=5, stall=5)

    assert outcome == "refused"
    assert api.completed == []  # the refused outcome is not sent again
    assert driver.actions[-1]["event"] == "refused"
    assert driver.actions[-1]["error"] == REFUSAL


class RecordingApi:
    """Records each POST; answers the submission like the API does."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, Any, dict[str, Any]]] = []

    def post(self, path: str, user: Any, **kwargs: Any) -> Any:
        self.posts.append((path, user, kwargs))
        if path == "/api/submissions":
            return {"claim_id": CLAIM, "missing_documents": [], "warnings": []}
        return {"policy_number": MEMBER_POLICY_NUMBER, "blocks_voided": 0}


def test_each_run_resets_the_tracer_policy_before_it_submits(tmp_path: Path) -> None:
    """Live runs left utilised and blocked amounts on the tracer member's mock policy, so a
    later run failed R10. Every run now starts from a clean balance."""
    api = RecordingApi()
    merged = tmp_path / "bundle_merged.pdf"
    merged.write_bytes(b"%PDF-1.4")

    submission = submit(api, merged, {"form": {"policy_number": MEMBER_POLICY_NUMBER}})  # type: ignore[arg-type]

    assert [path for path, _, _ in api.posts] == ["/mock-core/admin/reset-policy",
                                                  "/api/submissions"]  # fmt: skip
    _, actor, reset = api.posts[0]
    assert reset == {"json": {"policy_number": MEMBER_POLICY_NUMBER}}
    assert actor == "acceptance-run"
    assert submission["claim_id"] == CLAIM
