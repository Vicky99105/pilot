"""Eval scoring: pure functions from a case's ground truth (tools/mockgen/corpus.py) and what
the system recorded (stage record payloads, queries, events, model_calls rows) to metrics.
No I/O, so tests run without the stack or a model.

Text and amounts compare the way the acceptance runner does (tools/acceptance/run_tracer.py
`same`): numbers as decimals, text lower-cased with punctuation and titles dropped.
"""

from collections import Counter, defaultdict
from collections.abc import Callable
from typing import Any

from tools.acceptance.run_tracer import as_decimal, at_path, norm_text, same

Observed = dict[str, Any]


def _value(cell: Any) -> Any:
    """An extracted field is {"value": ...}; a ground-truth value is plain."""
    return cell.get("value") if isinstance(cell, dict) else cell


def _ratio(num: float, den: float) -> float | None:
    return num / den if den else None


# --- Per case ---------------------------------------------------------------------------


def page_labels(expected: list[str], got: list[dict[str, Any]]) -> dict[str, Any]:
    pages = sorted(got, key=lambda p: p["seq"])
    pairs = list(zip(expected, (p["doc_type"] for p in pages), strict=False))
    return {
        "correct": sum(want == have for want, have in pairs),
        "total": len(expected),
        "pages_seen": len(pages),
        "confusions": [(want, have) for want, have in pairs if want != have],
    }


def fields(truth: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for path, want in truth.items():
        got = _value(at_path(record, path))
        exact, normalised = same(want, got)
        rows.append(
            {"field": path, "expected": want, "got": got, "exact": exact, "normalised": normalised}
        )
    return {
        "exact": sum(r["exact"] for r in rows),
        "normalised": sum(r["normalised"] for r in rows),
        "total": len(rows),
        "wrong": [r for r in rows if not r["normalised"]],
    }


def _line_key(line: dict[str, Any]) -> tuple[str, Any]:
    return norm_text(_value(line["description"])), as_decimal(_value(line["amount"]))


def bill_lines(want: list[dict[str, Any]], got: list[dict[str, Any]]) -> dict[str, Any]:
    """Lines matched one to one on (normalised description, amount)."""
    matched = Counter(_line_key(w) for w in want) & Counter(_line_key(g) for g in got)
    return {"matched": sum(matched.values()), "expected": len(want), "extracted": len(got)}


def deductions(want: list[dict[str, Any]], got: list[dict[str, Any]]) -> dict[str, Any]:
    """Rule IDs that took something off each line, matched by normalised description.
    A rule on a matched line in both is a true positive; only expected, a false negative;
    only recorded (or on a line with no expected counterpart), a false positive."""
    pool: dict[str, list[set[str]]] = defaultdict(list)
    for line in got:
        rules = {d["rule_id"] for d in line.get("deductions", [])}
        pool[norm_text(_value(line["description"]))].append(rules)
    per_rule: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    for line in want:
        wanted = {d["rule_id"] for d in line.get("deductions", [])}
        candidates = pool.get(norm_text(line["description"]), [])
        have = candidates.pop(0) if candidates else set()
        for rule in wanted & have:
            per_rule[rule]["tp"] += 1
        for rule in wanted - have:
            per_rule[rule]["fn"] += 1
        for rule in have - wanted:
            per_rule[rule]["fp"] += 1
    for leftovers in pool.values():
        for rules in leftovers:
            for rule in rules:
                per_rule[rule]["fp"] += 1
    totals = {k: sum(r[k] for r in per_rule.values()) for k in ("tp", "fp", "fn")}
    return {**totals, "per_rule": dict(per_rule)}


def icd(expected: str | None, got: list[str]) -> dict[str, bool]:
    if not expected:
        return {"exact": False, "three_char": False}
    return {"exact": expected in got, "three_char": any(c[:3] == expected[:3] for c in got)}


def pcs_per_char(expected: str | None, got: list[str]) -> float | None:
    """The best share of the 7 characters any recorded code gets right, by position."""
    if not expected:
        return None
    return max(
        (sum(a == b for a, b in zip(expected, c, strict=False)) / len(expected) for c in got),
        default=0.0,
    )


def package(expected: str | None, got: list[str]) -> dict[str, bool | None]:
    return {"match": None if expected is None else expected in got}


# --- Seeded problem detectors -------------------------------------------------------------


def _hits(o: Observed) -> set[str]:
    return {h["rule_id"] for h in o.get("risk", {}).get("hits", [])}


def _failed(stage: dict[str, Any], *rule_ids: str) -> bool:
    return any(r["rule_id"] in rule_ids and not r["passed"] for r in stage.get("rule_results", []))


def _finding(o: Observed, *kinds: str) -> bool:
    return any(f.get("kind") in kinds for f in o.get("medical_review", {}).get("findings", []))


def _line_rule(o: Observed, description: str, *rule_ids: str) -> bool:
    want = norm_text(description)
    return any(
        norm_text(_value(line["description"])) == want
        and any(d["rule_id"] in rule_ids for d in line.get("deductions", []))
        for line in o.get("bill_review", {}).get("lines", [])
    )


def _missing(p: dict[str, Any], o: Observed) -> bool:
    doc = p["doc_type"]
    return (
        any(
            m.get("kind") == "missing" and m.get("doc_type") == doc
            for m in o.get("classification", {}).get("mismatches", [])
        )
        or doc in o.get("intake", {}).get("missing_documents", [])
        or any(
            doc.replace("_", " ") in " ".join(map(str, q.get("items", []))).lower()
            for q in o.get("queries", [])
        )
    )


DETECTORS: dict[str, Callable[[dict[str, Any], Observed], bool]] = {
    "missing_document": _missing,
    "hidden_non_payable": lambda p, o: _line_rule(o, p["description"], "R11", "R12"),
    "unbundling": lambda p, o: _line_rule(o, p["description"], "R23"),
    "waiting_period": lambda p, o: (
        _failed(o.get("registration", {}), "R5") or _failed(o.get("medical_review", {}), "R5")
    ),
    "long_stay": lambda p, o: "RF12" in _hits(o) or _finding(o, "length_of_stay"),
    "undeclared_ped": lambda p, o: (
        _finding(o, "pre_existing") or _failed(o.get("medical_review", {}), "R6", "R8")
    ),
    "duplicate_file": lambda p, o: (
        "RF3" in _hits(o)
        or bool(o.get("submission", {}).get("duplicate_of"))
        or bool(o.get("registration", {}).get("duplicates"))
    ),
    "bank_mismatch": lambda p, o: (
        any(
            c.get("check_id") == "bank_matches_cheque"
            for c in o.get("digitisation", {}).get("failed_checks", [])
        )
        or any(
            e.get("action") == "outcome_refused" and "Payee" in str(e.get("detail"))
            for e in o.get("events", [])
        )
    ),
    "inflated_bill": lambda p, o: "RF11" in _hits(o),
    "lapsed_policy": lambda p, o: _failed(o.get("registration", {}), "R2"),
}


def detected(problem: dict[str, Any], observed: Observed) -> bool:
    return DETECTORS[problem["type"]](problem, observed)


# --- Model calls ----------------------------------------------------------------------------


def model_calls(calls: list[dict[str, Any]]) -> dict[str, Any]:
    models: dict[str, set[str]] = defaultdict(set)
    for c in calls:
        models[c["role"]].add(c["model"])
    return {
        "count": len(calls),
        "invalid": sum(1 for c in calls if not c["valid"]),
        "cost_usd": round(sum(float(c["cost"] or 0) for c in calls), 6),
        "uncosted_calls": sum(1 for c in calls if c["cost"] is None),
        "latency_ms": sum(c["latency_ms"] or 0 for c in calls),
        "models": {role: sorted(m) for role, m in sorted(models.items())},
    }


# --- Corpus summary ---------------------------------------------------------------------------


def summarise(cases: list[dict[str, Any]]) -> dict[str, Any]:
    def total(path: str) -> float:
        return sum(float(at_path(c, path) or 0) for c in cases)

    tp, fp, fn = (total(f"deductions.{k}") for k in ("tp", "fp", "fn"))
    rules: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    for c in cases:
        for rule, counts in c["deductions"]["per_rule"].items():
            for k in ("tp", "fp", "fn"):
                rules[rule][k] += counts[k]
    detection: dict[str, dict[str, int]] = defaultdict(lambda: {"detected": 0, "seeded": 0})
    for c in cases:
        for p in c["problems"]:
            detection[p["type"]]["seeded"] += 1
            detection[p["type"]]["detected"] += int(p["detected"])
    pcs = [c["pcs_per_char"] for c in cases if c["pcs_per_char"] is not None]
    packages = [c["package"]["match"] for c in cases if c["package"]["match"] is not None]
    n = len(cases)
    return {
        "cases": n,
        "page_label_accuracy": _ratio(total("page_labels.correct"), total("page_labels.total")),
        "field_accuracy_exact": _ratio(total("fields.exact"), total("fields.total")),
        "field_accuracy_normalised": _ratio(total("fields.normalised"), total("fields.total")),
        "bill_line_match": _ratio(total("bill_lines.matched"), total("bill_lines.expected")),
        "deduction_precision": _ratio(tp, tp + fp),
        "deduction_recall": _ratio(tp, tp + fn),
        "per_rule": {
            rule: {
                "precision": _ratio(r["tp"], r["tp"] + r["fp"]),
                "recall": _ratio(r["tp"], r["tp"] + r["fn"]),
            }
            for rule, r in sorted(rules.items())
        },
        "payable_exact": _ratio(sum(c["payable"]["exact"] for c in cases), n),
        "icd_exact": _ratio(sum(c["icd"]["exact"] for c in cases), n),
        "icd_three_char": _ratio(sum(c["icd"]["three_char"] for c in cases), n),
        "pcs_per_char": _ratio(sum(pcs), len(pcs)),
        "package_match": _ratio(sum(packages), len(packages)),
        "detection": dict(sorted(detection.items())),
        "query_rate": _ratio(sum(bool(c["queried"]) for c in cases), n),
        "cost_per_claim_usd": _ratio(total("model_calls.cost_usd"), n),
        "model_latency_ms_per_claim": _ratio(total("model_calls.latency_ms"), n),
        "wall_seconds_per_claim": _ratio(total("wall_seconds"), n),
        "total_cost_usd": total("model_calls.cost_usd"),
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def markdown(summary: dict[str, Any], cases: list[dict[str, Any]], header: str = "") -> str:
    s = summary
    out = [f"# Eval report{': ' + header if header else ''}", "", "| Metric | Score |", "|---|---|"]
    rows = [
        ("Page label accuracy", _pct(s["page_label_accuracy"])),
        ("Field accuracy (exact)", _pct(s["field_accuracy_exact"])),
        ("Field accuracy (normalised)", _pct(s["field_accuracy_normalised"])),
        ("Bill line match", _pct(s["bill_line_match"])),
        ("Deduction precision", _pct(s["deduction_precision"])),
        ("Deduction recall", _pct(s["deduction_recall"])),
        ("Payable exact", _pct(s["payable_exact"])),
        ("ICD exact", _pct(s["icd_exact"])),
        ("ICD 3-character", _pct(s["icd_three_char"])),
        ("PCS per-character", _pct(s["pcs_per_char"])),
        ("Package match", _pct(s["package_match"])),
        ("Query rate", _pct(s["query_rate"])),
        (
            "Cost per claim (USD)",
            "n/a" if s["cost_per_claim_usd"] is None else f"{s['cost_per_claim_usd']:.4f}",
        ),
        (
            "Wall time per claim (s)",
            "n/a" if s["wall_seconds_per_claim"] is None else f"{s['wall_seconds_per_claim']:.0f}",
        ),
        ("Total cost (USD)", f"{s['total_cost_usd']:.4f}"),
    ]
    out += [f"| {name} | {value} |" for name, value in rows]
    out += ["", "## Deductions by rule", "", "| Rule | Precision | Recall |", "|---|---|---|"]
    out += [
        f"| {rule} | {_pct(r['precision'])} | {_pct(r['recall'])} |"
        for rule, r in s["per_rule"].items()
    ]
    out += [
        "",
        "## Seeded-problem detection",
        "",
        "| Problem | Detected | Seeded |",
        "|---|---|---|",
    ]
    out += [f"| {p} | {d['detected']} | {d['seeded']} |" for p, d in s["detection"].items()]
    out += [
        "",
        "## Per claim",
        "",
        "| Case | Outcome | Payable exact | Cost (USD) | Wall (s) | Problems detected |",
        "|---|---|---|---|---|---|",
    ]
    for c in cases:
        found = ", ".join(f"{p['type']}={'yes' if p['detected'] else 'no'}" for p in c["problems"])
        out.append(
            f"| {c['case_id']} | {c.get('outcome', '')} | {c['payable']['exact']} | "
            f"{c['model_calls']['cost_usd']:.4f} | {c['wall_seconds']:.0f} | {found or '-'} |"
        )
    return "\n".join(out) + "\n"
