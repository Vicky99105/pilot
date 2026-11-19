"""Fixed identifiers for Phase 1 seed data, shared by the claims and mock_core seeds.

All data is synthetic. The UUIDs are fixed so the X-User-Id header can name a user.
"""

from uuid import UUID

MEMBER_USER_ID = UUID("00000000-0000-4000-8000-000000000001")
HOSPITAL_USER_ID = UUID("00000000-0000-4000-8000-000000000002")
OPS_USER_ID = UUID("00000000-0000-4000-8000-000000000003")
DOCTOR_USER_ID = UUID("00000000-0000-4000-8000-000000000004")
FINANCE_USER_ID = UUID("00000000-0000-4000-8000-000000000005")

# The member user's policy and member ID in mock_core.
MEMBER_POLICY_NUMBER = "MOCK/AS/2026/000101"
MEMBER_MEMBER_ID = "MOCK-M-000101-01"

# Synthetic 13-digit ROHINI IDs. The hospital user is tied to the first one.
HOSPITAL_ROHINI_IDS = (
    "8900000000011",
    "8900000000028",
    "8900000000035",
    "8900000000042",
    "8900000000059",
)
