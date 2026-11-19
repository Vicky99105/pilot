"""The claim route of a request type: cashless or reimbursement."""

from app.shared.types import CASHLESS_REQUESTS, ClaimRoute, RequestType


def claim_route(request_type: RequestType) -> ClaimRoute | None:
    """Cashless or reimbursement, from the request type the claim was opened with. None for
    a request type that opens no claim of its own (a query reply, for example)."""
    if request_type in CASHLESS_REQUESTS:
        return "cashless"
    if request_type is RequestType.REIMBURSEMENT:
        return "reimbursement"
    return None
