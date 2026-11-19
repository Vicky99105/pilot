"""Default knowledge base over the Postgres knowledge tables.

Index search: every word of the term must appear in the entry's search text, the path plus
its nonessential modifiers (ILIKE, served by a trigram index on Postgres). Postgres ranks by
the trigram similarity of the path alone, so long modifier lists do not push an entry down;
SQLite, used only in tests, ranks by path length.

PCS: the tables are the source of truth. Options for the first three characters come from
the table headers; options for characters 4 to 7 come from the rows of one table that match
the prefix. A code is valid when one row holds all four of its last characters.
"""

import re
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import (
    IcdIndexRow,
    IcdTabularRow,
    KbSourceLoadRow,
    PcsDefinitionRow,
    PcsRowRow,
    PcsTableRow,
    PolicyClauseRow,
)
from app.services.knowledge_base import _reference
from app.services.knowledge_base.contract import (
    ICD10CM,
    ICD10PCS,
    MAX_INDEX_HITS,
    PCS_LENGTH,
    DrugCheck,
    Exclusion,
    GuidelineSection,
    IcdIndexHit,
    IcdTabularEntry,
    IndexName,
    LosNorm,
    NonPayableCandidate,
    PcsOption,
    PolicyClause,
    TariffCandidate,
)
from app.shared.types import Code

# Words too common in index paths to narrow a search.
_STOPWORDS = {"a", "an", "and", "of", "the", "with", "without", "in", "or", "due", "to", "by"}
_NOTE_FIELDS = (
    "inclusion_terms",
    "includes",
    "excludes1",
    "excludes2",
    "code_first",
    "use_additional",
    "code_also",
    "notes",
    "seven_chr_note",
)


def _words(term: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", term.lower())
    return [w for w in words if w not in _STOPWORDS] or words


def _like(word: str) -> str:
    return f"%{word}%"


def _cm_code(code: str) -> str:
    """K3580, k35.80 and K35.80- all become K35.80."""
    key = code.strip().upper().rstrip("-").rstrip(".").replace(".", "")
    return key if len(key) <= 3 else f"{key[:3]}.{key[3:]}"


def _range_key(code: str) -> str:
    return code.strip().upper().rstrip("-").replace(".", "")


class DefaultKnowledgeBase:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    # --- Versions ------------------------------------------------------------------------

    async def _version(self, session: AsyncSession, system: str, as_of: date | None) -> str | None:
        """The loaded version in force on as_of, else the latest loaded, else None (read
        whatever rows exist)."""
        loads = (
            await session.execute(
                select(
                    KbSourceLoadRow.version,
                    KbSourceLoadRow.effective_from,
                    KbSourceLoadRow.effective_to,
                )
                .where(KbSourceLoadRow.system == system)
                .distinct()
            )
        ).all()
        if not loads:
            return None
        covering = [r for r in loads if as_of and r.effective_from <= as_of <= r.effective_to]
        return str(max(covering or loads, key=lambda r: r.effective_from).version)

    # --- ICD-10-CM index -----------------------------------------------------------------

    async def search_icd_index(
        self, term: str, index: IndexName = "diseases", *, as_of: date | None = None
    ) -> list[IcdIndexHit]:
        words = _words(term)
        if not words:
            return []
        async with self._sessions() as session:
            version = await self._version(session, ICD10CM, as_of)
            query: Select[Any] = select(IcdIndexRow).where(
                IcdIndexRow.system == ICD10CM,
                IcdIndexRow.index_name == index,
                and_(*(IcdIndexRow.search_text.ilike(_like(w)) for w in words)),
            )
            if version is not None:
                query = query.where(IcdIndexRow.version == version)
            if session.get_bind().dialect.name == "postgresql":
                # Rank by the path: the modifiers only widen the match.
                score = func.similarity(func.lower(IcdIndexRow.path), " ".join(words))
                query = query.order_by(score.desc(), func.length(IcdIndexRow.path), IcdIndexRow.id)
            else:
                query = query.order_by(
                    func.length(IcdIndexRow.path), IcdIndexRow.path, IcdIndexRow.id
                )
            rows = await session.scalars(query.limit(MAX_INDEX_HITS))
            return [
                IcdIndexHit(
                    index=index,
                    term=r.term,
                    path=r.path,
                    code=r.code,
                    column=r.table_column,
                    manifestation=r.manifestation,
                    see=r.see,
                    see_also=r.see_also,
                )
                for r in rows
            ]

    # --- Tabular -------------------------------------------------------------------------

    async def get_icd_tabular(
        self, code: str, *, system: str = ICD10CM, as_of: date | None = None
    ) -> IcdTabularEntry | None:
        key = _cm_code(code) if system == ICD10CM else code.strip().upper()
        async with self._sessions() as session:
            version = await self._version(session, system, as_of)
            query = select(IcdTabularRow).where(
                IcdTabularRow.system == system, IcdTabularRow.code == key
            )
            if version is not None:
                query = query.where(IcdTabularRow.version == version)
            row = await session.scalar(query.order_by(IcdTabularRow.id.desc()).limit(1))
            if row is None:
                return None
            children = await session.scalars(
                select(IcdTabularRow.code)
                .where(
                    IcdTabularRow.system == system,
                    IcdTabularRow.version == row.version,
                    IcdTabularRow.parent == row.code,
                )
                .order_by(IcdTabularRow.code)
            )
            child_codes = list(children)
        notes = row.notes or {}
        return IcdTabularEntry(
            code=Code(system=row.system, version=row.version, code=row.code, display=row.title),
            billable=row.billable,
            **{name: notes.get(name, []) for name in _NOTE_FIELDS},
            seven_chr_def=notes.get("seven_chr_def", {}),
            chapter=row.chapter,
            section=row.section,
            parent=row.parent,
            children=child_codes,
            source=row.source,
        )

    # --- ICD-10-PCS ----------------------------------------------------------------------

    async def pcs_options(self, prefix: str, *, as_of: date | None = None) -> list[PcsOption]:
        prefix = prefix.strip().upper()
        position = len(prefix) + 1
        if position > PCS_LENGTH:
            return []
        async with self._sessions() as session:
            version = await self._version(session, ICD10PCS, as_of)
            if position <= 3:
                found = await self._header_options(session, prefix, version)
            else:
                found = await self._row_options(session, prefix, version)
            if not found:
                return []
            definitions = await self._definitions(
                session, prefix[:1] or None, position, {o.label for o in found}, version
            )
        options = []
        for option in sorted(found, key=lambda o: o.value):
            meaning = definitions.get(option.label)
            if meaning is not None:
                option = option.model_copy(
                    update={
                        "definition": option.definition or meaning.definition,
                        "explanation": meaning.explanation,
                        "includes": list(meaning.includes or []),
                    }
                )
            options.append(option)
        return options

    async def pcs_validate(self, code: str, *, as_of: date | None = None) -> bool:
        code = code.strip().upper()
        if len(code) != PCS_LENGTH:
            return False
        async with self._sessions() as session:
            version = await self._version(session, ICD10PCS, as_of)
            rows = await self._table_rows(session, code[:3], version)
        return any(_row_holds(axes, code[3:]) for axes in rows)

    async def _header_options(
        self, session: AsyncSession, prefix: str, version: str | None
    ) -> list[PcsOption]:
        query = select(PcsTableRow).where(
            PcsTableRow.system == ICD10PCS, PcsTableRow.code.startswith(prefix, autoescape=True)
        )
        if version is not None:
            query = query.where(PcsTableRow.version == version)
        index = len(prefix)
        found: dict[str, PcsOption] = {}
        for table in await session.scalars(query):
            axis, value, label = table.axes[index]
            if value not in found:
                found[value] = PcsOption(
                    position=index + 1,
                    axis=axis,
                    value=value,
                    label=label,
                    definition=table.operation_definition if index == 2 else None,
                )
        return list(found.values())

    async def _row_options(
        self, session: AsyncSession, prefix: str, version: str | None
    ) -> list[PcsOption]:
        chosen = prefix[3:]
        index = len(chosen)  # 0 to 3: which of the row's four axes comes next
        found: dict[str, PcsOption] = {}
        for axes in await self._table_rows(session, prefix[:3], version):
            if not _row_holds(axes, chosen):
                continue
            axis, labels = axes[index]
            for value, label in labels:
                found.setdefault(
                    value,
                    PcsOption(position=index + 4, axis=axis, value=value, label=label),
                )
        return list(found.values())

    async def _table_rows(
        self, session: AsyncSession, table: str, version: str | None
    ) -> list[list[Any]]:
        query = select(PcsRowRow.axes).where(
            PcsRowRow.system == ICD10PCS, PcsRowRow.table_code == table
        )
        if version is not None:
            query = query.where(PcsRowRow.version == version)
        return list(await session.scalars(query.order_by(PcsRowRow.row_number)))

    async def _definitions(
        self,
        session: AsyncSession,
        section: str | None,
        position: int,
        labels: set[str],
        version: str | None,
    ) -> dict[str, PcsDefinitionRow]:
        if section is None:
            return {}
        query = select(PcsDefinitionRow).where(
            PcsDefinitionRow.system == ICD10PCS,
            PcsDefinitionRow.section == section,
            PcsDefinitionRow.pos == position,
            PcsDefinitionRow.label.in_(labels),
        )
        if version is not None:
            query = query.where(PcsDefinitionRow.version == version)
        return {row.label: row for row in await session.scalars(query)}

    # --- Ranges --------------------------------------------------------------------------

    def code_in_ranges(self, code: str, ranges: list[str]) -> bool:
        key = _range_key(code)
        for item in ranges:
            low, _, high = item.partition("-") if _is_span(item) else (item, "", item)
            low, high = _range_key(low), _range_key(high)
            if low <= key and (key <= high or key.startswith(high)):
                return True
        return False

    # --- Policy --------------------------------------------------------------------------

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

    # --- Public reference data --------------------------------------------------------------

    async def tariff_candidates(
        self, procedure_codes: list[str], names: list[str], hospital_id: str | None
    ) -> list[TariffCandidate]:
        async with self._sessions() as session:
            return await _reference.tariff_candidates(session, procedure_codes, names, hospital_id)

    async def get_guideline(self, package_code: str) -> list[GuidelineSection]:
        async with self._sessions() as session:
            return await _reference.get_guideline(session, package_code)

    async def get_los_norm(self, package_code_or_name: str) -> LosNorm | None:
        async with self._sessions() as session:
            return await _reference.get_los_norm(session, package_code_or_name)

    async def check_drug(self, name: str, unit_price: Decimal | None) -> DrugCheck:
        async with self._sessions() as session:
            return await _reference.check_drug(session, name, unit_price)

    async def match_non_payable(self, text: str) -> list[NonPayableCandidate]:
        async with self._sessions() as session:
            return await _reference.match_non_payable(session, text)

    async def exclusion(self, code: str) -> Exclusion | None:
        async with self._sessions() as session:
            return await _reference.exclusion(session, code)


def _row_holds(axes: list[Any], chars: str) -> bool:
    """True when every character is one of the row's values at its position."""
    return all(any(value == char for value, _ in axes[i][1]) for i, char in enumerate(chars))


def _is_span(item: str) -> bool:
    """True for two codes joined by a hyphen, such as K35-K38. K35.- and K35- are one code
    with the codes below it."""
    low, sep, high = item.strip().partition("-")
    return bool(sep and low and high.strip())
