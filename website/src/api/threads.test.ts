/**
 * `threadsApi` -- the three calls behind a crewmate chat's reply threads. The
 * contract is the URL each one hits (slot and mid URL-encoded), the body the
 * reply carries, and that a non-2xx answer surfaces as an `ApiError` carrying
 * the backend's `code`, which the panel maps to its plain sentences.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// `client.ts` installs the blessed transport at module load; `threads.ts` only
// resolves it at call time.
import './client'
import { ApiError } from './apiError'
import { threadQueryKey, threadsApi, threadsQueryKey } from './threads'

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

describe('threadsApi', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch')
  })

  afterEach(() => {
    fetchSpy.mockRestore()
  })

  it('summary GETs the per-slot counts with the slot encoded', async () => {
    const threads = { 'm-1': { count: 2, last_reply_ts: 't', participants: ['user', 'assistant'] } }
    fetchSpy.mockResolvedValueOnce(json({ threads }))
    const out = await threadsApi.summary('member-radar/x')
    const [url] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/chat/threads?slot=member-radar%2Fx')
    expect(out.threads['m-1'].count).toBe(2)
  })

  it('detail GETs one thread by mid, both parts encoded', async () => {
    fetchSpy.mockResolvedValueOnce(json({ parent: { mid: 'm/1', role: 'assistant', content: 'p', ts: 't' }, replies: [], in_flight: false }))
    const out = await threadsApi.detail('member-radar', 'm/1')
    const [url] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/chat/threads/m%2F1?slot=member-radar')
    expect(out.in_flight).toBe(false)
    expect(out.replies).toEqual([])
  })

  it('reply POSTs the slot and text and returns the stored row with its run id', async () => {
    const reply = { id: 'r1', role: 'user', content: 'hi', ts: 't' }
    fetchSpy.mockResolvedValueOnce(json({ reply, run_id: 'run-1' }, 202))
    const out = await threadsApi.reply('member-radar', 'm-1', 'hi')
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/chat/threads/m-1/reply')
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual({ slot_key: 'member-radar', text: 'hi' })
    expect(out).toEqual({ reply, run_id: 'run-1' })
  })

  it('reply carries the client reply id when given, so a re-send is idempotent', async () => {
    const reply = { id: 'ab'.repeat(16), role: 'user', content: 'hi', ts: 't' }
    fetchSpy.mockResolvedValueOnce(json({ reply, run_id: '', duplicate: true }, 202))
    const out = await threadsApi.reply('member-radar', 'm-1', 'hi', 'ab'.repeat(16))
    const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(JSON.parse(String(init.body))).toEqual({ slot_key: 'member-radar', text: 'hi', reply_id: 'ab'.repeat(16) })
    expect(out.duplicate).toBe(true)
  })

  it('a refusal surfaces as an ApiError carrying the backend code', async () => {
    fetchSpy.mockResolvedValueOnce(json({ error: 'thread is full', code: 'thread_full' }, 409))
    const err = await threadsApi.reply('member-radar', 'm-1', 'hi').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(409)
    expect(String((err as ApiError).body)).toContain('thread_full')
  })

  it('query keys are stable per slot and per thread', () => {
    expect(threadsQueryKey('s')).toEqual(['chat-threads', 's'])
    expect(threadQueryKey('s', 'm')).toEqual(['chat-thread', 's', 'm'])
  })
})
