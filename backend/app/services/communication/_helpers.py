"""Letter text, the plain-language check and PDF rendering. Private to communication."""

import io
import re
from datetime import datetime
from decimal import Decimal
from xml.sax.saxutils import escape

from pydantic import BaseModel
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.services.communication.contract import DeductionRow, Letter
from app.services.model_gateway.contract import Message

PLAIN_PURPOSE = "letter_plain_reasons"
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

TITLES = {
    "approval": "Claim approved",
    "partial": "Claim approved in part",
    "rejection": "Claim not payable",
    "query": "Information needed for your claim",
}


class PlainReasons(BaseModel):
    items: list[str]


def rupees(value: Decimal) -> str:
    return f"Rs {value:,.2f}"


def row_reason(row: DeductionRow) -> str:
    """One raw reason per deduction row. Every number in it comes from the sheet."""
    clause = f" (clause {row.clause_ref})" if row.clause_ref else ""
    return (
        f"{row.description}: {rupees(row.claimed)} claimed, {rupees(row.deducted)} deducted. "
        f"{row.reason.rstrip('.')}.{clause}"
    )


def plain_messages(raw: list[str]) -> list[Message]:
    numbered = "\n".join(f"{i}. {r}" for i, r in enumerate(raw, start=1))
    return [
        Message(
            role="system",
            content=(
                "You rewrite the reasons in a health insurance claim letter in plain, polite "
                "English for the patient. Keep every number, rupee amount and clause reference "
                "exactly as written; do not add, round or compute any number. Return one item "
                "per reason, in the same order."
            ),
        ),
        Message(role="user", content=f"Reasons:\n{numbered}"),
    ]


def _numbers(text: str) -> set[str]:
    return {n.rstrip(",") for n in NUMBER.findall(text)}


def faithful(raw: list[str], rewritten: list[str], clauses: list[list[str]]) -> bool:
    """True when each rewritten reason keeps every number and clause ref of its raw reason."""
    if len(raw) != len(rewritten):
        return False
    for before, after, refs in zip(raw, rewritten, clauses, strict=True):
        if not _numbers(before) <= _numbers(after):
            return False
        if any(ref not in after for ref in refs):
            return False
    return True


def render_pdf(letter: Letter) -> bytes:
    styles = getSampleStyleSheet()
    body, small = styles["BodyText"], ParagraphStyle("small", parent=styles["BodyText"], fontSize=8)
    story: list[Flowable] = [
        Paragraph(escape(TITLES[letter.kind]), styles["Title"]),
        Paragraph(f"Claim number: {escape(letter.claim_number or 'pending')}", body),
        Paragraph(f"Date: {_date(letter.created_at)}", body),
        Paragraph(f"To: {escape(letter.addressee)}", body),
        Spacer(1, 6 * mm),
        Paragraph(escape(_opening(letter)), body),
        Spacer(1, 4 * mm),
    ]
    if letter.deductions:
        story += [_table(letter.deductions, small), Spacer(1, 4 * mm)]
    if letter.reasons:
        story.append(Paragraph("Reasons", styles["Heading3"]))
        story += [Paragraph(f"{i}. {escape(r)}", body) for i, r in enumerate(letter.reasons, 1)]
    if letter.query_items:
        story.append(Paragraph("Please send us", styles["Heading3"]))
        story += [Paragraph(f"{i}. {escape(q)}", body) for i, q in enumerate(letter.query_items, 1)]
    story += [
        Spacer(1, 8 * mm),
        Paragraph("Pilot in shadow mode: generated from mock data. Not a real decision.", small),
    ]
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, title=TITLES[letter.kind], invariant=1)
    doc.build(story)
    return buffer.getvalue()


def _opening(letter: Letter) -> str:
    if letter.kind == "approval":
        return f"Your claim is approved. Amount payable: {rupees(letter.amount)}."
    if letter.kind == "partial":
        claimed = rupees(letter.claimed) if letter.claimed is not None else "the amount claimed"
        return (
            f"Your claim is approved in part. Amount claimed: {claimed}. Amount payable: "
            f"{rupees(letter.amount)}. The deductions are listed below."
        )
    if letter.kind == "rejection":
        return "We are unable to pay this claim, for the reasons below."
    reply_by = _date(letter.reply_by) if letter.reply_by else "the date in your claim portal"
    return (
        "We need the following to continue processing your claim. Please upload them "
        f"through the claim portal by {reply_by}."
    )


def _table(rows: list[DeductionRow], style: ParagraphStyle) -> Table:
    data: list[list[object]] = [["Line", "Claimed (Rs)", "Deducted (Rs)", "Reason", "Clause"]]
    for row in rows:
        data.append(
            [
                str(row.line_id) if row.line_id is not None else "",
                f"{row.claimed:,.2f}",
                f"{row.deducted:,.2f}",
                Paragraph(escape(f"{row.description}: {row.reason}"), style),
                row.clause_ref or "",
            ]
        )
    table = Table(data, colWidths=[12 * mm, 26 * mm, 26 * mm, 86 * mm, 24 * mm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8ECF2")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#9AA5B1")),
                ("ALIGN", (1, 1), (2, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return table


def _date(value: datetime) -> str:
    return value.strftime("%d %b %Y")
