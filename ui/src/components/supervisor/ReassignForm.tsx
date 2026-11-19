import { useId, useState } from 'react'
import { api, type NearBreach } from '../../lib/api'
import { useAsync } from '../../hooks/useAsync'
import { ErrorList, inputClass, Loading } from '../ui/primitives'
import { ReasonForm } from './ReasonForm'

/** Moves one task to another person who holds its role. */
export function ReassignForm({
  task,
  onDone,
  onCancel,
}: {
  task: NearBreach
  onDone: () => void
  onCancel: () => void
}) {
  const id = useId()
  const [state] = useAsync(() => api.assignees(task.task_id), [task.task_id])
  const [to, setTo] = useState('')

  if (state.kind === 'loading') return <Loading label="Loading people" />
  if (state.kind === 'error') return <ErrorList error={state.error} />

  return (
    <ReasonForm
      label="Why reassign"
      confirm="Reassign"
      onCancel={onCancel}
      onSubmit={async (reason) => {
        if (!to) throw new Error('Pick the person to give the task to')
        await api.reassign(task.task_id, to, reason)
        onDone()
      }}
    >
      <div>
        <label htmlFor={id} className="mb-1 block text-xs font-medium">
          Give the task to
        </label>
        {state.data.length === 0 ? (
          <p className="text-sm text-muted">Nobody else can take this task.</p>
        ) : (
          <select id={id} className={inputClass} value={to} onChange={(e) => setTo(e.target.value)}>
            <option value="">Choose a person</option>
            {state.data.map((a) => (
              <option key={a.user_id} value={a.user_id}>
                {a.name}
              </option>
            ))}
          </select>
        )}
      </div>
    </ReasonForm>
  )
}
