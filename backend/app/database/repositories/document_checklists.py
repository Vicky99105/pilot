"""Document checklists per request type."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import DocumentChecklistRow
from app.shared.types import DocType, RequestType


async def checklist(
    session: AsyncSession, request_type: RequestType, claimed_amount: Decimal
) -> tuple[list[DocType], list[DocType]]:
    """(required, listed) document types for this request, in checklist order.

    Required documents are those marked required whose amount threshold, if any, is met.
    Listed documents are every document the checklist names, required or not.
    """
    rows = list(
        await session.scalars(
            select(DocumentChecklistRow)
            .where(DocumentChecklistRow.request_type == request_type.value)
            .order_by(DocumentChecklistRow.id)
        )
    )
    required = [
        DocType(r.doc_type)
        for r in rows
        if r.required
        and (r.mandatory_from_amount is None or claimed_amount >= r.mandatory_from_amount)
    ]
    return required, [DocType(r.doc_type) for r in rows]
