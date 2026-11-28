/**
 * The App SDK's `useBrowserMedia`: the owner's media buttons reach the app's own
 * browsing tab through the one declared path, and say why when they cannot.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { AppApiProvider, BROWSER_MEDIA_PATH, useBrowserMedia } from '../app-sdk/index'

const PLAYING = { kind: 'video', paused: false, ended: false, endId: null, currentTime: 12, duration: 200, volume: 1, muted: false }

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

  /** Mounts the hook with an `onEnded` that records each call's `currentTime`, and a
   *  route that answers whatever `setState` last set. */
  async function mountEnded(first: Record<string, unknown>) {
    let state = first
    answer = () => new Response(JSON.stringify({ ok: true, session_key: 'app-tunes-1', media: state }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    const ended: number[] = []
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(AppApiProvider, { appName: 'tunes', allowedApiPaths: [BROWSER_MEDIA_PATH], allowedEvents: [] }, children)
    const { result } = renderHook(
      () => useBrowserMedia('app-tunes-1', { onEnded: m => ended.push(m.currentTime) }),
      { wrapper },
    )
    await waitFor(() => expect(result.current.media).toEqual(first))
    const read = async (next: Record<string, unknown>) => {
      state = next
      await act(async () => { await result.current.refresh() })
    }
    return { ended, read }
  }

  it('calls onEnded once per ending the tab counted, however many polls see it', async () => {
    const { ended, read } = await mountEnded({ ...PLAYING })
    expect(ended).toEqual([])
    await read({ ...PLAYING, paused: true, ended: true, endId: 'd0c:1', currentTime: 200 })
    await read({ ...PLAYING, paused: true, ended: true, endId: 'd0c:1', currentTime: 200 })
    expect(ended).toEqual([200])
    // The next song plays and ends: a second call.
    await read({ ...PLAYING, endId: 'd0c:1', currentTime: 1 })
    await read({ ...PLAYING, paused: true, ended: true, endId: 'd0c:2', currentTime: 180 })
    expect(ended).toEqual([200, 180])
    // A navigation counts again from one under another document's id.
    await read({ ...PLAYING, currentTime: 3 })
    await read({ ...PLAYING, paused: true, ended: true, endId: 'e1f:1', currentTime: 90 })
    expect(ended).toEqual([200, 180, 90])
  })

  it('calls onEnded when the site took the source away at the end', async () => {
    // YouTube's element after a song: paused at 0, no duration, never `ended`
    // itself. The tab counted the ending as it happened.
    const { ended, read } = await mountEnded({ ...PLAYING })
    await read({ ...PLAYING, paused: true, ended: false, endId: 'd0c:1', currentTime: 0, duration: null })
    await read({ ...PLAYING, paused: true, ended: false, endId: 'd0c:1', currentTime: 0, duration: null })
    expect(ended).toEqual([0])
  })

  it('does not call onEnded for an element the tab did not count as ending', async () => {
    // An advert ending on the same page before the song: `ended` for a moment,
    // dropped by the tab once the song plays, so no `endId` moves.
    const { ended, read } = await mountEnded({ ...PLAYING })
    await read({ ...PLAYING, paused: true, ended: true, currentTime: 15, duration: 15 })
    await read({ ...PLAYING, currentTime: 1 })
    expect(ended).toEqual([])
  })

  it('calls onEnded once for media already ended when the page mounts', async () => {
    const { ended, read } = await mountEnded({ ...PLAYING, paused: true, ended: true, endId: 'd0c:4', currentTime: 200 })
    await read({ ...PLAYING, paused: true, ended: true, endId: 'd0c:4', currentTime: 200 })
    expect(ended).toEqual([200])
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

  function mountPoll(pollMs: unknown, sessionKey = 'app-tunes-1') {
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(AppApiProvider, { appName: 'tunes', allowedApiPaths: [BROWSER_MEDIA_PATH], allowedEvents: [] }, children)
    return renderHook(({ key }: { key: string }) => useBrowserMedia(key, { pollMs: pollMs as number }), {
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
    mountPoll(500)
    // The first read is out; ten seconds of ticks add nothing while it hangs.
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(gets()).toBe(1)
    // Let it answer: the next tick is free to send again.
    await act(async () => {
      resolvers[0](new Response(JSON.stringify({ ok: true, session_key: 'app-tunes-1', media: PLAYING }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(500) })
    expect(gets()).toBe(2)
  })

  it('polls once per interval when the route answers (unchanged count)', async () => {
    vi.useFakeTimers()
    mountPoll(500)
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000) })
    // The mount read, then one per 500 ms across five seconds.
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
    const { rerender } = mountPoll(500, 'app-tunes-1')
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
    mountPoll(500)
    await act(async () => { await vi.advanceTimersByTimeAsync(1_500) })
    // The mount read, then one per 500 ms: polling did not wedge after the failure.
    expect(gets()).toBe(4)
  })

  it('still sends a refresh while a poll read is in flight', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), 'http://localhost')
      bodies.push({ GET: Object.fromEntries(url.searchParams) })
      return new Promise<Response>(() => {})
    }))
    const { result } = mountPoll(500)
    await act(async () => {})
    expect(gets()).toBe(1)
    await act(async () => { void result.current.refresh() })
    expect(gets()).toBe(2)
  })

  it.each([
    [1000, 10_000, 11],
    [2000, 10_000, 6],
    ['1000', 10_000, 11],
  ])('polls pollMs %s as before (%s ms -> %s reads)', async (pollMs, ms, count) => {
    vi.useFakeTimers()
    mountPoll(pollMs)
    await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
    expect(gets()).toBe(count)
  })

  it('raises a pollMs below 500 to 500', async () => {
    vi.useFakeTimers()
    mountPoll(100)
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    // The mount read, then 500 and 1000 ms: not one per 100 ms.
    expect(gets()).toBe(3)
  })

  it.each([NaN, Infinity, -1, 0, 'soon'])('reads once for pollMs %s', async pollMs => {
    vi.useFakeTimers()
    mountPoll(pollMs)
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(gets()).toBe(1)
  })
})
