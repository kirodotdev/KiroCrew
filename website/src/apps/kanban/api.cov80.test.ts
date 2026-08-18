/**
 * The Kanban API client's error contract and its query-param assembly.
 *
 * Two things here are easy to get wrong and invisible until a user hits them.
 * First, `json()` is the single place a non-2xx becomes an exception — if it
 * ever returned the parsed body instead, every caller would treat a 500 as a
 * task and render garbage. Second, the list endpoint takes no
 * query params, so a stray one would silently narrow what the board asks for.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  createTask,
  deleteTask,
  fetchTasks,
  moveTask,
  reconcileTasks,
  runTask,
  updateTask,
} from './api'

/** The last fetch call's [url, init], for asserting method/body/params. */
function lastCall(): [string, RequestInit | undefined] {
  const mock = globalThis.fetch as unknown as { mock: { calls: unknown[][] } }
  const call = mock.mock.calls[mock.mock.calls.length - 1]
  return [String(call[0]), call[1] as RequestInit | undefined]
}

function ok(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response
}

function fail(status: number, body = 'boom'): Response {
  return {
    ok: false,
    status,
    json: async () => ({}),
    text: async () => body,
  } as unknown as Response
}

beforeEach(() => {
  globalThis.fetch = vi.fn(async () => ok({})) as unknown as typeof fetch
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('the error contract', () => {
  it('turns a non-2xx into an Error carrying status and body', async () => {
    globalThis.fetch = vi.fn(async () => fail(500, 'kaboom')) as unknown as typeof fetch
    await expect(fetchTasks()).rejects.toThrow('500: kaboom')
  })

  it('still throws when the error body cannot be read', async () => {
    const res = {
      ok: false,
      status: 503,
      text: async () => {
        throw new Error('stream already consumed')
      },
    } as unknown as Response
    globalThis.fetch = vi.fn(async () => res) as unknown as typeof fetch
    await expect(fetchTasks()).rejects.toThrow('503')
  })

  it('deleteTask surfaces the backend sentence like every other verb', async () => {
    // Delete used to raise its own `Delete failed: 404`, so a refusal reached the
    // user in a different voice from the rest of the board -- and never carried
    // the reason the backend gave.
    //
    // Asserted by EQUALITY, not substring: the un-parsed fallback
    // (`404: {"error":"Task not found",...}`) contains the sentence too, so a
    // substring match would pass while the wire wrapper was still on screen.
    globalThis.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ error: 'Task not found', code: 'task_not_found' }), {
          status: 404,
        }),
    ) as unknown as typeof fetch
    const err = await deleteTask('gone').catch((e: unknown) => e as Error)
    expect(err.message).toBe('Task not found')
  })

  it('falls back to the status when the body is not the error shape', async () => {
    // A proxy's HTML page or a crash before the handler leaves nothing to read.
    globalThis.fetch = vi.fn(
      async () => new Response('<html>502</html>', { status: 502 }),
    ) as unknown as typeof fetch
    const err = await deleteTask('x').catch((e: unknown) => e as Error)
    expect(err.message).toBe('502: <html>502</html>')
  })
})

describe('the list endpoint', () => {
  it('asks for the whole board with no query params', async () => {
    globalThis.fetch = vi.fn(async () => ok({ tasks: [], total: 0 })) as unknown as typeof fetch
    await fetchTasks()
    const [url] = lastCall()
    expect(url).toBe('/api/apps/kanban/tasks')
    expect(url).not.toContain('?')
  })
})

describe('the write endpoints', () => {
  it('createTask POSTs the input as JSON', async () => {
    await createTask({ title: 'New' })
    const [url, init] = lastCall()
    expect(url).toBe('/api/apps/kanban/tasks')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual({ title: 'New' })
  })

  it('updateTask PATCHes only the given fields', async () => {
    await updateTask('t1', { title: 'Renamed' })
    const [url, init] = lastCall()
    expect(url).toBe('/api/apps/kanban/tasks/t1')
    expect(init?.method).toBe('PATCH')
    expect(JSON.parse(String(init?.body))).toEqual({ title: 'Renamed' })
  })

  it('moveTask posts the target column', async () => {
    await moveTask('t1', 'done')
    const [url, init] = lastCall()
    expect(url).toBe('/api/apps/kanban/tasks/t1/move')
    expect(JSON.parse(String(init?.body))).toEqual({ status: 'done' })
  })

  it('runTask posts an empty object, not an empty body', async () => {
    await runTask('t1')
    const [url, init] = lastCall()
    expect(url).toBe('/api/apps/kanban/tasks/t1/run')
    expect(init?.body).toBe('{}')
  })

  it('reconcileTasks posts an empty object', async () => {
    globalThis.fetch = vi.fn(async () => ok({ reconciled: 2 })) as unknown as typeof fetch
    await expect(reconcileTasks()).resolves.toEqual({ reconciled: 2 })
    const [url, init] = lastCall()
    expect(url).toBe('/api/apps/kanban/reconcile')
    expect(init?.body).toBe('{}')
  })


  it('deleteTask resolves without a body on success', async () => {
    globalThis.fetch = vi.fn(async () => ok({})) as unknown as typeof fetch
    await expect(deleteTask('t1')).resolves.toBeUndefined()
    const [, init] = lastCall()
    expect(init?.method).toBe('DELETE')
  })

})

describe('credentials', () => {
  it('every request is same-origin, so the dashboard cookie is sent', async () => {
    await createTask({ title: 'x' })
    expect(lastCall()[1]?.credentials).toBe('same-origin')
    await fetchTasks()
    expect(lastCall()[1]?.credentials).toBe('same-origin')
  })
})
