import { describe, it, expect, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useScrollManager } from '../pages/chat/useScrollManager'

/**
 * Regression guard for the "far jump jumps to the top, second click works" bug.
 *
 * navToDisplayIndex mounts the target via the virtualizer (a React state
 * update) and then scrolls. For a FAR jump the new window is not painted into
 * the DOM within one frame, so scrollToDisplayIndex's row query misses. On a
 * miss it must report the miss (return false) and NOT scroll — never teleport
 * to top:0 — so the caller can retry on a later frame once the row mounts.
 */
function makeScroller(): { el: HTMLDivElement; scrollTo: ReturnType<typeof vi.fn> } {
  const el = document.createElement('div')
  const scrollTo = vi.fn()
  // jsdom doesn't implement scrollTo; install a spy.
  ;(el as unknown as { scrollTo: unknown }).scrollTo = scrollTo
  return { el, scrollTo }
}

describe('useScrollManager.scrollToDisplayIndex', () => {
  it('returns false and does NOT scroll to top when the row is not mounted', () => {
    const { result } = renderHook(() => useScrollManager())
    const { el, scrollTo } = makeScroller()
    ;(result.current.scrollerRef as { current: HTMLDivElement | null }).current = el

    const scrolled = result.current.scrollToDisplayIndex(42, { behavior: 'auto' })

    expect(scrolled).toBe(false)
    // The regression: an absent row must never teleport the scroller (to top:0
    // or anywhere). No scrollTo, no scrollTop write.
    expect(scrollTo).not.toHaveBeenCalled()
    expect(el.scrollTop).toBe(0)
  })

  it('returns true and scrolls when the target row IS mounted', () => {
    const { result } = renderHook(() => useScrollManager())
    const { el, scrollTo } = makeScroller()
    const row = document.createElement('div')
    row.setAttribute('data-display-index', '7')
    el.appendChild(row)
    ;(result.current.scrollerRef as { current: HTMLDivElement | null }).current = el

    const scrolled = result.current.scrollToDisplayIndex(7, { behavior: 'auto', align: 'center' })

    expect(scrolled).toBe(true)
    expect(scrollTo).toHaveBeenCalledTimes(1)
    expect(scrollTo.mock.calls[0][0]).toMatchObject({ behavior: 'auto' })
  })

  it('returns false when there is no scroller element at all', () => {
    const { result } = renderHook(() => useScrollManager())
    expect(result.current.scrollToDisplayIndex(0)).toBe(false)
  })
})
