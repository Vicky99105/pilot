"""Live end-to-end acceptance run of the tracer claim against a running stack.

Prerequisites: the stack runs with the local override (the app on 127.0.0.1:8000) and with
the key for the provider in config/models.yaml set (currently GEMINI_API_KEY); the claims
and mock_core databases are migrated and seeded.

    uv run --project backend python tools/acceptance/run_tracer.py --out .local/acceptance

What it does, through the HTTP API only (X-User-Id picks the seeded user):
1. Picks an admission date that overlaps none of the member's earlier claims, so it can be
   re-run, and generates the bundle with tools/mockgen/tracer_claim.py.
2. Submits the merged PDF as the member (POST /api/submissions).
3. Polls the queues of ops, doctor, finance and the senior approver, and completes each of
   this claim's tasks with the outcome the UI sends. At "fix labels" and "fix fields" it
   acts as an operator: a flagged value that differs from the ground truth is corrected.
4. Answers the open query with the payment receipt (POST /api/queries/{id}/reply).
5. When the claim closes, reads the stage records and model_calls from Postgres (read-only
   SELECTs through `docker compose exec postgres psql`) and scores the run against
   ground_truth.json. Writes report.json and report.md to the run folder.

It stops early when the model cost in the database passes the budget, when no new event
arrives for --stall-seconds, or when a checkpoint comes back refused (outcome "refused").
"""

import argparse
import json
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[2]
for path in (REPO, REPO / "backend"):  # run as a script: import the generator and the app
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app.shared.seed_ids import (  # noqa: E402
    DOCTOR_USER_ID,
    FINANCE_USER_ID,
    MEMBER_MEMBER_ID,
    MEMBER_POLICY_NUMBER,
    MEMBER_USER_ID,
    OPS_USER_ID,
    SENIOR_USER_ID,
)
from tools.mockgen import tracer_claim as gen  # noqa: E402

COMPOSE = ["docker", "compose", "-f", "deploy/docker-compose.yml"]
STAFF = {
    "ops": OPS_USER_ID,
    "doctor": DOCTOR_USER_ID,
    "finance": FINANCE_USER_ID,
    "senior": SENIOR_USER_ID,
}
STAY = timedelta(days=gen.STAY_DAYS)
_TITLES = {"mr", "mrs", "ms", "miss", "smt", "shri", "sri", "dr"}


# --- Plumbing -----------------------------------------------------------------------------


def now() -> datetime:
    return datetime.now(UTC)


def sql(query: str, db: str = "claims") -> Any:
    """Rows of a read-only SELECT as JSON, through psql in the postgres container."""
    wrapped = f"select coalesce(json_agg(t), '[]'::json) from ({query}) t;"
    shell = f'psql -X -q -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d {db}'
    done = subprocess.run(
        [*COMPOSE, "exec", "-T", "postgres", "sh", "-c", shell],
        input=wrapped,
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(f"psql failed: {done.stderr.strip()}")
    return json.loads(done.stdout)


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class Api:
    def __init__(self, base_url: str) -> None:
        self.http = httpx.Client(base_url=base_url, timeout=120)

    def get(self, path: str, user: Any) -> Any:
        response = self.http.get(path, headers={"X-User-Id": str(user)})
        response.raise_for_status()
        return response.json() if response.content else None

    def post(self, path: str, user: Any, **kwargs: Any) -> Any:
        response = self.http.post(path, headers={"X-User-Id": str(user)}, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"POST {path} -> {response.status_code}: {response.text}")
        return response.json() if response.content else None

    def file(self, key: str, user: Any) -> bytes:
        response = self.http.get(f"/api/files/{key}", headers={"X-User-Id": str(user)})
        response.raise_for_status()
        return response.content


# --- Choosing the admission date ------------------------------------------------------------


def pick_admission() -> date:
    """The latest admission 21 days back or earlier whose stay overlaps no earlier claim
    of the member, inside the policy period."""
    [policy] = sql(
        "select policy_start, policy_end from mc_policies where policy_number = "
        + literal(MEMBER_POLICY_NUMBER),
        db="mock_core",
    )
    start = date.fromisoformat(policy["policy_start"])
    taken = [
        (date.fromisoformat(r["admission_date"]), date.fromisoformat(r["discharge"]))
        for r in sql(
            "select admission_date, coalesce(form->>'discharge_date', admission_date::text) "
            "as discharge from submissions where member_id = "
            + literal(MEMBER_MEMBER_ID)
            + " and admission_date is not null"
        )
    ]
    day = date.today() - timedelta(days=21)
    while any(
        day <= end + timedelta(days=1) and other <= day + STAY + timedelta(days=1)
        for other, end in taken
    ):
        day -= timedelta(days=7)
    if day - timedelta(days=2) < start:
        raise SystemExit(f"no free admission date left after the policy start {start}")
    return day


# --- Driving the claim --------------------------------------------------------------------


def norm_text(value: Any) -> str:
    words = re.sub(r"[^a-z0-9 ]", " ", str(value).lower()).split()
    return "".join(w for w in words if w not in _TITLES)


def as_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "")) if value is not None else None
    except InvalidOperation:
        return None


def same(expected: Any, actual: Any) -> tuple[bool, bool]:
    """(exact, normalised) match of an extracted value against the ground truth."""
    if actual is None:
        return False, False
    exact = str(actual) == str(expected)
    left, right = as_decimal(expected), as_decimal(actual)
    if left is not None and right is not None:
        return exact, left == right
    if isinstance(expected, bool) or isinstance(actual, bool):
        return exact, bool(actual) == bool(expected)
    a, e = norm_text(actual), norm_text(expected)
    if e in ("female", "male"):
        return exact, a[:1] == e[:1]
    return exact, a == e


def at_path(data: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        elif isinstance(data, dict) and part in data:
            data = data[part]
        else:
            return None
    return data


_LINE_ATTRS = {
    "head": "section",
    "date": "date",
    "description": "description",
    "qty": "qty",
    "rate": "rate",
    "amount": "amount",
}


def expected_at(truth: dict[str, Any], path: str) -> Any:
    """The ground-truth value for a ClaimRecord field path, or None when it has none."""
    if path in truth["fields"]:
        return truth["fields"][path]
    match = re.fullmatch(r"bill\.lines\.(\d+)\.(\w+)", path)
    if match:
        lines = truth["bill"]["lines"]
        index, attr = int(match.group(1)), match.group(2)
        if index < len(lines) and attr in _LINE_ATTRS:
            return lines[index][_LINE_ATTRS[attr]]
    return None


class Driver:
    def __init__(self, api: Api, truth: dict[str, Any], run_dir: Path, budget: float) -> None:
        self.api = api
        self.truth = truth
        self.run_dir = run_dir
        self.budget = budget
        self.actions: list[dict[str, Any]] = []
        self.done: set[str] = set()
        self.replied = False

    def log(self, **entry: Any) -> None:
        entry = {"at": now().isoformat(timespec="seconds"), **entry}
        self.actions.append(entry)
        print(f"  [{entry['at']}] {json.dumps({k: v for k, v in entry.items() if k != 'at'})}")

    def outcome(self, task: dict[str, Any]) -> dict[str, Any]:
        kind, payload = task["kind"], task["payload"]
        if kind == "fix_labels":
            return {"action": "confirm", "labels": self._label_fixes(payload)}
        if kind == "fix_fields":
            return {"action": "confirm", "fields": self._field_fixes(payload)}
        if kind == "investigate":
            return {"action": "cleared", "note": "Acceptance run"}
        if kind == "approve":
            return {"action": "approve"}
        if kind == "crc":
            return {"action": "uphold"}
        if kind == "release_payment":
            return {"action": "release"}
        if kind == "query_no_reply":
            return {"action": "extend"}
        return {"action": "confirm"}  # confirm_registration, confirm_deductions, medical

    def _label_fixes(self, payload: dict[str, Any]) -> dict[str, str]:
        pages = sorted(payload.get("pages", []), key=lambda p: p["seq"])
        if len(pages) == len(self.truth["files"]["merged_pages"]):
            wanted = self.truth["files"]["merged_pages"]
        else:  # the reply
            wanted = [self.truth["files"]["reply"]["doc_type"]] * len(pages)
        return {
            p["page_id"]: want
            for p, want in zip(pages, wanted, strict=True)
            if p["doc_type"] != want
        }

    def _field_fixes(self, payload: dict[str, Any]) -> dict[str, Any]:
        fixes = {}
        for path, current in payload.get("fields", {}).items():
            expected = expected_at(self.truth, path)
            if expected is None:
                continue
            value = current.get("value") if isinstance(current, dict) else None
            if not same(expected, value)[1]:
                fixes[path] = expected
        return fixes

    def run(self, claim_id: str, receipt: Path, *, timeout: int, stall: int) -> str:
        started, last_change, seen = time.monotonic(), time.monotonic(), 0
        while True:
            for name, user in STAFF.items():
                for task in self.api.get("/api/tasks/queue", user):
                    if task["claim_id"] != claim_id or task["id"] in self.done:
                        continue
                    full = self.api.get(f"/api/tasks/{task['id']}", user)
                    outcome = self.outcome(full)
                    error = full["payload"].get("error")
                    if error:
                        # The service refused this checkpoint's outcome; the driver would
                        # send the same outcome again, which cannot pass.
                        self.log(event="refused", kind=full["kind"], error=error)
                        return "refused"
                    self.api.post(
                        f"/api/tasks/{task['id']}/complete", user, json={"outcome": outcome}
                    )
                    self.done.add(task["id"])
                    self.log(
                        event="task_completed",
                        kind=task["kind"],
                        user=name,
                        outcome=outcome,
                        payload_error=full["payload"].get("error"),
                    )
            detail = self.api.get(f"/api/claims/{claim_id}", MEMBER_USER_ID)
            query = detail.get("open_query")
            if query and not self.replied:
                files = {"payment_receipt": (receipt.name, receipt.read_bytes(), "application/pdf")}
                reply = self.api.post(query["reply_url"], MEMBER_USER_ID, files=files)
                self.replied = True
                self.log(
                    event="query_replied",
                    query_items=query["items"],
                    submission_id=reply["submission_id"],
                )
            events = self.api.get(f"/api/claims/{claim_id}/events", OPS_USER_ID)
            if len(events) != seen:
                seen, last_change = len(events), time.monotonic()
            if any(e["stage"] == "monitoring" and e["action"] == "stage_completed" for e in events):
                return "closed"
            spent = cost_so_far()
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


def cost_so_far() -> float:
    [row] = sql("select coalesce(sum(cost), 0) as cost from model_calls")
    return float(row["cost"])


# --- Scoring --------------------------------------------------------------------------------


def stage_history(claim_id: str, stage: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = sql(
        "select version, created_by, created_at, payload from stage_records where claim_id = "
        f"{literal(claim_id)} and stage = {literal(stage)} order by version"
    )
    return rows


def score(claim_id: str, truth: dict[str, Any], driver: Driver, api: Api) -> dict[str, Any]:
    report: dict[str, Any] = {"claim_id": claim_id}

    # Page labels: the model's first labels for the bundle, then the reply.
    classified = stage_history(claim_id, "classification")
    model_sets = [r for r in classified if r["created_by"] == "model"]
    first = sorted(model_sets[0]["payload"]["pages"], key=lambda p: p["seq"])
    expected = truth["files"]["merged_pages"]
    labels = [
        {
            "seq": p["seq"],
            "expected": e,
            "got": p["doc_type"],
            "confidence": p["confidence"],
            "ok": p["doc_type"] == e,
        }
        for p, e in zip(first, expected, strict=False)
    ]
    reply_pages = model_sets[1]["payload"]["pages"] if len(model_sets) > 1 else []
    report["page_labels"] = {
        "bundle_correct": sum(lb["ok"] for lb in labels),
        "bundle_total": len(expected),
        "bundle_pages_seen": len(first),
        "pages": labels,
        "reply": [{"got": p["doc_type"], "confidence": p["confidence"]} for p in reply_pages],
        "missing_found": [
            m["doc_type"] for m in model_sets[0]["payload"]["mismatches"] if m["kind"] == "missing"
        ],
        "person_relabelled": len(classified) - len(model_sets),
    }

    # Fields: the model's first extraction of the bundle.
    digitised = stage_history(claim_id, "digitisation")
    record = digitised[0]["payload"]
    fields = []
    for path, want in truth["fields"].items():
        field = at_path(record, path) or {}
        got = field.get("value")
        exact, normalised = same(want, got)
        fields.append(
            {
                "field": path,
                "expected": want,
                "got": got,
                "confidence": field.get("confidence"),
                "exact": exact,
                "normalised": normalised,
            }
        )
    report["fields"] = {
        "exact": sum(f["exact"] for f in fields),
        "normalised": sum(f["normalised"] for f in fields),
        "total": len(fields),
        "detail": fields,
        "failed_checks": record.get("failed_checks", []),
        "low_confidence_fields": record.get("low_confidence_fields", []),
        "versions": [(r["version"], r["created_by"]) for r in digitised],
    }
    clinical = record.get("clinical", {})
    report["clinical"] = {
        "diagnoses": [d.get("value") for d in clinical.get("diagnoses", [])],
        "procedures": [d.get("value") for d in clinical.get("procedures", [])],
        "hospital_codes": [c["code"] for c in clinical.get("hospital_codes", [])],
        "history_durations": [
            f.get("duration") for f in clinical.get("facts", []) if f.get("duration")
        ],
        "facts": len(clinical.get("facts", [])),
    }

    # Bill lines: count, amounts by position and as a multiset, and totals.
    bill = record.get("bill", {})
    got_lines = bill.get("lines", [])
    want_lines = truth["bill"]["lines"]
    by_position = sum(
        1
        for g, w in zip(got_lines, want_lines, strict=False)
        if as_decimal(g["amount"]["value"]) == as_decimal(w["amount"])
    )
    got_amounts = Counter(as_decimal(g["amount"]["value"]) for g in got_lines)
    want_amounts = Counter(as_decimal(w["amount"]) for w in want_lines)
    report["bill"] = {
        "lines_expected": len(want_lines),
        "lines_extracted": len(got_lines),
        "amounts_matched_by_position": by_position,
        "amounts_matched_as_multiset": sum((got_amounts & want_amounts).values()),
        "descriptions_matched_by_position": sum(
            1
            for g, w in zip(got_lines, want_lines, strict=False)
            if norm_text(g["description"]["value"]) == norm_text(w["description"])
        ),
        "section_totals": [
            (s["head"], s["total"]["value"]) for s in bill.get("section_totals", [])
        ],
        "grand_total": (bill.get("grand_total") or {}).get("value"),
        "grand_total_expected": truth["bill"]["grand_total"],
    }

    # Registration and bill rules.
    registered = stage_history(claim_id, "registration")[-1]["payload"]
    report["registration"] = {
        "claim_number": registered.get("claim_number"),
        "member_match": registered["member"]["match"],
        "hospital_match": registered["hospital"]["match"],
        "differential_billing": registered["hospital"]["differential_billing"],
        "rules": {r["rule_id"]: r["passed"] for r in registered["rule_results"]},
        "rules_expected": truth["registration_rules"],
        "duplicates": registered.get("duplicates", []),
    }
    sheets = stage_history(claim_id, "bill_review")
    sheet = sheets[-1]["payload"]
    want_sheet = truth["sheet"]
    want_by_line = {ln["line_id"]: ln for ln in want_sheet["lines"]}
    lines = []
    for line in sheet["lines"]:
        want = want_by_line.get(line["line_id"], {})
        lines.append(
            {
                "line_id": line["line_id"],
                "description": line["description"],
                "head": line["head"],
                "head_expected": want.get("head"),
                "claimed": line["claimed"],
                "deducted": line["deducted"],
                "deducted_expected": want.get("deducted"),
                "rule_id": line["rule_id"],
                "rule_expected": want.get("rule_id"),
                "clause_ref": line["clause_ref"],
            }
        )
    # Every rule's deductions, summed over the lines it touched (a line can carry several).
    fired: defaultdict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for line in sheet["lines"]:
        for taken in line.get("deductions", []):
            fired[(taken["rule_id"], taken["clause_ref"])] += Decimal(taken["amount"])
    fired[("R21", "CO-PAY")] += Decimal(sheet["co_pay"])
    want_fired: defaultdict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for line in want_sheet["lines"]:
        for taken in line.get("deductions", []):
            want_fired[(taken["rule_id"], taken["clause_ref"])] += Decimal(taken["amount"])
    want_fired[("R21", "CO-PAY")] += Decimal(want_sheet["co_pay"])
    package = sheet.get("package") or {}
    report["package"] = {
        "agreement": sheet.get("agreement"),
        "candidates": [
            (c["package_code"], c["name"], c["rate"], c["rate_basis"])
            for c in package.get("candidates", [])
        ],
        "choices": [(c["procedure"], c["chosen"]) for c in package.get("choices", [])],
        "packages": [
            (p["package_code"], p["name"], p["rate"]) for p in package.get("packages", [])
        ],
        "expected": truth["expected"]["package"],
        "billed_as_package": package.get("billed_as_package"),
    }
    report["bill_findings"] = [
        (f["kind"], f.get("line_id"), f["text"]) for f in sheet.get("findings", [])
    ]
    report["bill_findings_expected"] = [
        ("drug_price", f["line_id"], f["description"]) for f in truth["drug_price_findings"]
    ]
    report["r12"] = [
        (r["passed"], r["amount_effect"], r["reason"], r["judged_by"])
        for r in sheet["rule_results"]
        if r["rule_id"] == "R12"
    ]
    report["rules_failed"] = sorted(
        {r["rule_id"] for r in sheet["rule_results"] if not r["passed"]}
    )
    report["deductions"] = {
        "rules_fired": {f"{r} {c}": str(a) for (r, c), a in fired.items()},
        "rules_expected": {f"{r} {c}": str(a) for (r, c), a in want_fired.items()},
        "claimed_total": sheet["claimed_total"],
        "admissible_total": sheet["admissible_total"],
        "co_pay": sheet["co_pay"],
        "payable": sheet["payable"],
        "payable_expected": want_sheet["payable"],
        "heads_matched": sum(1 for ln in lines if ln["head"] == ln["head_expected"]),
        "lines": lines,
        "versions": [(r["version"], r["created_by"]) for r in sheets],
    }

    # Medical review: codes and where each came from.
    medical_history = stage_history(claim_id, "medical_review")
    medical = medical_history[-1]["payload"]
    calls = {c["call_id"]: c for c in medical.get("lookups", [])}
    codes = []
    for verified in medical.get("verified_codes", []):
        code = verified["code"]["code"]
        call = calls.get(verified.get("lookup_call_id"))
        codes.append(
            {
                "code": code,
                "display": verified["code"].get("display"),
                "phrase": verified.get("phrase"),
                "from_lookup": bool(call and code in call.get("returned_codes", [])),
                "lookup": f"{call['tool']}({call['argument']!r})" if call else None,
                "family_ok": code.startswith(truth["expected"]["icd_family"]),
            }
        )
    procedures = []
    for built in medical.get("procedure_codes", []):
        steps = built.get("steps", [])
        validated = calls.get(built.get("validate_call_id"))
        procedures.append(
            {
                "code": built["code"]["code"],
                "display": built["code"].get("display"),
                "version": built["code"].get("version"),
                "phrase": built.get("phrase"),
                "steps": [
                    f"{s['position']}:{s['value']} {s['label']} ({s['chosen_by']}, "
                    f"call {s['lookup_call_id']})"
                    for s in steps
                ],
                "every_step_offered": all(
                    s["prefix"] + s["value"]
                    in calls.get(s["lookup_call_id"], {}).get("returned_codes", [])
                    for s in steps
                ),
                "validated": bool(
                    validated and built["code"]["code"] in validated.get("returned_codes", [])
                ),
                "family_ok": built["code"]["code"].startswith(truth["expected"]["pcs_family"]),
            }
        )
    report["medical"] = {
        "runs": len(medical_history),
        "codes": codes,
        "procedures": procedures,
        "findings": [(f["kind"], f["severity"], f["text"]) for f in medical.get("findings", [])],
        "query_items": medical.get("query_items", []),
        "dropped": medical.get("dropped", []),
        "guideline": medical.get("guideline"),
        "rule_results": [(r["rule_id"], r["passed"]) for r in medical.get("rule_results", [])],
        "lookups": len(calls),
        "adverse": medical.get("adverse"),
    }
    risk = stage_history(claim_id, "risk")[-1]["payload"]
    report["risk"] = {
        "score": risk["score"],
        "refer": risk["refer"],
        "hits": [h["rule_id"] for h in risk["hits"]],
    }

    # Drafts, decision, letters, settlement.
    report["drafts"] = [
        {
            "version": r["version"],
            "decision": r["payload"]["decision"],
            "expected": r["payload"]["expected_decision"],
            "amount": r["payload"]["amount"],
            "guardrail_passed": r["payload"]["guardrail_passed"],
            "guardrail_errors": r["payload"]["guardrail_errors"],
            "self_check_errors": r["payload"]["self_check_errors"],
            "revisions": r["payload"]["revisions"],
            "query_items": r["payload"]["query_items"],
            "reasons": [
                (x.get("rule_id"), x.get("clause_ref"), x.get("line_id"))
                for x in r["payload"]["reasons"]
            ],
        }
        for r in stage_history(claim_id, "adjudication")
    ]
    report["decision"] = [r["payload"] for r in stage_history(claim_id, "decision")]
    detail = api.get(f"/api/claims/{claim_id}", MEMBER_USER_ID)
    letters: list[dict[str, Any]] = []
    for letter in detail.get("letters", []):
        pdf = api.file(letter["pdf_key"], MEMBER_USER_ID)
        name = f"letter_{len(letters) + 1}_{letter['kind']}.pdf"
        (driver.run_dir / name).write_bytes(pdf)
        letters.append(
            {"kind": letter["kind"], "sent": letter["sent"], "file": name, "bytes": len(pdf)}
        )
    report["letters"] = letters
    report["queries"] = sql(
        "select status, items, raised_at, answered_at from queries where claim_id = "
        + literal(claim_id)
        + " order by raised_at"
    )
    settlement = stage_history(claim_id, "settlement")
    report["settlement"] = settlement[-1]["payload"] if settlement else None
    report["final_status"] = detail["status"]
    report["closed_at"] = detail.get("closed_at")

    # Events and time per stage.
    events = api.get(f"/api/claims/{claim_id}/events", OPS_USER_ID)
    report["events"] = [(e["stage"], e["action"], e["created_at"]) for e in events]
    report["timing"] = timing(events)

    # Model calls.
    calls_rows = sql(
        "select purpose, role, model, attempt, valid, error, input_tokens, output_tokens, "
        "cost, latency_ms from model_calls where claim_id = "
        + literal(claim_id)
        + " order by created_at"
    )
    per_purpose: dict[str, dict[str, Any]] = {}
    for c in calls_rows:
        p = per_purpose.setdefault(
            c["purpose"],
            {
                "calls": 0,
                "invalid": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost": 0.0,
                "latency_ms": 0,
            },
        )
        p["calls"] += 1
        p["invalid"] += 0 if c["valid"] else 1
        p["input_tokens"] += c["input_tokens"] or 0
        p["output_tokens"] += c["output_tokens"] or 0
        p["cost"] += float(c["cost"] or 0)
        p["latency_ms"] += c["latency_ms"] or 0
    report["model_calls"] = {
        "count": len(calls_rows),
        "invalid": sum(1 for c in calls_rows if not c["valid"]),
        "errors": [c["error"] for c in calls_rows if c["error"]],
        "models": sorted({c["model"] for c in calls_rows}),
        "input_tokens": sum(c["input_tokens"] or 0 for c in calls_rows),
        "output_tokens": sum(c["output_tokens"] or 0 for c in calls_rows),
        "cost_usd": round(sum(float(c["cost"] or 0) for c in calls_rows), 6),
        "per_purpose": per_purpose,
        "all_runs_cost_usd": round(cost_so_far(), 6),
    }
    report["driver_actions"] = driver.actions
    return report


def timing(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Seconds between consecutive events, summed per (stage, kind of wait): a stage's own
    work ends in stage_completed and similar; a checkpoint wait ends in checkpoint_completed."""
    per_stage: dict[str, float] = defaultdict(float)
    human: dict[str, float] = defaultdict(float)
    steps = []
    for before, after in pairwise(events):
        seconds = (
            datetime.fromisoformat(after["created_at"])
            - datetime.fromisoformat(before["created_at"])
        ).total_seconds()
        target = (
            human if after["action"] in ("checkpoint_completed", "query_answered") else per_stage
        )
        target[after["stage"]] += seconds
        steps.append((after["stage"], after["action"], round(seconds, 1)))
    total = 0.0
    if len(events) > 1:
        total = (
            datetime.fromisoformat(events[-1]["created_at"])
            - datetime.fromisoformat(events[0]["created_at"])
        ).total_seconds()
    return {
        "system_seconds_per_stage": {k: round(v, 1) for k, v in per_stage.items()},
        "waiting_on_people_seconds": {k: round(v, 1) for k, v in human.items()},
        "steps": steps,
        "intake_to_close_seconds": round(total, 1),
    }


def markdown(report: dict[str, Any], outcome: str, wall: float) -> str:
    r = report
    out = [f"# Tracer acceptance run: {outcome}", ""]
    out.append(
        f"- Claim {r['claim_id']}, number {r['registration']['claim_number']}, "
        f"final status **{r['final_status']}**, wall clock {wall:.0f} s "
        f"(intake to close {r['timing']['intake_to_close_seconds']} s)"
    )
    pl = r["page_labels"]
    out.append(
        f"- Page labels: {pl['bundle_correct']}/{pl['bundle_total']} correct; reply "
        f"{pl['reply']}; missing found by classification {pl['missing_found']}"
    )
    f = r["fields"]
    out.append(
        f"- Fields: {f['exact']}/{f['total']} exact, {f['normalised']}/{f['total']} "
        f"normalised; failed checks {[c['check_id'] for c in f['failed_checks']]}; "
        f"low confidence {f['low_confidence_fields']}"
    )
    b = r["bill"]
    out.append(
        f"- Bill: {b['lines_extracted']}/{b['lines_expected']} lines; amounts by "
        f"position {b['amounts_matched_by_position']}, as multiset "
        f"{b['amounts_matched_as_multiset']}; grand total {b['grand_total']} "
        f"(expected {b['grand_total_expected']})"
    )
    d = r["deductions"]
    pk = r["package"]
    out.append(
        f"- Package: {pk['packages']} (expected {pk['expected']}); choices {pk['choices']}; "
        f"candidates {pk['candidates']}"
    )
    out.append(f"- Bill findings: {r['bill_findings']}")
    out.append(f"- Bill findings expected: {r['bill_findings_expected']}")
    out.append(f"- R12 readings: {r['r12']}; rules failed {r['rules_failed']}")
    out.append(f"- Rules fired: {d['rules_fired']}")
    out.append(f"- Rules expected: {d['rules_expected']}")
    out.append(
        f"- Payable {d['payable']} (expected {d['payable_expected']}); heads matched "
        f"{d['heads_matched']}/{len(d['lines'])}"
    )
    out.append(
        f"- Registration rules: {r['registration']['rules']}; member "
        f"{r['registration']['member_match']}, hospital "
        f"{r['registration']['hospital_match']}"
    )
    for c in r["medical"]["codes"]:
        out.append(
            f"- ICD {c['code']} ({c['display']}): from lookup {c['from_lookup']} via "
            f"{c['lookup']}; K35 family {c['family_ok']}"
        )
    for p in r["medical"]["procedures"]:
        out.append(
            f"- PCS {p['code']} ({p['display']}, {p['version']}) for {p['phrase']!r}: every step "
            f"offered by pcs_options {p['every_step_offered']}, validated {p['validated']}, "
            f"0DTJ family {p['family_ok']}; steps {p['steps']}"
        )
    out.append(
        f"- Guideline: {r['medical']['guideline']}; eligibility re-run "
        f"{r['medical']['rule_results']}"
    )
    out.append(
        f"- Medical findings: {r['medical']['findings']}; query items "
        f"{r['medical']['query_items']}; dropped {r['medical']['dropped']}"
    )
    out.append(
        f"- Risk: score {r['risk']['score']}, refer {r['risk']['refer']}, hits {r['risk']['hits']}"
    )
    for draft in r["drafts"]:
        out.append(
            f"- Draft v{draft['version']}: {draft['decision']} (expected "
            f"{draft['expected']}), amount {draft['amount']}, guardrail "
            f"{draft['guardrail_passed']}, revisions {draft['revisions']}, "
            f"errors {draft['guardrail_errors'] + draft['self_check_errors']}"
        )
    out.append(f"- Letters: {[(x['kind'], x['file']) for x in r['letters']]}")
    m = r["model_calls"]
    out.append(
        f"- Model calls: {m['count']} ({m['invalid']} invalid), models {m['models']}, "
        f"tokens in {m['input_tokens']} out {m['output_tokens']}, cost USD "
        f"{m['cost_usd']} (all runs USD {m['all_runs_cost_usd']})"
    )
    out += ["", "## Events", ""]
    out += [f"{i:>2}. {s} {a} ({t})" for i, (s, a, t) in enumerate(r["events"], start=1)]
    out += ["", "## Time per stage (seconds)", ""]
    out.append(f"- System: {r['timing']['system_seconds_per_stage']}")
    out.append(f"- Waiting on people: {r['timing']['waiting_on_people_seconds']}")
    out += ["", "## Driver actions", ""]
    out += [f"- {json.dumps(a)}" for a in r["driver_actions"]]
    out += ["", "## Per purpose", ""]
    out += [f"- {k}: {v}" for k, v in m["per_purpose"].items()]
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--out", type=Path, default=REPO / ".local" / "acceptance")
    parser.add_argument(
        "--budget-usd",
        type=float,
        default=3.0,
        help="stop when model cost across all runs passes this",
    )
    parser.add_argument("--timeout", type=int, default=1800, help="seconds for the whole run")
    parser.add_argument("--stall-seconds", type=int, default=420)
    args = parser.parse_args()

    api = Api(args.base_url)
    api.get("/health", MEMBER_USER_ID)
    spent = cost_so_far()
    if spent >= args.budget_usd:
        raise SystemExit(f"budget already used: USD {spent:.4f} >= {args.budget_usd}")
    admission = pick_admission()
    run_dir = args.out / f"run-{now():%Y%m%dT%H%M%S}"
    truth = gen.generate(run_dir, admission)
    print(f"Run folder {run_dir}; admission {admission}; spent so far USD {spent:.4f}")

    started = time.monotonic()
    merged = run_dir / gen.MERGED
    submission = api.post(
        "/api/submissions",
        MEMBER_USER_ID,
        data={"form": json.dumps(truth["form"])},
        files={"merged": (merged.name, merged.read_bytes(), "application/pdf")},
    )
    claim_id = submission["claim_id"]
    driver = Driver(api, truth, run_dir, args.budget_usd)
    driver.log(
        event="submitted",
        claim_id=claim_id,
        missing=submission["missing_documents"],
        warnings=submission["warnings"],
    )
    outcome = driver.run(
        claim_id, run_dir / gen.REPLY, timeout=args.timeout, stall=args.stall_seconds
    )
    wall = time.monotonic() - started
    try:
        report = score(claim_id, truth, driver, api)
    except Exception as exc:  # a stalled run lacks later stages: keep what is known
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
    (run_dir / "report.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    text = markdown(report, outcome, wall)
    (run_dir / "report.md").write_text(text)
    print(text)
    if outcome != "closed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
