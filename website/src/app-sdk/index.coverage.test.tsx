/**
 * The app SDK's hooks against a stubbed host context: the subscription pre-check
 * and its console diagnostics (logged once per app and event), the theme read
 * from the document and its live update, and the navigation badge event.
 */
import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { useAppEvents, useNavBadge, useTheme } from './index'

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
