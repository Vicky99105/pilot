"""Medical review through its contract, in its two steps: code() before bill review and
review() after it. The LangGraph agents run with a scripted model gateway, the real knowledge
base on the seeded ICD-10-CM fixture and the reference rows for AG095, and a real
registration."""

from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import ExclusionRow
from app.database.repositories.stage_records import StageRecordRepository
from app.database.seed import seed_claims_db
from app.database.seed_icd_fixture import seed_icd_fixture
from app.services.knowledge_base.service import DefaultKnowledgeBase
from app.services.medical_review.contract import (
    Citation,
    DocumentRequest,
    InvalidOverrule,
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
from tests.fakes.file_store import FakeFileStore
from tests.fakes.model_gateway import FakeModelGateway
from tests.services.claim_builders import (
    DIAGNOSIS,
    DISCHARGE_DOC,
    ClaimSpec,
    add_coding,
    add_digitised_claim,
    add_sheet,
    appendicitis,
    fact,
    page,
)
from tests.services.reference_rows import add_reference_rows
from tests.services.test_registration import asha_policy

APPENDIX_TERM = "appendicitis acute perforation localized peritonitis"
# The first query the graph builds from this item is APPENDIX_TERM.
APPENDIX_ITEM = {
    "phrase": DIAGNOSIS,
    "main_term": "appendicitis",
    "modifiers": ["acute", "perforation", "localized peritonitis"],
}
REWRITE = {"items": [APPENDIX_ITEM]}
HPE = "Histopathology report of the specimen removed"
PAC = "Pre-anesthesia check-up report"
NO_FINDINGS: dict[str, Any] = {"findings": [], "query_items": []}


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


def choose(*codes: dict[str, Any]) -> dict[str, Any]:
    return {"codes": list(codes)}


def coding_script() -> dict[str, list[Any]]:
    return {"medical_rewrite_terms": [REWRITE], "medical_choose_codes": [choose(k35_32())]}


@dataclass
class Env:
    sessions: async_sessionmaker[AsyncSession]
    policies: FakePolicyService
    files: FakeFileStore = field(default_factory=FakeFileStore)

    async def claim(self, spec: ClaimSpec | None = None) -> UUID:
        claim_id = await add_digitised_claim(self.sessions, spec)
        rules = DefaultRulesEngine(Settings().config_dir / "products")
        registration = DefaultRegistrationService(
            self.sessions, self.policies, FakeMoneyService(), rules
        )
        await registration.register(claim_id)
        return claim_id

    async def coded(self, spec: ClaimSpec | None = None, **coding: Any) -> UUID:
        """A claim through step 1 (built, not run) and bill review's AG095 sheet."""
        claim_id = await self.claim(spec)
        await add_coding(self.sessions, claim_id, **coding)
        await add_sheet(self.sessions, claim_id)
        return claim_id

    def service(self, gateway: FakeModelGateway) -> MedicalReviewService:
        return DefaultMedicalReviewService(
            self.sessions,
            gateway,
            DefaultKnowledgeBase(self.sessions),
            DefaultRulesEngine(Settings().config_dir / "products"),
            self.policies,
            self.files,
        )

    async def held(self, claim_id: UUID, *doc_types: str, image_pages: int = 0) -> None:
        """A document set holding these types. With image_pages, the discharge summary has
        that many pages with images in the file store."""
        page_ids = [str(uuid4()) for _ in range(image_pages)]
        pages = [
            {"page_id": pid, "image_key": await self.files.put(b"img", content_type="image/png")}
            for pid in page_ids
        ]
        documents = [{"doc_type": d, "document_id": str(uuid4()), "page_ids": []}
                     for d in doc_types]  # fmt: skip
        if image_pages:
            documents.append({"doc_type": "discharge_summary", "document_id": str(DISCHARGE_DOC),
                              "page_ids": page_ids})  # fmt: skip
        records = StageRecordRepository(self.sessions)
        payload = {"documents": documents, "pages": pages}
        await records.append(claim_id, Stage.CLASSIFICATION, payload, created_by="test")


@pytest.fixture
async def env(sessions: async_sessionmaker[AsyncSession]) -> Env:
    await seed_claims_db(sessions)
    await seed_icd_fixture(sessions)
    await add_reference_rows(sessions)
    return Env(sessions, FakePolicyService([asha_policy()]))


# --- Step 1: coding ------------------------------------------------------------------------


async def test_coding_takes_codes_from_the_lookup_with_citations(env: Env) -> None:
    claim_id = await env.claim()
    gateway = FakeModelGateway(coding_script())

    coding = await env.service(gateway).code(claim_id)

    [verified] = coding.verified_codes
    assert verified.code.code == "K35.32" and verified.code.system == "ICD-10-CM"
    assert verified.code.display == (
        "Acute appendicitis with perforation and localized peritonitis, without abscess"
    )
    lookup = next(c for c in coding.lookups if c.call_id == verified.lookup_call_id)
    assert (lookup.tool, lookup.argument) == ("search_icd_index", APPENDIX_TERM)
    assert "K35.32" in lookup.returned_codes
    assert verified.citations == [Citation(page=page(1)), Citation(page=page(2))]
    assert (coding.revisions, coding.dropped) == (0, [])
    assert [c.purpose for c in gateway.calls] == ["medical_rewrite_terms", "medical_choose_codes"]
    stored = await StageRecordRepository(env.sessions).latest(claim_id, Stage.MEDICAL_CODING)
    assert stored is not None and stored.created_by == "model"


async def test_the_coding_reply_allows_only_billable_codes_the_lookups_returned(
    env: Env,
) -> None:
    claim_id = await env.claim()
    gateway = FakeModelGateway(coding_script())

    coding = await env.service(gateway).code(claim_id)

    schema = gateway.calls[-1].schema
    allowed = enum_at(schema, "codes", "code")
    returned = {code for call in coding.lookups for code in call.returned_codes}
    assert "K35.32" in allowed and set(allowed) <= returned
    assert "K35.3" not in allowed  # a category: returned by the lookup, but not billable
    assert "K35.9" not in allowed  # never returned by a lookup
    invented = {"code": "K35.9", "phrase": DIAGNOSIS, "citations": [cite(1)], "rationale": "x"}
    assert schema is not None
    with pytest.raises(ValidationError):
        schema.model_validate(choose(k35_32(), invented))
    cites = enum_at(schema, "codes", "citations")
    assert cite(1) in cites and "clause:Excl01" in cites and "icd:K35.32" in cites
    assert cite(9) not in cites  # not a page of the claim


async def test_an_uncited_code_is_sent_back_once_then_dropped(env: Env) -> None:
    claim_id = await env.claim()
    uncited = {**k35_32(), "citations": []}
    gateway = FakeModelGateway(
        {"medical_rewrite_terms": [REWRITE], "medical_choose_codes": [choose(uncited)] * 2}
    )

    coding = await env.service(gateway).code(claim_id)

    assert coding.verified_codes == []
    [dropped] = coding.dropped
    assert (dropped.item, dropped.detail, dropped.reason) == ("code", "K35.32",
                                                              "no valid citation")  # fmt: skip
    assert coding.revisions == 1


async def test_coding_is_idempotent_per_record_version(env: Env) -> None:
    claim_id = await env.claim()
    gateway = FakeModelGateway(coding_script())
    service = env.service(gateway)

    first = await service.code(claim_id)
    again = await service.code(claim_id)

    assert again == first
    assert len(gateway.calls) == 2  # no new model calls


async def test_coding_needs_a_registered_claim(env: Env) -> None:
    claim_id = await add_claim(env.sessions, status=Stage.MEDICAL_CODING.value)

    with pytest.raises(NotReadyForMedicalReview):
        await env.service(FakeModelGateway({})).code(claim_id)


# --- Step 2: the full review --------------------------------------------------------------


async def test_the_review_reads_the_packages_guideline_and_norm_and_reruns_the_rules(
    env: Env,
) -> None:
    claim_id = await env.coded(diagnoses=(("K35.32", "Acute appendicitis, perforated"),))
    gateway = FakeModelGateway({"medical_findings": [NO_FINDINGS]})

    findings = await env.service(gateway).review(claim_id)

    guideline = findings.guideline
    assert guideline is not None
    # AG095 is CGHS; our crosswalk points it at the HBP SG017B guideline and norm.
    assert (guideline.package_code, guideline.guideline_code) == ("AG095", "SG017B")
    assert (guideline.los_norm_days, guideline.los_norm_source) == (3, "PMJAY SG017B")
    assert set(guideline.sections) == {"claim_documents", "alos", "clinical_pointers"}
    assert [c.code.code for c in findings.verified_codes] == ["K35.32"]  # from step 1
    assert [(r.rule_id, r.passed) for r in findings.rule_results] == [
        ("R5", True), ("R6", True), ("R7", True), ("R9", True),
    ]  # fmt: skip
    # The hospital wrote K35.8; the review found a more specific code.
    [hospital] = [f for f in findings.findings if f.kind == "hospital_codes"]
    assert hospital.severity == "info" and "K35.8" in hospital.text
    # A 4-day stay against the 3-day norm, with no stated reason.
    [stay] = [f for f in findings.findings if f.kind == "length_of_stay"]
    assert stay.severity == "info" and "4 days" in stay.text and "3 days" in stay.text
    assert Citation(ref="stg:SG017B:alos") in stay.citations
    assert findings.adverse is False
    assert [c.purpose for c in gateway.calls] == ["medical_findings"]  # no history pages read


async def test_query_items_are_an_enum_of_the_missing_mandatory_documents(env: Env) -> None:
    claim_id = await env.coded()
    await env.held(claim_id, "discharge_summary", "ot_notes", "final_bill")
    reply = {
        "findings": [],
        "query_items": [
            {"document": HPE, "text": "Histopathology report of the appendix"},
            {"document": PAC, "text": "Pre-anaesthesia check-up report"},
        ],
    }
    gateway = FakeModelGateway({"medical_findings": [reply]})

    findings = await env.service(gateway).review(claim_id)

    schema = gateway.calls[-1].schema
    # The guideline's mandatory documents the claim does not hold, then the medical document
    # types it may still be asked for. Operative notes and the discharge summary are held.
    assert enum_at(schema, "query_items", "document") == [
        HPE, PAC, "investigation_report", "prescription",
    ]  # fmt: skip
    assert findings.guideline is not None
    assert findings.guideline.mandatory_missing == [HPE, PAC]
    # Both are investigation reports: one request, both asked for.
    assert findings.query_items == [
        DocumentRequest(
            doc_type=DocType.INVESTIGATION_REPORT,
            text="Histopathology report of the appendix; Pre-anaesthesia check-up report",
        )
    ]
    assert schema is not None
    for held in ("ot_notes", "discharge_summary", "Detailed operative notes"):
        with pytest.raises(ValidationError):
            schema.model_validate({"query_items": [{"document": held, "text": "x"}]})


async def test_an_item_the_claimant_already_answered_is_not_asked_again(env: Env) -> None:
    """After a query reply the review runs again. A document type already answered is not
    offered, however it is worded, so the same query is never raised twice."""
    claim_id = await env.coded()
    await env.held(claim_id, "discharge_summary", "ot_notes")
    answered = {
        "query_id": "6f1c1d1e-5a43-4a8e-9a55-0c1a1f0c0002",
        "status": "answered",
        "items": [{"text": "HPE report", "source": "medical", "doc_type": "investigation_report"}],
    }
    await StageRecordRepository(env.sessions).append(claim_id, Stage.QUERY, answered,
                                                     created_by="test")  # fmt: skip
    gateway = FakeModelGateway({"medical_findings": [NO_FINDINGS]})

    findings = await env.service(gateway).review(claim_id)

    assert enum_at(gateway.calls[-1].schema, "query_items", "document") == ["prescription"]
    assert findings.query_items == []


async def test_uncited_findings_are_sent_back_once_then_dropped(env: Env) -> None:
    claim_id = await env.coded()
    fixed = {"text": "Perforation seen on CT", "severity": "info", "citations": []}
    bad = {"text": "Wound infection", "severity": "query", "citations": []}
    gateway = FakeModelGateway(
        {
            "medical_findings": [
                {"findings": [fixed, bad]},
                {"findings": [{**fixed, "citations": [cite(2)]}, bad]},
            ]
        }
    )

    findings = await env.service(gateway).review(claim_id)

    kept = [f for f in findings.findings if f.judged_by == "model"]
    assert [(f.text, f.citations) for f in kept] == [
        ("Perforation seen on CT", [Citation(page=page(2))])
    ]
    [dropped] = findings.dropped
    assert (dropped.item, dropped.detail) == ("finding", "Wound infection")
    assert findings.revisions == 1


async def test_findings_may_cite_the_packages_guideline(env: Env) -> None:
    claim_id = await env.coded()
    finding = {"text": "No histopathology on file", "severity": "query",
               "citations": ["stg:SG017B:claim_documents"]}  # fmt: skip
    gateway = FakeModelGateway({"medical_findings": [{"findings": [finding]}]})

    findings = await env.service(gateway).review(claim_id)

    [kept] = [f for f in findings.findings if f.judged_by == "model"]
    assert kept.citations == [Citation(ref="stg:SG017B:claim_documents")]
    cites = enum_at(gateway.calls[-1].schema, "findings", "citations")
    assert "stg:SG017B:claim_documents" in cites and "stg:SG017B:doctor_qualification" not in cites


async def test_a_cited_reason_for_the_long_stay_clears_the_stay_finding(env: Env) -> None:
    claim_id = await env.coded()
    reply = {**NO_FINDINGS, "long_stay_reason": {"text": "Ileus on day 2",
                                                 "citations": [cite(2)]}}  # fmt: skip
    gateway = FakeModelGateway({"medical_findings": [reply]})

    findings = await env.service(gateway).review(claim_id)

    assert not [f for f in findings.findings if f.kind == "length_of_stay"]


async def test_a_stay_within_its_norm_offers_no_long_stay_reason(env: Env) -> None:
    claim_id = await env.coded(ClaimSpec(discharge_date=date(2026, 11, 5)))
    gateway = FakeModelGateway({"medical_findings": [NO_FINDINGS]})

    findings = await env.service(gateway).review(claim_id)

    schema = gateway.calls[-1].schema
    assert schema is not None
    assert "long_stay_reason" not in schema.model_json_schema().get("required", [])
    with pytest.raises(ValidationError):
        schema.model_validate({"long_stay_reason": {"text": "x", "citations": []}})
    assert not [f for f in findings.findings if f.kind == "length_of_stay"]


async def test_a_history_older_than_the_policy_fails_r6_within_the_wait(env: Env) -> None:
    env.policies.policies = [
        asha_policy(inception_date=date(2025, 1, 1), continuous_cover_start=date(2025, 1, 1))
    ]
    diabetes = fact("history", "Known case of type 2 diabetes mellitus", 1, "since 5 years")
    claim_id = await env.coded(ClaimSpec(clinical=appendicitis(diabetes)))
    gateway = FakeModelGateway({"medical_findings": [NO_FINDINGS], "medical_ped_reread": []})

    findings = await env.service(gateway).review(claim_id)

    [ped] = [f for f in findings.findings if f.kind == "pre_existing"]
    assert ped.severity == "adverse" and ped.judged_by == "code"
    assert "2025-01-01" in ped.text  # the policy inception date
    assert Citation(page=page(1)) in ped.citations
    assert Citation(ref="clause:Excl01") in ped.citations
    [r6] = [r for r in findings.rule_results if r.rule_id == "R6"]
    assert (r6.passed, r6.clause_ref) == (False, "Excl01")
    assert findings.adverse is True


async def test_the_pre_existing_re_read_reads_only_the_cited_history_pages(env: Env) -> None:
    env.policies.policies = [
        asha_policy(inception_date=date(2025, 1, 1), continuous_cover_start=date(2025, 1, 1))
    ]
    claim_id = await env.coded()  # the history fact cites page 1 of the discharge summary
    await env.held(claim_id, "ot_notes", image_pages=2)
    statement = {"condition": "Hypertension", "onset": "since 2019", "page": cite(1)}
    gateway = FakeModelGateway(
        {"medical_ped_reread": [{"statements": [statement]}], "medical_findings": [NO_FINDINGS]}
    )

    findings = await env.service(gateway).review(claim_id)

    [reread] = [c for c in gateway.calls if c.purpose == "medical_ped_reread"]
    assert reread.image_count == 1  # page 1 only, not page 2
    assert enum_at(reread.schema, "statements", "page") == [cite(1)]
    [ped] = [f for f in findings.findings if f.kind == "pre_existing"]
    assert "Hypertension" in ped.text and "2019-12-31" in ped.text
    [r6] = [r for r in findings.rule_results if r.rule_id == "R6"]
    assert r6.passed is False


async def test_r7_fails_on_a_verified_code_in_its_waiting_period(env: Env) -> None:
    env.policies.policies = [
        asha_policy(inception_date=date(2025, 6, 1), continuous_cover_start=date(2025, 6, 1))
    ]
    claim_id = await env.coded(diagnoses=(("K40.90", "Inguinal hernia"),), procedures=())
    gateway = FakeModelGateway({"medical_findings": [NO_FINDINGS]})

    findings = await env.service(gateway).review(claim_id)

    [r7] = [r for r in findings.rule_results if r.rule_id == "R7"]
    assert (r7.passed, r7.clause_ref) == (False, "Excl02")
    [eligibility] = [f for f in findings.findings if f.rule_id == "R7"]
    assert "Hernia of all types" in eligibility.text
    assert eligibility.citations == [Citation(ref="clause:Excl02")]


async def test_r9_names_the_exclusion_from_the_exclusions_table(env: Env) -> None:
    async with env.sessions.begin() as session:
        session.add(ExclusionRow(scope="IRDAI", code="Excl12", title="Alcoholism, drug abuse",
                                 summary="Treatment for addiction is excluded.", page=8,
                                 source="exclusions", source_version="v"))  # fmt: skip
    claim_id = await env.coded(diagnoses=(("F10.20", "Alcohol dependence"),), procedures=())
    gateway = FakeModelGateway({"medical_findings": [NO_FINDINGS]})

    findings = await env.service(gateway).review(claim_id)

    [eligibility] = [f for f in findings.findings if f.rule_id == "R9"]
    assert "IRDAI Excl12: Alcoholism, drug abuse" in eligibility.text
    assert eligibility.citations == [Citation(ref="clause:Excl12")]


async def test_a_failed_initial_wait_on_rerun_is_adverse(env: Env) -> None:
    env.policies.policies = [
        asha_policy(inception_date=date(2026, 10, 20), continuous_cover_start=date(2026, 10, 20))
    ]
    claim_id = await env.coded()
    gateway = FakeModelGateway({"medical_findings": [NO_FINDINGS]})

    findings = await env.service(gateway).review(claim_id)

    [r5] = [r for r in findings.rule_results if r.rule_id == "R5"]
    assert r5.passed is False
    [eligibility] = [f for f in findings.findings if f.rule_id == "R5"]
    assert eligibility.citations == [Citation(ref="clause:Excl03")]
    assert findings.adverse is True


async def test_the_review_is_idempotent_per_coding_and_sheet_version(env: Env) -> None:
    claim_id = await env.coded()
    gateway = FakeModelGateway({"medical_findings": [NO_FINDINGS, NO_FINDINGS]})
    service = env.service(gateway)

    first = await service.review(claim_id)
    again = await service.review(claim_id)
    await add_sheet(env.sessions, claim_id)  # a new sheet version: the review runs again
    third = await service.review(claim_id)

    assert again == first
    assert (first.bill_review_version, third.bill_review_version) == (1, 2)
    assert len(gateway.calls) == 2


async def test_the_review_needs_the_coding_and_the_sheet(env: Env) -> None:
    claim_id = await env.claim()

    with pytest.raises(NotReadyForMedicalReview, match="coding or deduction sheet"):
        await env.service(FakeModelGateway({})).review(claim_id)


# --- P2-1: a medical officer's overrule takes effect ---------------------------------------


async def _reviewed_with_failed_wait(env: Env) -> tuple[MedicalReviewService, UUID]:
    env.policies.policies = [
        asha_policy(inception_date=date(2026, 10, 20), continuous_cover_start=date(2026, 10, 20))
    ]
    claim_id = await env.coded()
    service = env.service(FakeModelGateway({"medical_findings": [NO_FINDINGS]}))
    await service.review(claim_id)
    return service, claim_id


async def test_findings_carry_ids_in_order(env: Env) -> None:
    _, claim_id = await _reviewed_with_failed_wait(env)
    stored = await StageRecordRepository(env.sessions).latest(claim_id, Stage.MEDICAL_REVIEW)
    assert stored is not None
    findings = MedicalFindings.model_validate(stored.payload).findings

    assert [f.finding_id for f in findings] == list(range(1, len(findings) + 1))
    assert all(not f.overruled for f in findings)


async def test_overrule_writes_a_new_version_judged_by_a_person(env: Env) -> None:
    service, claim_id = await _reviewed_with_failed_wait(env)
    before = await StageRecordRepository(env.sessions).latest(claim_id, Stage.MEDICAL_REVIEW)
    assert before is not None
    [eligibility] = [
        f
        for f in MedicalFindings.model_validate(before.payload).findings
        if f.kind == "eligibility"
    ]

    after = await service.overrule(
        claim_id, [eligibility.finding_id], "Waiting period waived: portability", actor="user:x"
    )

    stored = await StageRecordRepository(env.sessions).latest(claim_id, Stage.MEDICAL_REVIEW)
    assert stored is not None and stored.version == before.version + 1
    assert stored.created_by == "user:x"
    [overruled] = [f for f in after.findings if f.finding_id == eligibility.finding_id]
    assert overruled.overruled is True and overruled.judged_by == "person"
    assert overruled.overrule_reason == "Waiting period waived: portability"
    assert after.adverse is False  # the only adverse finding no longer holds
    # The rule the finding came from no longer fails adjudication.
    [r5] = [r for r in after.rule_results if r.rule_id == "R5"]
    assert (r5.passed, r5.judged_by) == (True, "person")
    # A repeat review keeps the overruled version: its inputs did not change.
    assert await service.review(claim_id) == after


async def test_overrule_refuses_unknown_ids_and_a_blank_reason(env: Env) -> None:
    service, claim_id = await _reviewed_with_failed_wait(env)

    with pytest.raises(InvalidOverrule, match="99"):
        await service.overrule(claim_id, [99], "reason", actor="user:x")
    with pytest.raises(InvalidOverrule, match="reason"):
        await service.overrule(claim_id, [1], "  ", actor="user:x")
    with pytest.raises(InvalidOverrule, match="finding"):
        await service.overrule(claim_id, [], "reason", actor="user:x")
