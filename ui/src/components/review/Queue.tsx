import { useState } from 'react'
import { api, type Task, type User } from '../../lib/api'
import { duration, rupees } from '../../lib/format'
import { href, navigate } from '../../lib/router'
import { ROLE_LABELS } from '../../lib/users'
import { timeLeftShare, urgencyTone } from '../../lib/urgency'
import { useAsync } from '../../hooks/useAsync'
import { useNow } from '../../hooks/useNow'
import { Button, Card, Chip, Empty, ErrorList, Loading, Notice } from '../ui/primitives'

export function UrgencyChip({ task, now }: { task: Task; now: number }) {
  const share = timeLeftShare(task, now)
  const left = (Date.parse(task.deadline) - now) / 1000
  const tone = urgencyTone(share)
  return <Chip tone={tone}>{left < 0 ? duration(left) : `${duration(left)} left`}</Chip>
}

export function Queue({ user }: { user: User }) {
  const [state, reload] = useAsync(() => api.queue(), [user.id])
  const now = useNow(30_000)
  const [taking, setTaking] = useState(false)
  const [takeError, setTakeError] = useState<unknown>(null)
  const [nothing, setNothing] = useState(false)

  const takeNext = async () => {
    setTaking(true)
    setTakeError(null)
    setNothing(false)
    try {
      const task = await api.takeNext()
      if (task) navigate(`/tasks/${task.id}`)
      else setNothing(true)
    } catch (e) {
      setTakeError(e)
    } finally {
      setTaking(false)
    }
  }

  const tasks =
    state.kind === 'loaded'
      ? [...state.data].sort((a, b) => Date.parse(a.deadline) - Date.parse(b.deadline))
      : []
  const queueRoles = user.roles.filter((r) => r !== 'member' && r !== 'hospital_user')

  return (
    <Card
      title={
        <span>
          My queue <span className="font-normal text-muted">({queueRoles.map((r) => ROLE_LABELS[r]).join(', ')})</span>
        </span>
      }
      actions={
        <div className="flex gap-2">
          <Button variant="ghost" onClick={reload}>
            Refresh
          </Button>
          <Button variant="primary" onClick={takeNext} disabled={taking}>
            {taking ? 'Taking' : 'Take next'}
          </Button>
        </div>
      }
    >
      <div className="space-y-3">
        {nothing && <Notice tone="ok">Nothing waiting for you right now.</Notice>}
        <ErrorList error={takeError} />
        {state.kind === 'loading' && <Loading />}
        {state.kind === 'error' && <ErrorList error={state.error} />}
        {state.kind === 'loaded' && tasks.length === 0 && <Empty>The queue is empty.</Empty>}
        {tasks.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[40rem] text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs text-muted">
                  <th className="py-1.5 pr-3 font-medium">Time left</th>
                  <th className="py-1.5 pr-3 font-medium">Claim</th>
                  <th className="py-1.5 pr-3 font-medium">Patient</th>
                  <th className="py-1.5 pr-3 text-right font-medium">Claimed</th>
                  <th className="py-1.5 font-medium">Task</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((t) => (
                  <tr key={t.id} className="border-b border-line last:border-0 hover:bg-surface-2">
                    <td className="num py-2 pr-3">
                      <UrgencyChip task={t} now={now} />
                    </td>
                    <td className="py-2 pr-3">{t.claim?.claim_number ?? <span className="text-muted">Not numbered</span>}</td>
                    <td className="py-2 pr-3">{t.claim?.patient_name ?? '-'}</td>
                    <td className="num py-2 pr-3 text-right">{rupees(t.claim?.claimed_amount)}</td>
                    <td className="py-2">
                      <a className="font-medium text-accent hover:underline" href={href(`/tasks/${t.id}`)}>
                        {t.title}
                      </a>
                      {t.status === 'taken' && <span className="ml-2 text-xs text-muted">(taken by you)</span>}
                      {t.status === 'escalated' && (
                        <span className="ml-2">
                          <Chip tone="danger">Escalated</Chip>
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Card>
  )
}
