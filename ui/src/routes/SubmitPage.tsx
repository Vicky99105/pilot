import type { User } from '../lib/api'
import { ClaimForm } from '../components/submit/ClaimForm'
import { PreauthForm } from '../components/submit/PreauthForm'
import { MyClaims } from '../components/submit/MyClaims'

export function MyClaimsPage({ user }: { user: User }) {
  return <MyClaims userId={user.id} hospital={user.kind === 'hospital'} />
}

export function NewClaimPage({ user }: { user: User }) {
  return (
    <div className="space-y-3">
      <h1 className="text-lg font-semibold">
        {user.kind === 'hospital' ? 'New cashless pre-auth' : 'Claim reimbursement'}
      </h1>
      {user.kind === 'hospital' ? <PreauthForm key={user.id} user={user} /> : <ClaimForm key={user.id} user={user} />}
    </div>
  )
}
