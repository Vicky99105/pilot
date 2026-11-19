import type { Role, SystemOfMedicine, User } from './api'

/** Seeded Phase 1 users. Mirrors backend/app/shared/seed_ids.py; roles come from /api/me. */
export const SEEDED_USERS = [
  { id: '00000000-0000-4000-8000-000000000001', label: 'Asha Verma (member)' },
  { id: '00000000-0000-4000-8000-000000000002', label: 'Sunrise Hospital desk' },
  { id: '00000000-0000-4000-8000-000000000003', label: 'Ops (operator, bills, investigation)' },
  { id: '00000000-0000-4000-8000-000000000004', label: 'Doctor (medical, approver to Rs 1 lakh, CRC)' },
  { id: '00000000-0000-4000-8000-000000000006', label: 'Senior approver (no limit)' },
  { id: '00000000-0000-4000-8000-000000000005', label: 'Finance' },
  { id: '00000000-0000-4000-8000-000000000007', label: 'Supervisor' },
  { id: '00000000-0000-4000-8000-000000000008', label: 'AYUSH doctor (medical officer)' },
] as const

const OUTSIDE_ROLES: Role[] = ['member', 'hospital_user']

export function isStaff(roles: Role[]): boolean {
  return roles.some((r) => !OUTSIDE_ROLES.includes(r))
}

/** Roles that work a task queue, in pipeline order. Mirrors QUEUE_ROLES in the supervision service. */
export const QUEUE_ROLES: Role[] = [
  'claims_operator',
  'bill_processor',
  'medical_officer',
  'approver',
  'crc_reviewer',
  'investigator',
  'finance',
  'qc_auditor',
]

/** The queues this user works. A supervisor watches the queues instead of working one. */
export function queueRoles(roles: Role[]): Role[] {
  return QUEUE_ROLES.filter((r) => roles.includes(r))
}

export function isSupervisor(user: User): boolean {
  return user.roles.includes('supervisor')
}

export const SYSTEM_LABELS: Record<SystemOfMedicine, string> = {
  allopathy: 'Allopathy',
  ayush: 'AYUSH',
}

export const ROLE_LABELS: Record<Role, string> = {
  hospital_user: 'Hospital user',
  member: 'Member',
  claims_operator: 'Claims operator',
  bill_processor: 'Bill processor',
  medical_officer: 'Medical officer',
  approver: 'Approver',
  crc_reviewer: 'CRC reviewer',
  investigator: 'Investigator',
  finance: 'Finance',
  qc_auditor: 'QC auditor',
  supervisor: 'Supervisor',
  admin: 'Admin',
}
