/**
 * The App SDK's `useBrowserMedia`: the owner's media buttons reach the app's own
 * browsing tab through the one declared path, and say why when they cannot.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { AppApiProvider, BROWSER_MEDIA_PATH, useBrowserMedia } from '../app-sdk/index'

const PLAYING = { kind: 'video', paused: false, ended: false, currentTime: 12, duration: 200, volume: 1, muted: false }

let bodies: Record<string, unknown>[]
let answer: () => Response

beforeEach(() => {
  bodies = []
  answer = () => new Response(JSON.stringify({ ok: true, session_key: 'app-tunes-1', media: PLAYING }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost')
    expect(url.pathname).toBe(BROWSER_MEDIA_PATH)
    const method = (init?.method ?? 'GET').toUpperCase()
    bodies.push(method === 'GET'
      ? { GET: Object.fromEntries(url.searchParams) }
      : { [method]: JSON.parse(String(init?.body ?? '{}')) })
    return answer()
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function mount(api: string[]) {
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(AppApiProvider, { appName: 'tunes', allowedApiPaths: api, allowedEvents: [] }, children)
  return renderHook(() => useBrowserMedia('app-tunes-1'), { wrapper })
}

describe('useBrowserMedia', () => {
  it('reads the state with a GET, then posts each button as one action', async () => {
    const { result } = mount([BROWSER_MEDIA_PATH])
    await waitFor(() => expect(result.current.media).toEqual(PLAYING))
    await act(async () => { await result.current.pause() })
    await act(async () => { await result.current.seek(30) })
    await act(async () => { await result.current.setVolume(0.4) })
    expect(bodies).toEqual([
      { GET: { session_key: 'app-tunes-1' } },
      { POST: { session_key: 'app-tunes-1', action: 'pause' } },
      { POST: { session_key: 'app-tunes-1', action: 'seek', value: 30 } },
      { POST: { session_key: 'app-tunes-1', action: 'volume', value: 0.4 } },
    ])
  })

  it('calls onEnded once per ending, however many polls see it', async () => {
    let state = { ...PLAYING }
    answer = () => new Response(JSON.stringify({ ok: true, session_key: 'app-tunes-1', media: state }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    const ended: number[] = []
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(AppApiProvider, { appName: 'tunes', allowedApiPaths: [BROWSER_MEDIA_PATH], allowedEvents: [] }, children)
    const { result } = renderHook(
      () => useBrowserMedia('app-tunes-1', { onEnded: m => ended.push(m.currentTime) }),
      { wrapper },
    )
    await waitFor(() => expect(result.current.media).toEqual(PLAYING))
    expect(ended).toEqual([])
    state = { ...PLAYING, paused: true, ended: true, currentTime: 200 }
    await act(async () => { await result.current.refresh() })
    await act(async () => { await result.current.refresh() })
    expect(ended).toEqual([200])
    // The next song plays and ends: a second call.
    state = { ...PLAYING, currentTime: 1 }
    await act(async () => { await result.current.refresh() })
    state = { ...PLAYING, paused: true, ended: true, currentTime: 180 }
    await act(async () => { await result.current.refresh() })
    expect(ended).toEqual([200, 180])
  })

  it('reports the route refusal code', async () => {
    answer = () => new Response(JSON.stringify({ error: 'no tab', code: 'no_tab' }), { status: 409 })
    const { result } = mount([BROWSER_MEDIA_PATH])
    await waitFor(() => expect(result.current.error).toBe('no_tab'))
    expect(result.current.media).toBeNull()
  })

  it('sends nothing for an app that did not declare the path', async () => {
    const { result } = mount([])
    await waitFor(() => expect(result.current.error).toBe('not_declared'))
    expect(bodies).toEqual([])
  })
})

function gets(): number {
  return bodies.filter(b => 'GET' in b).length
}

describe('useBrowserMedia polling', () => {
  afterEach(() => { vi.useRealTimers() })

  function mountPoll(pollMs: number, sessionKey = 'app-tunes-1') {
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(AppApiProvider, { appName: 'tunes', allowedApiPaths: [BROWSER_MEDIA_PATH], allowedEvents: [] }, children)
    return renderHook(({ key }: { key: string }) => useBrowserMedia(key, { pollMs }), {
      wrapper,
      initialProps: { key: sessionKey },
    })
  }

  it('keeps one state read in flight while the route is slow, then resumes', async () => {
    vi.useFakeTimers()
    const resolvers: ((r: Response) => void)[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), 'http://localhost')
      bodies.push({ GET: Object.fromEntries(url.searchParams) })
      return new Promise<Response>(res => { resolvers.push(res) })
    }))
    mountPoll(100)
    // The first read is out; ten seconds of ticks add nothing while it hangs.
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(gets()).toBe(1)
    // Let it answer: the next tick is free to send again.
    await act(async () => {
      resolvers[0](new Response(JSON.stringify({ ok: true, session_key: 'app-tunes-1', media: PLAYING }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    expect(gets()).toBe(2)
  })

  it('polls once per interval when the route answers (unchanged count)', async () => {
    vi.useFakeTimers()
    mountPoll(100)
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    // The mount read, then one per 100 ms across the second.
    expect(gets()).toBe(11)
  })

  it('starts a fresh read for a new session key even while the old read hangs', async () => {
    vi.useFakeTimers()
    const seen: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), 'http://localhost')
      seen.push(url.searchParams.get('session_key') ?? '')
      return new Promise<Response>(() => {})
    }))
    const { rerender } = mountPoll(100, 'app-tunes-1')
    await act(async () => {})
    expect(seen).toEqual(['app-tunes-1'])
    await act(async () => { rerender({ key: 'app-tunes-2' }) })
    expect(seen).toEqual(['app-tunes-1', 'app-tunes-2'])
  })

  it('resumes polling on the next tick after a read fails', async () => {
    vi.useFakeTimers()
    // The route answers, but with a refusal: `send` catches it, so `.finally` must still
    // clear the in-flight flag and let the next tick read again.
    answer = () => new Response(JSON.stringify({ error: 'no tab', code: 'no_tab' }), { status: 409 })
    mountPoll(100)
    await act(async () => { await vi.advanceTimersByTimeAsync(300) })
    // The mount read, then one per 100 ms: polling did not wedge after the failure.
    expect(gets()).toBe(4)
  })

  it('still sends a refresh while a poll read is in flight', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), 'http://localhost')
      bodies.push({ GET: Object.fromEntries(url.searchParams) })
      return new Promise<Response>(() => {})
    }))
    const { result } = mountPoll(100)
    await act(async () => {})
    expect(gets()).toBe(1)
    await act(async () => { void result.current.refresh() })
    expect(gets()).toBe(2)
  })
})
