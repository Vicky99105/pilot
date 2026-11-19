import { Fragment, useState } from 'react'
import { api } from '../../lib/api'
import { duration } from '../../lib/format'
import { href } from '../../lib/router'
import { useAsync } from '../../hooks/useAsync'
import { STAGE_LABELS } from '../submit/status'
import { Button, Card, Empty, ErrorList, Loading } from '../ui/primitives'
import { ReasonForm } from './ReasonForm'

function stageLabel(state: string): string {
  if (state === 'received') return 'Received'
  return STAGE_LABELS[state as keyof typeof STAGE_LABELS] ?? state
}

/** Open claims a supervisor can put on hold. Held claims are listed separately and left out here. */
export function OpenClaims({
  userId,
  heldIds,
  version,
  onHeld,
}: {
  userId: string
  heldIds: Set<string>
  version: number
  onHeld: () => void
}) {
  const [state] = useAsync(() => api.monitoringClaims(), [userId, version])
  const [holding, setHolding] = useState<string | null>(null)

  const rows =
    state.kind === 'loaded' ? state.data.filter((r) => r.state !== 'closed' && !heldIds.has(r.claim_id)) : []

  return (
    <Card title="Open claims">
      {state.kind === 'loading' && <Loading />}
      {state.kind === 'error' && <ErrorList error={state.error} />}
      {state.kind === 'loaded' && rows.length === 0 && <Empty>No open claims to hold.</Empty>}
      {rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[34rem] text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs text-muted">
                <th className="py-1.5 pr-3 font-medium">Claim</th>
                <th className="py-1.5 pr-3 font-medium">Stage</th>
                <th className="py-1.5 pr-3 text-right font-medium">Time in stage</th>
                <th className="py-1.5 text-right font-medium">
                  <span className="sr-only">Action</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <Fragment key={row.claim_id}>
                  <tr className="border-b border-line last:border-0">
                    <td className="py-1.5 pr-3">
                      <a className="text-accent hover:underline" href={href(`/claims/${row.claim_id}`)}>
                        {row.claim_number ?? row.claim_id.slice(0, 8)}
                      </a>
                    </td>
                    <td className="py-1.5 pr-3">{stageLabel(row.state)}</td>
                    <td className="num py-1.5 pr-3 text-right">{duration(row.dwell_seconds)}</td>
                    <td className="py-1.5 text-right">
                      <Button
                        variant="ghost"
                        aria-expanded={holding === row.claim_id}
                        onClick={() => setHolding(holding === row.claim_id ? null : row.claim_id)}
                      >
                        Hold
                      </Button>
                    </td>
                  </tr>
                  {holding === row.claim_id && (
                    <tr className="border-b border-line">
                      <td colSpan={4} className="py-2">
                        <ReasonForm
                          label={`Why hold ${row.claim_number ?? 'this claim'}`}
                          confirm="Hold claim"
                          onCancel={() => setHolding(null)}
                          onSubmit={async (reason) => {
                            await api.hold(row.claim_id, reason)
                            setHolding(null)
                            onHeld()
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
