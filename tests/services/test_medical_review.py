"""Medical review through its contract: the LangGraph agent with a scripted model gateway,
the real knowledge base on the seeded ICD-10-CM fixture, and a real registration."""

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.repositories.stage_records import StageRecordRepository
from app.database.seed import seed_claims_db
from app.services.knowledge_base.service import DefaultKnowledgeBase
from app.services.medical_review.contract import (
    Citation,
    DocumentRequest,
    MedicalFindings,
    MedicalReviewService,
    NotReadyForMedicalReview,
)
from app.services.medical_review.service import DefaultMedicalReviewService
from app.services.registration.service import DefaultRegistrationService
from app.services.rules.service import DefaultRulesEngine
from app.shared.types import DocType, Stage
from tests.database.factories import add_claim
from tests.fakes.core import FakeMoneyService, FakePolicyService
from tests.fakes.model_gateway import FakeModelGateway
from tests.services.claim_builders import (
    DIAGNOSIS,
    DISCHARGE_DOC,
    ClaimSpec,
    add_digitised_claim,
    appendicitis,
    fact,
    page,
)
from tests.services.test_registration import asha_policy

APPENDIX_TERM = "appendicitis acute perforation localized peritonitis"
REWRITE = {"items": [{"phrase": DIAGNOSIS, "index_terms": [APPENDIX_TERM]}]}


def cite(n: int) -> str:
    return f"page:{DISCHARGE_DOC}:{n}"


def enum_at(schema: type[BaseModel] | None, *path: str) -> list[Any]:
    """The enum of a field of a reply schema, following arrays and $refs along the path."""
    assert schema is not None
    root = schema.model_json_schema()
    node: dict[str, Any] = root
    for name in path:
        node = node["properties"][name]
        node = next((n for n in node.get("anyOf", [node]) if n.get("type") != "null"), node)
        while "items" in node or "$ref" in node:
            if "items" in node:
                node = node["items"]
            if "$ref" in node:
                node = root["$defs"][node["$ref"].split("/")[-1]]
    return list(node["enum"])


def k35_32(*citations: str) -> dict[str, Any]:
    return {
        "code": "K35.32",
        "phrase": DIAGNOSIS,
        "citations": list(citations) or [cite(1), cite(2)],
        "rationale": "Perforation with localised peritonitis and no abscess on CT",
    }


def choose(*codes: dict[str, Any], findings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"codes": list(codes), "findings": findings or [], "query_items": []}


@dataclass
class Env:
    sessions: async_sessionmaker[AsyncSession]
    policies: FakePolicyService

    async def claim(self, spec: ClaimSpec | None = None) -> UUID:
        claim_id = await add_digitised_claim(self.sessions, spec)
        rules = DefaultRulesEngine(Settings().config_dir / "products")
        registration = DefaultRegistrationService(
            self.sessions, self.policies, FakeMoneyService(), rules
        )
        await registration.register(claim_id)
        return claim_id

    def service(self, gateway: FakeModelGateway) -> MedicalReviewService:
        return DefaultMedicalReviewService(
            self.sessions,
            gateway,
            DefaultKnowledgeBase(self.sessions),
            DefaultRulesEngine(Settings().config_dir / "products"),
            self.policies,
        )


@pytest.fixture
async def env(sessions: async_sessionmaker[AsyncSession]) -> Env:
    await seed_claims_db(sessions)
    return Env(sessions, FakePolicyService([asha_policy()]))


def codes(findings: MedicalFindings) -> list[str]:
    return [v.code.code for v in findings.verified_codes]


async def test_happy_path_codes_from_the_lookup_with_citations(env: Env) -> None:
    claim_id = await env.claim()
    gateway = FakeModelGateway(
        {"medical_rewrite_terms": [REWRITE], "medical_choose_codes": [choose(k35_32())]}
    )

    findings = await env.service(gateway).review(claim_id)

    assert codes(findings) == ["K35.32"]
    [verified] = findings.verified_codes
    assert verified.code.system == "ICD-10-CM"
    assert verified.code.display == (
        "Acute appendicitis with perforation and localized peritonitis, without abscess"
    )
    lookup = next(c for c in findings.lookups if c.call_id == verified.lookup_call_id)
    assert (lookup.tool, lookup.argument) == ("search_icd_index", APPENDIX_TERM)
    assert "K35.32" in lookup.returned_codes
    assert verified.citations == [Citation(page=page(1)), Citation(page=page(2))]
    # The hospital wrote K35.8; the review found a more specific code.
    [hospital] = [f for f in findings.findings if f.kind == "hospital_codes"]
    assert hospital.severity == "info" and "K35.8" in hospital.text
    assert hospital.citations
    assert [(r.rule_id, r.passed) for r in findings.rule_results] == [("R5", True)]
    assert findings.adverse is False
    assert (findings.revisions, findings.dropped) == (0, [])
    assert [c.purpose for c in gateway.calls] == ["medical_rewrite_terms", "medical_choose_codes"]


async def test_the_reply_schema_allows_only_billable_codes_the_lookups_returned(
    env: Env,
) -> None:
    claim_id = await env.claim()
    gateway = FakeModelGateway(
        {"medical_rewrite_terms": [REWRITE], "medical_choose_codes": [choose(k35_32())]}
    )

    findings = await env.service(gateway).review(claim_id)

    schema = gateway.calls[-1].schema
    allowed = enum_at(schema, "codes", "code")
    returned = {code for call in findings.lookups for code in call.returned_codes}
    assert "K35.32" in allowed and set(allowed) <= returned
    assert "K35.3" not in allowed  # a category: returned by the lookup, but not billable
    assert "K35.9" not in allowed  # never returned by a lookup
    invented = {"code": "K35.9", "phrase": DIAGNOSIS, "citations": [cite(1)], "rationale": "x"}
    assert schema is not None
    with pytest.raises(ValidationError):
        schema.model_validate(choose(k35_32(), invented))
    cites = enum_at(schema, "findings", "citations")
    assert cite(1) in cites and "clause:Excl01" in cites and "icd:K35.32" in cites
    assert cite(9) not in cites  # not a page of the claim


async def test_uncited_findings_are_sent_back_once_then_dropped(env: Env) -> None:
    claim_id = await env.claim()
    fixed = {"text": "Perforation seen on CT", "severity": "info", "citations": []}
    bad_page = {"text": "Wound infection", "severity": "query", "citations": []}
    gateway = FakeModelGateway(
        {
            "medical_rewrite_terms": [REWRITE],
            "medical_choose_codes": [
                choose(k35_32(), findings=[fixed, bad_page]),
                choose(k35_32(), findings=[{**fixed, "citations": [cite(2)]}, bad_page]),
            ],
        }
    )

    findings = await env.service(gateway).review(claim_id)

    kept = [f for f in findings.findings if f.judged_by == "model"]
    assert [(f.text, f.citations) for f in kept] == [
        ("Perforation seen on CT", [Citation(page=page(2))])
    ]
    [dropped] = findings.dropped
    assert (dropped.item, dropped.detail) == ("finding", "Wound infection")
    assert "no valid citation" in dropped.reason
    assert findings.revisions == 1


async def test_a_history_older_than_the_policy_is_an_adverse_pre_existing_finding(
    env: Env,
) -> None:
    diabetes = fact("history", "Known case of type 2 diabetes mellitus", 1, "since 5 years")
    claim_id = await env.claim(ClaimSpec(clinical=appendicitis(diabetes)))
    rewrite = {
        "items": [
            {"phrase": DIAGNOSIS, "index_terms": [APPENDIX_TERM]},
            {"phrase": diabetes.text, "index_terms": ["diabetes type 2"]},
        ]
    }
    e11 = {"code": "E11.9", "phrase": diabetes.text, "citations": [cite(1)], "rationale": "T2DM"}
    gateway = FakeModelGateway(
        {"medical_rewrite_terms": [rewrite], "medical_choose_codes": [choose(k35_32(), e11)]}
    )

    findings = await env.service(gateway).review(claim_id)

    assert codes(findings) == ["K35.32", "E11.9"]
    [ped] = [f for f in findings.findings if f.kind == "pre_existing"]
    assert ped.severity == "adverse" and ped.judged_by == "code"
    assert "2024-04-01" in ped.text  # the policy inception date
    assert Citation(page=page(1)) in ped.citations
    assert Citation(ref="clause:Excl01") in ped.citations
    assert findings.adverse is True


async def test_a_long_stay_with_no_stated_reason_is_an_info_finding(env: Env) -> None:
    claim_id = await env.claim(ClaimSpec(discharge_date=date(2026, 11, 12)))
    gateway = FakeModelGateway(
        {"medical_rewrite_terms": [REWRITE], "medical_choose_codes": [choose(k35_32())]}
    )

    findings = await env.service(gateway).review(claim_id)

    [stay] = [f for f in findings.findings if f.kind == "length_of_stay"]
    assert stay.severity == "info" and "10 days" in stay.text
    assert findings.adverse is False


async def test_a_failed_initial_wait_on_rerun_is_adverse(env: Env) -> None:
    env.policies.policies = [
        asha_policy(inception_date=date(2026, 10, 20), continuous_cover_start=date(2026, 10, 20))
    ]
    claim_id = await env.claim()
    gateway = FakeModelGateway(
        {"medical_rewrite_terms": [REWRITE], "medical_choose_codes": [choose(k35_32())]}
    )

    findings = await env.service(gateway).review(claim_id)

    assert [(r.rule_id, r.passed) for r in findings.rule_results] == [("R5", False)]
    [eligibility] = [f for f in findings.findings if f.kind == "eligibility"]
    assert eligibility.severity == "adverse"
    assert eligibility.citations == [Citation(ref="clause:Excl03")]
    assert findings.adverse is True


async def test_review_is_idempotent_per_record_version(env: Env) -> None:
    claim_id = await env.claim()
    gateway = FakeModelGateway(
        {"medical_rewrite_terms": [REWRITE], "medical_choose_codes": [choose(k35_32())]}
    )
    service = env.service(gateway)

    first = await service.review(claim_id)
    again = await service.review(claim_id)

    assert again == first
    assert len(gateway.calls) == 2  # no new model calls


async def test_a_claim_without_registration_is_not_ready(env: Env) -> None:
    claim_id = await add_claim(env.sessions, status=Stage.MEDICAL_REVIEW.value)

    with pytest.raises(NotReadyForMedicalReview):
        await env.service(FakeModelGateway({})).review(claim_id)


async def test_an_item_the_claimant_already_answered_is_not_asked_again(env: Env) -> None:
    """After a query reply the review runs again. The model sees what was answered, and an
    item for a document type already answered is dropped, however it is worded, so the same
    query is never raised twice."""
    claim_id = await env.claim()
    answered = {
        "query_id": "6f1c1d1e-5a43-4a8e-9a55-0c1a1f0c0002",
        "status": "answered",
        "items": [
            {
                "text": "Histopathology report",
                "source": "medical",
                "doc_type": "investigation_report",
            }
        ],
    }
    records = StageRecordRepository(env.sessions)
    await records.append(claim_id, Stage.QUERY, answered, created_by="test")
    reply = {
        **choose(k35_32()),
        "query_items": [{"doc_type": "ot_notes", "text": "Operation theatre notes"}],
    }
    gateway = FakeModelGateway(
        {"medical_rewrite_terms": [REWRITE], "medical_choose_codes": [reply]}
    )

    findings = await env.service(gateway).review(claim_id)

    assert findings.query_items == [
        DocumentRequest(doc_type=DocType.OT_NOTES, text="Operation theatre notes")
    ]
    schema = gateway.calls[-1].schema
    assert "investigation_report" not in enum_at(schema, "query_items", "doc_type")
    assert schema is not None
    asked_again = {"doc_type": "investigation_report", "text": "HPE report of the appendix"}
    with pytest.raises(ValidationError):
        schema.model_validate({**choose(k35_32()), "query_items": [asked_again]})


async def test_query_items_are_only_documents_the_claim_does_not_have(env: Env) -> None:
    """A query item names a medical document type the claim does not hold. The reply schema
    lists only those, so the model cannot ask for a document the claim already has."""
    claim_id = await env.claim()
    received = {"documents": [{"doc_type": "discharge_summary"}, {"doc_type": "final_bill"}]}
    records = StageRecordRepository(env.sessions)
    await records.append(claim_id, Stage.CLASSIFICATION, received, created_by="test")
    reply = {
        **choose(k35_32()),
        "query_items": [{"doc_type": "ot_notes", "text": "Complete OT notes"}],
    }
    gateway = FakeModelGateway(
        {"medical_rewrite_terms": [REWRITE], "medical_choose_codes": [reply]}
    )

    findings = await env.service(gateway).review(claim_id)

    assert findings.query_items == [
        DocumentRequest(doc_type=DocType.OT_NOTES, text="Complete OT notes")
    ]
    schema = gateway.calls[-1].schema
    assert enum_at(schema, "query_items", "doc_type") == [
        "ot_notes",
        "investigation_report",
        "prescription",
    ]
    assert schema is not None
    for held in ("final_bill", "discharge_summary", "other"):
        bill = {"doc_type": held, "text": "Detailed itemized hospital bill"}
        with pytest.raises(ValidationError):
            schema.model_validate({**choose(k35_32()), "query_items": [bill]})
