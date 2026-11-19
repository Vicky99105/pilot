import { api } from '../../lib/api'
import { date, humanise } from '../../lib/format'
import { href } from '../../lib/router'
import { useAsync } from '../../hooks/useAsync'
import { Button, Card, Chip, Empty, ErrorList, Loading } from '../ui/primitives'
import { statusLabel, statusTone } from './status'

export function MyClaims({ userId, hospital = false }: { userId: string; hospital?: boolean }) {
  const [state, reload] = useAsync(() => api.myClaims(), [userId])

  return (
    <Card
      title={hospital ? 'Admissions' : 'My claims'}
      actions={
        <div className="flex gap-2">
          <Button variant="ghost" onClick={reload}>
            Refresh
          </Button>
          <a href={href('/submit/new')} className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-accent-ink">
            {hospital ? 'New pre-auth' : 'New claim'}
          </a>
        </div>
      }
    >
      {state.kind === 'loading' && <Loading />}
      {state.kind === 'error' && <ErrorList error={state.error} />}
      {state.kind === 'loaded' && state.data.length === 0 && <Empty>No claims yet.</Empty>}
      {state.kind === 'loaded' && state.data.length > 0 && (
        <ul className="divide-y divide-line">
          {state.data.map((c) => (
            <li key={c.claim_id}>
              <a
                href={href(`/claims/${c.claim_id}`)}
                className="flex flex-wrap items-center justify-between gap-2 py-3 hover:bg-surface-2"
              >
                <div className="min-w-0">
                  <p className="text-sm font-medium">
                    {c.claim_number ?? 'Number not assigned yet'}
                    <span className="ml-2 text-muted">{c.patient_name}</span>
                  </p>
                  <p className="text-xs text-muted">
                    {humanise(c.request_type)}, sent {date(c.created_at)}
                    {c.deadline && <>, decision due by {date(c.deadline)}</>}
                  </p>
                </div>
                <Chip tone={statusTone(c.status)}>{statusLabel(c.status)}</Chip>
              </a>
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}
