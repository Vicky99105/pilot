import { api, type Override } from '../../lib/api'
import { dateTime, humanise } from '../../lib/format'
import { href } from '../../lib/router'
import { SEEDED_USERS } from '../../lib/users'
import { useAsync } from '../../hooks/useAsync'
import { Chip, Empty, ErrorList, Loading } from '../ui/primitives'

const KIND_LABELS: Record<string, string> = {
  label_fix: 'Label fix',
  field_fix: 'Field fix',
  registration_override: 'Registration override',
  deduction_edit: 'Deduction edit',
  medical_overrule: 'Medical overrule',
  decision_override: 'Decision override',
}

/** A before or after value: strings as they are, anything else as JSON. */
function shown(value: unknown): string {
  if (value === null || value === undefined) return '-'
  if (typeof value === 'string') return value
  return JSON.stringify(value)
}

/** A person by their seeded name when known, else a short id; system actors stay as they are. */
function who(row: Override): string {
  if (!row.user_id) return row.actor
  return SEEDED_USERS.find((u) => u.id === row.user_id)?.label ?? `user ${row.user_id.slice(0, 8)}`
}

export function OverrideTable({ rows, showClaim = true }: { rows: Override[]; showClaim?: boolean }) {
  if (rows.length === 0) return <Empty>No overrides recorded.</Empty>
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[56rem] text-sm">
        <thead>
          <tr className="border-b border-line text-left text-xs text-muted">
            <th className="py-1.5 pr-3 font-medium">When</th>
            <th className="py-1.5 pr-3 font-medium">Who</th>
            {showClaim && <th className="py-1.5 pr-3 font-medium">Claim</th>}
            <th className="py-1.5 pr-3 font-medium">Stage</th>
            <th className="py-1.5 pr-3 font-medium">Kind</th>
            <th className="py-1.5 pr-3 font-medium">Field</th>
            <th className="py-1.5 pr-3 font-medium">Before and after</th>
            <th className="py-1.5 font-medium">Reason</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.override_id} className="border-b border-line align-top last:border-0">
              <td className="num py-1.5 pr-3 whitespace-nowrap text-muted">{dateTime(row.created_at)}</td>
              <td className="py-1.5 pr-3 whitespace-nowrap" title={row.actor}>
                {who(row)}
              </td>
              {showClaim && (
                <td className="py-1.5 pr-3 whitespace-nowrap">
                  <a className="text-accent hover:underline" href={href(`/claims/${row.claim_id}`)}>
                    {row.claim_number ?? row.claim_id.slice(0, 8)}
                  </a>
                </td>
              )}
              <td className="py-1.5 pr-3 whitespace-nowrap">{humanise(row.stage)}</td>
              <td className="py-1.5 pr-3">
                <Chip tone="warn">{KIND_LABELS[row.kind] ?? humanise(row.kind)}</Chip>
              </td>
              <td className="py-1.5 pr-3 font-mono text-xs">{row.field ?? '-'}</td>
              <td className="max-w-[24rem] py-1.5 pr-3 font-mono text-xs break-words">
                <span className="text-muted line-through">{shown(row.before)}</span>
                <span aria-hidden className="mx-1">
                  {'->'}
                </span>
                <span className="sr-only">changed to</span>
                <span>{shown(row.after)}</span>
              </td>
              <td className="py-1.5">{row.reason ?? <span className="text-muted">-</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** The overrides on one claim, newest first. Staff only. */
export function ClaimOverrides({ claimId }: { claimId: string }) {
  const [state] = useAsync(() => api.overrides({ claim_id: claimId }), [claimId])
  if (state.kind === 'loading') return <Loading label="Loading overrides" />
  if (state.kind === 'error') return <ErrorList error={state.error} />
  return <OverrideTable rows={state.data} showClaim={false} />
}
