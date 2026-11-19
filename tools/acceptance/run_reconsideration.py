"""Live acceptance of P2-6 on a closed claim: hold and resume, then reconsideration.

Prerequisites: the stack runs with the local override (the app on 127.0.0.1:8000), and the
claim given is closed (run tools/acceptance/run_tracer.py first; its report.json names it).

    uv run --project backend python tools/acceptance/run_reconsideration.py \\
        --claim-id <closed tracer claim> --out .local/acceptance

What it does, through the HTTP API only (X-User-Id picks the seeded user):
1. The member asks for a reconsideration with grounds and one page of new evidence
   (POST /api/claims/{id}/reconsideration). A new workflow run starts on the claim.
2. The supervisor holds the claim at once (POST /api/claims/{id}/hold), waits, checks that
   the claim's tasks left the queues and no stage completed while held, then resumes it.
3. The claim runs again from the evidence. The driver completes each task like run_tracer
   does, except at "confirm deductions": the bill processor pays the nursing lines R12
   deducted, with a reason (the grounds), which the override log records.
4. When the run closes it prints the events of the run, the new settlement (what the
   reconsidered decision paid on top of the earlier payment), the letter, the risk report,
   the override log and the model cost of the run.
"""

import argparse
import io
import json
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
for path in (REPO, REPO / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from app.shared.seed_ids import (  # noqa: E402
    DOCTOR_USER_ID,
    FINANCE_USER_ID,
    MEMBER_USER_ID,
    OPS_USER_ID,
    SENIOR_USER_ID,
    SUPERVISOR_USER_ID,
)
from tools.acceptance.run_tracer import Api, cost_so_far, now, sql  # noqa: E402

STAFF = {"ops": OPS_USER_ID, "doctor": DOCTOR_USER_ID, "finance": FINANCE_USER_ID,
         "senior": SENIOR_USER_ID}  # fmt: skip
GROUNDS = (
    "The nursing charges were for ICU-level nursing during the first two days after surgery, "
    "billed separately by the hospital; they are not ward nursing inside the room rent."
)
HOLD_SECONDS = 20


def evidence_pdf() -> bytes:
    """One page: the hospital's note on the nursing charges (synthetic)."""
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=A4)
    page.setFont("Helvetica-Bold", 14)
    page.drawString(72, 780, "Sunrise Multispeciality Hospital, New Delhi")
    page.setFont("Helvetica", 11)
    lines = [
        "To whom it may concern",
        "",
        "The nursing charges on the final bill were for one-to-one post-operative nursing",
        "in the surgical high dependency unit on the first two days after surgery.",
        "They are not ward nursing and are not included in the room rent.",
        "",
        "Medical Superintendent (synthetic test document)",
    ]
    for n, text in enumerate(lines):
        page.drawString(72, 740 - 18 * n, text)
    page.showPage()
    page.save()
    return buffer.getvalue()


def events(api: Api, claim_id: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = api.get(f"/api/claims/{claim_id}/events", OPS_USER_ID)
    return found


def since(found: list[dict[str, Any]], start: datetime) -> list[dict[str, Any]]:
    """The events recorded at or after start."""
    return [e for e in found if datetime.fromisoformat(e["created_at"]) >= start]


def log(**entry: Any) -> None:
    print(f"  [{now().isoformat(timespec='seconds')}] {json.dumps(entry, default=str)}")


def outcome(task: dict[str, Any]) -> dict[str, Any]:
    kind, payload = task["kind"], task["payload"]
    if kind == "confirm_deductions":
        edits = [
            {"line_id": ln["line_id"], "admissible": ln["claimed"], "reason": GROUNDS}
            for ln in payload.get("lines", [])
            if ln["head"] == "nursing" and Decimal(ln["deducted"]) > 0
        ]
        return {"action": "confirm", "lines": edits}
    if kind == "investigate":
        return {"action": "cleared", "note": "Acceptance run: the bills match the hospital"}
    if kind == "approve":
        return {"action": "approve"}
    if kind == "crc":
        return {"action": "uphold"}
    if kind == "release_payment":
        return {"action": "release"}
    if kind == "query_no_reply":
        return {"action": "proceed"}
    return {"action": "confirm"}  # labels, fields, registration, medical findings


def hold_and_resume(api: Api, claim_id: str) -> dict[str, Any]:
    reason = "Acceptance run: checking the hold and the paused clock"
    held = api.post(f"/api/claims/{claim_id}/hold", SUPERVISOR_USER_ID, json={"reason": reason})
    log(event="held", held=held)
    before = [e for e in events(api, claim_id) if e["action"] == "stage_completed"]
    time.sleep(HOLD_SECONDS)
    after = [e for e in events(api, claim_id) if e["action"] == "stage_completed"]
    queued = [
        t["kind"]
        for user in STAFF.values()
        for t in api.get("/api/tasks/queue", user)
        if t["claim_id"] == claim_id
    ]
    overview = api.get("/api/supervisor/overview", SUPERVISOR_USER_ID)
    in_view = [h for h in overview["held_claims"] if h["claim_id"] == claim_id]
    api.post(f"/api/claims/{claim_id}/resume", SUPERVISOR_USER_ID,
             json={"reason": "Acceptance run: hold checked"})  # fmt: skip
    result = {
        "stages_completed_while_held": len(after) - len(before),
        "tasks_in_queues_while_held": queued,
        "held_in_supervisor_view": bool(in_view),
    }
    log(event="resumed", **result)
    return result


def drive(
    api: Api, claim_id: str, run_id: str, run_started: datetime, *, timeout: int, budget: float
) -> str:
    done: set[str] = set()
    started = time.monotonic()
    while True:
        for name, user in STAFF.items():
            for task in api.get("/api/tasks/queue", user):
                if task["claim_id"] != claim_id or task["id"] in done:
                    continue
                full = api.get(f"/api/tasks/{task['id']}", user)
                if full["workflow_id"] != run_id:
                    continue
                if full["payload"].get("error"):
                    log(event="refused", kind=full["kind"], error=full["payload"]["error"])
                    return "refused"
                body = outcome(full)
                api.post(f"/api/tasks/{task['id']}/complete", user, json={"outcome": body})
                done.add(task["id"])
                log(event="task_completed", kind=task["kind"], user=name, outcome=body)
        run_events = since(events(api, claim_id), run_started)
        if any(e["stage"] == "monitoring" and e["action"] == "stage_completed" for e in run_events):
            return "closed"
        if cost_so_far() > budget:
            return "budget_exceeded"
        if time.monotonic() - started > timeout:
            return "timed_out"
        time.sleep(2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--claim-id", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--out", type=Path, default=REPO / ".local" / "acceptance")
    parser.add_argument("--budget-usd", type=float, default=3.0)
    parser.add_argument("--timeout", type=int, default=1500)
    args = parser.parse_args()

    api = Api(args.base_url)
    claim_id = args.claim_id
    detail = api.get(f"/api/claims/{claim_id}", MEMBER_USER_ID)
    if detail["status"] != "closed":
        raise SystemExit(f"claim {claim_id} is {detail['status']}, not closed")
    cost_before = cost_so_far()
    run_started = now()
    started = time.monotonic()
    done = api.post(
        f"/api/claims/{claim_id}/reconsideration",
        MEMBER_USER_ID,
        data={"form": json.dumps({"grounds": GROUNDS})},
        files={"other": ("nursing-note.pdf", evidence_pdf(), "application/pdf")},
    )
    log(event="reconsideration_requested", number=done["number"],
        workflow_id=done["workflow_id"], previously_paid=done["previously_paid"])  # fmt: skip
    hold = hold_and_resume(api, claim_id)
    result = drive(api, claim_id, done["workflow_id"], run_started, timeout=args.timeout,
                   budget=args.budget_usd)  # fmt: skip
    wall = round(time.monotonic() - started, 1)

    run_events = since(events(api, claim_id), run_started)
    wanted = {"reconsideration_requested", "claim_held", "clock_paused", "claim_resumed",
              "clock_resumed", "claim_closed"}  # fmt: skip
    settlements = sql(
        f"select version, payload->>'status' as status, payload->>'decided_amount' as decided,"
        f" payload->>'previously_paid' as previously_paid, payload->>'paid_amount' as paid "
        f"from stage_records where claim_id = '{claim_id}' and stage = 'settlement' "
        f"order by version"
    )
    letters = sql(
        f"select kind, content->'reconsideration' as reconsideration, content->>'amount' as amount"
        f" from letters where claim_id = '{claim_id}' and query_id is null order by created_at"
    )
    risk = sql(
        f"select payload->'hits' as hits, payload->>'score' as score, payload->>'refer' as refer,"
        f" payload->'outlier' as outlier from stage_records where claim_id = '{claim_id}' "
        f"and stage = 'risk' order by version desc limit 1"
    )
    report = {
        "claim_id": claim_id,
        "outcome": result,
        "wall_seconds": wall,
        "cost_usd": round(cost_so_far() - cost_before, 4),
        "hold": hold,
        "events": [
            (e["stage"], e["action"], e["created_at"], e["detail"])
            for e in run_events
            if e["action"] in wanted
        ],
        "settlements": settlements,
        "letters": letters,
        "risk": risk,
        "overrides": api.get(f"/api/overrides?claim_id={claim_id}", SUPERVISOR_USER_ID),
        "status": api.get(f"/api/claims/{claim_id}", MEMBER_USER_ID)["status"],
    }
    out = args.out / f"reconsideration-{now():%Y%m%dT%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps(report, indent=2, default=str))
    if result != "closed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
