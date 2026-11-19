import type { ReactNode } from 'react'
import type { User } from './lib/api'
import { match, usePath } from './lib/router'
import { isStaff } from './lib/users'
import { CurrentUserProvider } from './hooks/CurrentUserProvider'
import { useCurrentUser } from './hooks/useCurrentUser'
import { TopBar } from './components/shell/TopBar'
import { Empty, Loading, Notice } from './components/ui/primitives'
import { ClaimPage } from './routes/ClaimPage'
import { MonitoringPage } from './routes/MonitoringPage'
import { ReviewPage, TaskPage } from './routes/ReviewPage'
import { MyClaimsPage, NewClaimPage } from './routes/SubmitPage'

function Route({ path, user }: { path: string; user: User }): ReactNode {
  const staff = isStaff(user.roles)
  let params: Record<string, string> | null

  if ((params = match('/claims/:id', path))) return <ClaimPage claimId={params.id} user={user} />
  if (staff) {
    if ((params = match('/tasks/:id', path))) return <TaskPage taskId={params.id} user={user} />
    if (match('/monitoring', path)) return <MonitoringPage user={user} />
    if (match('/review', path) || path === '/') return <ReviewPage user={user} />
  } else {
    if (match('/submit/new', path)) return <NewClaimPage user={user} />
    if (match('/submit', path) || path === '/') return <MyClaimsPage user={user} />
  }
  return (
    <Empty>
      This page is not available to {user.name}. <a className="text-accent hover:underline" href="#/">Go to your start page</a>.
    </Empty>
  )
}

function Shell() {
  const { userId, user, error, switchUser } = useCurrentUser()
  const path = usePath()
  return (
    <div className="min-h-screen">
      <TopBar userId={userId} user={user} path={path} onSwitch={switchUser} />
      <main className="mx-auto max-w-7xl px-4 py-4">
        {error && <Notice tone="danger">Could not load the selected user: {error}</Notice>}
        {!user && !error && <Loading />}
        {user && <Route key={user.id} path={path} user={user} />}
      </main>
    </div>
  )
}

export default function App() {
  return (
    <CurrentUserProvider>
      <Shell />
    </CurrentUserProvider>
  )
}
