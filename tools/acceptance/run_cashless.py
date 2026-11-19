"""Live end-to-end acceptance run of the cashless admission against a running stack.

Prerequisites as for run_tracer.py: the stack runs with the local override (the app on
127.0.0.1:8000, MOCK_CORE_ADMIN_ENABLED on) and with the model key set; the databases are
migrated and seeded.

    uv run --project backend python tools/acceptance/run_cashless.py --out .local/acceptance

What it does, through the HTTP API only (X-User-Id picks the seeded user):
1. Picks an admission whose stay overlaps none of the patient's earlier claims, and
   generates the three uploads with tools/mockgen/cashless_claim.py.
2. Resets the patient's mock policy balances, then sends the pre-auth as the Sunrise
   hospital desk user (POST /api/submissions, typed slots).
3. Polls the staff queues and completes this claim's tasks as each role would (operator,
   bill processor, medical officer, approver, CRC, finance). At "fix fields" a flagged value
   that differs from the ground truth is corrected.
4. When the claim waits in awaiting_discharge after the pre-auth, sends the enhancement;
   after the enhancement, the discharge (POST /api/claims/{id}/requests).
5. When the claim closes, reads the stage records, events and model_calls (read-only
   SELECTs through psql in the postgres container), scores the run against
   ground_truth.json, times each request's clock, and writes report.json and report.md.

It stops early when this claim's model cost passes the budget, when no new event arrives
for --stall-seconds, when a checkpoint comes back refused, or when a query is raised (the
scenario has no missing document).
"""

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
for path in (REPO, REPO / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app.shared.seed_ids import (  # noqa: E402
    DOCTOR_USER_ID,
    FINANCE_USER_ID,
    HOSPITAL_USER_ID,
    OPS_USER_ID,
    SENIOR_USER_ID,
)
from tools.acceptance.run_tracer import (  # noqa: E402
    Api,
    as_decimal,
    at_path,
    literal,
    now,
    same,
    sql,
    timing,
)
from tools.mockgen import cashless_claim as gen  # noqa: E402

STAFF = {
    "ops": OPS_USER_ID,
    "doctor": DOCTOR_USER_ID,
    "finance": FINANCE_USER_ID,
    "senior": SENIOR_USER_ID,
}
ACTOR = "acceptance-run"
NEXT = {1: "enhancement", 2: "discharge"}  # authorisations so far -> the next request


def pick_admission() -> date:
    """The latest admission whose stay ends by today and overlaps no earlier claim of the
    patient, inside the policy period."""
    [policy] = sql(
        "select policy_start from mc_policies where policy_number = " + literal(gen.POLICY_NUMBER),
        db="mock_core",
    )
    start = date.fromisoformat(policy["policy_start"])
    taken = [
        date.fromisoformat(r["admission_date"])
        for r in sql(
            "select distinct admission_date from submissions where member_id = "
            + literal(gen.MEMBER_ID)
            + " and admission_date is not null"
        )
    ]
    stay = timedelta(days=gen.STAY_DAYS + 1)
    day = date.today() - timedelta(days=gen.STAY_DAYS)
    while any(abs(day - other) <= stay for other in taken):
        day -= timedelta(days=7)
    if day < start:
        raise SystemExit(f"no free admission date left after the policy start {start}")
    return day


def claim_cost(claim_id: str) -> tuple[float, int]:
    [row] = sql(
        "select coalesce(sum(cost), 0) as cost, count(*) as calls from model_calls "
        f"where claim_id = {literal(claim_id)}"
    )
    return float(row["cost"]), int(row["calls"])


def uploads(run_dir: Path, truth: dict[str, Any], request: str) -> list[tuple[str, Any]]:
    """Multipart parts, one per file, named by its slot. Two files may share a slot."""
    return [
        (f["doc_type"], (f["file"], (run_dir / f["file"]).read_bytes(), "application/pdf"))
        for f in truth["files"][request]
    ]


class Driver:
    def __init__(self, api: Api, truth: dict[str, Any], run_dir: Path, budget: float) -> None:
        self.api, self.truth, self.run_dir, self.budget = api, truth, run_dir, budget
        self.actions: list[dict[str, Any]] = []
        self.done: set[str] = set()
        self.sent: list[str] = ["preauth"]

    def log(self, **entry: Any) -> None:
        entry = {"at": now().isoformat(timespec="seconds"), **entry}
        self.actions.append(entry)
        print(f"  [{entry['at']}] {json.dumps({k: v for k, v in entry.items() if k != 'at'})}")

    def outcome(self, task: dict[str, Any]) -> dict[str, Any]:
        kind, payload = task["kind"], task["payload"]
        if kind == "fix_fields":
            fixes = {}
            for path, current in payload.get("fields", {}).items():
                expected = self.truth["fields"].get(path)
                value = current.get("value") if isinstance(current, dict) else None
                if expected is not None and not same(expected, value)[1]:
                    fixes[path] = expected
            return {"action": "confirm", "fields": fixes}
        return {
            "investigate": {"action": "cleared", "note": "Acceptance run"},
            "approve": {"action": "approve"},
            "crc": {"action": "uphold"},
            "release_payment": {"action": "release"},
            "query_no_reply": {"action": "proceed"},
        }.get(kind, {"action": "confirm"})

    def send(self, claim_id: str, request: str) -> None:
        response = self.api.post(
            f"/api/claims/{claim_id}/requests",
            HOSPITAL_USER_ID,
            data={"form": json.dumps({"request_type": request})},
            files=uploads(self.run_dir, self.truth, request),
        )
        self.sent.append(request)
        self.log(
            event="request_sent",
            request=request,
            submission_id=response["submission_id"],
            clock_deadline=response["clock_deadline"],
            missing=response["missing_documents"],
            warnings=response["warnings"],
        )

    def run(self, claim_id: str, *, timeout: int, stall: int) -> str:
        started, last_change, seen = time.monotonic(), time.monotonic(), 0
        while True:
            for name, user in STAFF.items():
                for task in self.api.get("/api/tasks/queue", user):
                    if task["claim_id"] != claim_id or task["id"] in self.done:
                        continue
                    full = self.api.get(f"/api/tasks/{task['id']}", user)
                    if full["payload"].get("error"):
                        self.log(event="refused", kind=full["kind"], error=full["payload"]["error"])
                        return "refused"
                    outcome = self.outcome(full)
                    self.api.post(
                        f"/api/tasks/{task['id']}/complete", user, json={"outcome": outcome}
                    )
                    self.done.add(task["id"])
                    self.log(event="task_completed", kind=task["kind"], user=name, outcome=outcome)
            detail = self.api.get(f"/api/claims/{claim_id}", HOSPITAL_USER_ID)
            if detail.get("open_query"):
                self.log(event="unexpected_query", items=detail["open_query"]["items"])
                return "query_raised"
            done = len(detail["authorisations"])
            wanted = NEXT.get(done)
            if detail["status"] == "awaiting_discharge" and wanted and wanted not in self.sent:
                self.send(claim_id, wanted)
            events = self.api.get(f"/api/claims/{claim_id}/events", OPS_USER_ID)
            if len(events) != seen:
                seen, last_change = len(events), time.monotonic()
            if any(e["stage"] == "monitoring" and e["action"] == "stage_completed" for e in events):
                return "closed"
            spent, _ = claim_cost(claim_id)
            if spent > self.budget:
                self.log(event="budget_exceeded", spent=spent)
                return "budget_exceeded"
            if time.monotonic() - last_change > stall:
                self.log(event="stalled", last_event=events[-1] if events else None)
                return "stalled"
            if time.monotonic() - started > timeout:
                self.log(event="timed_out")
                return "timed_out"
            time.sleep(2)


# --- Scoring ----------------------------------------------------------------------------------


def history(claim_id: str, stage: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = sql(
        "select version, created_by, created_at, payload from stage_records where claim_id = "
        f"{literal(claim_id)} and stage = {literal(stage)} order by version"
    )
    return rows


def _at(row: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(row["created_at"])


def _money(value: Any) -> str:
    amount = as_decimal(value)
    return f"{amount:.2f}" if amount is not None else str(value)


def score_sheet(sheet: dict[str, Any], truth: dict[str, Any]) -> dict[str, Any]:
    results = {r["rule_id"]: r for r in sheet["rule_results"]}

    def effect(rule_id: str) -> str:
        found = [r for r in sheet["rule_results"] if r["rule_id"] == rule_id]
        return _money(sum((as_decimal(r["amount_effect"]) or 0 for r in found), 0))

    rules_fired = sorted({r["rule_id"] for r in sheet["rule_results"] if not r["passed"]})
    deducted = {}
    for line in sheet["lines"]:
        for d in line.get("deductions", []):
            deducted[d["rule_id"]] = deducted.get(d["rule_id"], 0) + (as_decimal(d["amount"]) or 0)
    expected_by_rule: dict[str, Any] = {}
    for line in truth["lines"]:
        for d in line["deductions"]:
            amount = as_decimal(d["amount"]) or 0
            expected_by_rule[d["rule_id"]] = expected_by_rule.get(d["rule_id"], 0) + amount
    packages = [p["package_code"] for p in (sheet.get("package") or {}).get("packages", [])]
    return {
        "claimed": [sheet["claimed_total"], truth["claimed_total"]],
        "admissible": [sheet["admissible_total"], truth["admissible_total"]],
        "payable": [sheet["payable"], truth["payable"]],
        "payable_exact": _money(sheet["payable"]) == _money(truth["payable"]),
        "rules_fired": [rules_fired, truth["rules_failed"]],
        "deducted_by_rule": {
            r: [_money(deducted.get(r, 0)), _money(expected_by_rule.get(r, 0))]
            for r in sorted(set(deducted) | set(expected_by_rule))
        },
        "r20": [effect("R20"), truth["r20"]["amount_effect"]],
        "r26": [effect("R26"), truth["r26"]["amount_effect"], results.get("R26", {}).get("reason")],
        "packages": packages,
        "multi_procedure": "single package, paid at 100%"
        if len(packages) == 1
        else f"{len(packages)} packages",
        "lines": len(sheet["lines"]),
    }


def clocks(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per request: from request_started to its letter (communication stage_completed)."""
    out = []
    starts = [e for e in events if e["action"] == "request_started"]
    for i, start in enumerate(starts):
        began = datetime.fromisoformat(start["created_at"])
        later = [e for e in events if datetime.fromisoformat(e["created_at"]) >= began]
        answered = next(
            (
                e
                for e in later
                if e["stage"] == "communication" and e["action"] == "stage_completed"
            ),
            None,
        )
        deadline = start["detail"].get("clock_deadline")
        limit = (datetime.fromisoformat(deadline) - began).total_seconds() if deadline else None
        took = (
            (datetime.fromisoformat(answered["created_at"]) - began).total_seconds()
            if answered
            else None
        )
        nxt = starts[i + 1]["created_at"] if i + 1 < len(starts) else None
        human = sum(
            _gap(events, e)
            for e in later
            if e["action"] == "checkpoint_completed"
            and (nxt is None or e["created_at"] < nxt)
            and (answered is None or e["created_at"] <= answered["created_at"])
        )
        out.append(
            {
                "request": start["detail"]["request_type"],
                "seconds_to_answer": round(took, 1) if took is not None else None,
                "seconds_waiting_on_people": round(human, 1),
                "limit_seconds": limit,
                "within_clock": took is not None and limit is not None and took <= limit,
            }
        )
    return out


def _gap(events: list[dict[str, Any]], event: dict[str, Any]) -> float:
    i = events.index(event)
    if i == 0:
        return 0.0
    return (_at(event) - _at(events[i - 1])).total_seconds()


def score(claim_id: str, truth: dict[str, Any], api: Api) -> dict[str, Any]:
    events = api.get(f"/api/claims/{claim_id}/events", OPS_USER_ID)
    detail = api.get(f"/api/claims/{claim_id}", HOSPITAL_USER_ID)
    authorised = history(claim_id, "authorisation")
    sheets = history(claim_id, "bill_review")
    per_request = {}
    for row in authorised:
        auth = row["payload"]
        request = auth["request_type"]
        before = [s for s in sheets if _at(s) <= _at(row)]
        expected = truth["requests"].get(request)
        if expected is None or not before:
            continue
        per_request[request] = {
            "sheet": score_sheet(before[-1]["payload"], expected["sheet"]),
            "approved_now": [auth["approved_now"], expected.get("approved_now")],
            "approved_total": [auth["approved_total"], expected["approved_total"]],
            "blocked": auth["blocked"],
            "status": auth["status"],
        }
    record = history(claim_id, "digitisation")[-1]["payload"]
    fields = {}
    for path, want in truth["fields"].items():
        got = at_path(record, path)
        value = got.get("value") if isinstance(got, dict) else got
        fields[path] = {"expected": want, "got": value, "match": same(want, value)[1]}
    coding = history(claim_id, "medical_coding")[-1]["payload"]
    medical = history(claim_id, "medical_review")
    settled = history(claim_id, "settlement")[-1]["payload"]
    letters = [r["payload"]["kind"] for r in history(claim_id, "communication")]
    cost, calls = claim_cost(claim_id)
    [latency] = sql(
        "select coalesce(sum(latency_ms), 0) as ms from model_calls where claim_id = "
        + literal(claim_id)
    )
    paid = settled["payment_ref"]["amount"] if settled.get("payment_ref") else None
    return {
        "claim_id": claim_id,
        "claim_number": detail["claim_number"],
        "status": detail["status"],
        "request_path": [
            e["detail"]["request_type"] for e in events if e["action"] == "request_started"
        ],
        "requests": per_request,
        "clocks": clocks(events),
        "clock_breaches": [e["detail"] for e in events if e["action"] == "clock_breached"],
        "fields": {
            "matched": sum(f["match"] for f in fields.values()),
            "of": len(fields),
            "misses": {p: f for p, f in fields.items() if not f["match"]},
        },
        "codes": {
            "icd": [v["code"]["code"] for v in coding["verified_codes"]],
            "pcs": [p["code"]["code"] for p in coding["procedure_codes"]],
            "icd_family_ok": any(
                v["code"]["code"].startswith(truth["expected"]["icd_family"])
                for v in coding["verified_codes"]
            ),
            "pcs_family_ok": any(
                p["code"]["code"].startswith(truth["expected"]["pcs_family"])
                for p in coding["procedure_codes"]
            ),
        },
        "findings": [
            {
                "request_review": i + 1,
                "kind": f["kind"],
                "severity": f["severity"],
                "judged_by": f["judged_by"],
                "text": f["text"][:160],
            }
            for i, m in enumerate(medical)
            for f in m["payload"]["findings"]
        ],
        "query_items": [q for m in medical for q in m["payload"]["query_items"]],
        "guideline": (medical[-1]["payload"].get("guideline") or {}).get("guideline_code")
        if medical
        else None,
        "letters": [letters, truth["expected"]["letters"]],
        "payment": {
            "payee": settled.get("payee"),
            "paid": [paid, truth["expected"]["amount_paid"]],
            "paid_exact": paid is not None
            and _money(paid) == _money(truth["expected"]["amount_paid"]),
            "released": settled.get("released"),
        },
        "originals": {
            "due_at": detail["originals_due_at"],
            "received_at": detail["originals_received_at"],
        },
        "cost_usd": round(cost, 4),
        "model_calls": calls,
        "model_seconds": round(latency["ms"] / 1000, 1),
        "timing": timing(events),
    }


def markdown(report: dict[str, Any], outcome: str) -> str:
    lines = [
        f"# Cashless acceptance run: {outcome}",
        "",
        f"- Claim {report['claim_number']} ({report['claim_id']}): status {report['status']}",
        f"- Request path: {' -> '.join(report['request_path'])}",
        f"- Cost USD {report['cost_usd']} over {report['model_calls']} model calls "
        f"({report['model_seconds']} s of model time); wall {report.get('wall_seconds')} s",
        f"- Fields {report['fields']['matched']}/{report['fields']['of']}; codes "
        f"{report['codes']['icd']} {report['codes']['pcs']}; guideline {report['guideline']}",
        f"- Letters {report['letters'][0]} (expected {report['letters'][1]})",
        f"- Paid {report['payment']['paid'][0]} to {(report['payment']['payee'] or {}).get('name')}"
        f" (expected {report['payment']['paid'][1]}, exact {report['payment']['paid_exact']})",
        f"- Clock breaches: {len(report['clock_breaches'])}",
        "",
        "| Request | Payable (got / expected) | Rules fired (got / expected) | R20 | R26 | "
        "Approved total | Package |",
        "|---|---|---|---|---|---|---|",
    ]
    for request, r in report["requests"].items():
        s = r["sheet"]
        lines.append(
            f"| {request} | {s['payable'][0]} / {s['payable'][1]} | {','.join(s['rules_fired'][0])}"
            f" / {','.join(s['rules_fired'][1])} | {s['r20'][0]} / {s['r20'][1]} | "
            f"{s['r26'][0]} / {s['r26'][1]} | {r['approved_total'][0]} / {r['approved_total'][1]}"
            f" | {','.join(s['packages'])} ({s['multi_procedure']}) |"
        )
    lines += [
        "",
        "| Request | System seconds to answer | Waiting on people (s) | Limit (s) | Within |",
        "|---|---|---|---|---|",
    ]
    for c in report["clocks"]:
        lines.append(
            f"| {c['request']} | {c['seconds_to_answer']} | {c['seconds_waiting_on_people']} | "
            f"{c['limit_seconds']} | {c['within_clock']} |"
        )
    lines += ["", "Findings:"] + [
        f"- review {f['request_review']}: {f['kind']} {f['severity']} ({f['judged_by']}): "
        f"{f['text']}"
        for f in report["findings"]
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--out", type=Path, default=REPO / ".local" / "acceptance")
    parser.add_argument("--budget-usd", type=float, default=1.0, help="this claim's model cost")
    parser.add_argument("--timeout", type=int, default=2400)
    parser.add_argument("--stall-seconds", type=int, default=420)
    args = parser.parse_args()

    api = Api(args.base_url)
    api.get("/health", HOSPITAL_USER_ID)
    admission = pick_admission()
    run_dir = args.out / f"cashless-{now():%Y%m%dT%H%M%S}"
    truth = gen.generate(run_dir, admission)
    print(f"Run folder {run_dir}; admission {admission}")

    started = time.monotonic()
    api.post("/mock-core/admin/reset-policy", ACTOR, json={"policy_number": gen.POLICY_NUMBER})
    submission = api.post(
        "/api/submissions",
        HOSPITAL_USER_ID,
        data={"form": json.dumps({**truth["form"], "request_type": "preauth"})},
        files=uploads(run_dir, truth, "preauth"),
    )
    claim_id = submission["claim_id"]
    driver = Driver(api, truth, run_dir, args.budget_usd)
    driver.log(
        event="preauth_sent",
        claim_id=claim_id,
        clock=submission["clock_deadline"],
        missing=submission["missing_documents"],
        warnings=submission["warnings"],
    )
    outcome = driver.run(claim_id, timeout=args.timeout, stall=args.stall_seconds)
    wall = time.monotonic() - started
    try:
        report = score(claim_id, truth, api)
    except Exception as exc:
        events = api.get(f"/api/claims/{claim_id}/events", OPS_USER_ID)
        partial = {
            "claim_id": claim_id,
            "outcome": outcome,
            "score_error": repr(exc),
            "events": [(e["stage"], e["action"], e["created_at"]) for e in events],
            "driver_actions": driver.actions,
        }
        (run_dir / "report.json").write_text(json.dumps(partial, indent=2, default=str) + "\n")
        print(json.dumps(partial, indent=2, default=str))
        raise
    report["outcome"], report["wall_seconds"] = outcome, round(wall, 1)
    report["driver_actions"] = driver.actions
    (run_dir / "report.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    text = markdown(report, outcome)
    (run_dir / "report.md").write_text(text)
    print(text)
    if outcome != "closed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
