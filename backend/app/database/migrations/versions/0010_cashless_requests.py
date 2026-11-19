"""cashless requests: clocks, current request, originals, hospital payee

Slice P2-5: each submission carries the clock of its request (1 hour for a pre-auth or
enhancement, 3 hours for a discharge); a claim records the request being processed and the
courier deadline for the hospital's originals; a hospital agreement names the account a
cashless claim is paid to.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-22 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "submissions", sa.Column("clock_deadline", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("claims", sa.Column("current_request", sa.String(length=30), nullable=True))
    op.add_column("claims", sa.Column("current_submission_id", sa.Uuid(), nullable=True))
    op.add_column(
        "claims", sa.Column("originals_due_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "claims", sa.Column("originals_received_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("claims", sa.Column("originals_received_by", sa.Uuid(), nullable=True))
    op.add_column(
        "hospital_agreements", sa.Column("payee_name", sa.String(length=200), nullable=True)
    )
    op.add_column(
        "hospital_agreements",
        sa.Column("payee_account_number", sa.String(length=18), nullable=True),
    )
    op.add_column(
        "hospital_agreements", sa.Column("payee_ifsc", sa.String(length=11), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    for column in ("payee_ifsc", "payee_account_number", "payee_name"):
        op.drop_column("hospital_agreements", column)
    for column in (
        "originals_received_by",
        "originals_received_at",
        "originals_due_at",
        "current_submission_id",
        "current_request",
    ):
        op.drop_column("claims", column)
    op.drop_column("submissions", "clock_deadline")
