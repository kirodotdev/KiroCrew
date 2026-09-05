/**
 * `api.chatSlotNote` — URL/body construction and the typed response.
 *
 * The method is a thin builder, so what can break is exactly what is asserted
 * here: the path (with the slot percent-encoded), which optional body fields are
 * omitted rather than sent as `undefined`, that an explicit `visibleOnly: false`
 * survives a truthiness-guarded spread rather than looking like an omission, and
 * that the response booleans arrive separately — a caller sequences a destructive
 * close on `appended === true`, so a collapsed or `any`-typed response is a
 * correctness bug, not a style one.
 *
 * `maxAge` and `ephemeral` are deliberately NOT part of this options type: no
 * production caller ever passed either. The similarly-named options on
 * `chatSlotContext` are a different method's and are untouched.
 *
 * Conventions follow `ApiClient.coverage.test.tsx`: `vi.stubGlobal('fetch', …)`
 * with a hand-rolled Response stub, assertions read off `fetchMock.mock.calls`.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { api, type ChatSlotNoteResult } from '../api/client'

type Init = RequestInit & { headers?: Record<string, string> }

const NOTE_OK: ChatSlotNoteResult = {
  ok: true,
  appended: true,
  visibleDeferred: false,
  deliveryConditional: false,
  contextSkipped: true,
  pending: 0,
}

function res(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    url: 'http://localhost:6776/api/probe',
    headers: { get: () => null },
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response
}

const fetchMock = vi.fn()

beforeEach(() => {
  fetchMock.mockReset()
  fetchMock.mockResolvedValue(res(200, NOTE_OK))
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function call(n = 0) {
  const [url, init] = fetchMock.mock.calls[n] as [string, Init | undefined]
  return {
    url,
    method: init?.method,
    headers: (init?.headers ?? {}) as Record<string, string>,
    body: typeof init?.body === 'string'
      ? (JSON.parse(init.body) as Record<string, unknown>)
      : undefined,
  }
}

describe('api.chatSlotNote', () => {
  it('POSTs content to the slot note endpoint and omits every unset option', async () => {
    await api.chatSlotNote('chat-1', 'closed by the user')

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const { url, method, headers, body } = call()
    expect(url).toBe('/api/chat/slots/chat-1/note')
    expect(method).toBe('POST')
    expect(headers['Content-Type']).toBe('application/json')
    // Only `content` — an absent option must not be sent as an explicit key,
    // or the backend's "omitted" defaults never apply.
    expect(body).toEqual({ content: 'closed by the user' })
  })

  it('percent-encodes the slot into the path', async () => {
    await api.chatSlotNote('chat/1 #2', 'note')

    expect(call().url).toBe('/api/chat/slots/chat%2F1%20%232/note')
  })

  it('forwards source and visibleOnly when given', async () => {
    // `maxAge` and `ephemeral` used to be forwarded here too. They were removed
    // from `ChatSlotNoteOptions`: zero production callers passed either (the
    // `maxAge`/`ephemeral` uses elsewhere in the app are on `chatSlotContext`, a
    // different method with its own options), so they were surface with no user.
    await api.chatSlotNote('chat-1', 'breadcrumb', {
      source: 'option-actions',
      visibleOnly: true,
    })

    expect(call().body).toEqual({
      content: 'breadcrumb',
      source: 'option-actions',
      visibleOnly: true,
    })
  })

  it('sends visibleOnly: false rather than dropping it', async () => {
    // A `?:` spread guarded on truthiness would silently drop this, leaving the
    // caller's explicit opt-out looking like an omission. The property is
    // unchanged; only `ephemeral` left the options type alongside it.
    await api.chatSlotNote('chat-1', 'note', { visibleOnly: false })

    expect(call().body).toEqual({ content: 'note', visibleOnly: false })
    expect('visibleOnly' in (call().body ?? {})).toBe(true)
  })

  it('drops an empty source so the backend applies its own "note" default', async () => {
    await api.chatSlotNote('chat-1', 'note', { source: '' })

    expect(call().body).toEqual({ content: 'note' })
  })

  it('returns each response flag separately, so a caller can gate a close on appended', async () => {
    const durable = await api.chatSlotNote('chat-1', 'note', { visibleOnly: true })
    expect(durable.appended).toBe(true)
    expect(durable.visibleDeferred).toBe(false)
    expect(durable.contextSkipped).toBe(true)
    expect(durable.pending).toBe(0)

    fetchMock.mockResolvedValue(
      res(200, {
        ok: true,
        appended: false,
        visibleDeferred: true,
        deliveryConditional: true,
        contextSkipped: true,
        pending: 2,
      } satisfies ChatSlotNoteResult),
    )
    const deferred = await api.chatSlotNote('chat-1', 'note', { visibleOnly: true })
    // The whole point of the split: a 200 is NOT proof the row is durable.
    expect(deferred.appended).toBe(false)
    expect(deferred.visibleDeferred).toBe(true)
    expect(deferred.deliveryConditional).toBe(true)
    expect(deferred.pending).toBe(2)
  })

  it('rejects on a non-2xx so a failed note cannot be read as a durable one', async () => {
    fetchMock.mockResolvedValue(res(400, { error: 'content required', code: 'invalid_content' }))

    await expect(api.chatSlotNote('chat-1', '')).rejects.toThrow()
  })
})
