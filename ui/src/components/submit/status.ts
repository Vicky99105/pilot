import type { Stage } from '../../lib/api'

/** The claim's journey as the submitter sees it, in order. claims.status holds a stage. */
export const JOURNEY: { label: string; states: string[] }[] = [
  { label: 'Received', states: ['received', 'intake'] },
  { label: 'Documents read', states: ['classification', 'digitisation'] },
  { label: 'Registered', states: ['registration'] },
  { label: 'Bills checked', states: ['bill_review'] },
  { label: 'Medical review', states: ['medical_review', 'risk', 'adjudication', 'query'] },
  { label: 'Decision', states: ['decision'] },
  { label: 'Letter sent', states: ['communication'] },
  { label: 'Payment', states: ['settlement'] },
  { label: 'Closed', states: ['monitoring', 'closed'] },
]

const STATUS_LABELS: Record<string, string> = {
  received: 'Received',
  intake: 'Received',
  classification: 'Reading documents',
  digitisation: 'Reading documents',
  registration: 'Registering',
  bill_review: 'Checking bills',
  medical_review: 'Medical review',
  risk: 'Under review',
  adjudication: 'Under review',
  query: 'Waiting for your reply',
  decision: 'Awaiting decision',
  communication: 'Letter being sent',
  settlement: 'Payment in progress',
  monitoring: 'Closed',
  closed: 'Closed',
}

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status
}

export function journeyIndex(status: string): number {
  return JOURNEY.findIndex((j) => j.states.includes(status))
}

export function statusTone(status: string): 'ok' | 'warn' | 'accent' | 'neutral' {
  if (status === 'closed' || status === 'monitoring') return 'ok'
  if (status === 'query') return 'warn'
  return 'accent'
}

export const STAGE_LABELS: Record<Stage, string> = {
  intake: 'Intake',
  classification: 'Classification',
  digitisation: 'Digitisation',
  registration: 'Registration',
  bill_review: 'Bill review',
  medical_review: 'Medical review',
  risk: 'Risk',
  adjudication: 'Adjudication',
  decision: 'Decision',
  communication: 'Communication',
  query: 'Query',
  settlement: 'Settlement',
  monitoring: 'Monitoring',
}
