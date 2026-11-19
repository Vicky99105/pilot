import { useState } from 'react'
import { api, type ClaimDetail, type LaterRequestType, type Submission, type User } from '../../lib/api'
import { date, dateTime, duration, humanise, rupees } from '../../lib/format'
import { useNow } from '../../hooks/useNow'
import { Button, Card, Chip, Detail, ErrorList, Notice } from '../ui/primitives'
import { ChecklistUploads } from './ChecklistUploads'
import { EMPTY_UPLOADS, uploadParts, type UploadValue } from './uploads'
import { SubmissionResult } from './SubmissionResult'

const LATER_REQUESTS: { type: LaterRequestType; label: string }[] = [
  { type: 'enhancement', label: 'Send an enhancement' },
  { type: 'discharge', label: 'Send the discharge' },
  { type: 'cancellation', label: 'Cancel the pre-auth' },
]

const AUTH_STATUS: Record<string, { label: string; tone: 'ok' | 'warn' | 'danger' | 'neutral' }> = {
  awaiting_discharge: { label: 'Approved, awaiting discharge', tone: 'ok' },
  final: { label: 'Final', tone: 'ok' },
  rejected: { label: 'Rejected', tone: 'danger' },
  cancelled: { label: 'Cancelled', tone: 'neutral' },
}

/** The cashless side of an admission: the clock, authorisations, originals and the
 * hospital's later requests. */
export function CashlessPanel({
  claim,
  user,
  onChanged,
}: {
  claim: ClaimDetail
  user: User
  onChanged: () => void
}) {
  const preauth = claim.request_type === 'preauth'
  const hospital = user.kind === 'hospital'
  const operator = user.roles.includes('claims_operator')

  return (
    <div className="space-y-4">
      {preauth && (
        <Card title="Cashless authorisation">
          <div className="space-y-4">
            <Clock claim={claim} />
            <Authorisations claim={claim} />
            <Originals claim={claim} operator={operator} onChanged={onChanged} />
          </div>
        </Card>
      )}
      {hospital && <Requests claim={claim} onChanged={onChanged} />}
    </div>
  )
}

/** The current request is answered once an authorisation of its type follows its upload. */
function answered(claim: ClaimDetail): boolean {
  const sent = claim.submissions
    .filter((s) => s.request_type === claim.current_request)
    .map((s) => Date.parse(s.created_at))
  const latest = sent.length ? Math.max(...sent) : 0
  return claim.authorisations.some(
    (a) => a.request_type === claim.current_request && Date.parse(a.authorised_at) >= latest,
  )
}

function Clock({ claim }: { claim: ClaimDetail }) {
  const now = useNow(30_000)
  const closed = claim.status === 'closed'
  const live = !!claim.clock_deadline && !closed && !answered(claim)
  const left = claim.clock_deadline ? (Date.parse(claim.clock_deadline) - now) / 1000 : 0
  const tone = left < 0 ? 'danger' : left < 15 * 60 ? 'warn' : 'ok'

  return (
    <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3">
      <Detail label="Current request">{claim.current_request ? humanise(claim.current_request) : '-'}</Detail>
      <Detail label="Respond by">{dateTime(claim.clock_deadline)}</Detail>
      <div className="min-w-0">
        <dt className="text-xs text-muted">Time left</dt>
        <dd className="text-sm">
          {live ? (
            <Chip tone={tone}>
              <span className="num">{left < 0 ? duration(left) : `${duration(left)} left`}</span>
            </Chip>
          ) : (
            <span className="text-muted">{closed ? 'Closed' : claim.clock_deadline ? 'Answered' : '-'}</span>
          )}
        </dd>
      </div>
    </dl>
  )
}

function Authorisations({ claim }: { claim: ClaimDetail }) {
  if (claim.authorisations.length === 0) {
    return <p className="text-sm text-muted">No authorisation yet.</p>
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="text-xs text-muted">
          <tr>
            <th scope="col" className="py-1 pr-3 font-medium">Request</th>
            <th scope="col" className="py-1 pr-3 text-right font-medium">Approved now</th>
            <th scope="col" className="py-1 pr-3 text-right font-medium">Total approved</th>
            <th scope="col" className="py-1 pr-3 font-medium">Status</th>
            <th scope="col" className="py-1 font-medium">When</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {claim.authorisations.map((a) => {
            const status = AUTH_STATUS[a.status] ?? { label: humanise(a.status), tone: 'neutral' as const }
            return (
              <tr key={`${a.request_type}-${a.authorised_at}`}>
                <td className="py-1.5 pr-3">{humanise(a.request_type)}</td>
                <td className="num py-1.5 pr-3 text-right">{rupees(a.approved_now)}</td>
                <td className="num py-1.5 pr-3 text-right font-medium">{rupees(a.approved_total)}</td>
                <td className="py-1.5 pr-3">
                  <Chip tone={status.tone}>{status.label}</Chip>
                </td>
                <td className="py-1.5 whitespace-nowrap text-muted">{dateTime(a.authorised_at)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function Originals({
  claim,
  operator,
  onChanged,
}: {
  claim: ClaimDetail
  operator: boolean
  onChanged: () => void
}) {
  const now = useNow(60_000)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  if (!claim.originals_due_at) return null

  if (claim.originals_received_at) {
    return <Notice tone="ok">Original documents received on {date(claim.originals_received_at)}.</Notice>
  }

  const overdue = Date.parse(claim.originals_due_at) < now
  const mark = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.markOriginalsReceived(claim.claim_id)
      onChanged()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-2">
      <Notice tone={overdue ? 'danger' : 'warn'}>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span>
            <span className="font-medium">Originals pending.</span> Courier the original documents by{' '}
            {date(claim.originals_due_at)}
            {overdue && ' (the courier deadline has passed)'}.
          </span>
          {operator && (
            <Button disabled={busy} onClick={mark}>
              {busy ? 'Saving' : 'Mark originals received'}
            </Button>
          )}
        </div>
      </Notice>
      <ErrorList error={error} />
    </div>
  )
}

function Requests({ claim, onChanged }: { claim: ClaimDetail; onChanged: () => void }) {
  const [open, setOpen] = useState<LaterRequestType | null>(null)
  const closed = claim.status === 'closed' || !!claim.closed_at
  const preauth = claim.request_type === 'preauth'
  const reason = !preauth
    ? 'Enhancement, discharge and cancellation apply only to an admission a pre-auth opened.'
    : closed
      ? 'This admission is closed, so it takes no more requests.'
      : null

  return (
    <Card title="Send a request on this admission">
      <div className="space-y-3">
        <div className="flex flex-wrap gap-2">
          {LATER_REQUESTS.map((r) => (
            <Button
              key={r.type}
              variant={open === r.type ? 'primary' : 'secondary'}
              disabled={!!reason}
              aria-pressed={open === r.type}
              onClick={() => setOpen(open === r.type ? null : r.type)}
            >
              {r.label}
            </Button>
          ))}
        </div>
        {reason && <p className="text-sm text-muted">{reason}</p>}
        {open && !reason && (
          <RequestForm
            key={open}
            claimId={claim.claim_id}
            requestType={open}
            onCancel={() => setOpen(null)}
            onDone={() => {
              setOpen(null)
              onChanged()
            }}
          />
        )}
      </div>
    </Card>
  )
}

function RequestForm({
  claimId,
  requestType,
  onCancel,
  onDone,
}: {
  claimId: string
  requestType: LaterRequestType
  onCancel: () => void
  onDone: () => void
}) {
  const [uploads, setUploads] = useState<UploadValue>(EMPTY_UPLOADS)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [result, setResult] = useState<Submission | null>(null)
  const cancellation = requestType === 'cancellation'
  const parts = cancellation ? [] : uploadParts(uploads)

  const send = async () => {
    setBusy(true)
    setError(null)
    try {
      setResult(await api.sendRequest(claimId, requestType, parts))
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  if (result) {
    return (
      <div className="space-y-2">
        <SubmissionResult submission={result} />
        <Button onClick={onDone}>Back to the admission</Button>
      </div>
    )
  }

  return (
    <div className="space-y-3 rounded-md border border-line p-3">
      {cancellation ? (
        <Notice tone="warn">
          Cancelling releases the amount blocked on the policy and closes this admission. Send it when the patient will
          not be treated under this pre-auth.
        </Notice>
      ) : (
        <ChecklistUploads requestType={requestType} channel="hospital" value={uploads} onChange={setUploads} />
      )}
      <ErrorList error={error} />
      <div className="flex justify-end gap-2">
        <Button onClick={onCancel} disabled={busy}>
          Back
        </Button>
        <Button
          variant={cancellation ? 'danger' : 'primary'}
          disabled={busy || (!cancellation && parts.length === 0)}
          onClick={send}
        >
          {busy ? 'Sending' : cancellation ? 'Confirm cancellation' : `Send ${humanise(requestType).toLowerCase()}`}
        </Button>
      </div>
    </div>
  )
}
