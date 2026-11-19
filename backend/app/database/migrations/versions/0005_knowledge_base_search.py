"""knowledge base search

Slice 4: the pg_trgm extension and a trigram index on icd_index.path, so the ICD index
search (every word ILIKE '%word%', ranked by similarity) does not scan the table once the
full CMS index is loaded in Phase 2.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-19 20:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_index(
        "ix_icd_index_path_trgm",
        "icd_index",
        ["path"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"path": "gin_trgm_ops"},
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_icd_index_path_trgm", table_name="icd_index")
    # The extension stays: other objects may depend on it.
