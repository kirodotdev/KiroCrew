/** Kanban board API client — thin fetch wrappers over /api/apps/kanban/* */

import type { TaskRecord, TaskStatus } from './types'

const BASE = '/api/apps/kanban'

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw await apiError(res)
  return res.json()
}

/**
 * Turn a non-2xx response into an Error carrying the message a user should read.
 *
 * Every kanban endpoint answers a refusal with `{error, code}`, so the `error`
 * string is the sentence to show. Raising the raw body instead put wire format
 * (`409: {"error": ..., "code": ...}`) straight into the board's alert. The
 * status is the fallback for a body that is not that shape -- a proxy's HTML
 * error page, or a crash before the handler ran.
 */
async function apiError(res: Response): Promise<Error> {
  const body = await res.text().catch(() => '')
  try {
    const parsed = JSON.parse(body)
    if (parsed && typeof parsed.error === 'string' && parsed.error) {
      return new Error(parsed.error)
    }
  } catch {
    // Not JSON — fall through to the status line.
  }
  return new Error(`${res.status}${body ? `: ${body}` : ''}`)
}

// ── Reconcile (settle stale running tasks on page load) ──

export async function reconcileTasks(): Promise<{ reconciled: number }> {
  const res = await fetch(`${BASE}/reconcile`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  })
  return json(res)
}

// ── List ──

export async function fetchTasks(): Promise<{ tasks: TaskRecord[]; total: number }> {
  const res = await fetch(`${BASE}/tasks`, { credentials: 'same-origin' })
  return json(res)
}

// ── Create ──

/**
 * Create a task. Pass `prompt` alone and the backend creates the card at once
 * with a provisional title, then names it in the background — the create call
 * never waits on a model.
 */
export async function createTask(input: {
  title?: string
  description?: string
  prompt?: string
  status?: TaskStatus
  tags?: string[]
  priority?: string
}): Promise<TaskRecord> {
  const res = await fetch(`${BASE}/tasks`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify(input),
  })
  return json(res)
}

// ── Update ──

export async function updateTask(
  id: string,
  patch: Partial<Pick<TaskRecord, 'title' | 'description' | 'prompt' | 'tags' | 'priority'>>,
): Promise<TaskRecord> {
  const res = await fetch(`${BASE}/tasks/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify(patch),
  })
  return json(res)
}

// ── Delete ──

export async function deleteTask(id: string): Promise<void> {
  const res = await fetch(`${BASE}/tasks/${id}`, {
    method: 'DELETE',
    credentials: 'same-origin',
  })
  if (!res.ok) throw await apiError(res)
}

// ── Move (change column) ──

export async function moveTask(id: string, status: TaskStatus): Promise<TaskRecord> {
  const res = await fetch(`${BASE}/tasks/${id}/move`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ status }),
  })
  return json(res)
}

// ── Run (trigger execution) ──

export async function runTask(id: string): Promise<{
  execution_id: string
  session_key: string | null
  status: string
}> {
  const res = await fetch(`${BASE}/tasks/${id}/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: '{}',
  })
  return json(res)
}
