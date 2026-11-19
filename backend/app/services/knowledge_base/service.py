"""Default knowledge base over the Postgres knowledge tables.

Index search: every word of the term must appear in the entry's path (ILIKE, served by a
trigram index on Postgres). Postgres ranks by trigram similarity; SQLite, used only in
tests, falls back to LIKE and ranks by path length.
"""

import re
from typing import Any

from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import IcdIndexRow, IcdTabularRow, PolicyClauseRow
from app.services.knowledge_base.contract import (
    ICD10CM,
    MAX_INDEX_HITS,
    IcdIndexHit,
    IcdTabularEntry,
    PolicyClause,
)
from app.shared.types import Code

# Words too common in index paths to narrow a search.
_STOPWORDS = {"a", "an", "and", "of", "the", "with", "without", "in", "or", "due", "to", "by"}


def _words(term: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", term.lower())
    return [w for w in words if w not in _STOPWORDS] or words


def _like(word: str) -> str:
    return f"%{word}%"


class DefaultKnowledgeBase:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def search_icd_index(self, term: str) -> list[IcdIndexHit]:
        words = _words(term)
        if not words:
            return []
        async with self._sessions() as session:
            query: Select[Any] = select(IcdIndexRow).where(
                IcdIndexRow.system == ICD10CM,
                and_(*(IcdIndexRow.path.ilike(_like(w)) for w in words)),
            )
            if session.get_bind().dialect.name == "postgresql":
                score = func.similarity(func.lower(IcdIndexRow.path), " ".join(words))
                query = query.order_by(score.desc(), func.length(IcdIndexRow.path))
            else:
                query = query.order_by(func.length(IcdIndexRow.path), IcdIndexRow.path)
            rows = await session.scalars(query.limit(MAX_INDEX_HITS))
            return [IcdIndexHit(term=r.term, path=r.path, code=r.code, see=r.see) for r in rows]

    async def get_icd_tabular(self, code: str) -> IcdTabularEntry | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(IcdTabularRow)
                .where(
                    IcdTabularRow.system == ICD10CM,
                    IcdTabularRow.code == code.strip().upper(),
                )
                .order_by(IcdTabularRow.id.desc())
                .limit(1)
            )
        if row is None:
            return None
        notes = row.notes or {}
        return IcdTabularEntry(
            code=Code(system=row.system, version=row.version, code=row.code, display=row.title),
            billable=row.billable,
            includes=notes.get("includes", []),
            excludes1=notes.get("excludes1", []),
            excludes2=notes.get("excludes2", []),
            code_first=notes.get("code_first", []),
            use_additional=notes.get("use_additional", []),
            source=row.source,
        )

    async def get_clause(self, product_uin: str, clause_ref: str) -> PolicyClause | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(PolicyClauseRow).where(
                    PolicyClauseRow.product_uin == product_uin,
                    PolicyClauseRow.clause_ref == clause_ref,
                )
            )
        if row is None:
            return None
        return PolicyClause(
            product_uin=row.product_uin, clause_ref=row.clause_ref, title=row.title, text=row.text
        )
