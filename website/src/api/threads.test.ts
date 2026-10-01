/**
 * `threadsApi` -- the three calls behind threads on a chat's messages. The
 * contract is the URL each one hits (slot and mid URL-encoded), the body `open`
 * carries, and that a non-2xx answer surfaces as an `ApiError` carrying the
 * backend's `code`.
 *
 * There is deliberately no call here that posts a thread's MESSAGE: a thread is
 * an ordinary chat session now, so its turns go through the ordinary chat
 * transport on the thread's own slot.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// `client.ts` installs the blessed transport at module load; `threads.ts` only
// resolves it at call time.
import './client'
import { ApiError } from './apiError'
import {
  ANCHOR_IN_FLIGHT,
  isLegacyThread,
  isSessionThread,
  threadQueryKey,
  threadsApi,
  threadsQueryKey,
} from './threads'

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

const LIVE = {
  kind: 'session' as const,
  thread_slot: 'chat-77-1758524400',
  title: 'The other eight',
  opened_by: 'user',
  opened_at: 't',
  closed_at: null,
  summary_mid: null,
}

describe('threadsApi', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch')
  })

  afterEach(() => {
    fetchSpy.mockRestore()
  })

  it('summary GETs the per-slot anchors with the slot encoded', async () => {
    fetchSpy.mockResolvedValueOnce(json({ threads: { 'm-1': LIVE } }))
    const out = await threadsApi.summary('member-radar/x')
    const [url] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/chat/threads?slot=member-radar%2Fx')
    const row = out.threads['m-1']
    expect(isSessionThread(row)).toBe(true)
    expect(isSessionThread(row) && row.thread_slot).toBe('chat-77-1758524400')
  })

  it('detail GETs one anchor by mid, both parts encoded', async () => {
    fetchSpy.mockResolvedValueOnce(json({
      parent: { mid: 'm/1', role: 'assistant', content: 'p', ts: 't' },
      anchor: null,
    }))
    const out = await threadsApi.detail('member-radar', 'm/1')
    const [url] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/chat/threads/m%2F1?slot=member-radar')
    expect(out.anchor).toBeNull()
  })

  it('open POSTs the parent slot and answers with the thread slot and its anchor', async () => {
    fetchSpy.mockResolvedValueOnce(json({
      thread_slot: 'chat-90-1758525000',
      anchor: { surface: 'dashboard', conversation: 'member-radar', mid: 'm-1' },
      title: 'Thread: the other eight',
      seeded: true,
    }, 201))
    const out = await threadsApi.open('member-radar', 'm-1')
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/chat/threads/m-1/open')
    expect(init.method).toBe('POST')
    // Only the fields that were given: an empty title is not sent as one, so the
    // backend derives its own from the anchored message.
    expect(JSON.parse(String(init.body))).toEqual({ slot_key: 'member-radar' })
    expect(out.thread_slot).toBe('chat-90-1758525000')
    expect(out.anchor.mid).toBe('m-1')
  })

  it('open carries a title, and nothing this side of the wire does not compose', async () => {
    // `title` is the only option the browser sends. The route also takes `agent`
    // and `note`, which the MCP `thread_open` tool fills; carrying them here would
    // be a parameter no surface on this side sets.
    fetchSpy.mockResolvedValueOnce(json({
      thread_slot: 'c', anchor: { surface: 'dashboard', conversation: 's', mid: 'm-1' }, title: 'T', seeded: true,
    }, 201))
    await threadsApi.open('s', 'm-1', { title: 'T' })
    const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(JSON.parse(String(init.body))).toEqual({ slot_key: 's', title: 'T' })
  })

  it('the streaming sentinel goes in the path where a mid would', async () => {
    // A reply still being written has no mid at all; this is the one segment the
    // UI can send instead, and it cannot collide with a mid (`^m-[0-9a-f]{16}$`).
    fetchSpy.mockResolvedValueOnce(json({
      thread_slot: 'c', anchor: { surface: 'dashboard', conversation: 's', mid: 'm-0000000000000001' }, title: 'T', seeded: true,
    }, 201))
    await threadsApi.open('s', ANCHOR_IN_FLIGHT)
    const [url] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/chat/threads/inflight/open')
    expect(ANCHOR_IN_FLIGHT).not.toMatch(/^m-[0-9a-f]{16}$/)
  })

  it('a refusal surfaces as an ApiError carrying the backend code', async () => {
    fetchSpy.mockResolvedValueOnce(json({ error: 'gone', code: 'parent_not_found' }, 404))
    const err = await threadsApi.open('member-radar', 'm-1').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(404)
    expect(String((err as ApiError).body)).toContain('parent_not_found')
  })

  it('the two thread kinds are told apart by their own discriminator', () => {
    const legacy = { kind: 'legacy' as const, count: 2, last_reply_ts: 't', participants: ['user'] }
    expect(isSessionThread(LIVE)).toBe(true)
    expect(isLegacyThread(LIVE)).toBe(false)
    expect(isLegacyThread(legacy)).toBe(true)
    expect(isSessionThread(legacy)).toBe(false)
    expect(isSessionThread(undefined)).toBe(false)
    expect(isLegacyThread(undefined)).toBe(false)
  })

  it('query keys are stable per slot and per thread', () => {
    expect(threadsQueryKey('s')).toEqual(['chat-threads', 's'])
    expect(threadQueryKey('s', 'm')).toEqual(['chat-thread', 's', 'm'])
  })
})
