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
    Citation,
    DroppedItem,
    Finding,
    MedicalFindings,
    NotReadyForMedicalReview,
)
from app.services.model_gateway.contract import ModelGateway
from app.services.query.contract import same_item
from app.services.registration.contract import RegistrationResult
from app.services.rules.contract import Facts, RulesEngine
from app.shared.types import PageRef, RuleResult, Stage

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

        answered = await self._answered_queries(claim_id)
        case = _case_file(record, policy, await self._clause_refs(policy.product_uin))
        case.answered_queries = answered
        result = await run_medical_review(
            case, gateway=self._gateway, kb=self._kb, claim_id=claim_id
        )
        query_items, repeated = [], []
        for item in result["query_items"]:
            if any(same_item(item, a) for a in answered):
                repeated.append(
                    DroppedItem(
                        item="query_item",
                        detail=item,
                        reason="already answered by the claimant in a query reply",
                    )
                )
            else:
                query_items.append(item)

        product = self._rules.product(policy.product_uin)
        facts = Facts(
            admission_date=record.admission.admission_date.value,
            accident=bool(record.admission.accident.value),
            policy=policy,
        )
        rule_results = self._rules.evaluate(RERUN_RULES, facts, product)
        findings = [*result["findings"], *_eligibility(rule_results)]
        medical = MedicalFindings(
            claim_id=claim_id,
            digitisation_version=digitised.version,
            registration_version=registered.version,
            verified_codes=result["verified_codes"],
            findings=findings,
            query_items=query_items,
            rule_results=rule_results,
            lookups=result["lookups"],
            dropped=[*result["dropped"], *repeated],
            revisions=result["revisions"],
            adverse=any(f.severity == "adverse" for f in findings),
        )
        await self._records.append(
            claim_id, Stage.MEDICAL_REVIEW, medical.model_dump(mode="json"), created_by="model"
        )
        return medical

    async def _answered_queries(self, claim_id: UUID) -> list[str]:
        """Items of the claim's answered queries, from each query's latest stage record."""
        latest: dict[str, dict[str, Any]] = {}
        for record in await self._records.history(claim_id, Stage.QUERY):
            latest[str(record.payload.get("query_id"))] = record.payload
        return [
            item["text"]
            for query in latest.values()
            if query.get("status") == "answered"
            for item in query.get("items", [])
        ]

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


def _eligibility(results: list[RuleResult]) -> list[Finding]:
    return [
        Finding(
            kind="eligibility",
            text=f"{r.rule_id} failed on the verified facts: {r.reason}",
            severity="adverse",
            citations=[Citation(ref=f"{CLAUSE_PREFIX}{r.clause_ref}")] if r.clause_ref else [],
            judged_by="code",
        )
        for r in results
        if not r.passed
    ]
