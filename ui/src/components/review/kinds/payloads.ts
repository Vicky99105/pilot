// Checkpoint payload shapes, as the workflow activities dump them (pydantic JSON mode).
import type { DocType, Money, PageRef, Stage, Uuid } from '../../../lib/api'

export type RuleResult = {
  rule_id: string
  passed: boolean
  amount_effect: Money
  reason: string
  clause_ref: string | null
  judged_by: 'code' | 'model' | 'person'
}

export type Common = {
  stage: Stage
  error?: string // the previous outcome was refused; this is why
  correction_error?: string // the previous corrections were refused; this is why
}

export type FixLabelsPayload = Common & {
  submission_id: Uuid
  pages: {
    page_id: Uuid
    seq: number
    doc_type: DocType
    confidence: number
    image_key: string
    quality_warning: string | null
  }[]
  mismatches: { kind: string; doc_type: DocType; detail: string; page_ids: Uuid[] }[]
}

export type FieldValue = {
  value: string | number | boolean | null
  confidence: number
  source: PageRef | null
  handwritten: boolean
}

export type FixFieldsPayload = Common & {
  low_confidence_fields: string[]
  failed_checks: { check_id: string; passed: boolean; detail: string; fields: string[] }[]
  fields?: Record<string, FieldValue>
}

export type InsuredMember = {
  member_id: string
  name: string
  dob: string
  gender: string
  relationship: string
}

export type RegistrationPayload = Common & {
  claim_number: string
  member: {
    match: string
    member_id: string | null
    name: string | null
    dob: string | null
    relationship: string | null
    reason: string
  }
  policy: {
    policy_number: string
    product_uin: string
    sum_insured: Money
    cumulative_bonus: Money
    utilised: Money
    blocked: Money
    balance: Money
    policy_start: string
    policy_end: string
    insured_members: InsuredMember[]
  } | null
  hospital: {
    match: string
    rohini_id: string | null
    name: string | null
    city: string | null
    differential_billing: boolean
  }
  admission_date: string | null
  accident: boolean
  block_amount: Money | null
  rule_results: RuleResult[]
  duplicates: { claim_id: Uuid; claim_number: string | null; admission_date: string; discharge_date: string | null }[]
  block_ref: { block_id: string; amount: Money; available_after: Money } | null
  overrides: string[]
}

export type DeductionLine = {
  line_id: number
  head: string
  head_as_written: string | null
  description: string
  claimed: Money
  admissible: Money
  deducted: Money
  rule_id: string | null
  clause_ref: string | null
  reason: string | null
  judged_by: 'code' | 'model' | 'person'
  flagged: boolean
}

export type DeductionsPayload = Common & {
  lines: DeductionLine[]
  rule_results: RuleResult[]
  claimed_total: Money
  admissible_total: Money
  co_pay: Money
  payable: Money
  flagged_lines: number[]
}

export type Citation = { page: PageRef | null; ref: string | null }

export type MedicalPayload = Common & {
  verified_codes: {
    code: { system: string; version: string; code: string; display: string | null }
    phrase: string
    lookup_call_id: number
    citations: Citation[]
    rationale: string
  }[]
  findings: { kind: string; text: string; severity: 'info' | 'query' | 'adverse'; citations: Citation[]; judged_by: string }[]
  query_items: string[]
  rule_results: RuleResult[]
  dropped: { item: string; detail: string; reason: string }[]
  adverse: boolean
}

export type RiskPayload = Common & {
  hits: { rule_id: string; weight: number; detail: string }[]
  score: number
  refer: boolean
  explanation: string
}

export type Draft = {
  decision: 'approve' | 'partial' | 'query' | 'reject'
  expected_decision: string
  amount: Money
  payable_on_sheet: Money
  available_balance: Money
  reasons: { text: string; clause_ref: string | null; rule_id: string | null; line_id: number | null }[]
  query_items: string[]
  summary: string
  self_check_errors: string[]
  guardrail_errors: string[]
  guardrail_passed: boolean
}

export type Routing = {
  draft_decision: string
  draft_guardrail_passed: boolean
  amount: Money
  band: string
  authority_amount: Money
  crc_required: boolean
}

export type ApprovePayload = Common & { routing: Routing; draft: Draft }
export type CrcPayload = ApprovePayload & { final_decision: 'approve' | 'partial' | 'reject' }

export type ReleasePayload = Common & {
  final_decision: string
  paid_amount: Money
  payee: { kind: string; name: string; account_number: string; ifsc: string } | null
  payee_checks: { check_id: string; passed: boolean; detail: string }[]
  status: string
}

export type QueryNoReplyPayload = Common & { query_id: Uuid }
