"""Default digitisation: one vision call per document, then assembly and checks in code."""

import copy
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.repositories.stage_records import StageRecordRepository
from app.services.classification.contract import DocumentSet
from app.services.digitisation import prompts
from app.services.digitisation._helpers import (
    assemble,
    failed_checks,
    low_confidence_fields,
    quantize_money,
    set_field,
)
from app.services.digitisation.contract import (
    EXTRACTED_TYPES,
    ClaimRecord,
    InvalidCorrection,
    NotClassified,
)
from app.services.file_store.contract import FileStore
from app.services.intake.contract import ClaimForm
from app.services.model_gateway.contract import ModelGateway, ModelRole
from app.shared.types import Stage


class DefaultDigitisationService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        file_store: FileStore,
        gateway: ModelGateway,
    ) -> None:
        self._files = file_store
        self._gateway = gateway
        self._records = StageRecordRepository(sessions)

    async def extract(self, claim_id: UUID) -> ClaimRecord:
        classified = await self._records.latest(claim_id, Stage.CLASSIFICATION)
        if classified is None:
            raise NotClassified(str(claim_id))
        latest = await self._records.latest(claim_id, Stage.DIGITISATION)
        if latest is not None and latest.payload["classification_version"] == classified.version:
            return ClaimRecord.model_validate(latest.payload)

        doc_set = DocumentSet.model_validate(classified.payload)
        image_keys = {p.page_id: p.image_key for p in doc_set.pages}
        docs = {}
        for doc in doc_set.documents:  # the first document of each type is read
            if doc.doc_type in EXTRACTED_TYPES and doc.doc_type not in docs:
                docs[doc.doc_type] = doc
        replies: dict[Any, BaseModel] = {}
        for doc_type, doc in docs.items():
            images = [await self._files.get(image_keys[p]) for p in doc.page_ids]
            replies[doc_type] = await self._gateway.vision(
                ModelRole.VISION_EXTRACT,
                prompts.PROMPTS[doc_type],
                images,
                prompts.REPLY_SCHEMAS[doc_type],
                claim_id=claim_id,
                purpose=prompts.purpose(doc_type),
            )
        record = assemble(claim_id, classified.version, docs, replies)
        return await self._finish(record, created_by="model")

    async def apply_corrections(
        self, claim_id: UUID, fields: dict[str, Any], *, actor: str
    ) -> ClaimRecord:
        latest = await self._records.latest(claim_id, Stage.DIGITISATION)
        if latest is None:
            raise InvalidCorrection(f"claim {claim_id} has no extracted record to correct")
        data = copy.deepcopy(latest.payload)
        for path, value in fields.items():
            set_field(data, path, value)
        try:
            record = ClaimRecord.model_validate(data)
        except ValidationError as exc:
            raise InvalidCorrection(str(exc)) from exc
        quantize_money(record)
        record.corrected_fields = [
            *record.corrected_fields,
            *(p for p in fields if p not in record.corrected_fields),
        ]
        return await self._finish(record, created_by=actor)

    async def _finish(self, record: ClaimRecord, *, created_by: str) -> ClaimRecord:
        record.failed_checks = failed_checks(record, await self._form(record.claim_id))
        record.low_confidence_fields = low_confidence_fields(record)
        await self._records.append(
            record.claim_id,
            Stage.DIGITISATION,
            record.model_dump(mode="json"),
            created_by=created_by,
        )
        return record

    async def _form(self, claim_id: UUID) -> ClaimForm | None:
        intake = await self._records.latest(claim_id, Stage.INTAKE)
        return ClaimForm.model_validate(intake.payload["form"]) if intake else None
