import type { User } from '../lib/api'
import { href } from '../lib/router'
import { isSupervisor, queueRoles } from '../lib/users'
import { Notice } from '../components/ui/primitives'
import { Queue } from '../components/review/Queue'
import { TaskScreen } from '../components/review/TaskScreen'

export function ReviewPage({ user }: { user: User }) {
  if (queueRoles(user.roles).length === 0 && isSupervisor(user)) {
    return (
      <Notice tone="accent">
        You work no task queue. Watch every queue from the{' '}
        <a className="font-medium underline" href={href('/supervisor')}>
          supervisor view
        </a>
        .
      </Notice>
    )
  }
  return <Queue user={user} />
}

export function TaskPage({ taskId, user }: { taskId: string; user: User }) {
  return <TaskScreen key={taskId} taskId={taskId} user={user} />
}
