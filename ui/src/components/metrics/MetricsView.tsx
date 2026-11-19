import { useState, type ReactNode } from 'react'
import { api, type OverrideRate, type PilotMetrics, type Role, type TaskTurnaround } from '../../lib/api'
import { dateTime, docLabel, duration, humanise, percent } from '../../lib/format'
import { href } from '../../lib/router'
import { ROLE_LABELS } from '../../lib/users'
import { useAsync } from '../../hooks/useAsync'
import { Button, Card, Empty, ErrorList, inputClass, Loading } from '../ui/primitives'

const PRESETS = [1, 7, 30, 90]
const DAY_MS = 86_400_000
const REQUEST_LABELS: Record<string, string> = {
  reimbursement: 'Reimbursement',
  preauth: 'Pre-auth',
  enhancement: 'Enhancement',
  discharge: 'Discharge',
  reconsideration: 'Reconsideration',
}

type Period = { preset: number | 'custom'; start: string; end: string }

function lastDays(days: number): Period {
  const end = new Date()
  return { preset: days, start: new Date(end.getTime() - days * DAY_MS).toISOString(), end: end.toISOString() }
}

/** "2026-11-01" in the browser's time zone, for a date input. */
function localDate(iso: string): string {
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/** Local midnight of a date input's value, as an ISO instant. */
function midnight(value: string): string {
  return new Date(`${value}T00:00:00`).toISOString()
}

function seconds(value: number | null): string {
  if (value === null) return '-'
  return value < 60 ? `${Math.round(value)}s` : duration(value)
}

function rate(value: number | null): string {
  return value === null ? '-' : percent(value)
}

function usd(value: number | null): string {
  if (value === null) return '-'
  return `$${value.toFixed(value !== 0 && Math.abs(value) < 0.01 ? 4 : 2)}`
}

function ms(value: number | null): string {
  if (value === null) return '-'
  return value < 1000 ? `${Math.round(value)} ms` : `${(value / 1000).toFixed(1)} s`
}

export function MetricsView({ userId }: { userId: string }) {
  const [period, setPeriod] = useState<Period>(() => lastDays(7))
  const [state] = useAsync(() => api.pilotMetrics(period.start, period.end), [userId, period])
  const metrics = state.kind === 'loaded' ? state.data : null

  const choose = (value: string) => {
    if (value === 'custom') setPeriod({ ...period, preset: 'custom' })
    else setPeriod(lastDays(Number(value)))
  }
  // The "to" date is inclusive: the period ends at the next local midnight.
  const setFrom = (value: string) => value && setPeriod({ ...period, start: midnight(value) })
  const setTo = (value: string) =>
    value && setPeriod({ ...period, end: new Date(new Date(midnight(value)).getTime() + DAY_MS).toISOString() })

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">Pilot metrics</h1>
          {metrics && (
            <p className="text-xs text-muted">
              {dateTime(metrics.start)} to {dateTime(metrics.end)}, as of {dateTime(metrics.generated_at)}
            </p>
          )}
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <div>
            <label htmlFor="pm-period" className="mb-1 block text-xs text-muted">
              Period
            </label>
            <select id="pm-period" className={inputClass} value={period.preset} onChange={(e) => choose(e.target.value)}>
              {PRESETS.map((d) => (
                <option key={d} value={d}>
                  Last {d} {d === 1 ? 'day' : 'days'}
                </option>
              ))}
              <option value="custom">Custom dates</option>
            </select>
          </div>
          {period.preset === 'custom' && (
            <>
              <div>
                <label htmlFor="pm-from" className="mb-1 block text-xs text-muted">
                  From
                </label>
                <input
                  id="pm-from"
                  type="date"
                  className={inputClass}
                  value={localDate(period.start)}
                  onChange={(e) => setFrom(e.target.value)}
                />
              </div>
              <div>
                <label htmlFor="pm-to" className="mb-1 block text-xs text-muted">
                  To (inclusive)
                </label>
                <input
                  id="pm-to"
                  type="date"
                  className={inputClass}
                  value={localDate(new Date(new Date(period.end).getTime() - 1).toISOString())}
                  onChange={(e) => setTo(e.target.value)}
                />
              </div>
            </>
          )}
          {period.preset !== 'custom' && (
            <Button variant="ghost" onClick={() => setPeriod(lastDays(period.preset as number))}>
              Refresh
            </Button>
          )}
        </div>
      </div>

      {state.kind === 'loading' && <Loading />}
      {state.kind === 'error' && <ErrorList error={state.error} />}
      {metrics && <Metrics m={metrics} />}
    </div>
  )
}

function Metrics({ m }: { m: PilotMetrics }) {
  const decisions = ['approve', 'partial', 'reject']
  return (
    <>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <Stat label="Claims received" value={String(m.queries.claims_received)} />
        <Stat label="Query rate" value={rate(m.queries.rate)} hint={`${m.queries.claims_queried} queried`} />
        <Stat label="Claims decided" value={String(m.outcomes.decided)} />
        <Stat
          label="Risk referral rate"
          value={rate(m.outcomes.risk_referral_rate)}
          hint={`${m.outcomes.risk_referred} of ${m.outcomes.risk_scored} scored`}
        />
        <Stat label="Clock breaches" value={String(m.clock_breaches)} tone={m.clock_breaches > 0 ? 'danger' : undefined} hint="cashless" />
        <Stat label="Model cost per claim" value={usd(m.models.cost_per_claim_mean)} hint={`p95 ${usd(m.models.cost_per_claim_p95)}`} />
      </div>

      <Card title="End-to-end turnaround by request type">
        <Table
          head={['Request', 'Answered', 'p50', 'p95', 'Breaches']}
          rows={m.request_turnaround.map((r) => [
            REQUEST_LABELS[r.request_type] ?? humanise(r.request_type),
            r.answered,
            seconds(r.p50_seconds),
            seconds(r.p95_seconds),
            <Count key="breaches" n={r.breaches} danger />,
          ])}
          empty="No request was answered in this period."
        />
        <p className="mt-2 text-xs text-muted">
          Reimbursement: first event to claim closed; breach when closed after the claim deadline. Cashless: request
          received to its letter; breach when its clock passed. Reconsideration: request to the next closure.
        </p>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Task turnaround by queue">
          <TurnaroundTable rows={m.turnaround_by_role} label={(k) => ROLE_LABELS[k as Role] ?? humanise(k)} first="Queue" />
        </Card>
        <Card title="Task turnaround by stage">
          <TurnaroundTable rows={m.turnaround_by_stage} label={humanise} first="Stage" />
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Query rate by reason">
          <Table
            head={['Reason', 'Claims', 'Rate']}
            rows={m.queries.by_reason.map((r) => [docLabel(r.reason), r.claims, rate(r.rate)])}
            empty="No claim received in this period was queried."
          />
          <p className="mt-2 text-xs text-muted">Share of the {m.queries.claims_received} claims received in the period.</p>
        </Card>
        <Card title="Outcomes">
          <Table
            head={['Outcome', 'Claims']}
            rows={[
              ...decisions.map((d) => [humanise(d), m.outcomes.by_decision[d] ?? 0]),
              ...Object.entries(m.outcomes.by_decision)
                .filter(([d]) => !decisions.includes(d))
                .map(([d, n]) => [humanise(d), n]),
              ['Referred to CRC', m.outcomes.referred_to_crc],
              ['Reconsiderations requested', m.outcomes.reconsiderations],
            ]}
            empty=""
          />
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Override rate by stage">
          <OverrideTable rows={m.overrides_by_stage} first="Stage" label={(o) => humanise(o.key)} />
        </Card>
        <Card title="Override rate by person">
          <OverrideTable
            rows={m.overrides_by_user}
            first="Person"
            label={(o) => (
              <a className="text-accent hover:underline" href={href(`/overrides?user_id=${o.key}`)}>
                {o.name ?? o.key.slice(0, 8)}
              </a>
            )}
          />
        </Card>
      </div>

      <Card title="Model cost and latency">
        <div className="mb-3 grid grid-cols-2 gap-3 md:grid-cols-4">
          <Stat label="Calls" value={String(m.models.calls)} hint={`${m.models.claims} claims`} />
          <Stat label="Total cost" value={usd(m.models.total_cost)} />
          <Stat
            label="Cost per claim p50 / p95"
            value={`${usd(m.models.cost_per_claim_p50)} / ${usd(m.models.cost_per_claim_p95)}`}
          />
          <Stat
            label="Model time per claim p50 / p95"
            value={`${ms(m.models.latency_per_claim_p50_ms)} / ${ms(m.models.latency_per_claim_p95_ms)}`}
          />
        </div>
        <Table
          head={['Model role', 'Calls', 'Claims', 'Invalid', 'Total cost', 'Cost per claim', 'Latency p50', 'Latency p95']}
          rows={m.models.by_role.map((r) => [
            humanise(r.role),
            r.calls,
            r.claims,
            <Count key="invalid" n={r.invalid} danger />,
            usd(r.total_cost),
            usd(r.cost_per_claim),
            ms(r.latency_p50_ms),
            ms(r.latency_p95_ms),
          ])}
          empty="No model call in this period."
        />
      </Card>
    </>
  )
}

function Stat({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: 'danger' }) {
  return (
    <div className="rounded-lg border border-line bg-surface p-3">
      <p className="text-xs text-muted">{label}</p>
      <p className={`num mt-1 text-lg font-semibold ${tone === 'danger' ? 'text-danger' : ''}`}>{value}</p>
      {hint && <p className="text-xs text-muted">{hint}</p>}
    </div>
  )
}

function Count({ n, danger }: { n: number; danger?: boolean }) {
  return <span className={danger && n > 0 ? 'font-medium text-danger' : ''}>{n}</span>
}

function TurnaroundTable({ rows, label, first }: { rows: TaskTurnaround[]; label: (key: string) => string; first: string }) {
  return (
    <Table
      head={[first, 'Completed', 'p50', 'p95', 'Late']}
      rows={rows.map((t) => [label(t.key), t.completed, seconds(t.p50_seconds), seconds(t.p95_seconds), <Count key="late" n={t.late} danger />])}
      empty="No task was completed in this period."
    />
  )
}

function OverrideTable({ rows, first, label }: { rows: OverrideRate[]; first: string; label: (o: OverrideRate) => ReactNode }) {
  return (
    <Table
      head={[first, 'Decided', 'Overridden', 'Rate', 'Fields changed']}
      rows={rows.map((o) => [label(o), o.decided, o.overridden, rate(o.rate), o.entries])}
      empty="No overridable decision in this period."
    />
  )
}

/** A table whose first column is text and the rest are numbers, right-aligned. */
function Table({ head, rows, empty }: { head: string[]; rows: ReactNode[][]; empty: string }) {
  if (rows.length === 0) return <Empty>{empty}</Empty>
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-line text-left text-xs text-muted">
            {head.map((h, i) => (
              <th key={h} className={`py-1.5 pr-3 font-medium ${i > 0 ? 'text-right' : ''}`}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, r) => (
            <tr key={r} className="border-b border-line last:border-0">
              {row.map((cell, i) => (
                <td key={i} className={`py-1.5 pr-3 ${i > 0 ? 'num text-right whitespace-nowrap' : ''}`}>
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
