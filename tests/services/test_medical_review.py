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

from app.agents.medical_case import DocumentOption, document_requests
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
from app.services.medical_review.service import (
    DefaultMedicalReviewService,
    document_options,
)
from app.services.overrides.service import DefaultOverrideLog
from app.services.registration.service import DefaultRegistrationService
from app.services.rules.service import DefaultRulesEngine
from app.shared.types import DocType, RequestType, Stage
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
from tests.services.reference_rows import SG017B_CLAIM_DOCUMENTS, add_reference_rows
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
        ("R5", True), ("R6", True), ("R7", True), ("R8", True), ("R9", True),
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
    gateway = FakeModelGateway({"medical_findings": [{"findings": [], "query_items": [HPE, PAC]}]})

    findings = await env.service(gateway).review(claim_id)

    schema = gateway.calls[-1].schema
    # Exactly the guideline's mandatory documents the claim does not hold. A guideline is
    # loaded, so no other medical document type is offered.
    assert enum_at(schema, "query_items") == [HPE, PAC]
    assert findings.guideline is not None
    assert findings.guideline.mandatory_missing == [HPE, PAC]
    # Both are investigation reports: one request, its text built in code from the
    # guideline's own wording.
    assert findings.query_items == [
        DocumentRequest(doc_type=DocType.INVESTIGATION_REPORT, text=f"{HPE}; {PAC}")
    ]
    assert schema is not None
    for other in ("ot_notes", "Operative notes", "Prescription", "prescription"):
        with pytest.raises(ValidationError):
            schema.model_validate({"query_items": [other]})
    # A query item is the option alone: the model cannot attach free text to it.
    with pytest.raises(ValidationError):
        schema.model_validate({"query_items": [{"document": HPE, "text": "x"}]})


@pytest.mark.parametrize("status", ["answered", "open", "overdue"])
async def test_a_document_type_an_earlier_query_asked_for_is_never_asked_again(
    env: Env, status: str
) -> None:
    """After a query reply the review runs again. A document type an earlier query asked
    for is not offered, answered or not, however it is worded. A mandatory document still
    missing becomes a finding for the approver, never a second query."""
    claim_id = await env.coded()
    await env.held(claim_id, "discharge_summary", "payment_receipt")
    asked = {
        "query_id": "6f1c1d1e-5a43-4a8e-9a55-0c1a1f0c0002",
        "status": status,
        "items": [
            {"text": "Payment receipt", "source": "missing_document",
             "doc_type": "payment_receipt"},
            {"text": "HPE report", "source": "medical", "doc_type": "investigation_report"},
            {"text": "Operative notes", "source": "medical", "doc_type": "ot_notes"},
        ],
    }  # fmt: skip
    await StageRecordRepository(env.sessions).append(claim_id, Stage.QUERY, asked,
                                                     created_by="test")  # fmt: skip
    gateway = FakeModelGateway({"medical_findings": [NO_FINDINGS]})

    findings = await env.service(gateway).review(claim_id)

    schema = gateway.calls[-1].schema
    assert schema is not None
    assert schema.model_json_schema()["properties"]["query_items"]["maxItems"] == 0
    for label in (HPE, PAC, "Detailed operative notes", "Prescription"):
        with pytest.raises(ValidationError):
            schema.model_validate({"query_items": [label]})
    assert findings.query_items == []
    assert findings.guideline is not None
    assert findings.guideline.mandatory_missing == ["Detailed operative notes", HPE, PAC]
    still = [f for f in findings.findings if f.kind == "documents"]
    assert [(f.severity, f.judged_by) for f in still] == [("query", "code")] * 3
    assert [f.text.split(":")[0] for f in still] == ["Detailed operative notes", HPE, PAC]
    assert all(f.citations == [Citation(ref="stg:SG017B:claim_documents")] for f in still)
    assert findings.adverse is False


async def test_without_a_guideline_only_the_checklists_required_medical_documents_are_offered(
    env: Env,
) -> None:
    """c07: no package, so no guideline. The reimbursement checklist requires the discharge
    summary and nothing else medical, so with the summary held there is no source for any
    query item: the reply must keep query_items empty. A prescription is never required."""
    claim_id = await env.claim()
    await add_coding(env.sessions, claim_id)
    await add_sheet(env.sessions, claim_id, packages=())
    await env.held(claim_id, "discharge_summary", "ot_notes")
    gateway = FakeModelGateway({"medical_findings": [NO_FINDINGS]})

    findings = await env.service(gateway).review(claim_id)

    assert findings.guideline is None
    query_items = gateway.calls[-1].schema.model_json_schema()["properties"]["query_items"]
    assert query_items.get("maxItems") == 0
    assert findings.query_items == []


async def test_without_a_guideline_a_missing_required_medical_document_is_offered(
    env: Env,
) -> None:
    claim_id = await env.claim()
    await add_coding(env.sessions, claim_id)
    await add_sheet(env.sessions, claim_id, packages=())
    await env.held(claim_id, "ot_notes")
    gateway = FakeModelGateway(
        {"medical_findings": [{"findings": [], "query_items": ["Discharge summary"]}]}
    )

    findings = await env.service(gateway).review(claim_id)

    assert enum_at(gateway.calls[-1].schema, "query_items") == ["Discharge summary"]
    assert findings.query_items == [
        DocumentRequest(doc_type=DocType.DISCHARGE_SUMMARY, text="Discharge summary")
    ]


# --- The option builder and the request text, as pure functions ----------------------------

GUIDELINE = {"claim_documents": SG017B_CLAIM_DOCUMENTS, "alos": ["3 Days"]}
OPERATIVE = "Detailed operative notes"
INVESTIGATION = DocType.INVESTIGATION_REPORT


def offered(options: list[DocumentOption]) -> list[tuple[str, DocType, bool]]:
    return [(o.label, o.doc_type, o.from_guideline) for o in options]


def test_with_a_guideline_the_options_are_exactly_its_missing_mandatory_documents() -> None:
    held = {DocType.DISCHARGE_SUMMARY, DocType.OT_NOTES}
    assert offered(document_options(GUIDELINE, held=held, requested=set())) == [
        (HPE, INVESTIGATION, True), (PAC, INVESTIGATION, True),
    ]  # fmt: skip


def test_a_type_an_earlier_query_asked_for_is_not_offered() -> None:
    requested = {INVESTIGATION, DocType.PAYMENT_RECEIPT}
    options = document_options(GUIDELINE, held={DocType.DISCHARGE_SUMMARY}, requested=requested)
    assert offered(options) == [(OPERATIVE, DocType.OT_NOTES, True)]


def test_with_every_missing_document_requested_nothing_is_offered() -> None:
    held = {DocType.DISCHARGE_SUMMARY, DocType.OT_NOTES}
    assert document_options(GUIDELINE, held=held, requested={INVESTIGATION}) == []
    # A guideline without document requirements offers nothing either: no fallback.
    assert document_options({"alos": ["3 Days"]}, held=set(), requested=set()) == []


def test_without_a_guideline_only_required_medical_documents_not_held_or_requested() -> None:
    required = [DocType.CLAIM_FORM_PART_A, DocType.PAYMENT_RECEIPT, DocType.DISCHARGE_SUMMARY,
                DocType.OT_NOTES, INVESTIGATION]  # fmt: skip
    options = document_options(
        None, held={DocType.OT_NOTES}, requested={INVESTIGATION}, required=required
    )
    # Non-medical documents are intake's to ask for; a held or asked type is never offered.
    assert offered(options) == [("Discharge summary", DocType.DISCHARGE_SUMMARY, False)]
    # Nothing required, nothing offered: a prescription has no source.
    assert document_options(None, held=set(), requested=set(), required=[]) == []


def test_the_request_text_is_built_from_the_chosen_options() -> None:
    ot, hpe, pac = document_options(GUIDELINE, held={DocType.DISCHARGE_SUMMARY}, requested=set())
    assert [ot.label, hpe.label, pac.label] == [OPERATIVE, HPE, PAC]
    assert document_requests([hpe, ot, pac, hpe]) == [
        DocumentRequest(doc_type=INVESTIGATION, text=f"{HPE}; {PAC}"),
        DocumentRequest(doc_type=DocType.OT_NOTES, text=OPERATIVE),
    ]
    assert document_requests([]) == []


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
    finding = {"text": "Pain shifted to the right iliac fossa", "severity": "info",
               "citations": ["stg:SG017B:clinical_pointers"]}  # fmt: skip
    gateway = FakeModelGateway({"medical_findings": [{"findings": [finding]}]})

    findings = await env.service(gateway).review(claim_id)

    [kept] = [f for f in findings.findings if f.judged_by == "model"]
    assert kept.citations == [Citation(ref="stg:SG017B:clinical_pointers")]
    cites = enum_at(gateway.calls[-1].schema, "findings", "citations")
    assert "stg:SG017B:clinical_pointers" in cites
    assert "stg:SG017B:doctor_qualification" not in cites


# --- Document findings are code-only ------------------------------------------------------


async def test_the_findings_reply_has_no_document_kind(env: Env) -> None:
    """Whether a document is held is decided in code. The model's finding kinds are a
    closed enum without "documents", so it cannot write a document-presence finding."""
    claim_id = await env.coded()
    gateway = FakeModelGateway({"medical_findings": [NO_FINDINGS]})

    await env.service(gateway).review(claim_id)

    schema = gateway.calls[-1].schema
    assert schema is not None
    kinds = enum_at(schema, "findings", "kind")
    assert "documents" not in kinds and kinds == ["clinical", "coding"]
    bad = {"kind": "documents", "text": "OT notes missing", "severity": "query",
           "citations": [cite(1)]}  # fmt: skip
    with pytest.raises(ValidationError):
        schema.model_validate({"findings": [bad]})


async def test_the_findings_call_never_sees_the_guidelines_document_list(env: Env) -> None:
    """The model is not given the mandatory document list, so it has no document-missing
    wording path: neither the guideline's documents section nor a citation of it."""
    claim_id = await env.coded()
    await env.held(claim_id, "discharge_summary", "final_bill")
    gateway = FakeModelGateway({"medical_findings": [NO_FINDINGS]})

    await env.service(gateway).review(claim_id)

    call = gateway.calls[-1]
    text = "\n".join(m.content for m in call.messages or [] if isinstance(m.content, str))
    assert "claim_documents" not in text
    assert SG017B_CLAIM_DOCUMENTS[1] not in text
    assert "documents_that_may_be_requested" not in text
    cites = enum_at(call.schema, "findings", "citations")
    assert "stg:SG017B:claim_documents" not in cites
    assert "stg:SG017B:clinical_pointers" in cites


async def test_a_held_document_never_gets_a_document_finding(env: Env) -> None:
    """The live failure: OT notes were held, yet a finding said they were missing. Code
    alone writes document findings, from the guideline's mandatory list against the held
    types; the model's findings are "clinical" or "coding"."""
    claim_id = await env.coded()
    await env.held(claim_id, "discharge_summary", "ot_notes", "final_bill")
    note = {"kind": "clinical", "text": "Perforation seen on CT", "severity": "info",
            "citations": [cite(2)]}  # fmt: skip
    gateway = FakeModelGateway({"medical_findings": [{"findings": [note], "query_items": []}]})

    findings = await env.service(gateway).review(claim_id)

    assert all(f.kind != "documents" for f in findings.findings)
    assert [(f.kind, f.judged_by) for f in findings.findings if f.text == note["text"]] == [
        ("clinical", "model")
    ]


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
    # Not declared, and within the 60-month moratorium: R8 lets the insurer contest it.
    [r8] = [r for r in findings.rule_results if r.rule_id == "R8"]
    assert (r8.passed, r8.clause_ref) == (False, "MORATORIUM")
    assert any(f.rule_id == "R8" and f.severity == "adverse" for f in findings.findings)


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


# --- P2-5: the pre-auth reads the guideline's pre-auth documents ---------------------------


async def test_a_preauth_checks_the_guidelines_preauth_documents(env: Env) -> None:
    """A cashless claim before discharge has no final bill. Its mandatory list is the
    guideline's pre-auth documents, not the claim documents a discharge needs, so the
    missing histopathology and operative notes are not asked for at pre-auth."""
    spec = ClaimSpec(request_type=RequestType.PREAUTH, lines=[])
    claim_id = await env.coded(spec)
    await env.held(claim_id, "preauth_form")
    gateway = FakeModelGateway({"medical_findings": [NO_FINDINGS]})

    findings = await env.service(gateway).review(claim_id)

    assert findings.guideline is not None
    assert "preauth_documents" in findings.guideline.sections
    assert "claim_documents" not in findings.guideline.sections
    assert findings.guideline.mandatory_missing == ["USG abdomen report"]
    assert enum_at(gateway.calls[-1].schema, "query_items") == ["USG abdomen report"]
    text = "\n".join(m.content for m in gateway.calls[-1].messages or [] if
                     isinstance(m.content, str))  # fmt: skip
    assert "preauth_documents" not in text  # a document list is never shown to the model


async def test_an_overrule_is_written_to_the_override_log(env: Env) -> None:
    service, claim_id = await _reviewed_with_failed_wait(env)
    findings = await service.review(claim_id)
    [eligibility] = [f for f in findings.findings if f.kind == "eligibility"]

    await service.overrule(claim_id, [eligibility.finding_id], "Portability", actor="user:x")

    log = await DefaultOverrideLog(env.sessions).list(claim_id=claim_id)
    assert [(o.kind, o.field, o.after, o.reason) for o in log] == [
        ("medical_overrule", f"finding {eligibility.finding_id}", "overruled", "Portability"),
    ]
    assert log[0].before == {"severity": "adverse", "text": eligibility.text}
