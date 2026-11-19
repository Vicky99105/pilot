// Typed client for the claims API. Shapes mirror the backend pydantic models; money and
// other Decimals arrive as strings (pydantic JSON mode) and stay strings until formatted.

export type Money = string
export type Uuid = string
export type IsoDateTime = string
export type IsoDate = string

export type Role =
  | 'hospital_user'
  | 'member'
  | 'claims_operator'
  | 'bill_processor'
  | 'medical_officer'
  | 'approver'
  | 'crc_reviewer'
  | 'investigator'
  | 'finance'
  | 'qc_auditor'
  | 'supervisor'
  | 'admin'

export type Stage =
  | 'intake'
  | 'classification'
  | 'digitisation'
  | 'registration'
  | 'medical_coding'
  | 'bill_review'
  | 'medical_review'
  | 'risk'
  | 'adjudication'
  | 'decision'
  | 'authorisation'
  | 'communication'
  | 'query'
  | 'settlement'
  | 'monitoring'

export const DOC_TYPES = [
  'claim_form_part_a',
  'claim_form_part_b',
  'final_bill',
  'payment_receipt',
  'discharge_summary',
  'ot_notes',
  'investigation_report',
  'prescription',
  'pharmacy_bill',
  'cancelled_cheque',
  'kyc',
  'fir_mlc',
  'preauth_form',
  'enhancement_request',
  'interim_bill',
  'other',
] as const
export type DocType = (typeof DOC_TYPES)[number]

export type RequestType =
  | 'preauth'
  | 'enhancement'
  | 'discharge'
  | 'reimbursement'
  | 'query_reply'
  | 'reconsideration'
  | 'cancellation'

/** The requests a hospital sends on a claim its pre-auth opened. */
export type LaterRequestType = 'enhancement' | 'discharge' | 'cancellation'

/** The stream a medical officer reviews and a medical task belongs to. */
export type SystemOfMedicine = 'allopathy' | 'ayush'

export type User = {
  id: Uuid
  name: string
  kind: 'staff' | 'member' | 'hospital'
  roles: Role[]
  max_amount: Money | null
  hospital_id: string | null
  policy_number: string | null
  member_id: string | null
  system_of_medicine: SystemOfMedicine | null
}

// --- Submit area ---------------------------------------------------------------------

export type ClaimFormFields = {
  policy_number: string
  member_id: string
  patient_name: string
  hospital_id?: string | null
  hospital_name?: string | null
  hospital_city?: string | null
  admission_date: IsoDate
  discharge_date?: IsoDate | null
  claimed_amount: Money
  claim_category?: 'inpatient' | 'day_care' | 'pre_post_hospitalisation' | null
  cause?: 'illness' | 'injury' | 'maternity' | null
  bank_account_number?: string | null
  ifsc?: string | null
  declaration_accepted?: boolean
}

export type SubmissionForm = ClaimFormFields & { request_type: RequestType }

export type SubmittedFile = {
  file_id: Uuid
  filename: string | null
  sha256: string
  slot: DocType | null
  mime: string
  size_bytes: number
  storage_key: string
  quality_warning: string | null
}

export type Submission = {
  submission_id: Uuid
  claim_id: Uuid
  request_type: RequestType
  parent_claim_id: Uuid | null
  form: ClaimFormFields
  files: SubmittedFile[]
  missing_documents: DocType[]
  warnings: string[]
  deadline: IsoDateTime | null
  clock_deadline: IsoDateTime | null
  duplicate_of: Uuid | null
  workflow_id: string | null
  created_at: IsoDateTime
}

/** One document a request needs, in checklist order. */
export type ChecklistItem = {
  doc_type: DocType
  required: boolean
  mandatory_from_amount: Money | null
  notes: string | null
}

export type ClaimSummary = {
  claim_id: Uuid
  claim_number: string | null
  request_type: RequestType
  status: string
  patient_name: string | null
  deadline: IsoDateTime | null
  created_at: IsoDateTime
}

export type SubmittedFileView = {
  file_id: Uuid
  filename: string | null
  slot: DocType | null
  mime: string
  size_bytes: number
  storage_key: string
  quality_warning: string | null
}

export type SubmissionView = {
  submission_id: Uuid
  request_type: RequestType
  created_at: IsoDateTime
  duplicate_of: Uuid | null
  files: SubmittedFileView[]
  missing_documents: DocType[]
  warnings: string[]
}

export type LetterView = {
  letter_id: Uuid
  kind: string
  created_at: IsoDateTime
  pdf_key: string
  download_url: string
  sent: boolean
}

export type OpenQueryView = {
  query_id: Uuid
  items: string[]
  status: string
  raised_at: IsoDateTime
  deadline: IsoDateTime
  reply_url: string
}

/** One cashless authorisation: what the request added and the total approved. */
export type AuthorisationView = {
  request_type: RequestType
  status: 'awaiting_discharge' | 'final' | 'rejected' | 'cancelled' | string
  approved_now: Money
  approved_total: Money
  authorised_at: IsoDateTime
}

export type OriginalsStatus = {
  claim_id: Uuid
  due_at: IsoDateTime
  received_at: IsoDateTime | null
  received_by: Uuid | null
}

/** One reconsideration of a closed claim, as the claim detail lists it. */
export type ReconsiderationView = {
  number: number
  grounds: string
  requested_at: IsoDateTime
  previous_decision: string | null
  previous_amount: Money
  previously_paid: Money
}

/** POST /api/claims/{id}/reconsideration returns this. */
export type Reconsideration = ReconsiderationView & {
  reconsideration_id: Uuid
  claim_id: Uuid
  submission_id: Uuid
  workflow_id: string
  requested_by: Uuid
  previous_closed_at: IsoDateTime | null
}

export type ClaimDetail = ClaimSummary & {
  policy_number: string | null
  member_id: string | null
  hospital_id: string | null
  submitted_by: Uuid | null
  closed_at: IsoDateTime | null
  submissions: SubmissionView[]
  letters: LetterView[]
  open_query: OpenQueryView | null
  // Cashless claims only; empty or null otherwise.
  current_request: RequestType | null
  clock_deadline: IsoDateTime | null
  authorisations: AuthorisationView[]
  originals_due_at: IsoDateTime | null
  originals_received_at: IsoDateTime | null
  // Set while a supervisor holds the claim.
  held_at: IsoDateTime | null
  hold_reason: string | null
  reconsiderations: ReconsiderationView[]
}

// --- Review area -----------------------------------------------------------------------

export type ClaimBrief = {
  claim_number: string | null
  patient_name: string | null
  claimed_amount: Money | null
}

export type TaskKind =
  | 'fix_labels'
  | 'fix_fields'
  | 'confirm_registration'
  | 'confirm_deductions'
  | 'medical_findings'
  | 'investigate'
  | 'approve'
  | 'crc'
  | 'query_no_reply'
  | 'release_payment'

export type Task = {
  id: Uuid
  claim_id: Uuid
  workflow_id: string
  kind: TaskKind | string
  role: Role
  title: string
  payload: Record<string, unknown>
  deadline: IsoDateTime
  status: 'open' | 'taken' | 'done' | 'escalated'
  assignee: Uuid | null
  outcome: Record<string, unknown> | null
  authority_amount: Money | null
  created_at: IsoDateTime | null
  claim: ClaimBrief | null
  system_of_medicine: SystemOfMedicine | null // medical officer tasks only
}

export type Assignee = { user_id: Uuid; name: string }

export type PageRef = { document_id: Uuid; page: number }

export type ClaimPage = {
  page_id: Uuid
  seq: number
  doc_type: DocType
  confidence: number
  image_key: string
  quality_warning: string | null
  document_id: Uuid | null
  page_in_document: number | null
}

export type ClaimPages = {
  claim_id: Uuid
  classification_version: number
  pages: ClaimPage[]
  documents: { document_id: Uuid; doc_type: DocType; page_ids: Uuid[] }[]
}

// --- Monitoring ------------------------------------------------------------------------

export type ClaimStatusRow = {
  claim_id: Uuid
  claim_number: string | null
  state: string
  entered_at: IsoDateTime
  dwell_seconds: number
  deadline: IsoDateTime | null
}

export type ClaimEvent = {
  stage: Stage
  action: string
  actor: string
  detail: Record<string, unknown> | null
  created_at: IsoDateTime
}

// --- Supervision -----------------------------------------------------------------------

export type QueueDepth = {
  role: Role
  unfinished: number // open, taken and escalated
  taken: number
  overdue: number
  by_kind: Record<string, number>
}

export type NearBreach = {
  task_id: Uuid
  claim_id: Uuid
  claim_number: string | null
  kind: string
  role: Role
  deadline: IsoDateTime
  minutes_left: number // negative once breached
  assignee: string | null
  held: boolean
}

export type UserActivity = {
  user_id: Uuid
  name: string
  completed: number
  decided: number
  overridden: number
  override_rate: number | null // 0..1
}

export type HeldClaim = {
  claim_id: Uuid
  claim_number: string | null
  status: string
  held_at: IsoDateTime
  held_by: Uuid | null
  reason: string
}

export type SupervisorOverview = {
  generated_at: IsoDateTime
  window_days: number
  near_breach_minutes: number
  queues: QueueDepth[]
  near_breach: NearBreach[]
  users: UserActivity[]
  held_claims: HeldClaim[]
}

// --- Pilot metrics -----------------------------------------------------------------------
// Every metric counts what happened in [start, end). Rates are 0..1, null when the base is 0.

export type TaskTurnaround = {
  key: string // a queue (role) or a stage
  completed: number
  p50_seconds: number | null
  p95_seconds: number | null
  late: number // completed after the task deadline
}

export type RequestTurnaround = {
  request_type: string // reimbursement, preauth, enhancement, discharge, reconsideration
  answered: number
  p50_seconds: number | null
  p95_seconds: number | null
  breaches: number
}

export type QueryReason = { reason: string; claims: number; rate: number | null }

export type QueryRate = {
  claims_received: number
  claims_queried: number
  rate: number | null
  by_reason: QueryReason[]
}

export type OverrideRate = {
  key: string // a stage or a user ID
  name: string | null
  decided: number
  overridden: number
  rate: number | null
  entries: number
}

export type Outcomes = {
  decided: number
  by_decision: Record<string, number>
  referred_to_crc: number
  risk_scored: number
  risk_referred: number
  risk_referral_rate: number | null
  reconsiderations: number
}

export type ModelUse = {
  role: string
  calls: number
  claims: number
  invalid: number
  total_cost: number // USD
  cost_per_claim: number | null
  latency_p50_ms: number | null
  latency_p95_ms: number | null
}

export type ModelCosts = {
  calls: number
  claims: number
  total_cost: number
  cost_per_claim_mean: number | null
  cost_per_claim_p50: number | null
  cost_per_claim_p95: number | null
  latency_per_claim_p50_ms: number | null
  latency_per_claim_p95_ms: number | null
  by_role: ModelUse[]
}

export type PilotMetrics = {
  start: IsoDateTime
  end: IsoDateTime
  generated_at: IsoDateTime
  turnaround_by_role: TaskTurnaround[]
  turnaround_by_stage: TaskTurnaround[]
  request_turnaround: RequestTurnaround[]
  clock_breaches: number
  queries: QueryRate
  overrides_by_stage: OverrideRate[]
  overrides_by_user: OverrideRate[]
  outcomes: Outcomes
  models: ModelCosts
}

// --- Override log ------------------------------------------------------------------------

export type OverrideKind =
  | 'label_fix'
  | 'field_fix'
  | 'registration_override'
  | 'deduction_edit'
  | 'medical_overrule'
  | 'decision_override'

export type Override = {
  override_id: Uuid
  claim_id: Uuid
  claim_number: string | null
  stage: Stage | string
  kind: OverrideKind | string
  field: string | null
  before: unknown
  after: unknown
  reason: string | null
  actor: string
  user_id: Uuid | null
  created_at: IsoDateTime
}

export type OverrideFilter = { claim_id?: Uuid; user_id?: Uuid; limit?: number }

// --- Transport ---------------------------------------------------------------------------

export class ApiError extends Error {
  readonly status: number
  readonly messages: string[]

  constructor(status: number, messages: string[]) {
    super(messages.join('; ') || `request failed (${status})`)
    this.status = status
    this.messages = messages
  }
}

let currentUserId: string | null = null

/** The X-User-Id header sent on every request. The user picker sets it. */
export function setApiUser(id: string | null): void {
  currentUserId = id
}

type PydanticError = { loc?: (string | number)[]; msg?: string }

function detailMessages(detail: unknown): string[] {
  if (typeof detail === 'string') return [detail]
  if (Array.isArray(detail)) {
    return detail.map((d: unknown) => {
      if (typeof d === 'string') return d
      const e = d as PydanticError
      const where = (e.loc ?? []).filter((p) => p !== 'body').join('.')
      return where ? `${where}: ${e.msg ?? 'invalid'}` : (e.msg ?? 'invalid')
    })
  }
  return []
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (currentUserId) headers.set('X-User-Id', currentUserId)
  const response = await fetch(path, { ...init, headers })
  if (response.status === 204) return null as T
  const text = await response.text()
  const body: unknown = text ? JSON.parse(text) : null
  if (!response.ok) {
    const detail = (body as { detail?: unknown } | null)?.detail
    throw new ApiError(response.status, detailMessages(detail))
  }
  return body as T
}

function json(body: unknown): RequestInit {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

/** A file part: its field name is the slot (a document type) or "merged". */
export type UploadPart = { slot: DocType | 'merged'; file: File }

type MultipartForm = SubmissionForm | { request_type: LaterRequestType } | { grounds: string }

function multipart(parts: UploadPart[], form?: MultipartForm): FormData {
  const data = new FormData()
  if (form) data.append('form', JSON.stringify(form))
  for (const part of parts) data.append(part.slot, part.file, part.file.name)
  return data
}

export const api = {
  me: () => request<User>('/api/me'),

  submit: (form: SubmissionForm, parts: UploadPart[]) =>
    request<Submission>('/api/submissions', { method: 'POST', body: multipart(parts, form) }),
  myClaims: () => request<ClaimSummary[]>('/api/claims/mine'),
  claim: (id: Uuid) => request<ClaimDetail>(`/api/claims/${id}`),
  claimEvents: (id: Uuid) => request<ClaimEvent[]>(`/api/claims/${id}/events`),
  claimPages: (id: Uuid) => request<ClaimPages>(`/api/claims/${id}/pages`),
  replyToQuery: (queryId: Uuid, parts: UploadPart[]) =>
    request<Submission>(`/api/queries/${queryId}/reply`, {
      method: 'POST',
      body: multipart(parts),
    }),

  checklist: (requestType: RequestType) => request<ChecklistItem[]>(`/api/checklists/${requestType}`),
  sendRequest: (claimId: Uuid, requestType: LaterRequestType, parts: UploadPart[]) =>
    request<Submission>(`/api/claims/${claimId}/requests`, {
      method: 'POST',
      body: multipart(parts, { request_type: requestType }),
    }),
  markOriginalsReceived: (claimId: Uuid) =>
    request<OriginalsStatus>(`/api/claims/${claimId}/originals-received`, { method: 'POST' }),

  reconsider: (claimId: Uuid, grounds: string, parts: UploadPart[]) =>
    request<Reconsideration>(`/api/claims/${claimId}/reconsideration`, {
      method: 'POST',
      body: multipart(parts, { grounds }),
    }),

  queue: () => request<Task[]>('/api/tasks/queue'),
  takeNext: () => request<Task | null>('/api/tasks/next', { method: 'POST' }),
  task: (id: Uuid) => request<Task>(`/api/tasks/${id}`),
  complete: (id: Uuid, outcome: Record<string, unknown>) =>
    request<Task>(`/api/tasks/${id}/complete`, json({ outcome })),

  assignees: (taskId: Uuid) => request<Assignee[]>(`/api/tasks/${taskId}/assignees`),
  reassign: (taskId: Uuid, toUserId: Uuid, reason: string) =>
    request<Task>(`/api/tasks/${taskId}/reassign`, json({ to_user_id: toUserId, reason })),

  monitoringClaims: () => request<ClaimStatusRow[]>('/api/monitoring/claims'),

  supervisorOverview: (windowDays: number, nearBreachMinutes: number) =>
    request<SupervisorOverview>(
      `/api/supervisor/overview?window_days=${windowDays}&near_breach_minutes=${nearBreachMinutes}`,
    ),
  hold: (claimId: Uuid, reason: string) => request<HeldClaim>(`/api/claims/${claimId}/hold`, json({ reason })),
  resume: (claimId: Uuid, reason: string) => request<null>(`/api/claims/${claimId}/resume`, json({ reason })),

  pilotMetrics: (start: IsoDateTime, end: IsoDateTime) =>
    request<PilotMetrics>(
      `/api/monitoring/metrics?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`,
    ),

  overrides: (filter: OverrideFilter = {}) => {
    const query = new URLSearchParams()
    if (filter.claim_id) query.set('claim_id', filter.claim_id)
    if (filter.user_id) query.set('user_id', filter.user_id)
    if (filter.limit) query.set('limit', String(filter.limit))
    const qs = query.toString()
    return request<Override[]>(`/api/overrides${qs ? `?${qs}` : ''}`)
  },
}

/** Files are permission checked by X-User-Id, so a plain link cannot fetch them. */
export async function fetchFileBlob(key: string): Promise<Blob> {
  const headers = new Headers()
  if (currentUserId) headers.set('X-User-Id', currentUserId)
  const response = await fetch(`/api/files/${key}`, { headers })
  if (!response.ok) throw new ApiError(response.status, [`file not available (${response.status})`])
  return response.blob()
}
