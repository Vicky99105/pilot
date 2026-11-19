import { useState } from 'react'
import type { PageRef } from '../../../lib/api'
import { humanise } from '../../../lib/format'
import { Button, Chip, Notice } from '../../ui/primitives'
import { Actions, CiteButton, RuleTable, Section, SendBack, TextArea, type KindProps } from './common'
import type { Citation, MedicalPayload, RiskPayload } from './payloads'

function Citations({ citations, cite }: { citations: Citation[]; cite: (ref: PageRef) => void }) {
  if (citations.length === 0) return <span className="text-xs text-muted">no citation</span>
  return (
    <span className="inline-flex flex-wrap gap-2">
      {citations.map((c, i) =>
        c.page ? (
          <CiteButton key={i} source={c.page} onCite={cite} />
        ) : (
          <span key={i} className="font-mono text-xs text-muted">
            {c.ref}
          </span>
        ),
      )}
    </span>
  )
}

const SEVERITY_TONE = { info: 'neutral', query: 'warn', adverse: 'danger' } as const

/** medical_findings: outcome {action: "confirm" | "overrule", reason} or a send-back. */
export function MedicalFindings({ payload, cite, submit, busy }: KindProps<MedicalPayload>) {
  const [overruling, setOverruling] = useState(false)
  const [reason, setReason] = useState('')

  return (
    <div className="space-y-4">
      <Section title="Findings">
        <ul className="space-y-2">
          {payload.findings.map((f, i) => (
            <li key={i} className="rounded-md border border-line p-2 text-sm">
              <div className="mb-1 flex flex-wrap items-center gap-2">
                <Chip tone={SEVERITY_TONE[f.severity]}>{humanise(f.severity)}</Chip>
                <span className="text-xs text-muted">
                  {humanise(f.kind)}, by {f.judged_by}
                </span>
              </div>
              <p>{f.text}</p>
              <div className="mt-1">
                <Citations citations={f.citations} cite={cite} />
              </div>
            </li>
          ))}
        </ul>
      </Section>
      <Section title="Verified codes">
        <ul className="space-y-2 text-sm">
          {payload.verified_codes.map((v) => (
            <li key={v.code.code}>
              <span className="font-mono">{v.code.code}</span> {v.code.display}
              <p className="text-xs text-muted">
                Written as "{v.phrase}". {v.rationale}
              </p>
              <Citations citations={v.citations} cite={cite} />
            </li>
          ))}
        </ul>
      </Section>
      {payload.query_items.length > 0 && (
        <Section title="To ask the claimant">
          <ul className="list-disc pl-5 text-sm">
            {payload.query_items.map((q) => (
              <li key={q}>{q}</li>
            ))}
          </ul>
        </Section>
      )}
      {payload.dropped.length > 0 && (
        <Section title="Dropped by the validator">
          <ul className="space-y-1 text-xs text-muted">
            {payload.dropped.map((d, i) => (
              <li key={i}>
                {humanise(d.item)} {d.detail}: {d.reason}
              </li>
            ))}
          </ul>
        </Section>
      )}
      <Section title="Eligibility rules on the verified facts">
        <RuleTable rules={payload.rule_results} />
      </Section>
      {overruling && (
        <>
          <Notice tone="warn">Overruling records your reason in the claim history. It does not change the findings.</Notice>
          <TextArea label="Why the adverse finding does not hold" value={reason} onChange={setReason} required />
        </>
      )}
      <Actions>
        {!overruling ? (
          <>
            <Button variant="primary" disabled={busy} onClick={() => submit({ action: 'confirm' })}>
              Confirm findings
            </Button>
            <Button disabled={busy} onClick={() => setOverruling(true)}>
              Overrule
            </Button>
          </>
        ) : (
          <>
            <Button
              variant="primary"
              disabled={busy || !reason.trim()}
              onClick={() => submit({ action: 'overrule', reason: reason.trim() })}
            >
              Overrule and continue
            </Button>
            <Button variant="ghost" onClick={() => setOverruling(false)}>
              Cancel
            </Button>
          </>
        )}
        <SendBack stage={payload.stage} busy={busy} submit={submit} />
      </Actions>
    </div>
  )
}

/** investigate: outcome {action: "confirm", verdict: "genuine" | "suspicious", note}. */
export function Investigate({ payload, submit, busy }: KindProps<RiskPayload>) {
  const [note, setNote] = useState('')
  return (
    <div className="space-y-4">
      <p className="text-sm">
        Risk score <span className="num font-semibold">{payload.score}</span> of 100.{' '}
        <span className="text-muted">{payload.explanation}</span>
      </p>
      <Section title="Red flags">
        <ul className="space-y-1 text-sm">
          {payload.hits.map((h) => (
            <li key={h.rule_id}>
              <Chip tone="danger">{h.rule_id}</Chip> <span className="num text-xs text-muted">weight {h.weight}</span>{' '}
              {h.detail}
            </li>
          ))}
        </ul>
      </Section>
      <TextArea label="What you found" value={note} onChange={setNote} required />
      <Actions>
        <Button
          variant="primary"
          disabled={busy || !note.trim()}
          onClick={() => submit({ action: 'confirm', verdict: 'genuine', note: note.trim() })}
        >
          Genuine, continue
        </Button>
        <Button
          disabled={busy || !note.trim()}
          onClick={() => submit({ action: 'confirm', verdict: 'suspicious', note: note.trim() })}
        >
          Suspicious, continue to the approver
        </Button>
        <SendBack stage={payload.stage} busy={busy} submit={submit} />
      </Actions>
    </div>
  )
}
