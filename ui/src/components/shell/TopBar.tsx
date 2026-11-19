import type { User } from '../../lib/api'
import { href } from '../../lib/router'
import { isStaff, SEEDED_USERS } from '../../lib/users'

export function TopBar({
  userId,
  user,
  path,
  onSwitch,
}: {
  userId: string
  user: User | null
  path: string
  onSwitch: (id: string) => void
}) {
  const staff = user ? isStaff(user.roles) : false
  const links = !user
    ? []
    : staff
      ? [
          { to: '/review', label: 'Review' },
          { to: '/monitoring', label: 'Monitoring' },
        ]
      : [
          { to: '/submit', label: 'My claims' },
          { to: '/submit/new', label: 'New claim' },
        ]

  return (
    <header className="border-b border-line bg-surface">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-2.5">
        <a href={href('/')} className="text-base font-semibold whitespace-nowrap">
          Claims Pilot
        </a>
        <nav aria-label="Main" className="flex flex-1 gap-1">
          {links.map((l) => {
            const active = path === l.to || (l.to !== '/submit' && path.startsWith(`${l.to}/`))
            return (
              <a
                key={l.to}
                href={href(l.to)}
                aria-current={active ? 'page' : undefined}
                className={`rounded-md px-2.5 py-1 text-sm ${
                  active ? 'bg-accent-soft font-medium text-accent' : 'text-muted hover:text-ink'
                }`}
              >
                {l.label}
              </a>
            )
          })}
        </nav>
        <div className="flex items-center gap-2">
          <label htmlFor="user-picker" className="text-xs text-muted">
            Acting as
          </label>
          <select
            id="user-picker"
            className="max-w-[16rem] rounded-md border border-line bg-surface px-2 py-1 text-sm"
            value={userId}
            onChange={(e) => onSwitch(e.target.value)}
          >
            {SEEDED_USERS.map((u) => (
              <option key={u.id} value={u.id}>
                {u.label}
              </option>
            ))}
          </select>
        </div>
      </div>
    </header>
  )
}
