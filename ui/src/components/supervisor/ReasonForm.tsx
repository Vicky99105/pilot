import { useId, useState, type ReactNode } from 'react'
import { Button, ErrorList, inputClass } from '../ui/primitives'

/** Every supervisor action needs a reason. This asks for one, runs the action and shows any error. */
export function ReasonForm({
  label,
  confirm,
  onSubmit,
  onCancel,
  children,
}: {
  label: string
  confirm: string
  onSubmit: (reason: string) => Promise<void>
  onCancel: () => void
  children?: ReactNode
}) {
  const id = useId()
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)

  const run = async () => {
    setBusy(true)
    setError(null)
    try {
      await onSubmit(reason.trim())
    } catch (e) {
      setError(e)
      setBusy(false)
    }
  }

  return (
    <form
      className="space-y-2 rounded-md border border-line bg-surface-2 p-3"
      onSubmit={(e) => {
        e.preventDefault()
        if (reason.trim()) void run()
      }}
    >
      {children}
      <div>
        <label htmlFor={id} className="mb-1 block text-xs font-medium">
          {label}
        </label>
        <input
          id={id}
          className={inputClass}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="Reason (recorded with the action)"
        />
      </div>
      <ErrorList error={error} />
      <div className="flex justify-end gap-2">
        <Button onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
        <Button type="submit" variant="primary" disabled={busy || !reason.trim()}>
          {busy ? 'Saving' : confirm}
        </Button>
      </div>
    </form>
  )
}
