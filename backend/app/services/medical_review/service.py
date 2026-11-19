"""Default medical review: build the case file, run the medical review graph, then re-run R5
on the verified facts."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.medical_case import CaseFact, CaseFile, unique
from app.agents.medical_review_graph import run_medical_review
from app.database.models import PolicyClauseRow
from app.database.repositories.stage_records import StageRecordRepository
from app.integrations.core.contract import MemberPolicy, PolicyService
from app.services.digitisation.contract import ClaimRecord
from app.services.knowledge_base.contract import KnowledgeBase
from app.services.medical_review.contract import (
    CLAUSE_PREFIX,
    MEDICAL_DOCUMENTS,
    Citation,
    DroppedItem,
    Finding,
    InvalidOverrule,
    MedicalFindings,
    NotReadyForMedicalReview,
)
from app.services.model_gateway.contract import ModelGateway
from app.services.registration.contract import RegistrationResult
from app.services.rules.contract import Facts, RulesEngine
from app.shared.types import DocType, PageRef, RuleResult, Stage

RERUN_RULES = ["R5"]


class DefaultMedicalReviewService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        gateway: ModelGateway,
        kb: KnowledgeBase,
        rules: RulesEngine,
        policies: PolicyService,
    ) -> None:
        self._sessions = sessions
        self._gateway = gateway
        self._kb = kb
        self._rules = rules
        self._policies = policies
        self._records = StageRecordRepository(sessions)

    async def review(self, claim_id: UUID) -> MedicalFindings:
        digitised = await self._records.latest(claim_id, Stage.DIGITISATION)
        registered = await self._records.latest(claim_id, Stage.REGISTRATION)
        if digitised is None or registered is None:
            raise NotReadyForMedicalReview(f"claim {claim_id} is not digitised and registered")
        latest = await self._records.latest(claim_id, Stage.MEDICAL_REVIEW)
        if latest is not None and (
            latest.payload["digitisation_version"],
            latest.payload["registration_version"],
        ) == (digitised.version, registered.version):
            return MedicalFindings.model_validate(latest.payload)

        record = ClaimRecord.model_validate(digitised.payload)
        registration = RegistrationResult.model_validate(registered.payload)
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

        answered = await self._answered_doc_types(claim_id)
        held = await self._held_doc_types(claim_id)
        case = _case_file(record, policy, await self._clause_refs(policy.product_uin))
        case.requestable = [d for d in MEDICAL_DOCUMENTS if d not in held | answered]
        result = await run_medical_review(
            case, gateway=self._gateway, kb=self._kb, claim_id=claim_id
        )
        query_items, repeated = [], []
        for item in result["query_items"]:  # second check behind the reply schema
            reason = (
                "already answered by the claimant in a query reply"
                if item.doc_type in answered
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
        )
        rule_results = self._rules.evaluate(RERUN_RULES, facts, product)
        findings = [
            f.model_copy(update={"finding_id": i})
            for i, f in enumerate([*result["findings"], *_eligibility(rule_results)], start=1)
        ]
        medical = MedicalFindings(
            claim_id=claim_id,
            digitisation_version=digitised.version,
            registration_version=registered.version,
            verified_codes=result["verified_codes"],
            procedure_codes=result["procedure_codes"],
            findings=findings,
            query_items=query_items,
            rule_results=rule_results,
            lookups=result["lookups"],
            dropped=[*result["dropped"], *repeated],
            revisions=result["revisions"],
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

    async def _answered_doc_types(self, claim_id: UUID) -> set[DocType]:
        """Document types asked for in the claim's answered queries, from each query's latest
        stage record."""
        latest: dict[str, dict[str, Any]] = {}
        for record in await self._records.history(claim_id, Stage.QUERY):
            latest[str(record.payload.get("query_id"))] = record.payload
        return {
            DocType(item["doc_type"])
            for query in latest.values()
            if query.get("status") == "answered"
            for item in query.get("items", [])
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


def _eligibility(results: list[RuleResult]) -> list[Finding]:
    return [
        Finding(
            kind="eligibility",
            rule_id=r.rule_id,
            text=f"{r.rule_id} failed on the verified facts: {r.reason}",
            severity="adverse",
            citations=[Citation(ref=f"{CLAUSE_PREFIX}{r.clause_ref}")] if r.clause_ref else [],
            judged_by="code",
        )
        for r in results
        if not r.passed
    ]
