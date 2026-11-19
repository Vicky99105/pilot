import { useState } from 'react'
import { api } from '../../lib/api'
import { useAsync } from '../../hooks/useAsync'
import { Button, Card, ErrorList, inputClass, Loading } from '../ui/primitives'
import { OverrideTable } from './OverrideTable'

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

type Filter = { claim_id: string; user_id: string }

/** Every change a person made to a machine suggestion, newest first, with optional filters. */
export function OverrideLog({ userId, initial }: { userId: string; initial: Filter }) {
  const [draft, setDraft] = useState<Filter>(initial)
  const [filter, setFilter] = useState<Filter>(initial)
  const [state, reload] = useAsync(
    () =>
      api.overrides({
        claim_id: filter.claim_id || undefined,
        user_id: filter.user_id || undefined,
        limit: 500,
      }),
    [userId, filter],
  )

  const badClaim = draft.claim_id !== '' && !UUID.test(draft.claim_id.trim())
  const badUser = draft.user_id !== '' && !UUID.test(draft.user_id.trim())

  return (
    <Card
      title="Override log"
      actions={
        <Button variant="ghost" onClick={reload}>
          Refresh
        </Button>
      }
    >
      <div className="space-y-3">
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            if (!badClaim && !badUser) setFilter({ claim_id: draft.claim_id.trim(), user_id: draft.user_id.trim() })
          }}
        >
          <div className="w-80 max-w-full">
            <label htmlFor="ov-claim" className="mb-1 block text-xs text-muted">
              Claim ID
            </label>
            <input
              id="ov-claim"
              className={inputClass}
              placeholder="Any claim"
              value={draft.claim_id}
              aria-invalid={badClaim}
              onChange={(e) => setDraft((d) => ({ ...d, claim_id: e.target.value }))}
            />
          </div>
          <div className="w-80 max-w-full">
            <label htmlFor="ov-user" className="mb-1 block text-xs text-muted">
              User ID
            </label>
            <input
              id="ov-user"
              className={inputClass}
              placeholder="Anyone"
              value={draft.user_id}
              aria-invalid={badUser}
              onChange={(e) => setDraft((d) => ({ ...d, user_id: e.target.value }))}
            />
          </div>
          <Button type="submit" variant="primary" disabled={badClaim || badUser}>
            Filter
          </Button>
          {(filter.claim_id || filter.user_id) && (
            <Button
              onClick={() => {
                setDraft({ claim_id: '', user_id: '' })
                setFilter({ claim_id: '', user_id: '' })
              }}
            >
              Clear
            </Button>
          )}
        </form>
        {(badClaim || badUser) && <p className="text-xs text-danger">Enter a full ID, for example from the claim link.</p>}
        {state.kind === 'loading' && <Loading />}
        {state.kind === 'error' && <ErrorList error={state.error} />}
        {state.kind === 'loaded' && <OverrideTable rows={state.data} />}
      </div>
    </Card>
  )
}
