import { useState, type FormEvent } from 'react'
import { api, type Submission, type SubmissionForm, type User } from '../../lib/api'
import { Button, Card, ErrorList, FieldError, inputClass, Label } from '../ui/primitives'
import { ChecklistUploads } from './ChecklistUploads'
import { EMPTY_UPLOADS, uploadParts, type UploadValue } from './uploads'
import { SubmissionResult } from './SubmissionResult'

type Values = {
  patient_name: string
  policy_number: string
  member_id: string
  admission_date: string
  claimed_amount: string
}

type Errors = Partial<Record<keyof Values | 'documents', string>>

const EMPTY: Values = { patient_name: '', policy_number: '', member_id: '', admission_date: '', claimed_amount: '' }

function validate(v: Values, hasDocuments: boolean): Errors {
  const e: Errors = {}
  if (!v.patient_name.trim()) e.patient_name = 'Enter the patient name'
  if (!v.policy_number.trim()) e.policy_number = 'Enter the policy number'
  if (!v.member_id.trim()) e.member_id = 'Enter the member ID from the health card'
  if (!v.admission_date) e.admission_date = 'Enter the admission date'
  const amount = Number(v.claimed_amount)
  if (!v.claimed_amount || !Number.isFinite(amount) || amount <= 0) {
    e.claimed_amount = 'Enter the estimate total, in rupees'
  }
  if (!hasDocuments) e.documents = 'Attach at least one document'
  return e
}

/** A network hospital's cashless pre-authorisation request. It opens the admission's claim. */
export function PreauthForm({ user }: { user: User }) {
  const [values, setValues] = useState<Values>(EMPTY)
  const [errors, setErrors] = useState<Errors>({})
  const [uploads, setUploads] = useState<UploadValue>(EMPTY_UPLOADS)
  const [busy, setBusy] = useState(false)
  const [serverError, setServerError] = useState<unknown>(null)
  const [result, setResult] = useState<Submission | null>(null)
  const parts = uploadParts(uploads)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const found = validate(values, parts.length > 0)
    setErrors(found)
    if (Object.keys(found).length) {
      document.getElementById(`f-${Object.keys(found)[0]}`)?.focus()
      return
    }
    const form: SubmissionForm = {
      request_type: 'preauth',
      patient_name: values.patient_name.trim(),
      policy_number: values.policy_number.trim(),
      member_id: values.member_id.trim(),
      hospital_id: user.hospital_id,
      admission_date: values.admission_date,
      discharge_date: null,
      claimed_amount: values.claimed_amount,
    }
    setBusy(true)
    setServerError(null)
    try {
      setResult(await api.submit(form, parts))
    } catch (e) {
      setServerError(e)
    } finally {
      setBusy(false)
    }
  }

  if (result) {
    const reset = () => {
      setResult(null)
      setValues(EMPTY)
      setUploads(EMPTY_UPLOADS)
      setErrors({})
    }
    return <SubmissionResult submission={result} onAnother={reset} />
  }

  const text = (
    key: keyof Values,
    label: string,
    props: { type?: string; hint?: string; placeholder?: string; inputMode?: 'decimal' } = {},
  ) => (
    <div>
      <Label htmlFor={`f-${key}`} hint={props.hint}>
        {label}
      </Label>
      <input
        id={`f-${key}`}
        className={inputClass}
        type={props.type ?? 'text'}
        inputMode={props.inputMode}
        placeholder={props.placeholder}
        autoComplete="off"
        value={values[key]}
        aria-invalid={!!errors[key]}
        aria-describedby={errors[key] ? `e-${key}` : undefined}
        onChange={(e) => setValues((v) => ({ ...v, [key]: e.target.value }))}
      />
      <FieldError id={`e-${key}`} message={errors[key]} />
    </div>
  )

  return (
    <form onSubmit={submit} noValidate className="space-y-4">
      <Card title="Patient and admission">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {text('patient_name', 'Patient name')}
          {text('policy_number', 'Policy number')}
          {text('member_id', 'Member ID', { hint: 'on the health card' })}
          {text('admission_date', 'Admitted on', { type: 'date' })}
          {text('claimed_amount', 'Estimate total (Rs)', {
            inputMode: 'decimal',
            placeholder: '85000',
            hint: 'as on Part C',
          })}
          <div>
            <Label htmlFor="f-hospital_id" hint="from your login">
              ROHINI ID
            </Label>
            <input
              id="f-hospital_id"
              className={`${inputClass} bg-surface-2`}
              readOnly
              value={user.hospital_id ?? 'Not set for this login'}
            />
          </div>
        </div>
      </Card>

      <Card title="Pre-auth documents">
        <ChecklistUploads requestType="preauth" channel="hospital" value={uploads} onChange={setUploads} />
        <FieldError id="e-documents" message={errors.documents} />
      </Card>

      <ErrorList error={serverError} />
      <div className="flex justify-end">
        <Button type="submit" variant="primary" disabled={busy}>
          {busy ? 'Sending' : 'Send pre-auth'}
        </Button>
      </div>
    </form>
  )
}
