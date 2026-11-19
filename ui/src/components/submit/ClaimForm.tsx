import { useState, type FormEvent, type ReactNode } from 'react'
import { api, type RequestType, type Submission, type SubmissionForm, type User } from '../../lib/api'
import { docLabel } from '../../lib/format'
import { Button, Card, ErrorList, FieldError, inputClass, Label } from '../ui/primitives'
import { FilePicker } from './FilePicker'
import { REIMBURSEMENT_SLOTS, slotParts, type Channel, type SlotFiles } from './uploads'
import { SubmissionResult } from './SubmissionResult'

type Values = {
  request_type: RequestType
  policy_number: string
  member_id: string
  patient_name: string
  claim_category: '' | 'inpatient' | 'day_care' | 'pre_post_hospitalisation'
  cause: '' | 'illness' | 'injury' | 'maternity'
  hospital_name: string
  hospital_city: string
  hospital_id: string
  admission_date: string
  discharge_date: string
  claimed_amount: string
  bank_account_number: string
  bank_account_confirm: string
  ifsc: string
  declaration_accepted: boolean
}

type Errors = Partial<Record<keyof Values | 'documents', string>>

const IFSC = /^[A-Z]{4}0[A-Z0-9]{6}$/
const ACCOUNT = /^[0-9]{6,18}$/
const HOSPITAL_REQUESTS: { value: RequestType; label: string }[] = [
  { value: 'preauth', label: 'Pre-authorisation' },
  { value: 'enhancement', label: 'Enhancement' },
  { value: 'discharge', label: 'Discharge' },
  { value: 'cancellation', label: 'Cancellation' },
]

function initial(user: User): Values {
  const hospital = user.kind === 'hospital'
  return {
    request_type: hospital ? 'preauth' : 'reimbursement',
    policy_number: user.policy_number ?? '',
    member_id: user.member_id ?? '',
    patient_name: '',
    claim_category: '',
    cause: '',
    hospital_name: '',
    hospital_city: '',
    hospital_id: user.hospital_id ?? '',
    admission_date: '',
    discharge_date: '',
    claimed_amount: '',
    bank_account_number: '',
    bank_account_confirm: '',
    ifsc: '',
    declaration_accepted: false,
  }
}

function validate(v: Values, member: boolean, hasDocuments: boolean): Errors {
  const e: Errors = {}
  if (!v.policy_number.trim()) e.policy_number = 'Enter the policy number'
  if (!v.member_id.trim()) e.member_id = 'Enter the member ID from the health card'
  if (!v.patient_name.trim()) e.patient_name = 'Enter the patient name'
  if (!v.admission_date) e.admission_date = 'Enter the admission date'
  if (v.discharge_date && v.admission_date && v.discharge_date < v.admission_date) {
    e.discharge_date = 'Discharge cannot be before admission'
  }
  const amount = Number(v.claimed_amount)
  if (!v.claimed_amount || !Number.isFinite(amount) || amount <= 0) {
    e.claimed_amount = 'Enter the amount you are claiming, in rupees'
  }
  if (member) {
    if (!v.claim_category) e.claim_category = 'Choose what the claim is for'
    if (!v.cause) e.cause = 'Choose the cause'
    if (!v.hospital_name.trim()) e.hospital_name = 'Enter the hospital name'
    if (!v.hospital_city.trim()) e.hospital_city = 'Enter the city'
    if (!ACCOUNT.test(v.bank_account_number)) e.bank_account_number = 'Use 6 to 18 digits'
    if (v.bank_account_confirm !== v.bank_account_number) {
      e.bank_account_confirm = 'The two account numbers do not match'
    }
    if (!IFSC.test(v.ifsc)) e.ifsc = 'An IFSC has 11 characters, like HDFC0001234'
    if (!v.declaration_accepted) e.declaration_accepted = 'You need to accept the declaration'
  }
  if (!hasDocuments) e.documents = 'Attach at least one document'
  return e
}

function toForm(v: Values, member: boolean): SubmissionForm {
  const opt = (s: string) => (s.trim() ? s.trim() : null)
  return {
    request_type: v.request_type,
    policy_number: v.policy_number.trim(),
    member_id: v.member_id.trim(),
    patient_name: v.patient_name.trim(),
    hospital_id: opt(v.hospital_id),
    hospital_name: opt(v.hospital_name),
    hospital_city: opt(v.hospital_city),
    admission_date: v.admission_date,
    discharge_date: v.discharge_date || null,
    claimed_amount: v.claimed_amount,
    ...(member
      ? {
          claim_category: v.claim_category || null,
          cause: v.cause || null,
          bank_account_number: v.bank_account_number,
          ifsc: v.ifsc,
          declaration_accepted: v.declaration_accepted,
        }
      : {}),
  }
}

export function ClaimForm({ user }: { user: User }) {
  const member = user.kind === 'member'
  const channel: Channel = member ? 'member' : 'hospital'
  const [values, setValues] = useState(() => initial(user))
  const [errors, setErrors] = useState<Errors>({})
  const [mode, setMode] = useState<'slots' | 'merged'>('slots')
  const [slotFiles, setSlotFiles] = useState<SlotFiles>({})
  const [merged, setMerged] = useState<File[]>([])
  const [busy, setBusy] = useState(false)
  const [serverError, setServerError] = useState<unknown>(null)
  const [result, setResult] = useState<Submission | null>(null)

  const set = <K extends keyof Values>(key: K, value: Values[K]) =>
    setValues((v) => ({ ...v, [key]: value }))

  const parts =
    mode === 'merged' ? merged.map((file) => ({ slot: 'merged' as const, file })) : slotParts(slotFiles)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const found = validate(values, member, parts.length > 0)
    setErrors(found)
    if (Object.keys(found).length) {
      document.getElementById(`f-${Object.keys(found)[0]}`)?.focus()
      return
    }
    setBusy(true)
    setServerError(null)
    try {
      setResult(await api.submit(toForm(values, member), parts))
    } catch (e) {
      setServerError(e)
    } finally {
      setBusy(false)
    }
  }

  if (result) {
    const reset = () => {
      setResult(null)
      setValues(initial(user))
      setSlotFiles({})
      setMerged([])
      setErrors({})
    }
    return <SubmissionResult submission={result} onAnother={reset} />
  }

  const text = (key: keyof Values, label: string, props: { type?: string; hint?: string; placeholder?: string; inputMode?: 'numeric' | 'decimal'; autoComplete?: string; readOnly?: boolean } = {}) => (
    <div>
      <Label htmlFor={`f-${key}`} hint={props.hint}>
        {label}
      </Label>
      <input
        id={`f-${key}`}
        className={`${inputClass} ${props.readOnly ? 'bg-surface-2' : ''}`}
        type={props.type ?? 'text'}
        inputMode={props.inputMode}
        placeholder={props.placeholder}
        autoComplete={props.autoComplete ?? 'off'}
        readOnly={props.readOnly}
        value={values[key] as string}
        aria-invalid={!!errors[key]}
        aria-describedby={errors[key] ? `e-${key}` : undefined}
        onChange={(e) => set(key, (key === 'ifsc' ? e.target.value.toUpperCase() : e.target.value) as never)}
      />
      <FieldError id={`e-${key}`} message={errors[key]} />
    </div>
  )

  const select = (key: 'claim_category' | 'cause' | 'request_type', label: string, options: { value: string; label: string }[]) => (
    <div>
      <Label htmlFor={`f-${key}`}>{label}</Label>
      <select
        id={`f-${key}`}
        className={inputClass}
        value={values[key]}
        aria-invalid={!!errors[key]}
        onChange={(e) => set(key, e.target.value as never)}
      >
        {key !== 'request_type' && <option value="">Choose</option>}
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      <FieldError id={`e-${key}`} message={errors[key]} />
    </div>
  )

  return (
    <form onSubmit={submit} noValidate className="space-y-4">
      <Section title="Patient and policy">
        {!member && select('request_type', 'Request', HOSPITAL_REQUESTS)}
        {text('policy_number', 'Policy number', { readOnly: member && !!user.policy_number })}
        {text('member_id', 'Member ID', { hint: 'on the health card' })}
        {text('patient_name', 'Patient name', { autoComplete: 'name' })}
        {member &&
          select('claim_category', 'What is the claim for', [
            { value: 'inpatient', label: 'Hospital stay' },
            { value: 'day_care', label: 'Day care treatment' },
            { value: 'pre_post_hospitalisation', label: 'Before or after a hospital stay' },
          ])}
        {member &&
          select('cause', 'Cause', [
            { value: 'illness', label: 'Illness' },
            { value: 'injury', label: 'Injury or accident' },
            { value: 'maternity', label: 'Maternity' },
          ])}
      </Section>

      <Section title="Hospital and stay">
        {text('hospital_name', 'Hospital name')}
        {text('hospital_city', 'City')}
        {text('hospital_id', 'ROHINI ID', { hint: 'if you know it', readOnly: !member })}
        {text('admission_date', 'Admitted on', { type: 'date' })}
        {text('discharge_date', 'Discharged on', { type: 'date' })}
        {text('claimed_amount', 'Amount claimed (Rs)', { inputMode: 'decimal', placeholder: '48250' })}
      </Section>

      {member && (
        <Section title="Bank account for payment">
          {text('bank_account_number', 'Account number', { inputMode: 'numeric' })}
          {text('bank_account_confirm', 'Account number again', { inputMode: 'numeric' })}
          {text('ifsc', 'IFSC', { placeholder: 'HDFC0001234' })}
        </Section>
      )}

      <Card title="Documents">
        <div role="radiogroup" aria-label="How to upload" className="mb-3 flex flex-wrap gap-2">
          {(['slots', 'merged'] as const).map((m) => (
            <label key={m} className="flex items-center gap-2 text-sm">
              <input type="radio" name="mode" checked={mode === m} onChange={() => setMode(m)} />
              {m === 'slots' ? 'One upload per document' : 'All documents in one PDF'}
            </label>
          ))}
        </div>
        <p className="mb-3 text-xs text-muted">
          PDF, JPG, PNG, TIFF or HEIC, up to {channel === 'member' ? 10 : 15} MB per file. Missing documents do not stop
          the claim; we will ask for them.
        </p>
        {mode === 'slots' ? (
          <div className="grid gap-2 sm:grid-cols-2">
            {REIMBURSEMENT_SLOTS.map(({ slot, required, note }) => (
              <FilePicker
                key={slot}
                label={docLabel(slot)}
                hint={note}
                required={required}
                channel={channel}
                files={slotFiles[slot] ?? []}
                onChange={(files) => setSlotFiles((s) => ({ ...s, [slot]: files }))}
              />
            ))}
          </div>
        ) : (
          <FilePicker
            label="Merged PDF"
            hint="Every document in one PDF, in any order"
            channel={channel}
            pdfOnly
            multiple={false}
            files={merged}
            onChange={setMerged}
          />
        )}
        <FieldError id="e-documents" message={errors.documents} />
      </Card>

      {member && (
        <Card>
          <label className="flex items-start gap-2 text-sm">
            <input
              id="f-declaration_accepted"
              type="checkbox"
              className="mt-0.5"
              checked={values.declaration_accepted}
              onChange={(e) => set('declaration_accepted', e.target.checked)}
            />
            <span>
              I declare that the details above are true and complete, that I have not claimed these expenses elsewhere,
              and I agree to the insurer seeking any medical information needed to settle the claim.
            </span>
          </label>
          <FieldError id="e-declaration_accepted" message={errors.declaration_accepted} />
        </Card>
      )}

      <ErrorList error={serverError} />
      <div className="flex justify-end">
        <Button type="submit" variant="primary" disabled={busy}>
          {busy ? 'Sending' : 'Submit claim'}
        </Button>
      </div>
    </form>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Card title={title}>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{children}</div>
    </Card>
  )
}
