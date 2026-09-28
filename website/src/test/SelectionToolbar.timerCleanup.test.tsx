/**
 * SelectionToolbar must not leave timers running after unmount.
 *
 * Three deferred selection checks fire 0-50ms after a pointer/key event:
 * `mouseup` and Shift+`keyup` schedule `checkSelection` at 50ms, and a
 * `mousedown` inside the container schedules a dismiss check at 0ms. None of
 * them were cancelled on unmount — only the touch-path `selectionchange`
 * debounce was. Unmounting inside that window (a panel closing right after a
 * click, or a test tearing down) left a timer that ran against a torn-down
 * document and surfaced as an uncaught `ReferenceError: window is not defined`.
 *
 * The component already documents this exact failure mode for its "copied!"
 * reset timer, which IS cleared on unmount. These tests extend the same
 * guarantee to the listener effect's timers.
 *
 * Asserting on the pending fake-timer count rather than on a thrown error is
 * deliberate: the throw only happens once the host is gone, which a normal test
 * never reproduces. The timer count tests the mechanism, and fails against the
 * pre-fix code.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, act } from '@testing-library/react'
import { createElement, createRef } from 'react'
import SelectionToolbar, { type SelectionAction } from '../components/SelectionToolbar'

// Typed explicitly: tsconfig.app.json excludes `src/test`, so `tsc -b` never
// checks this file. Annotating the fixture is what makes a wrong shape a
// compile error under `tsc --noEmit` on the test project instead of a silent
// pass that only holds because these assertions never invoke the action.
const ACTIONS: SelectionAction[] = [
  { id: 'quote', label: 'Quote', icon: null, onClick: () => {} },
]

function mount() {
  const containerRef = createRef<HTMLDivElement>()
  const view = render(
    createElement('div', { ref: containerRef },
      createElement(SelectionToolbar, {
        containerRef: containerRef as React.RefObject<HTMLElement>,
        actions: ACTIONS,
      }),
    ),
  )
  return { view, containerRef }
}

describe('SelectionToolbar timer cleanup', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('cancels the deferred check scheduled by mouseup', () => {
    const { view } = mount()
    act(() => { document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true })) })
    // The 50ms "let the selection finalize" timer is now armed.
    expect(vi.getTimerCount()).toBeGreaterThan(0)

    view.unmount()
    // Pre-fix this stayed > 0: the timer outlived the component and would fire
    // against a torn-down document.
    expect(vi.getTimerCount()).toBe(0)
  })

  it('cancels the deferred check scheduled by a Shift keyup', () => {
    const { view } = mount()
    act(() => { document.dispatchEvent(new KeyboardEvent('keyup', { key: 'ArrowRight', shiftKey: true, bubbles: true })) })
    expect(vi.getTimerCount()).toBeGreaterThan(0)

    view.unmount()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('cancels the 0ms dismiss check scheduled by mousedown inside the container', () => {
    const { view, containerRef } = mount()
    act(() => {
      containerRef.current!.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
    })
    expect(vi.getTimerCount()).toBeGreaterThan(0)

    view.unmount()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('cancels MULTIPLE deferred checks queued by rapid events', () => {
    // Each event arms its own timer — they are not coalesced — so cleanup has to
    // clear all of them, not just the most recent.
    const { view } = mount()
    act(() => {
      document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }))
      document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }))
      document.dispatchEvent(new KeyboardEvent('keyup', { key: 'ArrowLeft', shiftKey: true, bubbles: true }))
    })
    expect(vi.getTimerCount()).toBeGreaterThanOrEqual(3)

    view.unmount()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('still runs the deferred check when the component stays mounted', () => {
    // The fix must cancel on unmount WITHOUT breaking the normal path: a mouseup
    // on a live component still has to reach checkSelection.
    const getSelection = vi.spyOn(window, 'getSelection')
    mount()
    getSelection.mockClear()

    act(() => { document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true })) })
    expect(getSelection).not.toHaveBeenCalled()   // still deferred

    act(() => { vi.advanceTimersByTime(50) })
    expect(getSelection).toHaveBeenCalled()       // fired, as before

    getSelection.mockRestore()
  })

  it('drains its own timer bookkeeping as timers fire', () => {
    // The tracking Set must not grow unbounded: a fired timer removes itself, so
    // a long-lived toolbar does not accumulate dead ids.
    const { view } = mount()
    act(() => { document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true })) })
    act(() => { vi.advanceTimersByTime(50) })
    expect(vi.getTimerCount()).toBe(0)
    // And unmount after everything already drained is still clean.
    view.unmount()
    expect(vi.getTimerCount()).toBe(0)
  })
})
