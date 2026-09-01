import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, cleanup } from '@testing-library/react'
import { useEffect } from 'react'
import { useListKeyboardNav, type UseListKeyboardNavOptions } from './useListKeyboardNav'

/**
 * IME guard on the Enter-chooses-row path (document-capture NATIVE keydown).
 *
 * This hook's listener receives native KeyboardEvents, so it cannot consume
 * `useImeGuard`'s synthetic-only `claimEnter`; it shares the guard's tracked
 * latch via `createImeLatch` instead. On WebKit the keydown that commits an
 * IME candidate arrives AFTER `compositionend` with `isComposing` already
 * false — unguarded, committing a candidate into a picker's filter query
 * activated whatever row was highlighted (#5340).
 */

function Harness(props: Partial<UseListKeyboardNavOptions>) {
  useListKeyboardNav({
    open: true,
    count: 3,
    onChoose: () => {},
    onClose: () => {},
    ...props,
  })
  return <input data-testid="host-input" aria-label="host input" />
}

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

const enter = (init: KeyboardEventInit & { keyCode?: number } = {}) =>
  fireEvent.keyDown(document, { key: 'Enter', ...init })

const escape = (init: KeyboardEventInit & { keyCode?: number } = {}) =>
  fireEvent.keyDown(document, { key: 'Escape', ...init })

describe('useListKeyboardNav IME guard', () => {
  it('plain Enter still chooses the selected row (positive control)', () => {
    const onChoose = vi.fn()
    render(<Harness onChoose={onChoose} />)
    const notPrevented = enter()
    expect(onChoose).toHaveBeenCalledWith(0, false)
    expect(notPrevented).toBe(false) // consumed by the choose path
  })

  it('declines a mid-composition Enter (native flag) without cancelling the commit', () => {
    const onChoose = vi.fn()
    render(<Harness onChoose={onChoose} />)
    const notPrevented = enter({ isComposing: true })
    expect(onChoose).not.toHaveBeenCalled()
    // The browser is consuming this key for the candidate commit itself, so
    // the guard must not cancel its default action (claimEnter's split).
    expect(notPrevented).toBe(true)
  })

  it('declines a keyCode-229 Enter without cancelling the commit', () => {
    const onChoose = vi.fn()
    render(<Harness onChoose={onChoose} />)
    const notPrevented = enter({ keyCode: 229 })
    expect(onChoose).not.toHaveBeenCalled()
    expect(notPrevented).toBe(true)
  })

  it('declines the committing Enter in the post-composition window AND consumes it', () => {
    const onChoose = vi.fn()
    const { getByTestId } = render(<Harness onChoose={onChoose} />)
    const input = getByTestId('host-input')
    fireEvent.compositionStart(input)
    fireEvent.compositionEnd(input)
    // WebKit reports the committing keydown as non-composing; only the
    // tracked latch can identify it. Nothing live is cancelled, so the key is
    // fully consumed rather than falling through to the host composer.
    const notPrevented = enter()
    expect(onChoose).not.toHaveBeenCalled()
    expect(notPrevented).toBe(false)
  })

  it('chooses again once the post-composition window has elapsed', () => {
    vi.useFakeTimers()
    const onChoose = vi.fn()
    const { getByTestId } = render(<Harness onChoose={onChoose} />)
    const input = getByTestId('host-input')
    fireEvent.compositionStart(input)
    fireEvent.compositionEnd(input)
    vi.advanceTimersByTime(60)
    enter()
    expect(onChoose).toHaveBeenCalledWith(0, false)
  })

  it('does not inherit a stale latch across a close/reopen', () => {
    const onChoose = vi.fn()
    const { getByTestId, rerender } = render(<Harness onChoose={onChoose} />)
    // Abandoned mid-composition: no compositionend follows before close.
    fireEvent.compositionStart(getByTestId('host-input'))
    rerender(<Harness open={false} onChoose={onChoose} />)
    rerender(<Harness onChoose={onChoose} />)
    enter()
    expect(onChoose).toHaveBeenCalledWith(0, false)
  })

  it('keeps releasing Enter to the host on an empty list, composing or not', () => {
    // The empty-list release path has no claim on the keystroke and must stay
    // untouched: the host composer carries its own IME guard.
    const onChoose = vi.fn()
    const onClose = vi.fn()
    const { getByTestId } = render(
      <Harness count={0} releaseKeysWhenEmpty onChoose={onChoose} onClose={onClose} />,
    )
    const input = getByTestId('host-input')
    fireEvent.compositionStart(input)
    fireEvent.compositionEnd(input)
    const notPrevented = enter()
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onChoose).not.toHaveBeenCalled()
    expect(notPrevented).toBe(true) // released, not consumed
  })

  it('keeps the latch armed across an unrelated prop change inside the window', () => {
    // Consumers derive releaseKeysWhenEmpty from live query/fetch state, and
    // the commit's own input event mutates that state right before the
    // committing keydown arrives. The handler resubscribing on the prop
    // change must not reset the latch (the composition effect is keyed on
    // `open` alone).
    const onChoose = vi.fn()
    const { getByTestId, rerender } = render(<Harness onChoose={onChoose} />)
    const input = getByTestId('host-input')
    fireEvent.compositionStart(input)
    fireEvent.compositionEnd(input)
    rerender(<Harness onChoose={onChoose} releaseKeysWhenEmpty />)
    const notPrevented = enter()
    expect(onChoose).not.toHaveBeenCalled()
    expect(notPrevented).toBe(false)
  })

  it('declines Tab during composition — Tab is the same choose dispatch', () => {
    // IMEs use Tab to cycle the candidate list, and the hook's Tab branch is
    // an unconditional onChoose otherwise.
    const onChoose = vi.fn()
    const { getByTestId } = render(<Harness onChoose={onChoose} />)
    const input = getByTestId('host-input')
    fireEvent.compositionStart(input)
    fireEvent.compositionEnd(input)
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(onChoose).not.toHaveBeenCalled()
  })

  it('declines a mid-composition Escape without cancelling the IME candidate dismissal', () => {
    const onClose = vi.fn()
    render(<Harness onClose={onClose} />)
    const notPrevented = escape({ isComposing: true })
    expect(onClose).not.toHaveBeenCalled()
    expect(notPrevented).toBe(true)
  })

  it('declines the committing Escape in the post-composition window AND consumes it', () => {
    const onClose = vi.fn()
    const { getByTestId } = render(<Harness onClose={onClose} />)
    const input = getByTestId('host-input')
    fireEvent.compositionStart(input)
    fireEvent.compositionEnd(input)
    const notPrevented = escape()
    expect(onClose).not.toHaveBeenCalled()
    expect(notPrevented).toBe(false)
  })

  it('closes again once the post-composition window has elapsed', () => {
    vi.useFakeTimers()
    const onClose = vi.fn()
    const { getByTestId } = render(<Harness onClose={onClose} />)
    const input = getByTestId('host-input')
    fireEvent.compositionStart(input)
    fireEvent.compositionEnd(input)
    vi.advanceTimersByTime(60)
    escape()
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('recovers from an abandoned composition when focus moves away', () => {
    // No compositionend ever fires (OS-level IME cancel, focus stolen
    // mid-composition). Without the focusout recovery the latch would stay
    // set and consume every later Enter for the rest of the open.
    const onChoose = vi.fn()
    const { getByTestId } = render(<Harness onChoose={onChoose} />)
    const input = getByTestId('host-input')
    input.focus()
    fireEvent.compositionStart(input) // abandoned: no compositionEnd follows
    input.blur()
    enter()
    expect(onChoose).toHaveBeenCalledWith(0, false)
  })

  it('leaves the Tab and modifier-Enter contracts unchanged', () => {
    const onChoose = vi.fn()
    render(<Harness onChoose={onChoose} />)
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(onChoose).toHaveBeenLastCalledWith(0, false)
    enter({ metaKey: true })
    expect(onChoose).toHaveBeenLastCalledWith(0, true)
  })

  it('honors onAltEnter after the guard clears', () => {
    const onChoose = vi.fn()
    const onAltEnter = vi.fn(() => true)
    render(<Harness onChoose={onChoose} onAltEnter={onAltEnter} />)
    enter({ altKey: true })
    expect(onAltEnter).toHaveBeenCalledWith(0)
    expect(onChoose).not.toHaveBeenCalled()
  })
})

/**
 * A host-level WINDOW-capture interceptor (the palette's Tab / Alt+Enter
 * takeover) deliberately outranks the hook's document-capture listener, so it
 * bypasses the guard above along with the dispatch. The hook returns its
 * instance `claimKey` so such an interceptor declines through the SAME
 * tracked latch; this harness mirrors the palette's shape (claim first,
 * consume-and-act on true, bail on false).
 */
function InterceptorHarness(props: Partial<UseListKeyboardNavOptions> & {
  onTabAction: () => void
  onAltAction?: () => void
}) {
  const { onTabAction, onAltAction, ...opts } = props
  const { claimKey } = useListKeyboardNav({
    open: true,
    count: 3,
    onChoose: () => {},
    onClose: () => {},
    ...opts,
  })
  useEffect(() => {
    const onWinKey = (e: KeyboardEvent) => {
      if (e.key === 'Tab') {
        if (!claimKey(e)) return
        e.preventDefault()
        e.stopImmediatePropagation()
        onTabAction()
      } else if (e.key === 'Enter' && e.altKey) {
        if (!claimKey(e)) return
        e.preventDefault()
        e.stopImmediatePropagation()
        onAltAction?.()
      }
    }
    window.addEventListener('keydown', onWinKey, true)
    return () => window.removeEventListener('keydown', onWinKey, true)
  }, [claimKey, onTabAction, onAltAction])
  return <input data-testid="host-input" aria-label="host input" />
}

describe('claimKey for a window-capture interceptor', () => {
  it('a plain Tab reaches the interceptor and never the hook (positive control)', () => {
    const onTabAction = vi.fn()
    const onChoose = vi.fn()
    render(<InterceptorHarness onTabAction={onTabAction} onChoose={onChoose} />)
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(onTabAction).toHaveBeenCalledTimes(1)
    // stopImmediatePropagation: the hook's document listener never chooses.
    expect(onChoose).not.toHaveBeenCalled()
  })

  it('declines the committing Tab in the post-composition window AND consumes it', () => {
    const onTabAction = vi.fn()
    const onChoose = vi.fn()
    const { getByTestId } = render(
      <InterceptorHarness onTabAction={onTabAction} onChoose={onChoose} />,
    )
    const input = getByTestId('host-input')
    fireEvent.compositionStart(input)
    fireEvent.compositionEnd(input)
    // WebKit reports the committing keydown as non-composing; only the shared
    // latch identifies it. The decline is fully consumed: preventDefault
    // (nothing live to cancel) and stopPropagation (the hook's own document
    // listener must not see a key the host already claimed).
    const notPrevented = fireEvent.keyDown(document, { key: 'Tab' })
    expect(onTabAction).not.toHaveBeenCalled()
    expect(onChoose).not.toHaveBeenCalled()
    expect(notPrevented).toBe(false)
  })

  it('declines a mid-composition Tab without cancelling the IME candidate navigation', () => {
    const onTabAction = vi.fn()
    const { getByTestId } = render(<InterceptorHarness onTabAction={onTabAction} />)
    fireEvent.compositionStart(getByTestId('host-input'))
    const notPrevented = fireEvent.keyDown(document, { key: 'Tab', isComposing: true })
    expect(onTabAction).not.toHaveBeenCalled()
    // The browser is consuming Tab for the candidate list itself.
    expect(notPrevented).toBe(true)
  })

  it('intercepts again once the post-composition window has elapsed', () => {
    vi.useFakeTimers()
    const onTabAction = vi.fn()
    const { getByTestId } = render(<InterceptorHarness onTabAction={onTabAction} />)
    const input = getByTestId('host-input')
    fireEvent.compositionStart(input)
    fireEvent.compositionEnd(input)
    vi.advanceTimersByTime(60)
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(onTabAction).toHaveBeenCalledTimes(1)
  })

  it('declines a latched Alt+Enter preview through the same latch, then intercepts once it clears', () => {
    vi.useFakeTimers()
    const onTabAction = vi.fn()
    const onAltAction = vi.fn()
    const { getByTestId } = render(
      <InterceptorHarness onTabAction={onTabAction} onAltAction={onAltAction} />,
    )
    const input = getByTestId('host-input')
    fireEvent.compositionStart(input)
    fireEvent.compositionEnd(input)
    enter({ altKey: true })
    expect(onAltAction).not.toHaveBeenCalled()
    // Positive control: past the post-composition window the same chord
    // reaches the interceptor, so the decline above pins the latch and not
    // an interceptor that declines everything.
    vi.advanceTimersByTime(60)
    enter({ altKey: true })
    expect(onAltAction).toHaveBeenCalledTimes(1)
  })
})
