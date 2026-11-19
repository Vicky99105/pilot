import type { DocType } from './api'

/** Indian digit grouping: 133000 becomes "Rs 1,33,000"; paise shown only when present. */
export function rupees(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return 'Rs -'
  const n = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(n)) return `Rs ${value}`
  const hasPaise = Math.round(Math.abs(n) * 100) % 100 !== 0
  const text = new Intl.NumberFormat('en-IN', {
    minimumFractionDigits: hasPaise ? 2 : 0,
    maximumFractionDigits: 2,
  }).format(n)
  return `Rs ${text}`
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return '-'
  return new Date(iso).toLocaleString('en-IN', {
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function date(iso: string | null | undefined): string {
  if (!iso) return '-'
  return new Date(iso).toLocaleDateString('en-IN', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  })
}

/** A duration in seconds as "2d 4h", "3h 10m" or "12m". Negative means overdue. */
export function duration(seconds: number): string {
  const s = Math.abs(Math.round(seconds))
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  const text = d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m`
  return seconds < 0 ? `${text} overdue` : text
}

export function percent(confidence: number): string {
  return `${Math.round(confidence * 100)}%`
}

const DOC_LABELS: Record<DocType, string> = {
  claim_form_part_a: 'Claim form Part A',
  claim_form_part_b: 'Claim form Part B',
  final_bill: 'Itemised final bill',
  payment_receipt: 'Payment receipt',
  discharge_summary: 'Discharge summary',
  investigation_report: 'Investigation reports',
  prescription: 'Prescriptions',
  pharmacy_bill: 'Pharmacy bills',
  cancelled_cheque: 'Cancelled cheque',
  kyc: 'KYC document',
  other: 'Other',
}

export function docLabel(doc: DocType | string): string {
  return DOC_LABELS[doc as DocType] ?? humanise(doc)
}

/** "bill_review" becomes "Bill review". */
export function humanise(code: string): string {
  const text = code.replaceAll('_', ' ')
  return text.charAt(0).toUpperCase() + text.slice(1)
}

export function fileSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${Math.max(1, Math.round(bytes / 1024))} KB`
}
