import type { User } from '../lib/api'
import { ClaimForm } from '../components/submit/ClaimForm'
import { MyClaims } from '../components/submit/MyClaims'

export function MyClaimsPage({ user }: { user: User }) {
  return <MyClaims userId={user.id} />
}

export function NewClaimPage({ user }: { user: User }) {
  return (
    <div className="space-y-3">
      <h1 className="text-lg font-semibold">
        {user.kind === 'member' ? 'Claim reimbursement' : 'Send a request'}
      </h1>
      <ClaimForm key={user.id} user={user} />
    </div>
  )
}
