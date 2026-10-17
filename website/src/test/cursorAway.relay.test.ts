/**
 * The embedded-pane half of the off-window cursor relay.
 *
 * A remote instance pane is a cross-origin iframe with no preload, so it cannot
 * ask the Electron main process how far the cursor went. It asks its host frame
 * over postMessage instead (`mc-cursor-away-watch`), and the host answers once
 * (`mc-cursor-away`). A host that never answers — an older host, a plain browser
 * parent, a background pane — leaves the surface up.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useHoverIntent, HOVER_OPEN_MS, HOVER_MIN_VISIBLE_MS } from '../hooks/useHoverIntent'

const LONG_MS = 10_000

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => true) }))
const HOST_ORIGIN = 'http://127.0.0.1:5476'
vi.mock('../lib/nativeNotify', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/nativeNotify')>()),
  relayTargetOrigin: vi.fn(() => HOST_ORIGIN),
}))

type Posted = { type: string; v: number; id: string }

describe('useHoverIntent — embedded pane, cursor distance relayed by the host', () => {
  let parent: { postMessage: ReturnType<typeof vi.fn> }
  let restoreParent: () => void

  beforeEach(() => {
    vi.useFakeTimers()
    parent = { postMessage: vi.fn() }
    const spy = vi.spyOn(window, 'parent', 'get').mockReturnValue(parent as unknown as Window)
    restoreParent = () => spy.mockRestore()
  })
  afterEach(() => {
    restoreParent()
    vi.useRealTimers()
  })

  const advance = (ms: number) => act(() => { vi.advanceTimersByTime(ms) })
  const opts = { departWhen: (e: MouseEvent) => e.clientY > 48, dismissOnWindowExit: true }
  const leaveWindow = () => act(() => {
    document.dispatchEvent(new MouseEvent('mouseout', {
      bubbles: true, relatedTarget: null, clientX: 4, clientY: 4,
    }))
  })
  const reenterWindow = () => act(() => {
    document.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, relatedTarget: null }))
  })
  const openByHover = (result: { current: ReturnType<typeof useHoverIntent> }) => {
    act(() => { result.current.triggerProps.onMouseEnter() })
    advance(HOVER_OPEN_MS)
    expect(result.current.open).toBe(true)
  }
  const posts = () => parent.postMessage.mock.calls.map(c => c as [Posted, string])
  /** The watch the pane armed — the one it is waiting to hear about. */
  const armed = () => {
    const call = posts().find(([m]) => m.type === 'mc-cursor-away-watch')
    if (!call) throw new Error('no relay watch was posted')
    return call[0].id
  }
  const fromHost = (data: unknown, { origin = HOST_ORIGIN, source = parent as unknown }: { origin?: string; source?: unknown } = {}) =>
    act(() => {
      window.dispatchEvent(new MessageEvent('message', { data, origin, source: source as Window }))
    })
  const answer = (id: string, away: boolean, o?: { origin?: string; source?: unknown }) =>
    fromHost({ type: 'mc-cursor-away', v: 1, id, away }, o)

  it('asks the host, addressed to its exact origin, and dismisses on away=true', () => {
    const { result } = renderHook(() => useHoverIntent(opts))
    openByHover(result)

    leaveWindow()
    const id = armed()
    expect(posts()[0][1]).toBe(HOST_ORIGIN)
    advance(LONG_MS)
    expect(result.current.open).toBe(true)

    answer(id, true)
    expect(result.current.open).toBe(false)
    for (const [, target] of posts()) expect(target).not.toBe('*')
  })

  it('stays open when the host reports the cursor came back inside', () => {
    const { result } = renderHook(() => useHoverIntent(opts))
    openByHover(result)

    leaveWindow()
    const id = armed()
    answer(id, false)
    advance(LONG_MS)
    expect(result.current.open).toBe(true)
  })

  it('stays open when the host never answers', () => {
    // An older host, a browser parent, or a background pane: silence.
    const { result } = renderHook(() => useHoverIntent(opts))
    openByHover(result)
    advance(HOVER_MIN_VISIBLE_MS)

    leaveWindow()
    armed()
    advance(LONG_MS)
    expect(result.current.open).toBe(true)
  })

  it('ignores a reply from the wrong frame, the wrong origin, or another watch', () => {
    const { result } = renderHook(() => useHoverIntent(opts))
    openByHover(result)

    leaveWindow()
    const id = armed()
    answer(id, true, { source: window })                       // not our parent
    answer(id, true, { origin: 'http://127.0.0.1:9999' })     // not the host origin
    answer('some-other-watch', true)                          // not this watch
    fromHost({ type: 'mc-cursor-away', v: 2, id, away: true }) // unknown version
    advance(LONG_MS)
    expect(result.current.open).toBe(true)
  })

  it('cancels the host-side watch when the pointer comes back', () => {
    const { result } = renderHook(() => useHoverIntent(opts))
    openByHover(result)

    leaveWindow()
    const id = armed()
    reenterWindow()
    expect(posts()).toContainEqual([{ type: 'mc-cursor-away-cancel', v: 1, id }, HOST_ORIGIN])
    // A late answer for the cancelled watch changes nothing.
    answer(id, true)
    expect(result.current.open).toBe(true)
  })
})
