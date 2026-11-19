"""Default medical review, in two steps around bill review.

code(): build the case file and run the coding graph.
review(): read the package bill review mapped, its guideline and length-of-stay norm,
re-read the cited history pages for pre-existing disease, run the review graph, then re-run
R5, R6, R7 and R9 on the verified codes."""

import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.medical_case import (
    CaseFact,
    CaseFile,
    DocumentOption,
    Statement,
    history_pages,
    pre_existing_onsets,
    stg_key,
    unique,
)
from app.agents.medical_review_graph import run_medical_coding, run_medical_review
from app.agents.prompts import (
    REREAD_PURPOSE,
    page_key,
    reread_prompt,
    reread_schema,
)
from app.database.models import PolicyClauseRow
from app.database.repositories.stage_records import StageRecord, StageRecordRepository
from app.integrations.core.contract import MemberPolicy, PolicyService
from app.services.bill_review.contract import DeductionSheet
from app.services.digitisation.contract import ClaimRecord
from app.services.file_store.contract import FileStore
from app.services.knowledge_base.contract import KnowledgeBase
from app.services.medical_review.contract import (
    CLAUSE_PREFIX,
    MEDICAL_DOCUMENTS,
    Citation,
    DroppedItem,
    Finding,
    GuidelineUsed,
    InvalidOverrule,
    MedicalCoding,
    MedicalFindings,
    NotReadyForMedicalReview,
)
from app.services.model_gateway.contract import ModelGateway, ModelRole
from app.services.registration.contract import RegistrationResult
from app.services.rules.contract import Facts, RulesEngine
from app.shared.types import DocType, PageRef, RuleResult, Stage

RERUN_RULES = ["R5", "R6", "R7", "R9"]
# The names the claimant reads for the medical document types.
DOCUMENT_NAMES = {
    DocType.DISCHARGE_SUMMARY: "Discharge summary",
    DocType.OT_NOTES: "Operation theatre notes",
    DocType.INVESTIGATION_REPORT: "Investigation report",
    DocType.PRESCRIPTION: "Prescription",
}
PMJAY = "PMJAY"
# The guideline sections the review reads: what the treatment needs and what a claim must
# hold. Pre-auth and doctor-qualification sections are for other stages.
REVIEW_SECTIONS = ("clinical_pointers", "claim_checks", "claim_documents", "tms_rules", "alos")
# Mandatory claim documents of a guideline, mapped to the document type that holds each.
# Checked in order; an item that names none of these is not a document a query can ask for.
_GUIDELINE_DOCUMENTS: list[tuple[str, DocType]] = [
    (r"histopatholog|\bhpe\b|biopsy", DocType.INVESTIGATION_REPORT),
    (r"operative notes?|operation notes?|\bot notes?", DocType.OT_NOTES),
    (r"discharge summary", DocType.DISCHARGE_SUMMARY),
    (r"prescription", DocType.PRESCRIPTION),
    (r"\breport\b|investigation|x-?ray|scan|ultrasound|\busg\b|\bct\b|\bmri\b",
     DocType.INVESTIGATION_REPORT),
]  # fmt: skip


class DefaultMedicalReviewService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        gateway: ModelGateway,
        kb: KnowledgeBase,
        rules: RulesEngine,
        policies: PolicyService,
        files: FileStore,
    ) -> None:
        self._sessions = sessions
        self._gateway = gateway
        self._kb = kb
        self._rules = rules
        self._policies = policies
        self._files = files
        self._records = StageRecordRepository(sessions)

    async def code(self, claim_id: UUID) -> MedicalCoding:
        digitised = await self._records.latest(claim_id, Stage.DIGITISATION)
        registered = await self._records.latest(claim_id, Stage.REGISTRATION)
        if digitised is None or registered is None:
            raise NotReadyForMedicalReview(f"claim {claim_id} is not digitised and registered")
        latest = await self._records.latest(claim_id, Stage.MEDICAL_CODING)
        if latest is not None and (
            latest.payload["digitisation_version"],
            latest.payload["registration_version"],
        ) == (digitised.version, registered.version):
            return MedicalCoding.model_validate(latest.payload)
        record = ClaimRecord.model_validate(digitised.payload)
        policy = await self._policy(claim_id, RegistrationResult.model_validate(registered.payload))
        case = _case_file(record, policy, await self._clause_refs(policy.product_uin))
        result = await run_medical_coding(
            case, gateway=self._gateway, kb=self._kb, claim_id=claim_id
        )
        coding = MedicalCoding(
            claim_id=claim_id,
            digitisation_version=digitised.version,
            registration_version=registered.version,
            verified_codes=result["verified_codes"],
            procedure_codes=result["procedure_codes"],
            lookups=result["lookups"],
            dropped=result["dropped"],
            revisions=result["revisions"],
        )
        await self._records.append(
            claim_id, Stage.MEDICAL_CODING, coding.model_dump(mode="json"), created_by="model"
        )
        return coding

    async def review(self, claim_id: UUID) -> MedicalFindings:
        coded = await self._records.latest(claim_id, Stage.MEDICAL_CODING)
        reviewed = await self._records.latest(claim_id, Stage.BILL_REVIEW)
        if coded is None or reviewed is None:
            raise NotReadyForMedicalReview(
                f"claim {claim_id} has no medical coding or deduction sheet yet"
            )
        latest = await self._records.latest(claim_id, Stage.MEDICAL_REVIEW)
        if latest is not None and (
            latest.payload.get("coding_version"),
            latest.payload.get("bill_review_version"),
        ) == (coded.version, reviewed.version):
            return MedicalFindings.model_validate(latest.payload)

        coding = MedicalCoding.model_validate(coded.payload)
        sheet = DeductionSheet.model_validate(reviewed.payload)
        digitised = await self._version(claim_id, Stage.DIGITISATION, coding.digitisation_version)
        registered = await self._version(claim_id, Stage.REGISTRATION, coding.registration_version)
        if digitised is None or registered is None:
            raise NotReadyForMedicalReview(f"claim {claim_id}: the coded record is missing")
        record = ClaimRecord.model_validate(digitised.payload)
        policy = await self._policy(claim_id, RegistrationResult.model_validate(registered.payload))

        requested = await self._requested_doc_types(claim_id)
        held = await self._held_doc_types(claim_id)
        case = _case_file(record, policy, await self._clause_refs(policy.product_uin))
        case.verified_codes = [v.code.code for v in coding.verified_codes]
        guideline = await self._guideline(case, sheet)
        loaded = case.guideline if case.guideline_code else None
        missing = _mandatory_missing(loaded or {}, held)
        if guideline is not None:
            guideline.mandatory_missing = [label for label, _ in missing]
        case.documents = document_options(loaded, held=held, requested=requested)

        reread = await self._reread_history(claim_id, case)
        looked_up = {code for call in coding.lookups for code in call.returned_codes}
        result = await run_medical_review(
            case,
            gateway=self._gateway,
            claim_id=claim_id,
            looked_up=looked_up,
            reread=reread,
        )
        query_items, repeated = [], []
        for item in result["query_items"]:  # second check behind the reply schema
            reason = (
                f"an earlier query already asked for {item.doc_type.value}"
                if item.doc_type in requested
                else f"{item.doc_type.value} is already in the claim's documents"
                if item.doc_type in held
                else None
            )
            if reason:
                repeated.append(DroppedItem(item="query_item", detail=item.text, reason=reason))
            else:
                query_items.append(item)

        product = self._rules.product(policy.product_uin)
        facts = Facts(
            admission_date=record.admission.admission_date.value,
            accident=bool(record.admission.accident.value),
            policy=policy,
            diagnosis_codes=case.verified_codes,
            procedure_codes=[p.code.code for p in coding.procedure_codes],
            pre_existing_onsets=[started for _, started in pre_existing_onsets(case, reread)],
        )
        rule_results = self._rules.evaluate(RERUN_RULES, facts, product)
        eligibility = await self._eligibility(rule_results)
        still_missing = _still_missing(case, missing, requested)
        findings = [
            f.model_copy(update={"finding_id": i})
            for i, f in enumerate([*result["findings"], *still_missing, *eligibility], start=1)
        ]
        medical = MedicalFindings(
            claim_id=claim_id,
            digitisation_version=coding.digitisation_version,
            registration_version=coding.registration_version,
            coding_version=coded.version,
            bill_review_version=reviewed.version,
            guideline=guideline,
            verified_codes=coding.verified_codes,
            procedure_codes=coding.procedure_codes,
            findings=findings,
            query_items=query_items,
            rule_results=rule_results,
            lookups=coding.lookups,
            dropped=[*coding.dropped, *result["dropped"], *repeated],
            revisions=coding.revisions + result["revisions"],
            adverse=_adverse(findings),
        )
        await self._records.append(
            claim_id, Stage.MEDICAL_REVIEW, medical.model_dump(mode="json"), created_by="model"
        )
        return medical

    async def overrule(
        self, claim_id: UUID, finding_ids: list[int], reason: str, *, actor: str
    ) -> MedicalFindings:
        reason = reason.strip()
        if not finding_ids:
            raise InvalidOverrule("an overrule names at least one finding")
        if not reason:
            raise InvalidOverrule("an overrule needs a reason")
        latest = await self._records.latest(claim_id, Stage.MEDICAL_REVIEW)
        if latest is None:
            raise InvalidOverrule(f"claim {claim_id} has no medical findings to overrule")
        current = MedicalFindings.model_validate(latest.payload)
        known = {f.finding_id for f in current.findings}
        unknown = sorted(set(finding_ids) - known)
        if unknown:
            raise InvalidOverrule(
                f"no findings with ids {unknown}; the findings are {sorted(known)}"
            )
        chosen = set(finding_ids)
        findings = [
            f.model_copy(
                update={"overruled": True, "judged_by": "person", "overrule_reason": reason}
            )
            if f.finding_id in chosen
            else f
            for f in current.findings
        ]
        waived = {f.rule_id for f in findings if f.overruled and f.rule_id}
        rule_results = [
            r.model_copy(
                update={
                    "passed": True,
                    "judged_by": "person",
                    "reason": f"overruled by a medical officer: {reason} (was: {r.reason})",
                }
            )
            if r.rule_id in waived and not r.passed
            else r
            for r in current.rule_results
        ]
        overruled = current.model_copy(
            update={
                "findings": findings,
                "rule_results": rule_results,
                "adverse": _adverse(findings),
            }
        )
        await self._records.append(
            claim_id, Stage.MEDICAL_REVIEW, overruled.model_dump(mode="json"), created_by=actor
        )
        return overruled

    # --- Inputs --------------------------------------------------------------------------

    async def _version(self, claim_id: UUID, stage: Stage, version: int) -> StageRecord | None:
        """The record the coding step read, so the review sees the same claim."""
        return next(
            (r for r in await self._records.history(claim_id, stage) if r.version == version),
            None,
        )

    async def _policy(self, claim_id: UUID, registration: RegistrationResult) -> MemberPolicy:
        if registration.policy is None:
            raise NotReadyForMedicalReview(f"claim {claim_id} has no policy")
        policy = await self._policies.find_member(
            policy_number=registration.policy.policy_number,
            member_id=registration.member.member_id,
        )
        if policy is None:
            raise NotReadyForMedicalReview(
                f"policy {registration.policy.policy_number} not found in the core system"
            )
        return policy

    async def _guideline(self, case: CaseFile, sheet: DeductionSheet) -> GuidelineUsed | None:
        """The first mapped package with a guideline or a length-of-stay norm: its HBP code
        from our crosswalk (a PM-JAY package is its own), its guideline sections, and the
        norm by the package code, then the HBP code, then the package name."""
        packages = sheet.package.packages if sheet.package else []
        if not packages:
            return None
        package = packages[0]
        crosswalk = await self._kb.package_crosswalk(package.scheme, package.package_code)
        code = (
            crosswalk.guideline_code
            if crosswalk is not None
            else package.package_code
            if package.scheme == PMJAY
            else None
        )
        sections = await self._kb.get_guideline(code) if code else []
        read: dict[str, list[str]] = {}
        for section in sections:
            if section.section in REVIEW_SECTIONS:
                read.setdefault(section.section, [])
                read[section.section] += [
                    i for i in section.items if i not in read[section.section]
                ]
        norm = None
        for key in (package.package_code, code, package.name):
            if key and norm is None:
                norm = await self._kb.get_los_norm(key)
        case.guideline_code = code if read else None
        case.guideline = read
        if norm is not None and norm.los_days:
            case.los_norm_days = norm.los_days
            case.los_norm_source = f"{norm.scheme} {norm.package_code}"
        return GuidelineUsed(
            package_code=package.package_code,
            guideline_code=code,
            sections=list(read),
            los_norm_days=case.los_norm_days,
            los_norm_source=case.los_norm_source,
        )

    async def _reread_history(self, claim_id: UUID, case: CaseFile) -> list[Statement]:
        """One vision call over the pages the history facts cite, and only those, for the
        conditions and onsets written there. Code dates each onset afterwards."""
        pages = history_pages(case)
        images = await self._page_images(claim_id)
        readable = [p for p in pages if (p.document_id, p.page) in images]
        if not readable:
            return []
        reply = await self._gateway.vision(
            ModelRole.VISION_EXTRACT,
            reread_prompt(readable),
            [await self._files.get(images[(p.document_id, p.page)]) for p in readable],
            reread_schema(readable),
            claim_id=claim_id,
            purpose=REREAD_PURPOSE,
        )
        by_key = {page_key(p): p for p in readable}
        return [
            Statement(condition=s.condition, onset=s.onset, page=by_key.get(s.page))
            for s in reply.statements
            if s.onset.strip()
        ]

    async def _page_images(self, claim_id: UUID) -> dict[tuple[UUID, int], str]:
        """(document, page) -> image key, across every document set of the claim."""
        found: dict[tuple[UUID, int], str] = {}
        for record in await self._records.history(claim_id, Stage.CLASSIFICATION):
            keys = {p["page_id"]: p["image_key"] for p in record.payload.get("pages", [])}
            for document in record.payload.get("documents", []):
                for n, page_id in enumerate(document.get("page_ids", []), start=1):
                    if page_id in keys:
                        found[(UUID(document["document_id"]), n)] = keys[page_id]
        return found

    async def _eligibility(self, results: list[RuleResult]) -> list[Finding]:
        """An adverse finding for each failed rule, with the exclusion's own title when R9
        names one."""
        findings = []
        for r in results:
            if r.passed:
                continue
            text = f"{r.rule_id} failed on the verified facts: {r.reason}"
            if r.rule_id == "R9" and r.clause_ref:
                exclusion = await self._kb.exclusion(r.clause_ref)
                if exclusion is not None:
                    text += f" (IRDAI {exclusion.code}: {exclusion.title})"
            findings.append(
                Finding(
                    kind="eligibility",
                    rule_id=r.rule_id,
                    text=text,
                    severity="adverse",
                    citations=[Citation(ref=f"{CLAUSE_PREFIX}{r.clause_ref}")]
                    if r.clause_ref
                    else [],
                    judged_by="code",
                )
            )
        return findings

    async def _requested_doc_types(self, claim_id: UUID) -> set[DocType]:
        """Document types any of the claim's queries asked for, answered or not."""
        return {
            DocType(item["doc_type"])
            for record in await self._records.history(claim_id, Stage.QUERY)
            for item in record.payload.get("items", [])
            if item.get("doc_type")
        }

    async def _held_doc_types(self, claim_id: UUID) -> set[DocType]:
        """Document types in any of the claim's document sets (the bundle and each reply)."""
        return {
            DocType(document["doc_type"])
            for record in await self._records.history(claim_id, Stage.CLASSIFICATION)
            for document in record.payload.get("documents", [])
        }

    async def _clause_refs(self, product_uin: str) -> list[str]:
        async with self._sessions() as session:
            refs = await session.scalars(
                select(PolicyClauseRow.clause_ref)
                .where(PolicyClauseRow.product_uin == product_uin)
                .order_by(PolicyClauseRow.clause_ref)
            )
            return list(refs)


def guideline_doc_type(item: str) -> DocType | None:
    """The document type that holds a guideline's mandatory document, or None."""
    text = item.lower()
    return next((t for pattern, t in _GUIDELINE_DOCUMENTS if re.search(pattern, text)), None)


def _clean(item: str) -> str:
    """A guideline check as a document name: "Is the X available ...? Yes" becomes "X"."""
    text = re.sub(r"\?\s*(yes|no)?\s*$", "", item.strip(), flags=re.IGNORECASE)
    text = re.sub(r"^(is|are)\s+(there\s+)?(the\s+)?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+(is|are)?\s*available.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+available.*$", "", text, flags=re.IGNORECASE)
    return text[:1].upper() + text[1:] if text else item


def _documents_section(guideline: dict[str, list[str]]) -> str:
    """The guideline section that lists the mandatory claim documents."""
    return "claim_documents" if guideline.get("claim_documents") else "claim_checks"


def _mandatory_missing(
    guideline: dict[str, list[str]], held: set[DocType]
) -> list[tuple[str, DocType]]:
    """The guideline's mandatory claim documents whose document type the claim does not
    hold: (document name, type)."""
    items = guideline.get(_documents_section(guideline)) or []
    missing = []
    for item in items:
        doc_type = guideline_doc_type(item)
        if doc_type is not None and doc_type not in held:
            missing.append((_clean(item), doc_type))
    return list(dict.fromkeys(missing))


def document_options(
    guideline: dict[str, list[str]] | None, *, held: set[DocType], requested: set[DocType]
) -> list[DocumentOption]:
    """The documents a query may ask for, never a type the claim holds or an earlier query
    asked for. With a guideline (its sections by name): exactly its mandatory documents.
    Without one: the medical document types."""
    if guideline is not None:
        return [
            DocumentOption(label=label, doc_type=t, from_guideline=True)
            for label, t in _mandatory_missing(guideline, held)
            if t not in requested
        ]
    return [
        DocumentOption(label=DOCUMENT_NAMES[d], doc_type=d)
        for d in MEDICAL_DOCUMENTS
        if d not in held | requested
    ]


def _still_missing(
    case: CaseFile, missing: list[tuple[str, DocType]], requested: set[DocType]
) -> list[Finding]:
    """A finding for each of the guideline's mandatory documents an earlier query asked for
    that the claim still does not hold. The approver weighs it; no second query asks."""
    if case.guideline_code is None:
        return []
    ref = stg_key(case.guideline_code, _documents_section(case.guideline))
    return [
        Finding(
            kind="documents",
            text=(
                f"{label}: mandatory under guideline {case.guideline_code}. A query asked for "
                f"the {DOCUMENT_NAMES.get(t, t.value).lower()} and the claim still does not "
                "hold one."
            ),
            severity="query",
            citations=[Citation(ref=ref)],
            judged_by="code",
        )
        for label, t in missing
        if t in requested
    ]


def _case_file(record: ClaimRecord, policy: MemberPolicy, clause_refs: list[str]) -> CaseFile:
    clinical = record.clinical
    facts = [
        CaseFact(kind=f.kind, text=f.text, duration=f.duration, source=f.source)
        for f in clinical.facts
    ]
    diagnoses = [
        CaseFact(kind="diagnosis", text=d.value, source=d.source)
        for d in clinical.diagnoses
        if d.value
    ]
    seen = {d.text.strip().lower() for d in diagnoses}
    diagnoses += [f for f in facts if f.kind == "diagnosis" and f.text.strip().lower() not in seen]
    procedures = [
        CaseFact(kind="procedure", text=p.value, source=p.source)
        for p in clinical.procedures
        if p.value
    ] or [f for f in facts if f.kind == "procedure"]
    admission = record.admission
    date_sources = [
        s for s in (admission.admission_date.source, admission.discharge_date.source) if s
    ]
    pages = [f.source for f in facts if f.source]
    pages += [d.source for d in clinical.diagnoses if d.source]
    pages += [p.source for p in clinical.procedures if p.source]
    pages += date_sources
    # Page 1 of every document read can always be cited.
    pages += [PageRef(document_id=doc, page=1) for doc in record.documents.values()]
    return CaseFile(
        diagnoses=diagnoses,
        procedures=unique(procedures),
        facts=facts,
        hospital_codes=clinical.hospital_codes,
        admission_date=admission.admission_date.value,
        discharge_date=admission.discharge_date.value,
        date_sources=date_sources,
        inception_date=policy.inception_date,
        declared_peds=policy.declared_peds,
        known_pages=unique(pages),
        clause_refs=clause_refs,
    )


def _adverse(findings: list[Finding]) -> bool:
    return any(f.severity == "adverse" and not f.overruled for f in findings)
