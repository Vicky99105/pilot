import { useState } from 'react'
import { date, humanise, rupees } from '../../../lib/format'
import { Button, Chip, Detail, inputClass, Notice } from '../../ui/primitives'
import { Actions, RuleTable, Section, SendBack, TextArea, type KindProps } from './common'
import type { DeductionsPayload, RegistrationPayload } from './payloads'

/** confirm_registration: outcome {action: "confirm", overrides?: RegistrationOverrides}. */
export function ConfirmRegistration({ payload, submit, busy }: KindProps<RegistrationPayload>) {
  const [memberId, setMemberId] = useState('')
  const [hospitalId, setHospitalId] = useState('')
  const [accepted, setAccepted] = useState<Record<string, string>>({})
  const [reason, setReason] = useState('')
  const failed = payload.rule_results.filter((r) => !r.passed)
  const rules = Object.fromEntries(Object.entries(accepted).filter(([, why]) => why.trim()))
  const overrides = {
    ...(memberId ? { member_id: memberId } : {}),
    ...(hospitalId.trim() ? { hospital_id: hospitalId.trim() } : {}),
    ...(Object.keys(rules).length ? { rules } : {}),
  }
  const overriding = Object.keys(overrides).length > 0
  const p = payload.policy

  return (
    <div className="space-y-4">
      <dl className="grid grid-cols-2 gap-3">
        <Detail label="Claim number">{payload.claim_number}</Detail>
        <Detail label="Admission">{date(payload.admission_date)}</Detail>
        <Detail label="Block on sum insured">{rupees(payload.block_ref?.amount ?? payload.block_amount)}</Detail>
        <Detail label="Accident">{payload.accident ? 'Yes' : 'No'}</Detail>
      </dl>

      <Section title="Member">
        <p className="text-sm">
          <Chip tone={payload.member.match === 'exact_id' ? 'ok' : 'warn'}>{humanise(payload.member.match)}</Chip>{' '}
          {payload.member.name ?? 'No member identified'} {payload.member.member_id && `(${payload.member.member_id})`}
        </p>
        <p className="text-xs text-muted">{payload.member.reason}</p>
      </Section>

      {p ? (
        <Section title="Policy">
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            <Detail label="Policy">{p.policy_number}</Detail>
            <Detail label="Cover">
              {date(p.policy_start)} to {date(p.policy_end)}
            </Detail>
            <Detail label="Sum insured">
              <span className="num">{rupees(p.sum_insured)}</span>
            </Detail>
            <Detail label="Bonus">
              <span className="num">{rupees(p.cumulative_bonus)}</span>
            </Detail>
            <Detail label="Used and blocked">
              <span className="num">{rupees(Number(p.utilised) + Number(p.blocked))}</span>
            </Detail>
            <Detail label="Available">
              <span className="num">{rupees(p.balance)}</span>
            </Detail>
          </dl>
        </Section>
      ) : (
        <Notice tone="danger">No policy was found for this claim.</Notice>
      )}

      <Section title="Hospital">
        <p className="text-sm">
          <Chip tone={payload.hospital.match === 'rohini_id' ? 'ok' : 'warn'}>{humanise(payload.hospital.match)}</Chip>{' '}
          {payload.hospital.name ?? 'Unknown'} {payload.hospital.city && `, ${payload.hospital.city}`}{' '}
          {payload.hospital.rohini_id && <span className="text-muted">ROHINI {payload.hospital.rohini_id}</span>}
        </p>
      </Section>

      {payload.duplicates.length > 0 && (
        <Notice tone="warn">
          Possible duplicate of{' '}
          {payload.duplicates.map((d) => `${d.claim_number ?? d.claim_id.slice(0, 8)} (${date(d.admission_date)})`).join(', ')}
        </Notice>
      )}

      <Section title="Eligibility rules">
        <RuleTable rules={payload.rule_results} />
      </Section>

      <Section title="Corrections">
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label htmlFor="ov-member" className="mb-1 block text-sm font-medium">
              Patient is
            </label>
            <select id="ov-member" className={inputClass} value={memberId} onChange={(e) => setMemberId(e.target.value)}>
              <option value="">Keep the match</option>
              {(p?.insured_members ?? []).map((m) => (
                <option key={m.member_id} value={m.member_id}>
                  {m.name} ({humanise(m.relationship)}, {m.member_id})
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="ov-hospital" className="mb-1 block text-sm font-medium">
              Treating hospital ROHINI ID
            </label>
            <input
              id="ov-hospital"
              className={inputClass}
              placeholder="Keep the match"
              value={hospitalId}
              onChange={(e) => setHospitalId(e.target.value)}
            />
          </div>
        </div>
        {failed.map((r) => (
          <TextArea
            key={r.rule_id}
            label={`Accept despite ${r.rule_id}: why`}
            value={accepted[r.rule_id] ?? ''}
            onChange={(v) => setAccepted((a) => ({ ...a, [r.rule_id]: v }))}
          />
        ))}
        {overriding && <TextArea label="Reason for the corrections" value={reason} onChange={setReason} required />}
      </Section>

      <Actions>
        <Button
          variant="primary"
          disabled={busy || (overriding && !reason.trim())}
          onClick={() =>
            submit(overriding ? { action: 'confirm', overrides: { ...overrides, reason: reason.trim() } } : { action: 'confirm' })
          }
        >
          {overriding ? 'Save corrections and confirm' : 'Confirm registration'}
        </Button>
        <SendBack stage={payload.stage} busy={busy} submit={submit} />
      </Actions>
    </div>
  )
}

type LineEdit = { admissible: string; reason: string }

/** confirm_deductions: outcome {action: "confirm", lines: [{line_id, admissible, reason}]}. */
export function ConfirmDeductions({ payload, submit, busy }: KindProps<DeductionsPayload>) {
  const [edits, setEdits] = useState<Record<number, LineEdit>>({})
  const changed = Object.entries(edits)
    .map(([id, e]) => ({ line: payload.lines.find((l) => l.line_id === Number(id)), ...e }))
    .filter((e) => e.line && Number(e.admissible) !== Number(e.line.admissible))
  const invalid = changed.some(
    (e) =>
      !e.reason.trim() ||
      e.admissible.trim() === '' ||
      !Number.isFinite(Number(e.admissible)) ||
      Number(e.admissible) < 0 ||
      Number(e.admissible) > Number(e.line?.claimed),
  )

  return (
    <div className="space-y-4">
      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Detail label="Claimed">
          <span className="num">{rupees(payload.claimed_total)}</span>
        </Detail>
        <Detail label="Admissible">
          <span className="num">{rupees(payload.admissible_total)}</span>
        </Detail>
        <Detail label="Co-pay">
          <span className="num">{rupees(payload.co_pay)}</span>
        </Detail>
        <Detail label="Payable">
          <span className="num font-semibold">{rupees(payload.payable)}</span>
        </Detail>
      </dl>
      {payload.package && (
        <Section title="Package">
          {payload.package.packages.length === 0 ? (
            <p className="text-sm text-muted">No package applies; every line is priced on its own.</p>
          ) : (
            <ul className="space-y-1 text-sm">
              {payload.package.packages.map((p) => (
                <li key={p.package_code}>
                  <Chip tone="neutral">{p.package_code}</Chip> {p.name} ({p.scheme}){' '}
                  <span className="num">{rupees(p.rate)}</span>
                </li>
              ))}
            </ul>
          )}
          <p className="mt-1 text-xs text-muted">
            Chosen from {payload.package.candidates.length} candidate
            {payload.package.candidates.length === 1 ? '' : 's'}
            {payload.package.billed_as_package && '; the bill charges a package'}
          </p>
        </Section>
      )}
      {payload.findings && payload.findings.length > 0 && (
        <Section title="Checks to note (no deduction)">
          <ul className="space-y-1 text-sm">
            {payload.findings.map((f, i) => (
              <li key={`${f.kind}-${i}`}>
                <Chip tone="warn">{humanise(f.kind)}</Chip>
                {f.line_id !== null && <span className="text-muted"> line {f.line_id}:</span>} {f.text}
              </li>
            ))}
          </ul>
        </Section>
      )}
      <Section title="Bill lines">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[36rem] text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs text-muted">
                <th className="py-1 pr-2 font-medium">Line</th>
                <th className="py-1 pr-2 text-right font-medium">Claimed</th>
                <th className="py-1 pr-2 text-right font-medium">Admissible</th>
                <th className="py-1 font-medium">Why</th>
              </tr>
            </thead>
            <tbody>
              {payload.lines.map((l) => {
                const edit = edits[l.line_id]
                const deducted = Number(l.deducted) > 0
                return (
                  <tr key={l.line_id} className={`border-b border-line align-top last:border-0 ${l.flagged ? 'bg-warn-soft' : ''}`}>
                    <td className="py-1.5 pr-2">
                      <p>{l.description}</p>
                      <p className="text-xs text-muted">
                        {humanise(l.head)}
                        {l.head_as_written && ` (${l.head_as_written})`}
                        {l.in_package && ', priced by the package'}
                        {l.flagged && `, check: ${l.flag_reason ?? 'head not recognised'}`}
                      </p>
                    </td>
                    <td className="num py-1.5 pr-2 text-right whitespace-nowrap">{rupees(l.claimed)}</td>
                    <td className="py-1.5 pr-2 text-right">
                      <label className="sr-only" htmlFor={`adm-${l.line_id}`}>
                        Admissible for line {l.line_id}
                      </label>
                      <input
                        id={`adm-${l.line_id}`}
                        inputMode="decimal"
                        className={`${inputClass} num w-28 text-right`}
                        value={edit?.admissible ?? l.admissible}
                        onChange={(e) =>
                          setEdits((x) => ({ ...x, [l.line_id]: { reason: x[l.line_id]?.reason ?? '', admissible: e.target.value } }))
                        }
                      />
                    </td>
                    <td className="py-1.5 text-xs">
                      {deducted && (
                        <p>
                          <span className="num text-danger">-{rupees(l.deducted)}</span> {l.reason}
                          {l.rule_id && <Chip tone="neutral">{l.rule_id}</Chip>}
                          {l.clause_ref && <span className="ml-1 text-muted">clause {l.clause_ref}</span>}
                        </p>
                      )}
                      {l.judged_by !== 'code' && <p className="text-muted">set by {l.judged_by}</p>}
                      {edit && Number(edit.admissible) !== Number(l.admissible) && (
                        <input
                          aria-label={`Reason for changing line ${l.line_id}`}
                          className={`${inputClass} mt-1`}
                          placeholder="Reason for the change (required)"
                          value={edit.reason}
                          onChange={(e) => setEdits((x) => ({ ...x, [l.line_id]: { ...x[l.line_id], reason: e.target.value } }))}
                        />
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </Section>
      <Section title="Rules">
        <RuleTable rules={payload.rule_results.filter((r) => !r.passed || Number(r.amount_effect) !== 0)} />
      </Section>
      {invalid && <p className="text-xs text-danger">Each changed line needs an amount between 0 and the claimed amount, and a reason.</p>}
      <Actions>
        <Button
          variant="primary"
          disabled={busy || invalid}
          onClick={() =>
            submit({
              action: 'confirm',
              lines: changed.map((e) => ({ line_id: e.line!.line_id, admissible: e.admissible.trim(), reason: e.reason.trim() })),
            })
          }
        >
          {changed.length ? `Save ${changed.length} line change(s)` : 'Confirm deductions'}
        </Button>
        <SendBack stage={payload.stage} busy={busy} submit={submit} />
      </Actions>
    </div>
  )
}
