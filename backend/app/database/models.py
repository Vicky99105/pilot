"""SQLAlchemy models for the claims database.

Postgres is the target. JSON columns become JSONB there; the SQLite variant exists only so
tests can run without a database server. All timestamps are stored in UTC.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Dialect,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonType = JSON().with_variant(JSONB(), "postgresql")
Money = Numeric(14, 2)


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware datetime that is always written and read back as UTC."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; pass a timezone-aware value")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:  # SQLite drops the offset
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


# --- People -------------------------------------------------------------------------------


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))  # staff, member, hospital
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    hospital_id: Mapped[str | None] = mapped_column(String(13))  # ROHINI ID
    policy_number: Mapped[str | None] = mapped_column(String(50))
    member_id: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class UserRoleRow(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(40), primary_key=True)
    # APPROVER rows: the largest amount this user may approve. NULL on an APPROVER row means
    # no limit (senior approver). Unused for other roles.
    max_amount: Mapped[Decimal | None] = mapped_column(Money)


# --- Intake and documents -----------------------------------------------------------------


class SubmissionRow(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        Index("ix_submissions_dedupe", "member_id", "admission_date", "files_digest"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    request_type: Mapped[str] = mapped_column(String(30))
    submitted_by: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    parent_claim_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("claims.id"))
    # The claim this submission belongs to: its own new claim, the parent, or the claim of
    # the earlier submission it duplicates.
    claim_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("claims.id"), index=True)
    channel: Mapped[str | None] = mapped_column(String(20))  # member, hospital
    form: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    # Duplicate detection: same member, same admission date, same set of file hashes.
    member_id: Mapped[str | None] = mapped_column(String(50))
    admission_date: Mapped[date | None] = mapped_column(Date)
    files_digest: Mapped[str | None] = mapped_column(String(64))
    duplicate_of: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("submissions.id"))
    missing_documents: Mapped[list[str]] = mapped_column(JsonType, default=list)
    warnings: Mapped[list[str]] = mapped_column(JsonType, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class FileRow(Base):
    __tablename__ = "files"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    submission_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("submissions.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)  # upload order, 0-based
    filename: Mapped[str | None] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    slot_hint: Mapped[str | None] = mapped_column(String(60))
    mime: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_key: Mapped[str] = mapped_column(String(100))
    quality_warning: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DocumentRow(Base):
    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    submission_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("submissions.id"), index=True)
    doc_type: Mapped[str] = mapped_column(String(60))
    page_start: Mapped[int] = mapped_column(Integer)
    page_end: Mapped[int] = mapped_column(Integer)
    confidence: Mapped[float | None]
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PageRow(Base):
    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("file_id", "page_number"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    file_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("files.id"), index=True)
    page_number: Mapped[int] = mapped_column(Integer)  # 1-based within the file
    image_key: Mapped[str | None] = mapped_column(String(100))
    text_layer: Mapped[str | None] = mapped_column(Text)
    doc_type: Mapped[str | None] = mapped_column(String(60))
    confidence: Mapped[float | None]
    quality_warning: Mapped[str | None] = mapped_column(Text)
    document_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("documents.id"))


# --- Claims and their stage records -------------------------------------------------------


class ClaimRow(Base):
    __tablename__ = "claims"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    claim_number: Mapped[str | None] = mapped_column(String(40), unique=True)
    submission_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("submissions.id", use_alter=True, name="fk_claims_submission_id_submissions"),
    )
    member_id: Mapped[str | None] = mapped_column(String(50))
    policy_number: Mapped[str | None] = mapped_column(String(50))
    hospital_id: Mapped[str | None] = mapped_column(String(13))
    request_type: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(40))  # current state (a Stage value or "closed")
    status_changed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    deadline: Mapped[datetime | None] = mapped_column(UTCDateTime)
    workflow_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class StageRecordRow(Base):
    __tablename__ = "stage_records"
    __table_args__ = (UniqueConstraint("claim_id", "stage", "version"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    claim_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("claims.id"))
    stage: Mapped[str] = mapped_column(String(40))
    version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_by: Mapped[str] = mapped_column(String(100))


# --- Communication ------------------------------------------------------------------------


class QueryRow(Base):
    """A query to the claimant. Each change is also appended to stage_records (stage query)."""

    __tablename__ = "queries"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    claim_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("claims.id"), index=True)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JsonType)
    status: Mapped[str] = mapped_column(String(20))  # open, overdue, answered, closed
    letter_id: Mapped[UUID | None] = mapped_column(Uuid)
    deadline: Mapped[datetime] = mapped_column(UTCDateTime)
    reminders: Mapped[list[int]] = mapped_column(JsonType, default=list)  # days sent
    reply_submission_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("submissions.id"))
    raised_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    answered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class LetterRow(Base):
    __tablename__ = "letters"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    claim_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("claims.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # approval, partial, rejection, query
    query_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("queries.id"))
    decision_version: Mapped[int | None] = mapped_column(Integer)
    recipient: Mapped[str | None] = mapped_column(String(320))  # email address
    pdf_key: Mapped[str] = mapped_column(String(100), unique=True)
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    send_error: Mapped[str | None] = mapped_column(Text)
    content: Mapped[dict[str, Any]] = mapped_column(JsonType)  # the Letter record
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# --- Work and audit -----------------------------------------------------------------------


class TaskRow(Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_queue", "status", "role", "deadline"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    claim_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("claims.id"), index=True)
    workflow_id: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(60))
    role: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300))
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    deadline: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(20))  # open, taken, done, escalated
    # Approver tasks: the amount the approver's limit must cover. None for other tasks.
    authority_amount: Mapped[Decimal | None] = mapped_column(Money)
    assignee: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    outcome: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    taken_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class EventRow(Base):
    """Append-only audit log. claim_id has no foreign key so the log never blocks a write."""

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_claim_time", "claim_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    claim_id: Mapped[UUID] = mapped_column(Uuid)
    stage: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(80))
    actor: Mapped[str] = mapped_column(String(100))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ModelCallRow(Base):
    __tablename__ = "model_calls"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    claim_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    purpose: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(40))
    provider: Mapped[str] = mapped_column(String(60))
    model: Mapped[str] = mapped_column(String(200))
    attempt: Mapped[int] = mapped_column(Integer)
    prompt: Mapped[list[dict[str, Any]]] = mapped_column(JsonType)  # messages, no image bytes
    image_keys: Mapped[list[str]] = mapped_column(JsonType, default=list)  # FileStore keys
    output: Mapped[str | None] = mapped_column(Text)
    valid: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# --- Knowledge base (each row carries its source) ----------------------------------------


class IcdIndexRow(Base):
    """One entry of an ICD-10-CM alphabetic index: the disease index, the external cause
    index, or one cell of the drug or neoplasm table."""

    __tablename__ = "icd_index"
    __table_args__ = (
        Index("ix_icd_index_term", "system", "version", "term"),
        # Trigram index for ILIKE '%word%' search on Postgres (pg_trgm).
        Index(
            "ix_icd_index_search_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    system: Mapped[str] = mapped_column(String(30))
    version: Mapped[str] = mapped_column(String(20))
    # diseases, external_causes, drugs or neoplasms
    index_name: Mapped[str] = mapped_column(String(20), default="diseases")
    term: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)  # full index path, e.g. "Pneumonia > lobar"
    # The path plus every nonessential modifier along it; the column search runs on.
    search_text: Mapped[str] = mapped_column(Text, default="")
    table_column: Mapped[str | None] = mapped_column(Text)  # drug and neoplasm tables only
    code: Mapped[str | None] = mapped_column(String(10))  # as written, e.g. "H93.29-"
    manifestation: Mapped[str | None] = mapped_column(Text)
    see: Mapped[str | None] = mapped_column(Text)
    see_also: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(60))
    source_version: Mapped[str] = mapped_column(String(40))


class IcdTabularRow(Base):
    """One code of a code set (ICD-10-CM or ICD-10-PCS) with its title and notes."""

    __tablename__ = "icd_tabular"
    __table_args__ = (
        UniqueConstraint("system", "version", "code"),
        Index("ix_icd_tabular_parent", "system", "version", "parent"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    system: Mapped[str] = mapped_column(String(30))
    version: Mapped[str] = mapped_column(String(20))
    code: Mapped[str] = mapped_column(String(10))
    title: Mapped[str] = mapped_column(Text)
    billable: Mapped[bool] = mapped_column(Boolean)
    notes: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)  # includes, excludes
    parent: Mapped[str | None] = mapped_column(String(10))  # the next shorter code
    chapter: Mapped[str | None] = mapped_column(Text)
    section: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(60))
    source_version: Mapped[str] = mapped_column(String(40))


class KbSourceLoadRow(Base):
    """One source file loaded into the knowledge base: what it was, its hash, and the code
    version it carries with the dates that version is in force."""

    __tablename__ = "kb_source_loads"
    __table_args__ = (Index("ix_kb_source_loads_system", "system", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    system: Mapped[str] = mapped_column(String(30))
    version: Mapped[str] = mapped_column(String(20))
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date] = mapped_column(Date)
    file_name: Mapped[str] = mapped_column(Text)  # the downloaded archive
    member: Mapped[str] = mapped_column(Text)  # the file inside it
    sha256: Mapped[str] = mapped_column(String(64))  # of the downloaded archive
    rows: Mapped[int] = mapped_column(Integer)
    loaded_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PcsTableRow(Base):
    """One ICD-10-PCS table: the first three characters and their labels."""

    __tablename__ = "pcs_tables"
    __table_args__ = (UniqueConstraint("system", "version", "code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    system: Mapped[str] = mapped_column(String(30))
    version: Mapped[str] = mapped_column(String(20))
    code: Mapped[str] = mapped_column(String(3))  # e.g. "0DT"
    # [[axis title, character, label], ...] for positions 1 to 3
    axes: Mapped[list[Any]] = mapped_column(JsonType)
    operation_definition: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(60))
    source_version: Mapped[str] = mapped_column(String(40))


class PcsRowRow(Base):
    """One row of a PCS table: every combination of its positions 4 to 7 values is a valid
    code."""

    __tablename__ = "pcs_rows"
    __table_args__ = (Index("ix_pcs_rows_table", "system", "version", "table_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    system: Mapped[str] = mapped_column(String(30))
    version: Mapped[str] = mapped_column(String(20))
    table_code: Mapped[str] = mapped_column(String(3))
    row_number: Mapped[int] = mapped_column(Integer)  # 1-based within the table
    # [[axis title, [[character, label], ...]], ...] for positions 4 to 7
    axes: Mapped[list[Any]] = mapped_column(JsonType)
    codes: Mapped[int] = mapped_column(Integer)  # how many codes the row makes
    source: Mapped[str] = mapped_column(String(60))
    source_version: Mapped[str] = mapped_column(String(40))


class PcsDefinitionRow(Base):
    """A PCS definition: the meaning of one value of one axis within a section."""

    __tablename__ = "pcs_definitions"
    __table_args__ = (Index("ix_pcs_definitions_axis", "system", "version", "section", "pos"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    system: Mapped[str] = mapped_column(String(30))
    version: Mapped[str] = mapped_column(String(20))
    section: Mapped[str] = mapped_column(String(1))
    pos: Mapped[int] = mapped_column(Integer)
    axis: Mapped[str] = mapped_column(Text)  # e.g. "Operation", "Approach"
    label: Mapped[str] = mapped_column(Text)  # the value's label, e.g. "Resection"
    definition: Mapped[str | None] = mapped_column(Text)
    explanation: Mapped[str | None] = mapped_column(Text)
    includes: Mapped[list[str]] = mapped_column(JsonType, default=list)
    source: Mapped[str] = mapped_column(String(60))
    source_version: Mapped[str] = mapped_column(String(40))


class NonPayableItemRow(Base):
    __tablename__ = "non_payable_items"
    __table_args__ = (UniqueConstraint("list_name", "item_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    list_name: Mapped[str] = mapped_column(String(20))  # IRDAI List I to IV
    item_number: Mapped[int] = mapped_column(Integer)
    item: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(100))
    condition: Mapped[str | None] = mapped_column(Text)  # when the item is not paid separately
    source: Mapped[str] = mapped_column(String(60))
    source_version: Mapped[str] = mapped_column(String(40))


class PolicyClauseRow(Base):
    __tablename__ = "policy_clauses"
    __table_args__ = (UniqueConstraint("product_uin", "clause_ref"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_uin: Mapped[str] = mapped_column(String(60))
    clause_ref: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300))
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(60))
    source_version: Mapped[str] = mapped_column(String(40))


class DocumentChecklistRow(Base):
    __tablename__ = "document_checklists"
    __table_args__ = (UniqueConstraint("request_type", "doc_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_type: Mapped[str] = mapped_column(String(30))
    doc_type: Mapped[str] = mapped_column(String(60))
    required: Mapped[bool] = mapped_column(Boolean)
    # When set, the document is required only at or above this claimed amount (rupees).
    mandatory_from_amount: Mapped[Decimal | None] = mapped_column(Money)
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(60))
    source_version: Mapped[str] = mapped_column(String(40))


class HospitalRow(Base):
    __tablename__ = "hospitals"

    rohini_id: Mapped[str] = mapped_column(String(13), primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    city: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(100))
    pincode: Mapped[str | None] = mapped_column(String(6))
    network: Mapped[bool] = mapped_column(Boolean, default=True)
    # False when the hospital charges the same rates whatever the room category.
    differential_billing: Mapped[bool] = mapped_column(Boolean, default=True)
    beds: Mapped[int | None] = mapped_column(Integer)
    registered_on: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(60))
    source_version: Mapped[str] = mapped_column(String(40))
