"""review area: override log, holds, system of medicine, reconsiderations

Slice P2-6: every person's change to a machine suggestion is logged (overrides); a
supervisor's hold is kept on the claim; medical officer tasks, users and hospitals carry a
system of medicine (allopathy or ayush); a reconsideration records what the claim was before
it reopened.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-19 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "overrides",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("field", sa.String(length=200), nullable=False),
        sa.Column("before", JSON, nullable=True),
        sa.Column("after", JSON, nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_overrides_claim_time", "overrides", ["claim_id", "created_at"])
    op.create_index("ix_overrides_user_time", "overrides", ["user_id", "created_at"])
    op.create_table(
        "reconsiderations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.String(length=100), nullable=False),
        sa.Column("grounds", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("previous_decision", sa.String(length=20), nullable=True),
        sa.Column("previous_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("previously_paid", sa.Numeric(14, 2), nullable=False),
        sa.Column("previous_closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"]),
        sa.ForeignKeyConstraint(["submission_id"], ["submissions.id"]),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("claim_id", "number"),
    )
    op.create_index("ix_reconsiderations_claim_id", "reconsiderations", ["claim_id"])
    op.add_column("claims", sa.Column("held_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("claims", sa.Column("held_by", sa.Uuid(), nullable=True))
    op.add_column("claims", sa.Column("hold_reason", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("system_of_medicine", sa.String(length=20), nullable=True))
    op.add_column("users", sa.Column("system_of_medicine", sa.String(length=20), nullable=True))
    op.add_column(
        "hospitals",
        sa.Column(
            "system_of_medicine",
            sa.String(length=20),
            nullable=False,
            server_default="allopathy",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("hospitals", "system_of_medicine")
    op.drop_column("users", "system_of_medicine")
    op.drop_column("tasks", "system_of_medicine")
    op.drop_column("claims", "hold_reason")
    op.drop_column("claims", "held_by")
    op.drop_column("claims", "held_at")
    op.drop_index("ix_reconsiderations_claim_id", table_name="reconsiderations")
    op.drop_table("reconsiderations")
    op.drop_index("ix_overrides_user_time", table_name="overrides")
    op.drop_index("ix_overrides_claim_time", table_name="overrides")
    op.drop_table("overrides")
