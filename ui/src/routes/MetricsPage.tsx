import type { User } from '../lib/api'
import { MetricsView } from '../components/metrics/MetricsView'

export function MetricsPage({ user }: { user: User }) {
  return <MetricsView userId={user.id} />
}
