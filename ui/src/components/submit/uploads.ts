import type { DocType, UploadPart } from '../../lib/api'
import { fileSize } from '../../lib/format'

// Mirrors backend intake: accepted types (sniffed there from the bytes) and size limits.
const MB = 1024 * 1024
export const MAX_FILE_BYTES = { member: 10 * MB, hospital: 15 * MB } as const
export type Channel = keyof typeof MAX_FILE_BYTES

const ACCEPTED_EXTENSIONS = ['pdf', 'jpg', 'jpeg', 'png', 'tif', 'tiff', 'heic', 'heif']
export const ACCEPT_ATTR = '.pdf,.jpg,.jpeg,.png,.tif,.tiff,.heic,.heif'

/** The reason a file would be refused, or null. The server checks again from the bytes. */
export function fileProblem(file: File, channel: Channel, pdfOnly = false): string | null {
  const ext = file.name.split('.').pop()?.toLowerCase() ?? ''
  if (pdfOnly && ext !== 'pdf') return `${file.name}: the merged upload must be one PDF`
  if (!ACCEPTED_EXTENSIONS.includes(ext)) {
    return `${file.name}: unsupported file type; upload PDF, JPG, PNG, TIFF or HEIC`
  }
  const limit = MAX_FILE_BYTES[channel]
  if (file.size > limit) {
    return `${file.name}: ${fileSize(file.size)} is larger than the ${limit / MB} MB limit`
  }
  if (file.size === 0) return `${file.name}: the file is empty`
  return null
}

/** Reimbursement checklist. Mirrors REIMBURSEMENT_CHECKLIST in backend/app/database/seed.py. */
export const REIMBURSEMENT_SLOTS: { slot: DocType; required: boolean; note?: string }[] = [
  { slot: 'claim_form_part_a', required: true },
  { slot: 'claim_form_part_b', required: true, note: 'Filled and sealed by the hospital' },
  { slot: 'final_bill', required: true, note: 'Itemised, with every line' },
  { slot: 'payment_receipt', required: true },
  { slot: 'discharge_summary', required: true },
  { slot: 'investigation_report', required: false, note: 'If tests were billed' },
  { slot: 'prescription', required: false, note: 'If medicines were bought outside' },
  { slot: 'pharmacy_bill', required: false, note: 'If medicines were bought outside' },
  { slot: 'cancelled_cheque', required: true, note: 'For payment to your account' },
  { slot: 'kyc', required: false, note: 'Needed when the claim is Rs 1 lakh or more' },
  { slot: 'other', required: false, note: 'Anything else that supports the claim' },
]

export type SlotFiles = Partial<Record<DocType, File[]>>

export function slotParts(files: SlotFiles): UploadPart[] {
  return Object.entries(files).flatMap(([slot, list]) =>
    (list ?? []).map((file) => ({ slot: slot as DocType, file })),
  )
}
