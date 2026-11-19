import { useId, useState, type ReactNode } from 'react'
import type { PageRef, Stage } from '../../../lib/api'
import { rupees } from '../../../lib/format'
import { STAGE_LABELS } from '../../submit/status'
import { Button, Chip, inputClass } from '../../ui/primitives'
import type { RuleResult } from './payloads'
import { sendBackTargets } from './stages'

export type Outcome = Record<string, unknown>

export type KindProps<P> = {
  payload: P
  cite: (ref: PageRef) => void
  focusPage: (pageId: string) => void
  submit: (outcome: Outcome) => void
  busy: boolean
}

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-2">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">{title}</h3>
      {children}
    </section>
  )
}

export function CiteButton({ source, onCite }: { source: PageRef | null | undefined; onCite: (ref: PageRef) => void }) {
  if (!source) return <span className="text-xs text-muted">no page</span>
  return (
    <button
      type="button"
      className="whitespace-nowrap text-xs text-accent underline-offset-2 hover:underline"
      onClick={() => onCite(source)}
    >
      page {source.page}
    </button>
  )
}

export function Confidence({ value }: { value: number }) {
  return <Chip tone={value < 0.85 ? 'warn' : 'ok'}>{Math.round(value * 100)}%</Chip>
}

export function TextArea({
  label,
  value,
  onChange,
  required,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  required?: boolean
}) {
  const id = useId()
  return (
    <div>
      <label htmlFor={id} className="mb-1 block text-sm font-medium">
        {label}
        {required && <span className="ml-1 text-xs font-normal text-muted">(required)</span>}
      </label>
      <textarea id={id} rows={2} className={inputClass} value={value} onChange={(e) => onChange(e.target.value)} />
    </div>
  )
}

export function RuleTable({ rules }: { rules: RuleResult[] }) {
  if (rules.length === 0) return <p className="text-sm text-muted">No rules ran.</p>
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <tbody>
          {rules.map((r, i) => (
            <tr key={`${r.rule_id}-${i}`} className="border-b border-line align-top last:border-0">
              <td className="py-1.5 pr-2">
                <Chip tone={r.passed ? 'ok' : 'danger'}>{r.rule_id}</Chip>
              </td>
              <td className="py-1.5 pr-2">
                {r.reason}
                {r.clause_ref && <span className="ml-1 text-xs text-muted">clause {r.clause_ref}</span>}
                {r.judged_by !== 'code' && <span className="ml-1 text-xs text-muted">by {r.judged_by}</span>}
              </td>
              <td className="num py-1.5 text-right whitespace-nowrap">
                {Number(r.amount_effect) !== 0 && rupees(r.amount_effect)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** Send back to this stage or an earlier one, with a note. */
export function SendBack({
  stage,
  busy,
  submit,
  noteRequired = false,
}: {
  stage: Stage
  busy: boolean
  submit: (outcome: Outcome) => void
  noteRequired?: boolean
}) {
  const targets = sendBackTargets(stage)
  const [open, setOpen] = useState(false)
  const [target, setTarget] = useState<Stage>(targets[targets.length - 1] ?? stage)
  const [note, setNote] = useState('')
  const id = useId()
  if (targets.length === 0) return null
  if (!open) {
    return (
      <Button onClick={() => setOpen(true)} disabled={busy}>
        Send back
      </Button>
    )
  }
  return (
    <div className="w-full space-y-2 rounded-md border border-line p-3">
      <div>
        <label htmlFor={id} className="mb-1 block text-sm font-medium">
          Re-run from
        </label>
        <select id={id} className={inputClass} value={target} onChange={(e) => setTarget(e.target.value as Stage)}>
          {targets.map((s) => (
            <option key={s} value={s}>
              {STAGE_LABELS[s]}
            </option>
          ))}
        </select>
      </div>
      <TextArea label="What needs another look" value={note} onChange={setNote} required={noteRequired} />
      <div className="flex gap-2">
        <Button
          variant="danger"
          disabled={busy || (noteRequired && !note.trim())}
          onClick={() => submit({ action: 'send_back', stage: target, note: note.trim() })}
        >
          Send back to {STAGE_LABELS[target]}
        </Button>
        <Button variant="ghost" onClick={() => setOpen(false)}>
          Cancel
        </Button>
      </div>
    </div>
  )
}

export function Actions({ children }: { children: ReactNode }) {
  return <div className="flex flex-wrap items-start gap-2 border-t border-line pt-3">{children}</div>
}
