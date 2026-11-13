/**
 * The app SDK's hooks against a stubbed host context: the subscription pre-check
 * and its console diagnostics (logged once per app and event), the theme read
 * from the document and its live update, the navigation badge event, and the
 * app's own notification list.
 */
import { createElement, type ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { useAppEvents, useAppNotifications, useNavBadge, useTheme } from './index'

const ctx = vi.hoisted(() => ({ current: {} as Record<string, unknown> }))

vi.mock('./scopedApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./scopedApi')>()),
  useCtx: () => ctx.current,
}))

function setHost(name: string, events: string[], subscribe = vi.fn(() => vi.fn())) {
  ctx.current = {
    info: { name, permissions: { events } },
    subscribe,
    navigate: vi.fn(),
    notify: vi.fn(),
  }
  return subscribe
}

afterEach(() => {
  vi.restoreAllMocks()
  delete document.documentElement.dataset.theme
  delete document.documentElement.dataset.colorTheme
})

describe('useAppEvents', () => {
  let info: MockInstance<typeof console.info>
  let warn: MockInstance<typeof console.warn>
  beforeEach(() => {
    info = vi.spyOn(console, 'info').mockImplementation(() => {})
    warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
  })

  it('logs the own-slot hint once per app and event, however often it remounts', () => {
    const subscribe = setHost('cov-own-only', [])
    const first = renderHook(() => useAppEvents('chat_chunk', vi.fn()))
    first.unmount()
    renderHook(() => useAppEvents('chat_chunk', vi.fn()))
    expect(info).toHaveBeenCalledTimes(1)
    expect(info.mock.calls[0][0]).toContain('"chat_chunk" (own-slot only)')
    expect(subscribe).toHaveBeenCalledWith('chat_chunk', expect.any(Function))
  })

  it('warns that the manifest denies a known event that is not declared', () => {
    setHost('cov-denied', [])
    renderHook(() => useAppEvents('artifact_update', vi.fn()))
    expect(warn).toHaveBeenCalledTimes(1)
    expect(warn.mock.calls[0][0]).toContain('the manifest denies it')
  })

  it('warns that an unrecognised event name is not in the gateway table', () => {
    setHost('cov-unknown', ['*'])
    renderHook(() => useAppEvents('no_such_event', vi.fn()))
    expect(warn).not.toHaveBeenCalled()
    setHost('cov-unknown-2', [])
    renderHook(() => useAppEvents('no_such_event', vi.fn()))
    expect(warn.mock.calls[0][0]).toContain('"no_such_event" is not recognized')
  })

  it('forwards events to the latest callback and unsubscribes on unmount', () => {
    const unsubscribe = vi.fn()
    const subscribe = vi.fn((_event: string, _cb: (data: unknown) => void) => unsubscribe)
    setHost('cov-forward', ['*'], subscribe)
    const early = vi.fn()
    const late = vi.fn()
    const { rerender, unmount } = renderHook(({ cb }) => useAppEvents('notification', cb), {
      initialProps: { cb: early },
    })
    rerender({ cb: late })
    const registered = subscribe.mock.calls[0][1]
    registered({ id: 7 })
    expect(early).not.toHaveBeenCalled()
    expect(late).toHaveBeenCalledWith({ id: 7 })
    unmount()
    expect(unsubscribe).toHaveBeenCalledTimes(1)
  })
})

describe('useTheme', () => {
  it('reads the mode and colour theme from the document root', () => {
    setHost('cov-theme', [])
    document.documentElement.dataset.theme = 'light'
    document.documentElement.dataset.colorTheme = 'ocean'
    const { result, unmount } = renderHook(() => useTheme())
    expect(result.current.mode).toBe('light')
    expect(result.current.colorTheme).toBe('ocean')
    unmount()
  })

  it('falls back to dark and the default colour theme when the root carries none', () => {
    setHost('cov-theme-default', [])
    const { result, unmount } = renderHook(() => useTheme())
    expect(result.current.mode).toBe('dark')
    expect(result.current.colorTheme).toBe('default')
    unmount()
  })

  it('re-reads the theme when the root attribute changes', async () => {
    setHost('cov-theme-live', [])
    document.documentElement.dataset.theme = 'light'
    const { result, unmount } = renderHook(() => useTheme())
    act(() => { document.documentElement.dataset.theme = 'dark' })
    await waitFor(() => expect(result.current.mode).toBe('dark'))
    unmount()
  })
})

describe('useNavBadge', () => {
  it('dispatches the app badge count on the window', () => {
    setHost('cov-badge', [])
    const seen: unknown[] = []
    const listener = (event: Event) => seen.push((event as CustomEvent).detail)
    window.addEventListener('mc:app:badge', listener)
    try {
      const { result } = renderHook(() => useNavBadge())
      result.current(4)
      result.current(0)
    } finally {
      window.removeEventListener('mc:app:badge', listener)
    }
    expect(seen).toEqual([
      { appName: 'cov-badge', count: 4 },
      { appName: 'cov-badge', count: 0 },
    ])
  })
})

describe('useAppNotifications', () => {
  it("reads this app's own notes and acks one, then reads them again", async () => {
    setHost('cov-notes', [])
    const unread = [{ ts: 't1', channel: 'cov-notes.updates', title: 'a', body: 'b' }]
    const read = [{ ...unread[0], acked: true }]
    const get = vi.fn()
      .mockResolvedValue({ notifications: read, unread: 0 })
      .mockResolvedValueOnce({ notifications: unread, unread: 1 })
    const post = vi.fn().mockResolvedValue({ ok: true })
    ctx.current.api = { get, post }
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children)

    const { result } = renderHook(() => useAppNotifications(), { wrapper })
    expect(result.current.status).toBe('loading')
    await waitFor(() => expect(result.current.unread).toBe(1))
    expect(result.current.status).toBe('ready')
    expect(result.current.items).toEqual(unread)
    expect(get).toHaveBeenCalledWith('/api/notifications/own?app=cov-notes')

    await act(() => result.current.ack('t1'))
    expect(post).toHaveBeenCalledWith('/api/notifications/own/ack', { app: 'cov-notes', ts: 't1' })
    await waitFor(() => expect(result.current.unread).toBe(0))
    expect(result.current.items).toEqual(read)
    client.clear()
  })

  it('acks each unread note it holds through ack, past one already gone, then reads once', async () => {
    setHost('cov-notes', [])
    const held = [
      { ts: 't1', channel: 'cov-notes.updates', title: 'a', body: 'b', acked: true },
      { ts: 't2', channel: 'cov-notes.updates', title: 'c', body: 'd' },
      { ts: 't3', channel: 'cov-notes.updates', title: 'e', body: 'f' },
    ]
    const get = vi.fn().mockResolvedValue({ notifications: held, unread: 2 })
    // t2 left the bell after the page read the list: the host answers 404 for it.
    const gone = Object.assign(new Error('API 404: no such notification'), { status: 404, body: '' })
    const post = vi.fn().mockRejectedValueOnce(gone).mockResolvedValue({ ok: true })
    ctx.current.api = { get, post }
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children)

    const { result } = renderHook(() => useAppNotifications(), { wrapper })
    await waitFor(() => expect(result.current.unread).toBe(2))
    await act(() => result.current.ackAll())
    expect(post.mock.calls).toEqual([
      ['/api/notifications/own/ack', { app: 'cov-notes', ts: 't2' }],
      ['/api/notifications/own/ack', { app: 'cov-notes', ts: 't3' }],
    ])
    expect(get).toHaveBeenCalledTimes(2)
    client.clear()
  })
})
