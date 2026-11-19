import type { User } from '../lib/api'
import { OverrideLog } from '../components/overrides/OverrideLog'

/** "#/overrides?claim_id=...&user_id=..." opens the log already filtered. */
export function OverridesPage({ user, path }: { user: User; path: string }) {
  const query = new URLSearchParams(path.split('?')[1] ?? '')
  const initial = { claim_id: query.get('claim_id') ?? '', user_id: query.get('user_id') ?? '' }
  return <OverrideLog key={path} userId={user.id} initial={initial} />
}
