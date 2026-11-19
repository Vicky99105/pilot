import { useEffect, useState, type ReactNode } from 'react'
import { api, setApiUser, type User } from '../lib/api'
import { SEEDED_USERS } from '../lib/users'
import { CurrentUserContext } from './useCurrentUser'

const STORAGE_KEY = 'claims-pilot.user-id'

function storedUserId(): string {
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY)
    if (saved && SEEDED_USERS.some((u) => u.id === saved)) return saved
  } catch {
    // Storage can be blocked; fall back to the first seeded user.
  }
  return SEEDED_USERS[0].id
}

export function CurrentUserProvider({ children }: { children: ReactNode }) {
  const [userId, setUserId] = useState(() => {
    const id = storedUserId()
    setApiUser(id) // before any child effect sends a request
    return id
  })
  const [loaded, setLoaded] = useState<{ id: string; user: User | null; error: string | null }>({
    id: '',
    user: null,
    error: null,
  })

  useEffect(() => {
    const run = { live: true }
    api
      .me()
      .then((user) => run.live && setLoaded({ id: userId, user, error: null }))
      .catch((e: unknown) => run.live && setLoaded({ id: userId, user: null, error: String(e) }))
    return () => {
      run.live = false
    }
  }, [userId])

  const switchUser = (id: string) => {
    setApiUser(id)
    try {
      window.localStorage.setItem(STORAGE_KEY, id)
    } catch {
      // Not remembered; the choice still holds for this tab.
    }
    setUserId(id)
  }

  const current = loaded.id === userId ? loaded : { user: null, error: null }
  return (
    <CurrentUserContext.Provider value={{ userId, user: current.user, error: current.error, switchUser }}>
      {children}
    </CurrentUserContext.Provider>
  )
}
