import { useMemo } from 'react'
import { api, type ClaimStatusRow } from '../../lib/api'
import { dateTime, duration } from '../../lib/format'
import { href } from '../../lib/router'
import { useAsync } from '../../hooks/useAsync'
import { useNow } from '../../hooks/useNow'
import { STAGE_LABELS } from '../submit/status'
import { Button, Card, Chip, Empty, ErrorList, Loading } from '../ui/primitives'

const ORDER = [...Object.keys(STAGE_LABELS), 'received', 'closed']

function stageLabel(state: string): string {
  if (state === 'received') return 'Received'
  if (state === 'closed') return 'Closed'
  return STAGE_LABELS[state as keyof typeof STAGE_LABELS] ?? state
}

export function MonitoringList({ userId }: { userId: string }) {
  const [state, reload] = useAsync(() => api.monitoringClaims(), [userId])
  const now = useNow(30_000)

  const groups = useMemo(() => {
    if (state.kind !== 'loaded') return []
    const byState = new Map<string, ClaimStatusRow[]>()
    for (const row of state.data) byState.set(row.state, [...(byState.get(row.state) ?? []), row])
    return [...byState.entries()].sort(
      ([a], [b]) => (ORDER.indexOf(a) + 1 || 99) - (ORDER.indexOf(b) + 1 || 99),
    )
  }, [state])

  return (
    <Card
      title="Claims by stage"
      actions={
        <Button variant="ghost" onClick={reload}>
          Refresh
        </Button>
      }
    >
      {state.kind === 'loading' && <Loading />}
      {state.kind === 'error' && <ErrorList error={state.error} />}
      {state.kind === 'loaded' && state.data.length === 0 && <Empty>No claims yet.</Empty>}
      <div className="space-y-5">
        {groups.map(([stage, rows]) => (
          <section key={stage}>
            <h3 className="mb-1 flex items-center gap-2 text-sm font-semibold">
              {stageLabel(stage)} <Chip>{rows.length}</Chip>
            </h3>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[34rem] text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs text-muted">
                    <th className="py-1.5 pr-3 font-medium">Claim</th>
                    <th className="py-1.5 pr-3 font-medium">In this stage since</th>
                    <th className="py-1.5 pr-3 text-right font-medium">Time in stage</th>
                    <th className="py-1.5 text-right font-medium">Decision due</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => {
                    const left = row.deadline ? (Date.parse(row.deadline) - now) / 1000 : null
                    return (
                      <tr key={row.claim_id} className="border-b border-line last:border-0">
                        <td className="py-1.5 pr-3">
                          <a className="text-accent hover:underline" href={href(`/claims/${row.claim_id}`)}>
                            {row.claim_number ?? row.claim_id.slice(0, 8)}
                          </a>
                        </td>
                        <td className="num py-1.5 pr-3 text-muted">{dateTime(row.entered_at)}</td>
                        <td className="num py-1.5 pr-3 text-right">{duration(row.dwell_seconds)}</td>
                        <td className="num py-1.5 text-right">
                          {left === null ? (
                            <span className="text-muted">-</span>
                          ) : (
                            <span className={left < 0 ? 'font-medium text-danger' : ''}>
                              {duration(left)}
                              {left >= 0 && ' left'}
                            </span>
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </section>
        ))}
      </div>
    </Card>
  )
}
