import { useState } from 'react'
import { humanise, rupees } from '../../../lib/format'
import { Button, Chip, Detail, inputClass, Notice } from '../../ui/primitives'
import { Actions, Section, SendBack, TextArea, type KindProps } from './common'
import type { ApprovePayload, CrcPayload, Draft, QueryNoReplyPayload, ReleasePayload } from './payloads'

const DECISION_TONE = { approve: 'ok', partial: 'warn', query: 'accent', reject: 'danger' } as const

function DraftView({ draft, routing }: { draft: Draft; routing?: ApprovePayload['routing'] }) {
  return (
    <div className="space-y-4">
      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Detail label="Draft decision">
          <Chip tone={DECISION_TONE[draft.decision]}>{humanise(draft.decision)}</Chip>
        </Detail>
        <Detail label="Amount">
          <span className="num font-semibold">{rupees(draft.amount)}</span>
        </Detail>
        <Detail label="Payable on the sheet">
          <span className="num">{rupees(draft.payable_on_sheet)}</span>
        </Detail>
        <Detail label="Available balance">
          <span className="num">{rupees(draft.available_balance)}</span>
        </Detail>
        {routing && (
          <Detail label="Authority band">
            {humanise(routing.band)} (<span className="num">{rupees(routing.authority_amount)}</span>)
          </Detail>
        )}
        <Detail label="Rules say">{humanise(draft.expected_decision)}</Detail>
      </dl>
      {!draft.guardrail_passed && (
        <Notice tone="danger">
          <p className="font-medium">The draft failed its checks:</p>
          <ul className="list-disc pl-5">
            {[...draft.guardrail_errors, ...draft.self_check_errors].map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        </Notice>
      )}
      <Section title="Summary">
        <p className="text-sm">{draft.summary}</p>
      </Section>
      <Section title="Reasons">
        <ul className="space-y-1 text-sm">
          {draft.reasons.map((r, i) => (
            <li key={i}>
              {r.text}
              <span className="ml-1 inline-flex flex-wrap gap-1 align-middle">
                {r.rule_id && <Chip>{r.rule_id}</Chip>}
                {r.clause_ref && <Chip>clause {r.clause_ref}</Chip>}
                {r.line_id !== null && <Chip>bill line {r.line_id}</Chip>}
              </span>
            </li>
          ))}
        </ul>
      </Section>
      {draft.query_items.length > 0 && (
        <Section title="Open questions">
          <ul className="list-disc pl-5 text-sm">
            {draft.query_items.map((q) => (
              <li key={q}>{q}</li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  )
}

/** approve: outcome {action: "approve"} | {action: "override", decision, amount, reason} | send-back with a note. */
export function Approve({ payload, submit, busy }: KindProps<ApprovePayload>) {
  const [overriding, setOverriding] = useState(false)
  const [decision, setDecision] = useState<'approve' | 'partial' | 'reject'>('partial')
  const [amount, setAmount] = useState(payload.draft.amount)
  const [reason, setReason] = useState('')
  const amountOk = decision === 'reject' || (amount.trim() !== '' && Number.isFinite(Number(amount)) && Number(amount) > 0)

  return (
    <div className="space-y-4">
      <DraftView draft={payload.draft} routing={payload.routing} />
      {overriding && (
        <Section title="Your decision">
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label htmlFor="ov-decision" className="mb-1 block text-sm font-medium">
                Decision
              </label>
              <select
                id="ov-decision"
                className={inputClass}
                value={decision}
                onChange={(e) => setDecision(e.target.value as typeof decision)}
              >
                <option value="approve">Approve</option>
                <option value="partial">Partial</option>
                <option value="reject">Reject</option>
              </select>
            </div>
            {decision !== 'reject' && (
              <div>
                <label htmlFor="ov-amount" className="mb-1 block text-sm font-medium">
                  Amount to pay (Rs)
                </label>
                <input
                  id="ov-amount"
                  inputMode="decimal"
                  className={`${inputClass} num`}
                  value={amount}
                  onChange={(e) => setAmount(e.target.value)}
                />
                <p className="mt-1 text-xs text-muted">At most {rupees(payload.draft.payable_on_sheet)}, the sheet payable.</p>
              </div>
            )}
          </div>
          <TextArea label="Reason for the change" value={reason} onChange={setReason} required />
        </Section>
      )}
      <Actions>
        {!overriding ? (
          <>
            <Button
              variant="primary"
              disabled={busy || payload.draft.decision === 'query'}
              title={payload.draft.decision === 'query' ? 'A query draft needs a decision: change it or send it back' : undefined}
              onClick={() => submit({ action: 'approve' })}
            >
              Approve the draft
            </Button>
            <Button disabled={busy} onClick={() => setOverriding(true)}>
              Change the decision
            </Button>
          </>
        ) : (
          <>
            <Button
              variant="primary"
              disabled={busy || !reason.trim() || !amountOk}
              onClick={() =>
                submit({
                  action: 'override',
                  decision,
                  ...(decision === 'reject' ? {} : { amount: amount.trim() }),
                  reason: reason.trim(),
                })
              }
            >
              Record my decision
            </Button>
            <Button variant="ghost" onClick={() => setOverriding(false)}>
              Cancel
            </Button>
          </>
        )}
        <SendBack stage={payload.stage} busy={busy} submit={submit} noteRequired />
      </Actions>
    </div>
  )
}

/** crc: outcome {action: "uphold"} or a send-back with a note. */
export function Crc({ payload, submit, busy }: KindProps<CrcPayload>) {
  return (
    <div className="space-y-4">
      <Notice tone="warn">
        The approver decided: <strong>{humanise(payload.final_decision)}</strong>. A partial payment or a rejection needs your
        review before any letter goes out.
      </Notice>
      <DraftView draft={payload.draft} routing={payload.routing} />
      <Actions>
        <Button variant="primary" disabled={busy} onClick={() => submit({ action: 'uphold' })}>
          Uphold the decision
        </Button>
        <SendBack stage={payload.stage} busy={busy} submit={submit} noteRequired />
      </Actions>
    </div>
  )
}

/** release_payment: outcome {action: "release"} or a send-back. */
export function ReleasePayment({ payload, submit, busy }: KindProps<ReleasePayload>) {
  const failed = payload.payee_checks.filter((c) => !c.passed)
  return (
    <div className="space-y-4">
      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Detail label="Decision">{humanise(payload.final_decision)}</Detail>
        <Detail label="To pay">
          <span className="num font-semibold">{rupees(payload.paid_amount)}</span>
        </Detail>
        <Detail label="Payee">{payload.payee ? `${payload.payee.name} (${payload.payee.kind})` : '-'}</Detail>
        <Detail label="Account">
          {payload.payee ? (
            <span className="num">
              {payload.payee.account_number}, {payload.payee.ifsc}
            </span>
          ) : (
            '-'
          )}
        </Detail>
      </dl>
      <Section title="Payee checks">
        <ul className="space-y-1 text-sm">
          {payload.payee_checks.map((c) => (
            <li key={c.check_id}>
              <Chip tone={c.passed ? 'ok' : 'danger'}>{humanise(c.check_id)}</Chip> <span className="ml-1">{c.detail}</span>
            </li>
          ))}
        </ul>
      </Section>
      {failed.length > 0 && (
        <Notice tone="danger">A payee check failed. The release will be refused until the payee details are fixed.</Notice>
      )}
      <Actions>
        <Button variant="primary" disabled={busy} onClick={() => submit({ action: 'release' })}>
          Release {rupees(payload.paid_amount)}
        </Button>
        <SendBack stage={payload.stage} busy={busy} submit={submit} />
      </Actions>
    </div>
  )
}

/** query_no_reply: outcome {action: "extend"} (wait again) or {action: "proceed"}. */
export function QueryNoReply({ submit, busy }: KindProps<QueryNoReplyPayload>) {
  return (
    <div className="space-y-4">
      <p className="text-sm">
        The claimant did not reply to the query by the deadline. Wait for another reply window, or send the claim to the
        approver as it stands.
      </p>
      <Actions>
        <Button variant="primary" disabled={busy} onClick={() => submit({ action: 'extend' })}>
          Wait for another reply window
        </Button>
        <Button disabled={busy} onClick={() => submit({ action: 'proceed' })}>
          Decide without the reply
        </Button>
      </Actions>
    </div>
  )
}
