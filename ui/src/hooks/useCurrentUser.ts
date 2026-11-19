import { createContext, useContext } from 'react'
import type { User } from '../lib/api'

export type CurrentUser = {
  userId: string
  user: User | null
  error: string | null
  switchUser: (id: string) => void
}

export const CurrentUserContext = createContext<CurrentUser | null>(null)

export function useCurrentUser(): CurrentUser {
  const value = useContext(CurrentUserContext)
  if (!value) throw new Error('useCurrentUser needs CurrentUserProvider')
  return value
}
