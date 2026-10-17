import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useSilentLoadWatch, SILENT_LOAD_GRACE_MS } from '../hooks/useSilentLoadWatch'

// The silent-load watch is the affordance ArtifactBody has always carried,
// lifted into one hook so WidgetFrame, ArtifactThumbs and RemoteArtifactDetailPage
// stop being three frames that go permanently blank on a load that never fires.
// These assertions pin the state machine; each is written so the fix must be
// present for it to pass (see the mutation note on each).
describe('useSilentLoadWatch', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('does not report silent before the grace window elapses', () => {
    const { result } = renderHook(() => useSilentLoadWatch('/sandbox-doc/a'))
    expect(result.current.silent).toBe(false)
    act(() => { vi.advanceTimersByTime(SILENT_LOAD_GRACE_MS - 1) })
    // Mutation: if the timer were armed at 0ms the frame would flash a false
    // notice the instant it mounted. It must wait out the whole window.
    expect(result.current.silent).toBe(false)
  })

  it('reports silent once the grace window elapses with no load', () => {
    const { result } = renderHook(() => useSilentLoadWatch('/sandbox-doc/a'))
    act(() => { vi.advanceTimersByTime(SILENT_LOAD_GRACE_MS) })
    // Mutation: remove the setTimeout(setSilent(true)) and this stays false —
    // the whole defect (a blank frame with no notice) is back.
    expect(result.current.silent).toBe(true)
  })

  it('never reports silent when the frame loads within the window', () => {
    const { result } = renderHook(() => useSilentLoadWatch('/sandbox-doc/a'))
    act(() => {
      vi.advanceTimersByTime(SILENT_LOAD_GRACE_MS - 500)
      result.current.onLoaded()
    })
    act(() => { vi.advanceTimersByTime(SILENT_LOAD_GRACE_MS) })
    // Mutation: if onLoaded did not record the loaded url the arming effect
    // would re-fire and mark a loaded frame silent.
    expect(result.current.silent).toBe(false)
  })

  it('does not arm while the url is null (mint in flight)', () => {
    const { result } = renderHook(() => useSilentLoadWatch(null))
    act(() => { vi.advanceTimersByTime(SILENT_LOAD_GRACE_MS * 2) })
    // A frame with no url yet is a pending mint, not a silent load — the mint
    // failure path (useSandboxDoc.failed) owns that case, not this one.
    expect(result.current.silent).toBe(false)
  })

  it('re-arms a fresh window when the url changes (a re-mint)', () => {
    const { result, rerender } = renderHook(
      ({ url }: { url: string }) => useSilentLoadWatch(url),
      { initialProps: { url: '/sandbox-doc/a' } },
    )
    act(() => {
      vi.advanceTimersByTime(SILENT_LOAD_GRACE_MS - 500)
      result.current.onLoaded()
    })
    expect(result.current.silent).toBe(false)
    // A new mint (theme change, content refetch). The previous verdict must not
    // carry over, and the new document gets its own full window.
    rerender({ url: '/sandbox-doc/b' })
    expect(result.current.silent).toBe(false)
    act(() => { vi.advanceTimersByTime(SILENT_LOAD_GRACE_MS - 1) })
    expect(result.current.silent).toBe(false)
    act(() => { vi.advanceTimersByTime(1) })
    // Mutation: if the url-change effect did not reset, a document that loaded
    // once would be immune to a later silent re-mint.
    expect(result.current.silent).toBe(true)
  })
})
