import type { User } from '../lib/api'
import { SupervisorView } from '../components/supervisor/SupervisorView'

export function SupervisorPage({ user }: { user: User }) {
  return <SupervisorView userId={user.id} />
}
