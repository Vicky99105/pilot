"""Run the eval corpus live against the isolated eval app and score it.

Prerequisites (see tools/eval/isolated.py): the app-eval container runs on 127.0.0.1:8001
with its own databases, Temporal namespace and task queue; the corpus is generated:

    uv run --project backend python tools/mockgen/corpus.py --out .local/eval/corpus
    uv run --project backend python tools/eval/run_eval.py --corpus .local/eval/corpus \
        [--cases c01-clean,c02-room-breach] [--budget-usd 1.50] [--label <model set>]
    uv run --project backend python tools/eval/run_eval.py --combine <run dir> <run dir> ...

For each case, through the HTTP API only: reset the persona's mock policy, submit the bundle
(the member for a reimbursement, the hospital for a cashless pre-auth), act the people at
each checkpoint (`outcome`), answer a query with the reply file when the case says so, send
the later cashless requests, and stop when the claim closes. Then read the stage records,
queries, events and model_calls of the claim from claims_eval (read-only SELECTs) and score
it (tools/eval/scoring.py). Writes .local/eval/<timestamp>/report.json, report.md and one
<case>.json per case (its score and everything observed). --combine re-scores saved runs
against the corpus's current ground truth and writes one report, with no model spend.

Model choice: the app reads MODEL_ROLE_OVERRIDES at start (deploy/docker-compose.eval.yml);
the report records the models each role actually used, from model_calls. The unanswered-
query case runs the real deadline path (reminders, overdue, the reject for non-submission,
the CRC): start the app with a short query clock, EVAL_QUERY_REPLY_WINDOW=PT6M and
EVAL_QUERY_REMINDERS='["PT2M","PT4M"]'. Cases that answer their query reply at once, so the
short clock does not change them.
"""

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
for _path in (REPO, REPO / "backend"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from app.shared.seed_ids import (  # noqa: E402
    DOCTOR_USER_ID,
    FINANCE_USER_ID,
    HOSPITAL_USER_ID,
    MEMBER_USER_ID,
    OPS_USER_ID,
    SENIOR_USER_ID,
)
from tools.acceptance.run_tracer import Api, literal, same, sql  # noqa: E402
from tools.eval import scoring  # noqa: E402
from tools.eval.isolated import BASE_URL, CLAIMS_DB  # noqa: E402

STAFF = {
    "ops": OPS_USER_ID,
    "doctor": DOCTOR_USER_ID,
    "finance": FINANCE_USER_ID,
    "senior": SENIOR_USER_ID,
}
NEXT = {1: "enhancement", 2: "discharge"}  # cashless: authorisations so far -> next request
NOTE = "Eval run"


# --- The people at the checkpoints --------------------------------------------------------


def _expected_labels(truth: dict[str, Any], count: int) -> list[str] | None:
    files = truth["files"]
    if files.get("merged") and len(files["merged_pages"]) == count:
        return list(files["merged_pages"])
    uploads = [files["bundle"], *files.get("requests", {}).values()]
    for upload in uploads:
        labels = [f["doc_type"] for f in upload for _ in range(f["pages"])]
        if labels and len(labels) == count:
            return labels
    reply = files.get("reply")
    return [reply["doc_type"]] * count if reply else None


def outcome(task: dict[str, Any], truth: dict[str, Any]) -> dict[str, Any]:
    """What a person sends at this checkpoint. Drafts are accepted as the system wrote them;
    an operator fixes labels and fields to what the documents say."""
    kind, payload, expect = task["kind"], task["payload"], truth["expected"]
    if kind == "fix_labels":
        pages = sorted(payload.get("pages", []), key=lambda p: p["seq"])
        wanted = _expected_labels(truth, len(pages)) or [p["doc_type"] for p in pages]
        labels = {
            p["page_id"]: want
            for p, want in zip(pages, wanted, strict=True)
            if p["doc_type"] != want
        }
        return {"action": "confirm", "labels": labels}
    if kind == "fix_fields":
        fixes = {}
        for path, current in payload.get("fields", {}).items():
            want = truth["fields"].get(path)
            value = current.get("value") if isinstance(current, dict) else None
            if want is not None and not same(want, value)[1]:
                fixes[path] = want
        failed = {c.get("check_id") for c in payload.get("failed_checks", [])}
        if expect.get("fix_bank") and "bank_matches_cheque" in failed:
            # The operator confirms the account with the member: the form's account stands.
            fixes["bank.account_number"] = truth["form"]["bank_account_number"]
        return {"action": "confirm", "fields": fixes}
    if kind == "investigate":
        return {"action": expect.get("investigator", "cleared"), "note": NOTE}
    if kind == "approve":
        # The approver accepts the draft as the system wrote it, including the reject for
        # non-submission the workflow drafts when a query passes its deadline unanswered.
        return {"action": "approve"}
    return {"crc": {"action": "uphold"}, "release_payment": {"action": "release"}}.get(
        kind, {"action": "confirm"}
    )


# --- Driving one case -----------------------------------------------------------------------


def db(query: str) -> Any:
    return sql(query, db=CLAIMS_DB)


def spent() -> float:
    [row] = db("select coalesce(sum(cost), 0) as cost from model_calls")
    return float(row["cost"])


def _parts(folder: Path, files: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    return [
        (f["doc_type"], (f["file"], (folder / f["file"]).read_bytes(), "application/pdf"))
        for f in files
    ]


def submit(api: Api, folder: Path, truth: dict[str, Any]) -> dict[str, Any]:
    api.post(
        "/mock-core/admin/reset-policy",
        "eval-run",
        json={"policy_number": truth["persona"]["policy_number"]},
    )
    files = truth["files"]
    if truth["route"] == "cashless":
        user, parts = HOSPITAL_USER_ID, _parts(folder, files["requests"]["preauth"])
    elif files["merged"]:
        merged = folder / files["merged"]
        user = MEMBER_USER_ID
        parts = [("merged", (merged.name, merged.read_bytes(), "application/pdf"))]
    else:
        user, parts = MEMBER_USER_ID, _parts(folder, files["bundle"])
    result: dict[str, Any] = api.post(
        "/api/submissions", user, data={"form": json.dumps(truth["form"])}, files=parts
    )
    return result


class Driver:
    def __init__(self, api: Api, folder: Path, truth: dict[str, Any], budget: float) -> None:
        self.api, self.folder, self.truth, self.budget = api, folder, truth, budget
        self.actions: list[dict[str, Any]] = []
        self.done: set[str] = set()
        self.sent = ["preauth"]
        self.replied = False
        self.submitter = HOSPITAL_USER_ID if truth["route"] == "cashless" else MEMBER_USER_ID

    def log(self, **entry: Any) -> None:
        entry = {"at": datetime.now(UTC).isoformat(timespec="seconds"), **entry}
        self.actions.append(entry)
        shown = {k: v for k, v in entry.items() if k != "at"}
        print(f"    {json.dumps(shown, default=str)}", flush=True)

    def step_tasks(self, claim_id: str) -> str | None:
        for name, user in STAFF.items():
            for task in self.api.get("/api/tasks/queue", user):
                if task["claim_id"] != claim_id or task["id"] in self.done:
                    continue
                full = self.api.get(f"/api/tasks/{task['id']}", user)
                act = outcome(full, self.truth)
                if full["payload"].get("error") and act.get("action") != "override":
                    self.log(event="refused", kind=full["kind"], error=full["payload"]["error"])
                    return "refused"
                self.api.post(f"/api/tasks/{task['id']}/complete", user, json={"outcome": act})
                self.done.add(task["id"])
                self.log(event="task_completed", kind=full["kind"], user=name, outcome=act)
        return None

    def step_claimant(self, claim_id: str, detail: dict[str, Any]) -> None:
        query = detail.get("open_query")
        reply = self.truth["files"].get("reply")
        if query and not self.replied and self.truth["expected"].get("query_reply", True):
            if reply:
                self.api.post(
                    query["reply_url"], self.submitter, files=_parts(self.folder, [reply])
                )
            self.replied = True
            event = "query_replied" if reply else "query_unanswerable"
            self.log(event=event, items=query["items"])
        if self.truth["route"] == "cashless" and detail["status"] == "awaiting_discharge":
            wanted = NEXT.get(len(detail.get("authorisations", [])))
            requests = self.truth["files"]["requests"]
            if wanted and wanted not in self.sent and wanted in requests:
                self.api.post(
                    f"/api/claims/{claim_id}/requests",
                    HOSPITAL_USER_ID,
                    data={"form": json.dumps({"request_type": wanted})},
                    files=_parts(self.folder, requests[wanted]),
                )
                self.sent.append(wanted)
                self.log(event="request_sent", request=wanted)

    def run(self, claim_id: str, *, timeout: int, stall: int) -> str:
        started = last = time.monotonic()
        seen = 0
        while True:
            refused = self.step_tasks(claim_id)
            if refused:
                return refused
            detail = self.api.get(f"/api/claims/{claim_id}", self.submitter)
            self.step_claimant(claim_id, detail)
            events = self.api.get(f"/api/claims/{claim_id}/events", OPS_USER_ID)
            if len(events) != seen:
                seen, last = len(events), time.monotonic()
            closed = any(
                e["stage"] == "monitoring" and e["action"] == "stage_completed" for e in events
            )
            if closed or (detail["status"] == "closed" and self.truth["route"] == "cashless"):
                return "closed"
            if spent() > self.budget:
                self.log(event="budget_exceeded")
                return "budget_exceeded"
            if time.monotonic() - last > stall:
                self.log(event="stalled", last_event=events[-1] if events else None)
                return "stalled"
            if time.monotonic() - started > timeout:
                return "timed_out"
            time.sleep(2)


# --- Reading what the system recorded -----------------------------------------------------


def _stages(claim_id: str) -> dict[str, list[dict[str, Any]]]:
    rows = db(
        "select stage, version, created_by, payload from stage_records where claim_id = "
        f"{literal(claim_id)} order by stage, version"
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(row["stage"], []).append(row)
    return out


def _latest(stages: dict[str, list[dict[str, Any]]], stage: str, by: str | None = None) -> Any:
    rows = [r for r in stages.get(stage, []) if by is None or r["created_by"] == by]
    return rows[-1]["payload"] if rows else {}


def observe(claim_id: str, submission: dict[str, Any]) -> dict[str, Any]:
    stages = _stages(claim_id)
    classified = [r for r in stages.get("classification", []) if r["created_by"] == "model"]
    claim = literal(claim_id)
    return {
        "stages": stages,
        "first_classification": classified[0]["payload"] if classified else {},
        "classification": classified[0]["payload"] if classified else {},
        "intake": _latest(stages, "intake"),
        "digitisation": _latest(stages, "digitisation", by="model"),
        "registration": _latest(stages, "registration"),
        "bill_review": _latest(stages, "bill_review") or {"lines": []},
        "medical_review": _latest(stages, "medical_review"),
        "risk": _latest(stages, "risk"),
        "adjudication": [r["payload"] for r in stages.get("adjudication", [])],
        "decision": _latest(stages, "decision"),
        "authorisation": [r["payload"] for r in stages.get("authorisation", [])],
        "settlement": _latest(stages, "settlement"),
        "queries": db(
            f"select status, items, reminders from queries where claim_id = {claim} "
            "order by raised_at"
        ),
        "events": db(
            f"select stage, action, detail, created_at from events where claim_id = {claim} "
            "order by created_at"
        ),
        "model_calls": db(
            "select purpose, role, model, valid, cost, latency_ms from model_calls "
            f"where claim_id = {claim} order by created_at"
        ),
        "submission": submission,
        "claim": (db(f"select status from claims where id = {claim}") or [{}])[0],
    }


def score_case(truth: dict[str, Any], o: dict[str, Any], outcome_: str, wall: float) -> Any:
    expect = truth["expected"]
    medical = o["medical_review"]
    icds = [v["code"]["code"] for v in medical.get("verified_codes", [])]
    pcs = [p["code"]["code"] for p in medical.get("procedure_codes", [])]
    package = o["bill_review"].get("package") or {}
    packages = [p["package_code"] for p in package.get("packages", [])]
    record = o["digitisation"]
    pages = o["first_classification"].get("pages", [])
    labels = scoring.page_labels(_expected_labels(truth, len(pages)) or [], pages)
    settled = o["settlement"].get("status") == "settled"
    paid = o["settlement"].get("paid_amount") if settled else "0"
    final = o["decision"].get("final_decision") or o["decision"].get("decision")
    drafts = [d.get("decision") for d in o["adjudication"]]
    has_bill = "discharge" in (truth["requests"] or {"discharge": 1})
    return {
        "case_id": truth["case_id"],
        "category": truth["category"],
        "outcome": outcome_,
        "end_state": {
            "status": o["claim"].get("status"),
            "final_decision": final,
            "drafts": drafts,
            "expected_decision": expect["decision"],
            "reached": outcome_ == "closed" and final == expect["decision"],
        },
        "page_labels": labels,
        "fields": scoring.fields(truth["fields"], record),
        "bill_lines": scoring.bill_lines(
            truth["bill"]["lines"] if has_bill else [], (record.get("bill") or {}).get("lines", [])
        ),
        "deductions": scoring.deductions(truth["sheet"]["lines"], o["bill_review"]["lines"]),
        "payable": {
            "exact": same(expect["amount_paid"], paid)[1],
            "got": paid,
            "expected": expect["amount_paid"],
        },
        "icd": scoring.icd(expect.get("icd_code"), icds),
        "codes": {"icd": icds, "pcs": pcs, "packages": packages},
        "pcs_per_char": scoring.pcs_per_char(expect.get("pcs_code"), pcs),
        "package": scoring.package(expect.get("package"), packages),
        "problems": [{**p, "detected": scoring.detected(p, o)} for p in truth["problems"]],
        "queried": bool(o["queries"]),
        "risk": {
            "score": o["risk"].get("score"),
            "refer": o["risk"].get("refer"),
            "hits": [h["rule_id"] for h in o["risk"].get("hits", [])],
        },
        "model_calls": scoring.model_calls(o["model_calls"]),
        "wall_seconds": round(wall, 1),
    }


# --- Reports ------------------------------------------------------------------------------


def run_case(api: Api, folder: Path, args: argparse.Namespace, out: Path) -> dict[str, Any]:
    truth = json.loads((folder / "ground_truth.json").read_text())
    print(f"  {truth['case_id']} ({truth['category']})", flush=True)
    started = time.monotonic()
    submission = submit(api, folder, truth)
    claim_id = submission["claim_id"]
    driver = Driver(api, folder, truth, args.budget_usd)
    driver.log(
        event="submitted",
        claim_id=claim_id,
        duplicate_of=submission.get("duplicate_of"),
        missing=submission.get("missing_documents"),
    )
    result = driver.run(claim_id, timeout=args.timeout, stall=args.stall_seconds)
    wall = time.monotonic() - started
    observed = observe(claim_id, submission)
    scored: dict[str, Any] = score_case(truth, observed, result, wall)
    scored["claim_id"], scored["driver_actions"] = claim_id, driver.actions
    (out / f"{truth['case_id']}.json").write_text(
        json.dumps({"score": scored, "observed": observed}, indent=2, default=str) + "\n"
    )
    print(
        f"    -> {result}; end state {scored['end_state']}; cost USD "
        f"{scored['model_calls']['cost_usd']:.4f}; {wall:.0f} s",
        flush=True,
    )
    return scored


def write_report(out: Path, cases: list[dict[str, Any]], label: str, before: float) -> None:
    summary = scoring.summarise(cases)
    summary |= {
        "label": label,
        "spent_before_usd": before,
        "spent_after_usd": spent(),
        "end_states_reached": sum(c["end_state"]["reached"] for c in cases),
        "models": sorted(
            {f"{r}:{m}" for c in cases for r, ms in c["model_calls"]["models"].items() for m in ms}
        ),
        "isolation": f"database {CLAIMS_DB}, Temporal namespace eval, task queue "
        "claims-eval, app on 127.0.0.1:8001 (tools/eval/isolated.py)",
    }
    (out / "report.json").write_text(
        json.dumps({"summary": summary, "cases": cases}, indent=2, default=str) + "\n"
    )
    text = scoring.markdown(summary, cases, header=label)
    text += f"\n- End states reached: {summary['end_states_reached']}/{len(cases)}\n"
    text += f"- Models: {summary['models']}\n- Isolation: {summary['isolation']}\n"
    (out / "report.md").write_text(text)
    print(text)


def combine(runs: list[Path], corpus: Path, out_root: Path, label: str) -> None:
    """One report over the cases of several runs (the last run of a case wins), re-scored
    from each case's saved observations against the corpus's current ground truth, so a
    scoring or ground-truth fix needs no new model spend."""
    by_case: dict[str, dict[str, Any]] = {}
    for run in runs:
        for path in sorted(run.glob("c*.json")):
            saved = json.loads(path.read_text())
            old = saved["score"]
            truth = json.loads((corpus / old["case_id"] / "ground_truth.json").read_text())
            new = score_case(truth, saved["observed"], old["outcome"], old["wall_seconds"])
            new["claim_id"], new["driver_actions"] = old["claim_id"], old["driver_actions"]
            new["run"] = run.name
            by_case[old["case_id"]] = new
    out = out_root / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-combined")
    out.mkdir(parents=True, exist_ok=True)
    write_report(out, [by_case[k] for k in sorted(by_case)], label, 0.0)


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--corpus", type=Path, default=REPO / ".local" / "eval" / "corpus")
    parser.add_argument("--cases", default="", help="comma-separated case IDs (default: all)")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--out", type=Path, default=REPO / ".local" / "eval")
    parser.add_argument(
        "--budget-usd",
        type=float,
        default=1.50,
        help="stop when model cost in claims_eval passes this",
    )
    parser.add_argument("--timeout", type=int, default=1500)
    parser.add_argument("--stall-seconds", type=int, default=420)
    parser.add_argument("--label", default="", help="a name for this run, e.g. the model set")
    parser.add_argument("--combine", type=Path, nargs="*", help="earlier run folders to merge")
    args = parser.parse_args()
    if args.combine:
        combine(args.combine, args.corpus, args.out, args.label)
        return
    api = Api(args.base_url)
    api.get("/health", MEMBER_USER_ID)
    wanted = [c for c in args.cases.split(",") if c]
    folders = sorted(p for p in args.corpus.iterdir() if (p / "ground_truth.json").exists())
    folders = [f for f in folders if not wanted or f.name in wanted]
    out = args.out / datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    before = spent()
    print(f"Eval run {out}; {len(folders)} cases; spent before USD {before:.4f}", flush=True)
    cases = []
    for folder in folders:
        if spent() >= args.budget_usd:
            print(f"  budget reached (USD {spent():.4f}); stopping before {folder.name}")
            break
        cases.append(run_case(api, folder, args, out))
    write_report(out, cases, args.label, before)


if __name__ == "__main__":
    main()
