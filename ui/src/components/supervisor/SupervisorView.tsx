import { Fragment, useState } from 'react'
import { api, type HeldClaim, type NearBreach, type QueueDepth, type UserActivity } from '../../lib/api'
import { dateTime, duration, humanise, percent } from '../../lib/format'
import { href } from '../../lib/router'
import { ROLE_LABELS } from '../../lib/users'
import { useAsync } from '../../hooks/useAsync'
import { statusLabel } from '../submit/status'
import { Button, Card, Chip, Empty, ErrorList, inputClass, Loading } from '../ui/primitives'
import { OpenClaims } from './OpenClaims'
import { ReassignForm } from './ReassignForm'
import { ReasonForm } from './ReasonForm'

const WINDOWS = [1, 7, 30]
const NEAR_BREACH = [30, 60, 240, 1440]

const th = 'py-1.5 pr-3 font-medium'
const thNum = 'py-1.5 pr-3 text-right font-medium'

export function SupervisorView({ userId }: { userId: string }) {
  const [windowDays, setWindowDays] = useState(7)
  const [nearMinutes, setNearMinutes] = useState(60)
  const [version, setVersion] = useState(0)
  const [state] = useAsync(
    () => api.supervisorOverview(windowDays, nearMinutes),
    [userId, windowDays, nearMinutes, version],
  )
  const refresh = () => setVersion((v) => v + 1)
  const overview = state.kind === 'loaded' ? state.data : null

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">Supervisor</h1>
          {overview && <p className="text-xs text-muted">As of {dateTime(overview.generated_at)}</p>}
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <div>
            <label htmlFor="sv-window" className="mb-1 block text-xs text-muted">
              Throughput window
            </label>
            <select
              id="sv-window"
              className={inputClass}
              value={windowDays}
              onChange={(e) => setWindowDays(Number(e.target.value))}
            >
              {WINDOWS.map((d) => (
                <option key={d} value={d}>
                  Last {d} {d === 1 ? 'day' : 'days'}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="sv-near" className="mb-1 block text-xs text-muted">
              Near breach within
            </label>
            <select
              id="sv-near"
              className={inputClass}
              value={nearMinutes}
              onChange={(e) => setNearMinutes(Number(e.target.value))}
            >
              {NEAR_BREACH.map((m) => (
                <option key={m} value={m}>
                  {duration(m * 60)}
                </option>
              ))}
            </select>
          </div>
          <Button variant="ghost" onClick={refresh}>
            Refresh
          </Button>
        </div>
      </div>

      {state.kind === 'loading' && <Loading />}
      {state.kind === 'error' && <ErrorList error={state.error} />}
      {overview && (
        <>
          <QueueDepths queues={overview.queues} />
          <NearBreachList rows={overview.near_breach} minutes={overview.near_breach_minutes} onChanged={refresh} />
          <div className="grid gap-4 lg:grid-cols-2">
            <Throughput users={overview.users} windowDays={overview.window_days} />
            <HeldClaims rows={overview.held_claims} onChanged={refresh} />
          </div>
          <OpenClaims
            userId={userId}
            version={version}
            heldIds={new Set(overview.held_claims.map((h) => h.claim_id))}
            onHeld={refresh}
          />
        </>
      )}
    </div>
  )
}

function QueueDepths({ queues }: { queues: QueueDepth[] }) {
  return (
    <Card title="Queue depth">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[36rem] text-sm">
          <thead>
            <tr className="border-b border-line text-left text-xs text-muted">
              <th className={th}>Queue</th>
              <th className={thNum}>Unfinished</th>
              <th className={thNum}>Taken</th>
              <th className={thNum}>Overdue</th>
              <th className="py-1.5 font-medium">By task</th>
            </tr>
          </thead>
          <tbody>
            {queues.map((q) => (
              <tr key={q.role} className="border-b border-line last:border-0">
                <td className="py-1.5 pr-3">{ROLE_LABELS[q.role] ?? q.role}</td>
                <td className="num py-1.5 pr-3 text-right">{q.unfinished}</td>
                <td className="num py-1.5 pr-3 text-right">{q.taken}</td>
                <td className={`num py-1.5 pr-3 text-right ${q.overdue > 0 ? 'font-medium text-danger' : ''}`}>
                  {q.overdue}
                </td>
                <td className="py-1.5">
                  <span className="flex flex-wrap gap-1">
                    {Object.entries(q.by_kind).length === 0 ? (
                      <span className="text-muted">-</span>
                    ) : (
                      Object.entries(q.by_kind).map(([kind, n]) => (
                        <Chip key={kind}>
                          {humanise(kind)}: {n}
                        </Chip>
                      ))
                    )}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  )
}

function NearBreachList({ rows, minutes, onChanged }: { rows: NearBreach[]; minutes: number; onChanged: () => void }) {
  const [open, setOpen] = useState<string | null>(null)
  return (
    <Card title={`Tasks near breach (due within ${duration(minutes * 60)} or overdue)`}>
      {rows.length === 0 ? (
        <Empty>No task is close to its deadline.</Empty>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[44rem] text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs text-muted">
                <th className={th}>Time left</th>
                <th className={th}>Claim</th>
                <th className={th}>Task</th>
                <th className={th}>Queue</th>
                <th className={th}>With</th>
                <th className="py-1.5 text-right font-medium">
                  <span className="sr-only">Action</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((t) => (
                <Fragment key={t.task_id}>
                  <tr className="border-b border-line last:border-0">
                    <td className="num py-1.5 pr-3">
                      <Chip tone={t.minutes_left < 0 ? 'danger' : 'warn'}>
                        {t.minutes_left < 0 ? duration(t.minutes_left * 60) : `${duration(t.minutes_left * 60)} left`}
                      </Chip>
                    </td>
                    <td className="py-1.5 pr-3">
                      <a className="text-accent hover:underline" href={href(`/claims/${t.claim_id}`)}>
                        {t.claim_number ?? t.claim_id.slice(0, 8)}
                      </a>
                      {t.held && (
                        <span className="ml-2">
                          <Chip tone="warn">On hold</Chip>
                        </span>
                      )}
                    </td>
                    <td className="py-1.5 pr-3">{humanise(t.kind)}</td>
                    <td className="py-1.5 pr-3">{ROLE_LABELS[t.role] ?? t.role}</td>
                    <td className="py-1.5 pr-3">{t.assignee ?? <span className="text-muted">Not taken</span>}</td>
                    <td className="py-1.5 text-right">
                      <Button
                        variant="ghost"
                        aria-expanded={open === t.task_id}
                        onClick={() => setOpen(open === t.task_id ? null : t.task_id)}
                      >
                        Reassign
                      </Button>
                    </td>
                  </tr>
                  {open === t.task_id && (
                    <tr className="border-b border-line">
                      <td colSpan={6} className="py-2">
                        <ReassignForm
                          task={t}
                          onCancel={() => setOpen(null)}
                          onDone={() => {
                            setOpen(null)
                            onChanged()
                          }}
                        />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}

function Throughput({ users, windowDays }: { users: UserActivity[]; windowDays: number }) {
  return (
    <Card title={`Throughput, last ${windowDays} ${windowDays === 1 ? 'day' : 'days'}`}>
      {users.length === 0 ? (
        <Empty>No completed tasks in this window.</Empty>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[30rem] text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs text-muted">
                <th className={th}>Person</th>
                <th className={thNum}>Completed</th>
                <th className={thNum}>Decided</th>
                <th className={thNum}>Overridden</th>
                <th className="py-1.5 text-right font-medium">Override rate</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.user_id} className="border-b border-line last:border-0">
                  <td className="py-1.5 pr-3">{u.name}</td>
                  <td className="num py-1.5 pr-3 text-right">{u.completed}</td>
                  <td className="num py-1.5 pr-3 text-right">{u.decided}</td>
                  <td className="num py-1.5 pr-3 text-right">
                    {u.overridden > 0 ? (
                      <a className="text-accent hover:underline" href={href(`/overrides?user_id=${u.user_id}`)}>
                        {u.overridden}
                      </a>
                    ) : (
                      u.overridden
                    )}
                  </td>
                  <td className="num py-1.5 text-right">
                    {u.override_rate === null ? <span className="text-muted">-</span> : percent(u.override_rate)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}

function HeldClaims({ rows, onChanged }: { rows: HeldClaim[]; onChanged: () => void }) {
  const [open, setOpen] = useState<string | null>(null)
  return (
    <Card title="Held claims">
      {rows.length === 0 ? (
        <Empty>No claim is on hold.</Empty>
      ) : (
        <ul className="divide-y divide-line">
          {rows.map((h) => (
            <li key={h.claim_id} className="space-y-2 py-2 text-sm">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="min-w-0">
                  <p>
                    <a className="font-medium text-accent hover:underline" href={href(`/claims/${h.claim_id}`)}>
                      {h.claim_number ?? h.claim_id.slice(0, 8)}
                    </a>{' '}
                    <span className="text-muted">
                      {statusLabel(h.status)}, held {dateTime(h.held_at)}
                    </span>
                  </p>
                  <p className="text-xs text-muted">Reason: {h.reason}</p>
                </div>
                <Button
                  variant="ghost"
                  aria-expanded={open === h.claim_id}
                  onClick={() => setOpen(open === h.claim_id ? null : h.claim_id)}
                >
                  Resume
                </Button>
              </div>
              {open === h.claim_id && (
                <ReasonForm
                  label="Why resume"
                  confirm="Resume claim"
                  onCancel={() => setOpen(null)}
                  onSubmit={async (reason) => {
                    await api.resume(h.claim_id, reason)
                    setOpen(null)
                    onChanged()
                  }}
                />
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}
