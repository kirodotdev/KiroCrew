import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, cleanup } from '@testing-library/react'
import { useRef } from 'react'
import { useDialogFocusTrap } from './useDialogFocusTrap'

/**
 * IME guard on the Tab-cycles-focus path (window-capture NATIVE keydown).
 *
 * The trap's listener receives native KeyboardEvents, so it cannot consume
 * `useImeGuard`'s synthetic-only `claimEnter`; it shares the guard's tracked
 * latch via `createImeLatch` instead. IMEs use Tab to cycle the candidate
 * list, and on WebKit the keydown that commits a candidate arrives AFTER
 * `compositionend` with `isComposing` already false — unguarded, a Tab
 * composed into a dialog input that is the last focusable element yanked
 * focus and aborted the composition (#5410, same class as #5340/#5396).
 */

function Harness({
  enabled = true,
  onEscape = () => {},
  handleEscape = true,
}: {
  enabled?: boolean
  onEscape?: () => void
  handleEscape?: boolean
}) {
  const ref = useRef<HTMLDivElement>(null)
  useDialogFocusTrap(ref, onEscape, enabled, handleEscape)
  return (
    <div ref={ref} role="dialog">
      <button data-testid="first">first</button>
      <input data-testid="middle" aria-label="middle input" />
      <input data-testid="last" aria-label="dialog input" />
    </div>
  )
}

/**
 * The trap filters focusable candidates on `offsetParent !== null`, and the
 * test DOM has no layout engine so every element reports null. Pin a
 * non-null offsetParent on the dialog's focusables so the trap sees them.
 */
function layOut(...els: HTMLElement[]) {
  for (const el of els) {
    Object.defineProperty(el, 'offsetParent', {
      get: () => document.body,
      configurable: true,
    })
  }
}

function mount(props: Parameters<typeof Harness>[0] = {}) {
  const utils = render(<Harness {...props} />)
  const first = utils.getByTestId('first')
  const middle = utils.getByTestId('middle')
  const last = utils.getByTestId('last')
  layOut(first, middle, last)
  return { ...utils, first, middle, last }
}

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

const tab = (target: Element, init: KeyboardEventInit & { keyCode?: number } = {}) =>
  fireEvent.keyDown(target, { key: 'Tab', ...init })

describe('useDialogFocusTrap IME guard', () => {
  it('plain Tab on the last focusable still wraps to the first (positive control)', () => {
    const { first, last } = mount()
    last.focus()
    const notPrevented = tab(last)
    expect(document.activeElement).toBe(first)
    expect(notPrevented).toBe(false) // consumed by the trap
  })

  it('plain Shift+Tab on the first focusable still wraps to the last', () => {
    const { first, last } = mount()
    first.focus()
    const notPrevented = tab(first, { shiftKey: true })
    expect(document.activeElement).toBe(last)
    expect(notPrevented).toBe(false)
  })

  it('declines a mid-composition Tab (native flag) without cancelling the commit', () => {
    const { first, last } = mount()
    last.focus()
    const notPrevented = tab(last, { isComposing: true })
    expect(document.activeElement).toBe(last)
    expect(document.activeElement).not.toBe(first)
    // The browser is consuming this key for candidate navigation itself, so
    // the guard must not cancel its default action (claimKey's split).
    expect(notPrevented).toBe(true)
  })

  it('declines a keyCode-229 Tab without cancelling the commit', () => {
    const { last } = mount()
    last.focus()
    const notPrevented = tab(last, { keyCode: 229 })
    expect(document.activeElement).toBe(last)
    expect(notPrevented).toBe(true)
  })

  it('declines the committing Tab in the post-composition window AND consumes it', () => {
    const { first, last } = mount()
    last.focus()
    fireEvent.compositionStart(last)
    fireEvent.compositionEnd(last)
    // WebKit reports the committing keydown as non-composing; only the
    // tracked latch can identify it. Nothing live is cancelled, so the key
    // is fully consumed rather than acted on by the trap.
    const notPrevented = tab(last)
    expect(document.activeElement).toBe(last)
    expect(document.activeElement).not.toBe(first)
    expect(notPrevented).toBe(false)
  })

  it('declines a post-composition Shift+Tab the same way', () => {
    const { first, last } = mount()
    first.focus()
    fireEvent.compositionStart(first)
    fireEvent.compositionEnd(first)
    tab(first, { shiftKey: true })
    expect(document.activeElement).toBe(first)
    expect(document.activeElement).not.toBe(last)
  })

  it('traps again once the post-composition window has elapsed', () => {
    vi.useFakeTimers()
    const { first, last } = mount()
    last.focus()
    fireEvent.compositionStart(last)
    fireEvent.compositionEnd(last)
    vi.advanceTimersByTime(60)
    tab(last)
    expect(document.activeElement).toBe(first)
  })

  it('keeps the latch armed across an unrelated re-render inside the window', () => {
    // Callers routinely pass an inline `onEscape`, so the keydown effect
    // re-attaches on every host re-render — and the commit's own input event
    // re-renders the host right before the committing keydown arrives. The
    // resubscription must not reset the latch (the composition effect is
    // keyed on `enabled` alone).
    const { first, last, rerender } = mount()
    last.focus()
    fireEvent.compositionStart(last)
    fireEvent.compositionEnd(last)
    rerender(<Harness onEscape={() => {}} />)
    tab(last)
    expect(document.activeElement).toBe(last)
    expect(document.activeElement).not.toBe(first)
  })

  it('does not inherit a stale latch across a disable/re-enable', () => {
    // Abandoned mid-composition while an inner dialog owned the keyboard:
    // no compositionend ever follows.
    const { first, last, rerender } = mount()
    last.focus()
    fireEvent.compositionStart(last)
    rerender(<Harness enabled={false} />)
    rerender(<Harness enabled />)
    last.focus()
    tab(last)
    expect(document.activeElement).toBe(first)
  })

  it('recovers from an abandoned composition when focus moves away', () => {
    // No compositionend ever fires (OS-level IME cancel, focus stolen
    // mid-composition). Without the focusout recovery the latch would stay
    // set and consume every later Tab for the dialog's lifetime.
    const { first, last } = mount()
    last.focus()
    fireEvent.compositionStart(last) // abandoned: no compositionEnd follows
    last.blur()
    last.focus()
    tab(last)
    expect(document.activeElement).toBe(first)
  })

  it('leaves a mid-dialog Tab untouched inside the post-composition window', () => {
    // The trap only consumes boundary Tabs; a Tab on a mid-dialog field is
    // the browser's to move, so the guard must not claim it — claiming would
    // consume legitimate navigation for 50ms after every composition.
    const { middle } = mount()
    middle.focus()
    fireEvent.compositionStart(middle)
    fireEvent.compositionEnd(middle)
    const notPrevented = tab(middle)
    // Released: neither prevented nor redirected by the trap.
    expect(notPrevented).toBe(true)
    expect(document.activeElement).toBe(middle)
  })

  it('leaves Escape dismissal unaffected by the guard wiring', () => {
    const onEscape = vi.fn()
    const { last } = mount({ onEscape })
    last.focus()
    fireEvent.keyDown(last, { key: 'Escape' })
    expect(onEscape).toHaveBeenCalledTimes(1)
  })
})

/**
 * IME guard on the Escape-dismisses path (#5420, the Escape half of the
 * defect whose Tab half is pinned above). A CJK user pressing Escape to
 * cancel an in-flight candidate list must not have that same keypress close
 * the dialog and discard a part-filled form.
 */
describe('useDialogFocusTrap Escape IME guard', () => {
  const esc = (target: Element, init: KeyboardEventInit & { keyCode?: number } = {}) =>
    fireEvent.keyDown(target, { key: 'Escape', ...init })

  it('declines a mid-composition Escape (native flag) without cancelling the IME cancel', () => {
    const onEscape = vi.fn()
    const { last } = mount({ onEscape })
    last.focus()
    const notPrevented = esc(last, { isComposing: true })
    expect(onEscape).not.toHaveBeenCalled()
    // The IME is consuming this key to cancel its own candidate; the guard
    // must not preventDefault or the candidate cancellation itself breaks
    // (claimKey's split: consume only where the browser would otherwise act).
    expect(notPrevented).toBe(true)
  })

  it('declines a keyCode-229 Escape without cancelling the IME cancel', () => {
    const onEscape = vi.fn()
    const { last } = mount({ onEscape })
    last.focus()
    const notPrevented = esc(last, { keyCode: 229 })
    expect(onEscape).not.toHaveBeenCalled()
    expect(notPrevented).toBe(true)
  })

  it('declines the Escape landing in the post-composition window AND consumes it', () => {
    const onEscape = vi.fn()
    const { last } = mount({ onEscape })
    last.focus()
    fireEvent.compositionStart(last)
    fireEvent.compositionEnd(last)
    // WebKit reports the keydown that follows a candidate commit/cancel as
    // non-composing; only the tracked latch can identify it. Nothing live is
    // cancelled, so the key is fully consumed rather than dismissing.
    const notPrevented = esc(last)
    expect(onEscape).not.toHaveBeenCalled()
    expect(notPrevented).toBe(false)
  })

  it('dismisses again once the post-composition window has elapsed', () => {
    // A guard that never released would make dialogs un-closable by keyboard,
    // which is an accessibility regression as bad as the defect itself.
    vi.useFakeTimers()
    const onEscape = vi.fn()
    const { last } = mount({ onEscape })
    last.focus()
    fireEvent.compositionStart(last)
    fireEvent.compositionEnd(last)
    vi.advanceTimersByTime(60)
    esc(last)
    expect(onEscape).toHaveBeenCalledTimes(1)
  })

  it('recovers Escape dismissal from an abandoned composition when focus moves away', () => {
    // No compositionend ever fires (OS-level IME cancel, focus stolen
    // mid-composition). The focusout recovery resets the latch; without it
    // every later Escape would be consumed for the dialog's lifetime — the
    // un-closable dialog the release case above pins, on its other path.
    const onEscape = vi.fn()
    const { last } = mount({ onEscape })
    last.focus()
    fireEvent.compositionStart(last) // abandoned: no compositionEnd follows
    last.blur()
    last.focus()
    esc(last)
    expect(onEscape).toHaveBeenCalledTimes(1)
  })

  it('never claims an Escape the caller owns (handleEscape=false), even mid-latch', () => {
    // The claim lives INSIDE the handleEscape branch. `claimKey` stops
    // propagation on the keys it declines, so a hoisted claim would swallow
    // Escapes that must reach a caller's own bubble-phase listener (Modal,
    // CommandBarOverlay pass handleEscape=false for nested-overlay handling).
    const onEscape = vi.fn()
    const bubbleListener = vi.fn()
    const { last } = mount({ onEscape, handleEscape: false })
    last.focus()
    fireEvent.compositionStart(last)
    fireEvent.compositionEnd(last)
    window.addEventListener('keydown', bubbleListener)
    try {
      esc(last)
      // Untouched: not dismissed by the hook, and still propagating to the
      // bubble phase where the caller's own Escape handling lives.
      expect(onEscape).not.toHaveBeenCalled()
      expect(bubbleListener).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener('keydown', bubbleListener)
    }
  })
})
