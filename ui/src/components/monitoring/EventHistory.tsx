import { api } from '../../lib/api'
import { dateTime, humanise } from '../../lib/format'
import { useAsync } from '../../hooks/useAsync'
import { STAGE_LABELS } from '../submit/status'
import { Chip, Empty, ErrorList, Loading } from '../ui/primitives'

/** A claim's events, oldest first. Staff only on the server. */
export function EventHistory({ claimId }: { claimId: string }) {
  const [state] = useAsync(() => api.claimEvents(claimId), [claimId])
  if (state.kind === 'loading') return <Loading />
  if (state.kind === 'error') return <ErrorList error={state.error} />
  if (state.data.length === 0) return <Empty>No events yet.</Empty>
  return (
    <ol className="divide-y divide-line">
      {state.data.map((e, i) => (
        <li key={i} className="grid gap-1 py-2 text-sm sm:grid-cols-[9rem_8rem_minmax(0,1fr)]">
          <span className="num text-xs text-muted">{dateTime(e.created_at)}</span>
          <span>
            <Chip tone={e.action.includes('fail') || e.action.includes('refused') ? 'danger' : 'neutral'}>
              {STAGE_LABELS[e.stage] ?? e.stage}
            </Chip>
          </span>
          <div className="min-w-0">
            <p>
              {humanise(e.action)} <span className="text-xs text-muted">by {e.actor}</span>
            </p>
            {e.detail && Object.keys(e.detail).length > 0 && (
              <details className="mt-0.5">
                <summary className="cursor-pointer text-xs text-muted">Details</summary>
                <pre className="mt-1 overflow-x-auto rounded bg-surface-2 p-2 text-xs">
                  {JSON.stringify(e.detail, null, 2)}
                </pre>
              </details>
            )}
          </div>
        </li>
      ))}
    </ol>
  )
}
