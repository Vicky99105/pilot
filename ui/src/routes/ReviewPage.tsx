import type { User } from '../lib/api'
import { Queue } from '../components/review/Queue'
import { TaskScreen } from '../components/review/TaskScreen'

export function ReviewPage({ user }: { user: User }) {
  return <Queue user={user} />
}

export function TaskPage({ taskId, user }: { taskId: string; user: User }) {
  return <TaskScreen key={taskId} taskId={taskId} user={user} />
}
