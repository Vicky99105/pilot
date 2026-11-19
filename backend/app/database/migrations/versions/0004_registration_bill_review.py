"""registration and bill review

Slice 3: the non-payable item condition and its natural key, and the hospital's
differential billing flag. Server defaults fill rows that already exist.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-19 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("non_payable_items", sa.Column("condition", sa.Text(), nullable=True))
    op.create_unique_constraint(
        op.f("uq_non_payable_items_list_name"), "non_payable_items", ["list_name", "item_number"]
    )
    op.add_column(
        "hospitals",
        sa.Column("differential_billing", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("hospitals", "differential_billing")
    op.drop_constraint(op.f("uq_non_payable_items_list_name"), "non_payable_items", type_="unique")
    op.drop_column("non_payable_items", "condition")
