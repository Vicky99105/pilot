import { useState } from 'react'
import { api, type ClaimDetail, type DocType, type Reconsideration, type ReconsiderationView, type User } from '../../lib/api'
import { dateTime, docLabel, humanise, rupees } from '../../lib/format'
import { Button, Card, ErrorList, FieldError, inputClass, Label, Notice } from '../ui/primitives'
import { FilePicker } from './FilePicker'
import { slotParts, type SlotFiles } from './uploads'

const MIN_GROUNDS = 10

/** New evidence a claimant may send with a reconsideration. "other" takes anything else. */
const SLOTS: { slot: DocType; note: string }[] = [
  { slot: 'other', note: 'Any document that supports your request' },
  { slot: 'discharge_summary', note: 'A corrected or fuller summary' },
  { slot: 'investigation_report', note: 'Reports not sent before' },
  { slot: 'final_bill', note: 'A corrected bill' },
]

function decisionText(r: ReconsiderationView): string {
  return r.previous_decision ? humanise(r.previous_decision) : 'No decision'
}

export function Reconsiderations({ items }: { items: ReconsiderationView[] }) {
  return (
    <Card title="Reconsiderations">
      <ul className="divide-y divide-line">
        {items.map((r) => (
          <li key={r.number} className="py-2 text-sm">
            <p className="font-medium">
              Reconsideration {r.number} <span className="font-normal text-muted">{dateTime(r.requested_at)}</span>
            </p>
            <p className="mt-0.5 whitespace-pre-line">{r.grounds}</p>
            <p className="mt-1 text-xs text-muted">
              Before: {decisionText(r)}, decided <span className="num">{rupees(r.previous_amount)}</span>, paid{' '}
              <span className="num">{rupees(r.previously_paid)}</span>
            </p>
          </li>
        ))}
      </ul>
    </Card>
  )
}

/** A closed claim: the claimant asks for the decision to be made again, with grounds and new evidence. */
export function ReconsiderationRequest({
  claim,
  channel,
  onSent,
}: {
  claim: ClaimDetail
  channel: User['kind']
  onSent: () => void
}) {
  const [open, setOpen] = useState(false)
  const [grounds, setGrounds] = useState('')
  const [touched, setTouched] = useState(false)
  const [files, setFiles] = useState<SlotFiles>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [result, setResult] = useState<Reconsideration | null>(null)
  const limit = channel === 'hospital' ? 'hospital' : 'member'
  const short = grounds.trim().length < MIN_GROUNDS
  const groundsError = touched && short ? `Explain your grounds in at least ${MIN_GROUNDS} characters` : null

  const send = async () => {
    setTouched(true)
    if (short) return
    setBusy(true)
    setError(null)
    try {
      setResult(await api.reconsider(claim.claim_id, grounds.trim(), slotParts(files)))
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  if (result) {
    return (
      <div className="space-y-2">
        <Notice tone="ok">
          Reconsideration {result.number} received. We will decide the claim again and send you a new letter.
        </Notice>
        <Button onClick={onSent}>Back to the claim</Button>
      </div>
    )
  }

  if (!open) {
    return (
      <Card title="Not happy with the decision?">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm text-muted">You can ask us to decide this claim again, with any new documents.</p>
          <Button variant="primary" onClick={() => setOpen(true)}>
            Ask for reconsideration
          </Button>
        </div>
      </Card>
    )
  }

  return (
    <Card title="Ask for reconsideration">
      <div className="space-y-3">
        <div>
          <Label htmlFor="recon-grounds" hint="(why the decision should change)">
            Grounds
          </Label>
          <textarea
            id="recon-grounds"
            rows={4}
            className={inputClass}
            value={grounds}
            aria-invalid={!!groundsError}
            aria-describedby={groundsError ? 'recon-grounds-error' : undefined}
            onChange={(e) => setGrounds(e.target.value)}
            onBlur={() => setTouched(true)}
          />
          <FieldError id="recon-grounds-error" message={groundsError} />
        </div>
        <div className="grid gap-2 sm:grid-cols-2">
          {SLOTS.map(({ slot, note }) => (
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
        <div className="flex justify-end gap-2">
          <Button onClick={() => setOpen(false)} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" disabled={busy} onClick={send}>
            {busy ? 'Sending' : 'Send request'}
          </Button>
        </div>
      </div>
    </Card>
  )
}
