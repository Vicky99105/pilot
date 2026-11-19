import type { User } from '../lib/api'
import { ClaimView } from '../components/submit/ClaimView'

export function ClaimPage({ claimId, user }: { claimId: string; user: User }) {
  return <ClaimView claimId={claimId} user={user} />
}
