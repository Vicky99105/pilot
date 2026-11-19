import type { Stage } from '../../../lib/api'

// The workflow's stage order. A send-back may name the checkpoint's stage or an earlier one.
const PIPELINE: Stage[] = [
  'classification',
  'digitisation',
  'registration',
  'bill_review',
  'medical_review',
  'risk',
  'adjudication',
  'decision',
  'communication',
  'settlement',
]

export function sendBackTargets(stage: Stage): Stage[] {
  const at = PIPELINE.indexOf(stage)
  return at < 0 ? [] : PIPELINE.slice(0, at + 1)
}
