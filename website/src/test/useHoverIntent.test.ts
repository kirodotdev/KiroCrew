/**
 * Hover intent for floating surfaces.
 *
 * Locks the contract, and both delays are asymmetric on purpose:
 *  (1) Hover does NOT open immediately — sweeping the pointer across a trigger
 *      on the way somewhere else must not summon the surface.
 *  (2) Leaving does NOT close immediately — the pointer needs time to travel
 *      into the surface, including across a gap.
 *  (3) Entering the surface cancels the pending close.
 *  (4) Focus alone does NOT open — that would be a WCAG 3.2.1 change of context
 *      and would make the trigger impossible to Tab past. ArrowDown (the ARIA
 *      menu-button opener) opens with no delay and reports keyboard intent, so
 *      the caller may move focus in; a hover-open must NOT report that.
 *  (5) Escape and outside-pointerdown close. Escape also hands focus back to
 *      the trigger when focus is inside the pair, and does not propagate.
 *      Pointerdown inside the trigger or the surface does not close.
 *  (6) Disabling mid-hover retracts the surface instead of freezing it.
 *  (7) Pending timers do not fire after unmount.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useHoverIntent, HOVER_OPEN_MS, HOVER_CLOSE_MS } from '../hooks/useHoverIntent'

/** The synthetic ArrowDown `arrowDown()` hands the hook: a keyboard event whose
 *  `preventDefault` is observable, so a test can assert the hook called it. */
type ArrowDownEvent = React.KeyboardEvent & { wasPrevented(): boolean }

describe('useHoverIntent', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  const advance = (ms: number) => act(() => { vi.advanceTimersByTime(ms) })

  it('does not open until the pointer has rested for the open delay', () => {
    const { result } = renderHook(() => useHoverIntent())
    act(() => { result.current.triggerProps.onMouseEnter() })
    expect(result.current.open).toBe(false)
    advance(HOVER_OPEN_MS - 1)
    expect(result.current.open).toBe(false)
    advance(1)
    expect(result.current.open).toBe(true)
    expect(result.current.openedBy).toBe('hover')
  })

  it('a pointer sweeping across the trigger never opens it', () => {
    const { result } = renderHook(() => useHoverIntent())
    act(() => { result.current.triggerProps.onMouseEnter() })
    advance(HOVER_OPEN_MS - 50)
    act(() => { result.current.triggerProps.onMouseLeave() })
    advance(HOVER_OPEN_MS + HOVER_CLOSE_MS)
    expect(result.current.open).toBe(false)
  })

  it('focus entering the SURFACE opens it immediately as keyboard intent', () => {
    // An off-screen surface keeps its controls in the tab order, so Tab can
    // land inside it while closed — the focus ring would be invisible. Focused
    // chrome must be visible chrome. Contrast (4): focus on the TRIGGER still
    // never opens; only focus already inside the surface does.
    const { result } = renderHook(() => useHoverIntent())
    act(() => { result.current.surfaceProps.onFocus() })
    expect(result.current.open).toBe(true)
    expect(result.current.openedBy).toBe('keyboard')
  })

  it('keeps the surface up for the grace period after the pointer leaves', () => {
    const { result } = renderHook(() => useHoverIntent())
    act(() => { result.current.triggerProps.onMouseEnter() })
    advance(HOVER_OPEN_MS)
    act(() => { result.current.triggerProps.onMouseLeave() })
    advance(HOVER_CLOSE_MS - 1)
    expect(result.current.open).toBe(true)
    advance(1)
    expect(result.current.open).toBe(false)
  })

  it('entering the surface during the grace period cancels the close', () => {
    const { result } = renderHook(() => useHoverIntent())
    act(() => { result.current.triggerProps.onMouseEnter() })
    advance(HOVER_OPEN_MS)
    act(() => { result.current.triggerProps.onMouseLeave() })
    advance(HOVER_CLOSE_MS - 50)
    act(() => { result.current.surfaceProps.onMouseEnter() })
    advance(HOVER_CLOSE_MS * 3)
    expect(result.current.open).toBe(true)
  })

  it('leaving the surface closes it after the grace period', () => {
    const { result } = renderHook(() => useHoverIntent())
    act(() => { result.current.triggerProps.onMouseEnter() })
    advance(HOVER_OPEN_MS)
    act(() => { result.current.surfaceProps.onMouseEnter() })
    act(() => { result.current.surfaceProps.onMouseLeave() })
    advance(HOVER_CLOSE_MS)
    expect(result.current.open).toBe(false)
  })

  const arrowDown = () => {
    let prevented = false
    return {
      key: 'ArrowDown',
      preventDefault: () => { prevented = true },
      wasPrevented: () => prevented,
    } as unknown as ArrowDownEvent
  }

  it('ArrowDown opens immediately and reports keyboard intent', () => {
    const { result } = renderHook(() => useHoverIntent())
    const e = arrowDown()
    act(() => { result.current.triggerProps.onKeyDown(e) })
    expect(result.current.open).toBe(true)
    expect(result.current.openedBy).toBe('keyboard')
    // preventDefault so ArrowDown does not also scroll the page.
    expect((e as ArrowDownEvent).wasPrevented()).toBe(true)
  })

  it('ignores other keys on the trigger', () => {
    const { result } = renderHook(() => useHoverIntent())
    for (const key of ['ArrowUp', 'Enter', ' ', 'Tab', 'a']) {
      act(() => { result.current.triggerProps.onKeyDown({ key, preventDefault() {} } as unknown as React.KeyboardEvent) })
      expect(result.current.open, key).toBe(false)
    }
  })

  it('closes on Escape', () => {
    const { result } = renderHook(() => useHoverIntent())
    act(() => { result.current.triggerProps.onKeyDown(arrowDown()) })
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })) })
    expect(result.current.open).toBe(false)
    expect(result.current.openedBy).toBe(null)
  })

  it('Escape hands focus back to the trigger when focus is inside the pair', () => {
    // Without this, focus falls to <body> when the surface unmounts, so the next
    // Tab restarts from the top of the page and the trigger's state is never
    // re-announced.
    const trigger = document.createElement('button')
    const surface = document.createElement('div')
    const row = document.createElement('button')
    surface.appendChild(row)
    document.body.append(trigger, surface)
    const { result } = renderHook(() => useHoverIntent({
      triggerRef: { current: trigger },
      surfaceRef: { current: surface },
    }))
    act(() => { result.current.triggerProps.onKeyDown(arrowDown()) })
    row.focus()
    expect(document.activeElement).toBe(row)
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })) })
    expect(result.current.open).toBe(false)
    expect(document.activeElement).toBe(trigger)
    trigger.remove(); surface.remove()
  })

  it('Escape does NOT yank focus when the surface was hover-opened', () => {
    // The pointer path leaves focus wherever the user was typing; stealing it
    // on Escape would be worse than leaving it alone.
    const trigger = document.createElement('button')
    const surface = document.createElement('div')
    const elsewhere = document.createElement('input')
    document.body.append(trigger, surface, elsewhere)
    const { result } = renderHook(() => useHoverIntent({
      triggerRef: { current: trigger },
      surfaceRef: { current: surface },
    }))
    act(() => { result.current.triggerProps.onMouseEnter() })
    advance(HOVER_OPEN_MS)
    elsewhere.focus()
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })) })
    expect(result.current.open).toBe(false)
    expect(document.activeElement).toBe(elsewhere)
    trigger.remove(); surface.remove(); elsewhere.remove()
  })

  it('Escape does not propagate, so it cannot dismiss unrelated surfaces', () => {
    // One Escape used to both close this and trip the page's other
    // dismiss-on-Escape handlers, clearing a card the user never aimed at.
    const { result } = renderHook(() => useHoverIntent())
    act(() => { result.current.triggerProps.onKeyDown(arrowDown()) })
    const bubbled: string[] = []
    const spy = (e: Event) => bubbled.push((e as KeyboardEvent).key)
    window.addEventListener('keydown', spy)
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })) })
    window.removeEventListener('keydown', spy)
    expect(bubbled).toEqual([])
  })

  it('Escape beats a document listener that registered EARLIER', () => {
    // The real case: ChatInput binds a document keydown to cancel dictation and
    // defers only to `[role="dialog"]`, which this menu is not. On the bubble
    // phase the winner is whoever registered first, so opening the flyout
    // mid-dictation and pressing Escape discarded the captured audio. Register
    // the rival FIRST — capture phase must still win.
    const rival: string[] = []
    const dictationCancel = (e: Event) => {
      if ((e as KeyboardEvent).defaultPrevented) return
      rival.push('cancelled-dictation')
    }
    document.addEventListener('keydown', dictationCancel)
    const { result } = renderHook(() => useHoverIntent())
    act(() => { result.current.triggerProps.onKeyDown(arrowDown()) })
    expect(result.current.open).toBe(true)
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })) })
    document.removeEventListener('keydown', dictationCancel)
    expect(result.current.open).toBe(false)
    expect(rival).toEqual([])
  })

  it('marks Escape handled, for peers that gate on defaultPrevented', () => {
    const { result } = renderHook(() => useHoverIntent())
    act(() => { result.current.triggerProps.onKeyDown(arrowDown()) })
    const e = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    act(() => { document.dispatchEvent(e) })
    expect(e.defaultPrevented).toBe(true)
  })

  it('ignores Escape mid-IME-composition', () => {
    // Escape during composition cancels the candidate window, not the surface.
    const { result } = renderHook(() => useHoverIntent())
    act(() => { result.current.triggerProps.onKeyDown(arrowDown()) })
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', isComposing: true, bubbles: true })) })
    expect(result.current.open).toBe(true)
  })

  it('a closed surface stops swallowing Escape', () => {
    // Locks the behavioural contract: once closed, Escape belongs to the page
    // again. NOTE it does NOT prove the `, true` on removeEventListener —
    // happy-dom does not match capture vs bubble when removing, so dropping the
    // flag still passes here while leaking a listener in a real browser. The
    // flag is required by spec; this test cannot be its guard.
    const { result, unmount } = renderHook(() => useHoverIntent())
    act(() => { result.current.triggerProps.onKeyDown(arrowDown()) })
    act(() => { result.current.close() })
    const seen: string[] = []
    const after = (e: Event) => seen.push((e as KeyboardEvent).key)
    document.addEventListener('keydown', after)
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })) })
    document.removeEventListener('keydown', after)
    expect(seen).toEqual(['Escape'])
    unmount()
  })

  it('closes on outside pointerdown but not on pointerdown inside the anchors', () => {
    const trigger = document.createElement('button')
    const surface = document.createElement('div')
    const inner = document.createElement('span')
    surface.appendChild(inner)
    const outside = document.createElement('div')
    document.body.append(trigger, surface, outside)
    const { result } = renderHook(() => useHoverIntent({
      triggerRef: { current: trigger },
      surfaceRef: { current: surface },
    }))

    act(() => { result.current.triggerProps.onKeyDown(arrowDown()) })
    // Inside the trigger.
    act(() => { trigger.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true })) })
    expect(result.current.open).toBe(true)
    // Nested inside the surface — `contains` must reach descendants, or clicking
    // a session row would dismiss the surface before the click landed.
    act(() => { inner.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true })) })
    expect(result.current.open).toBe(true)
    // Genuinely outside.
    act(() => { outside.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true })) })
    expect(result.current.open).toBe(false)

    trigger.remove(); surface.remove(); outside.remove()
  })

  it('retracts when disabled mid-hover', () => {
    const { result, rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useHoverIntent({ enabled }),
      { initialProps: { enabled: true } },
    )
    act(() => { result.current.triggerProps.onMouseEnter() })
    advance(HOVER_OPEN_MS)
    expect(result.current.open).toBe(true)
    rerender({ enabled: false })
    expect(result.current.open).toBe(false)
  })

  it('ignores handlers while disabled', () => {
    const { result } = renderHook(() => useHoverIntent({ enabled: false }))
    act(() => { result.current.triggerProps.onMouseEnter() })
    advance(HOVER_OPEN_MS * 2)
    expect(result.current.open).toBe(false)
    act(() => { result.current.triggerProps.onKeyDown(arrowDown()) })
    expect(result.current.open).toBe(false)
  })

  it('does not fire a pending open after unmount', () => {
    const { result, unmount } = renderHook(() => useHoverIntent())
    act(() => { result.current.triggerProps.onMouseEnter() })
    unmount()
    // A surviving timer would call setState on an unmounted hook. Advancing past
    // the delay must produce no error and leave no scheduled work.
    expect(() => { vi.advanceTimersByTime(HOVER_OPEN_MS * 2) }).not.toThrow()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('close() skips the grace period', () => {
    const { result } = renderHook(() => useHoverIntent())
    act(() => { result.current.triggerProps.onKeyDown(arrowDown()) })
    act(() => { result.current.close() })
    expect(result.current.open).toBe(false)
  })
})

describe('useHoverIntent — departWhen (positional close for window-drag surfaces)', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  const advance = (ms: number) => act(() => { vi.advanceTimersByTime(ms) })
  // Territory: the top 48px band, mirroring the focus-mode header's predicate.
  const departWhen = (e: MouseEvent) => e.clientY > 48
  const moveTo = (y: number) => act(() => {
    document.dispatchEvent(new MouseEvent('mousemove', { clientY: y, bubbles: true }))
  })
  const openByHover = (result: { current: ReturnType<typeof useHoverIntent> }) => {
    act(() => { result.current.triggerProps.onMouseEnter() })
    advance(HOVER_OPEN_MS)
    expect(result.current.open).toBe(true)
  }

  it('ignores anchor mouseleave while open — event silence is not departure', () => {
    // The surface is a window-drag area: a -webkit-app-region:drag rect is
    // resolved by the compositor BEFORE hit-testing, so a pointer resting on
    // the surface's own empty region produces mouseleave-then-silence that is
    // indistinguishable from departure. Closing on it oscillates (close → drag
    // region unmounts → events revive → trigger reopens → …). Positional mode
    // therefore never closes on leave events, only on observed position.
    const { result } = renderHook(() => useHoverIntent({ departWhen }))
    openByHover(result)

    act(() => { result.current.surfaceProps.onMouseLeave() })
    act(() => { result.current.triggerProps.onMouseLeave() })
    advance(HOVER_CLOSE_MS * 4)
    expect(result.current.open).toBe(true)
  })

  it('closes only on a mousemove observed outside the territory', () => {
    const { result } = renderHook(() => useHoverIntent({ departWhen }))
    openByHover(result)

    // Movement INSIDE the band never closes, no matter how long.
    moveTo(10)
    advance(HOVER_CLOSE_MS * 4)
    expect(result.current.open).toBe(true)

    // First sighting below the band starts the grace; it is a grace, not a snap.
    moveTo(300)
    expect(result.current.open).toBe(true)
    advance(HOVER_CLOSE_MS)
    expect(result.current.open).toBe(false)
  })

  it('returning to the territory during the grace cancels the close', () => {
    const { result } = renderHook(() => useHoverIntent({ departWhen }))
    openByHover(result)

    moveTo(300)
    advance(HOVER_CLOSE_MS - 50)
    moveTo(10) // back into the band before the grace elapsed
    advance(HOVER_CLOSE_MS * 4)
    expect(result.current.open).toBe(true)
  })

  it('the grace measures time since the FIRST outside sighting', () => {
    // Continuous movement below the band must not keep restarting the timer —
    // that would turn the grace into "closes only when the pointer stops".
    const { result } = renderHook(() => useHoverIntent({ departWhen }))
    openByHover(result)

    moveTo(300)
    advance(HOVER_CLOSE_MS - 50)
    moveTo(400)
    moveTo(500)
    advance(50)
    expect(result.current.open).toBe(false)
  })

  it('a trigger leave while still CLOSED cancels the pending open', () => {
    // Positional mode owns closing only while OPEN; sweeping across the trigger
    // must still not summon the surface.
    const { result } = renderHook(() => useHoverIntent({ departWhen }))
    act(() => { result.current.triggerProps.onMouseEnter() })
    act(() => { result.current.triggerProps.onMouseLeave() })
    advance(HOVER_OPEN_MS + HOVER_CLOSE_MS)
    expect(result.current.open).toBe(false)
  })

  it('without departWhen, anchor leaves close as before', () => {
    // Positional close is opt-in; the hook's other consumers keep the
    // event-based contract.
    const { result } = renderHook(() => useHoverIntent())
    openByHover(result)
    act(() => { result.current.surfaceProps.onMouseLeave() })
    advance(HOVER_CLOSE_MS)
    expect(result.current.open).toBe(false)
  })
})
