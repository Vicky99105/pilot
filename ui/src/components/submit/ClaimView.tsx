import { useState } from 'react'
import { api, type ClaimDetail, type OpenQueryView, type Submission, type User } from '../../lib/api'
import { date, dateTime, docLabel, fileSize, humanise } from '../../lib/format'
import { isStaff } from '../../lib/users'
import { downloadFile } from '../../hooks/useFileUrl'
import { useAsync } from '../../hooks/useAsync'
import { EventHistory } from '../monitoring/EventHistory'
import { Button, Card, Chip, Detail, ErrorList, Loading, Notice } from '../ui/primitives'
import { FilePicker } from './FilePicker'
import { JOURNEY, journeyIndex, statusLabel, statusTone } from './status'
import { SubmissionResult } from './SubmissionResult'
import { REIMBURSEMENT_SLOTS, slotParts, type SlotFiles } from './uploads'

export function ClaimView({ claimId, user }: { claimId: string; user: User }) {
  const [state, reload] = useAsync(() => api.claim(claimId), [claimId, user.id])
  const staff = isStaff(user.roles)

  if (state.kind === 'loading') return <Loading />
  if (state.kind === 'error') return <ErrorList error={state.error} />
  const claim = state.data

  return (
    <div className="space-y-4">
      <Card
        title={
          <span className="flex flex-wrap items-center gap-2">
            {claim.claim_number ?? 'Claim'}
            <Chip tone={statusTone(claim.status)}>{statusLabel(claim.status)}</Chip>
          </span>
        }
        actions={
          <Button variant="ghost" onClick={reload}>
            Refresh
          </Button>
        }
      >
        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Detail label="Patient">{claim.patient_name ?? '-'}</Detail>
          <Detail label="Request">{humanise(claim.request_type)}</Detail>
          <Detail label="Sent">{date(claim.created_at)}</Detail>
          <Detail label="Decision due by">{date(claim.deadline)}</Detail>
          <Detail label="Policy">{claim.policy_number ?? '-'}</Detail>
          <Detail label="Member ID">{claim.member_id ?? '-'}</Detail>
          <Detail label="Hospital ROHINI ID">{claim.hospital_id ?? '-'}</Detail>
          <Detail label="Closed">{claim.closed_at ? date(claim.closed_at) : 'Not yet'}</Detail>
        </dl>
      </Card>

      {claim.open_query && !staff && <QueryReply query={claim.open_query} channel={user.kind} onReplied={reload} />}
      {claim.open_query && staff && (
        <Notice tone="warn">
          Query {claim.open_query.status}, reply due {date(claim.open_query.deadline)}:{' '}
          {claim.open_query.items.join('; ')}
        </Notice>
      )}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <Card title="Progress">
          <Journey status={claim.status} />
        </Card>
        <div className="space-y-4">
          <Letters claim={claim} />
          <Submissions claim={claim} />
        </div>
      </div>

      {staff && (
        <Card title="Event history">
          <EventHistory claimId={claim.claim_id} />
        </Card>
      )}
    </div>
  )
}

function Journey({ status }: { status: string }) {
  const at = journeyIndex(status)
  return (
    <ol className="space-y-2">
      {JOURNEY.map((step, i) => {
        const done = i < at || status === 'closed'
        const current = i === at && status !== 'closed'
        return (
          <li key={step.label} className="flex items-center gap-3 text-sm">
            <span
              aria-hidden
              className={`h-2.5 w-2.5 shrink-0 rounded-full ${
                done ? 'bg-ok' : current ? 'bg-accent ring-4 ring-accent-soft' : 'bg-line'
              }`}
            />
            <span className={current ? 'font-semibold' : done ? '' : 'text-muted'}>{step.label}</span>
            {current && <span className="sr-only">(current step)</span>}
          </li>
        )
      })}
    </ol>
  )
}

function Letters({ claim }: { claim: ClaimDetail }) {
  const [error, setError] = useState<unknown>(null)
  return (
    <Card title="Letters">
      {claim.letters.length === 0 ? (
        <p className="text-sm text-muted">No letters yet.</p>
      ) : (
        <ul className="divide-y divide-line">
          {claim.letters.map((l) => (
            <li key={l.letter_id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
              <span>
                {humanise(l.kind)} letter <span className="text-muted">{dateTime(l.created_at)}</span>
              </span>
              <Button
                variant="ghost"
                onClick={() =>
                  downloadFile(l.pdf_key, `${claim.claim_number ?? 'claim'}-${l.kind}.pdf`).catch(setError)
                }
              >
                Download PDF
              </Button>
            </li>
          ))}
        </ul>
      )}
      <ErrorList error={error} />
    </Card>
  )
}

function Submissions({ claim }: { claim: ClaimDetail }) {
  return (
    <Card title="Documents sent">
      <ul className="space-y-3">
        {claim.submissions.map((s) => (
          <li key={s.submission_id} className="text-sm">
            <p className="font-medium">
              {humanise(s.request_type)} <span className="font-normal text-muted">{dateTime(s.created_at)}</span>
            </p>
            <ul className="mt-1 space-y-0.5 text-xs">
              {s.files.map((f) => (
                <li key={f.file_id} className="flex flex-wrap gap-x-2">
                  <span>{f.slot ? docLabel(f.slot) : 'Merged PDF'}:</span>
                  <span className="truncate text-muted">{f.filename}</span>
                  <span className="num text-muted">{fileSize(f.size_bytes)}</span>
                  {f.quality_warning && <span className="text-warn">{f.quality_warning}</span>}
                </li>
              ))}
            </ul>
            {s.missing_documents.length > 0 && (
              <p className="mt-1 text-xs text-warn">Missing: {s.missing_documents.map(docLabel).join(', ')}</p>
            )}
            {s.warnings.map((w) => (
              <p key={w} className="mt-1 text-xs text-warn">
                {w}
              </p>
            ))}
          </li>
        ))}
      </ul>
    </Card>
  )
}

function QueryReply({
  query,
  channel,
  onReplied,
}: {
  query: OpenQueryView
  channel: User['kind']
  onReplied: () => void
}) {
  const [files, setFiles] = useState<SlotFiles>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [result, setResult] = useState<Submission | null>(null)
  const parts = slotParts(files)
  const limit = channel === 'hospital' ? 'hospital' : 'member'

  const send = async () => {
    setBusy(true)
    setError(null)
    try {
      setResult(await api.replyToQuery(query.query_id, parts))
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
        <Button onClick={onReplied}>Back to the claim</Button>
      </div>
    )
  }

  return (
    <Card title="We need more from you">
      <div className="space-y-3">
        <Notice tone={query.status === 'overdue' ? 'danger' : 'warn'}>
          <p className="font-medium">
            Please send these by {date(query.deadline)}
            {query.status === 'overdue' && ' (the reply date has passed, but you can still send them)'}:
          </p>
          <ul className="mt-1 list-disc pl-5">
            {query.items.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </Notice>
        <div className="grid gap-2 sm:grid-cols-2">
          {REIMBURSEMENT_SLOTS.map(({ slot, note }) => (
            <FilePicker
              key={slot}
              label={docLabel(slot)}
              hint={note}
              channel={limit}
              files={files[slot] ?? []}
              onChange={(list) => setFiles((f) => ({ ...f, [slot]: list }))}
            />
          ))}
        </div>
        <ErrorList error={error} />
        <div className="flex justify-end">
          <Button variant="primary" disabled={busy || parts.length === 0} onClick={send}>
            {busy ? 'Sending' : 'Send reply'}
          </Button>
        </div>
      </div>
    </Card>
  )
}
