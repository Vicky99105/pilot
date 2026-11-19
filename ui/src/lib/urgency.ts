import type { Task } from './api'

const DEFAULT_SLA_MS = 4 * 60 * 60 * 1000 // the workflow's checkpoint SLA

/** Share of the task's time still left: 1 when just opened, 0 or less when overdue. */
export function timeLeftShare(task: Task, now: number): number {
  const deadline = Date.parse(task.deadline)
  const opened = task.created_at ? Date.parse(task.created_at) : deadline - DEFAULT_SLA_MS
  const total = Math.max(deadline - opened, 1)
  return (deadline - now) / total
}

/** Red under a quarter of the time left, amber under half, green otherwise. */
export function urgencyTone(share: number): 'danger' | 'warn' | 'ok' {
  return share < 0.25 ? 'danger' : share < 0.5 ? 'warn' : 'ok'
}
