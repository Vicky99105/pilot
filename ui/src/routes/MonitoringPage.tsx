import type { User } from '../lib/api'
import { MonitoringList } from '../components/monitoring/MonitoringList'

export function MonitoringPage({ user }: { user: User }) {
  return <MonitoringList userId={user.id} />
}
