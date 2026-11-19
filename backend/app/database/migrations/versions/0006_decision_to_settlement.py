"""decision to settlement

Slice 5: queries and letters, claims.closed_at, tasks.authority_amount for approver bands,
and user_roles.approval_limit renamed to max_amount.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-19 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column("user_roles", "approval_limit", new_column_name="max_amount")
    op.add_column("claims", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "tasks",
        sa.Column("authority_amount", sa.Numeric(precision=14, scale=2), nullable=True),
    )
    op.create_table(
        "queries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("items", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("letter_id", sa.Uuid(), nullable=True),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reminders", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reply_submission_id", sa.Uuid(), nullable=True),
        sa.Column("raised_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"]),
        sa.ForeignKeyConstraint(["reply_submission_id"], ["submissions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_queries_claim_id"), "queries", ["claim_id"], unique=False)
    op.create_table(
        "letters",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("query_id", sa.Uuid(), nullable=True),
        sa.Column("decision_version", sa.Integer(), nullable=True),
        sa.Column("recipient", sa.String(length=320), nullable=True),
        sa.Column("pdf_key", sa.String(length=100), nullable=False),
        sa.Column("sent", sa.Boolean(), nullable=False),
        sa.Column("send_error", sa.Text(), nullable=True),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"]),
        sa.ForeignKeyConstraint(["query_id"], ["queries.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pdf_key"),
    )
    op.create_index(op.f("ix_letters_claim_id"), "letters", ["claim_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_letters_claim_id"), table_name="letters")
    op.drop_table("letters")
    op.drop_index(op.f("ix_queries_claim_id"), table_name="queries")
    op.drop_table("queries")
    op.drop_column("tasks", "authority_amount")
    op.drop_column("claims", "closed_at")
    op.alter_column("user_roles", "max_amount", new_column_name="approval_limit")
