// On a phone, scrolling the chat list kept opening a row's ⋮ menu. Radix opens
// a DropdownMenu trigger on `pointerdown` for every pointer type, so the menu
// opened the moment a finger landed on the trigger (or its enlarged touch-hit
// area), before the browser could tell a tap from a scroll. The shared
// `DropdownMenuTrigger` defers a touch or pen press to the tap's `click`, which
// a scroll never produces. A mouse press still opens on `pointerdown`.
import { act, createEvent, fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../components/ui/dropdown-menu'

function Harness({ onOpenChange, onTriggerClick, disabled }: {
  onOpenChange?: (open: boolean) => void
  onTriggerClick?: () => void
  disabled?: boolean
}) {
  return (
    <DropdownMenu onOpenChange={onOpenChange}>
      <DropdownMenuTrigger asChild disabled={disabled}>
        <button type="button" data-testid="trigger" onClick={onTriggerClick}>more</button>
      </DropdownMenuTrigger>
      <DropdownMenuContent data-testid="content">
        <DropdownMenuItem>Rename</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

const trigger = () => screen.getByTestId('trigger')
const menuOpen = () => screen.queryByTestId('content') !== null

/** A finger lands on the trigger. Whether it is a tap or a scroll is not known yet. */
function touchDown(el: HTMLElement, pointerType: 'touch' | 'pen' = 'touch', timeStamp?: number): boolean {
  const pointerDown = new PointerEvent('pointerdown', {
    bubbles: true, cancelable: true, pointerType, button: 0,
  })
  if (timeStamp !== undefined) Object.defineProperty(pointerDown, 'timeStamp', { value: timeStamp })
  let notCancelled = true
  act(() => {
    notCancelled = el.dispatchEvent(pointerDown)
  })
  return !notCancelled
}

/** The finger lifts without moving: the browser fires the tap's click (detail 1). */
function tapRelease(el: HTMLElement, timeStamp?: number): void {
  const pointerUp = createEvent.pointerUp(el, { pointerType: 'touch', button: 0 })
  if (timeStamp !== undefined) Object.defineProperty(pointerUp, 'timeStamp', { value: timeStamp })
  fireEvent(el, pointerUp)
  const click = createEvent.click(el, { button: 0, detail: 1 })
  if (timeStamp !== undefined) Object.defineProperty(click, 'timeStamp', { value: timeStamp + 50 })
  fireEvent(el, click)
}

/** A click with no pointer event of its own, stamped at *timeStamp* so no test waits on the clock. */
function clickAt(el: HTMLElement, timeStamp: number): void {
  const click = new MouseEvent('click', { bubbles: true, cancelable: true, detail: 0 })
  Object.defineProperty(click, 'timeStamp', { value: timeStamp })
  act(() => { el.dispatchEvent(click) })
}

/** The finger moves and lifts elsewhere: a scroll, so the browser fires no click. */
function scrollAway(el: HTMLElement, timeStamp?: number): void {
  fireEvent.pointerMove(el, { pointerType: 'touch', clientY: 80 })
  const pointerCancel = createEvent.pointerCancel(el, { pointerType: 'touch' })
  if (timeStamp !== undefined) Object.defineProperty(pointerCancel, 'timeStamp', { value: timeStamp })
  fireEvent(el, pointerCancel)
}

describe('DropdownMenuTrigger on a touch pointer', () => {
  it('does not open on pointerdown, and a scroll that started on it leaves the menu closed', () => {
    const onOpenChange = vi.fn()
    render(<Harness onOpenChange={onOpenChange} />)

    touchDown(trigger())
    expect(menuOpen()).toBe(false)

    scrollAway(trigger())
    expect(menuOpen()).toBe(false)
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('opens on the tap click, and the next tap closes it', () => {
    const onOpenChange = vi.fn()
    render(<Harness onOpenChange={onOpenChange} />)

    touchDown(trigger())
    tapRelease(trigger())
    expect(menuOpen()).toBe(true)
    expect(onOpenChange).toHaveBeenLastCalledWith(true)

    touchDown(trigger(), 'touch', 1_000)
    tapRelease(trigger(), 1_000)
    expect(menuOpen()).toBe(false)
    expect(onOpenChange).toHaveBeenLastCalledWith(false)
    expect(onOpenChange).toHaveBeenCalledTimes(2)
  })

  it('a press on the trigger of an open menu closes it at once, and the tap\'s click does not reopen it', () => {
    const onOpenChange = vi.fn()
    render(<Harness onOpenChange={onOpenChange} />)
    touchDown(trigger())
    tapRelease(trigger())
    expect(menuOpen()).toBe(true)

    touchDown(trigger(), 'touch', 1_000)
    expect(menuOpen()).toBe(false)
    // The menu's own outside-dismiss may run between the press and its click;
    // either way the click must not open the menu again.
    tapRelease(trigger(), 1_000)
    expect(menuOpen()).toBe(false)
    expect(onOpenChange).toHaveBeenCalledTimes(2)
  })

  it('cancels only the pointerdown, so the scroll itself is not blocked, and a pen is treated like a finger', () => {
    render(<Harness />)
    // The cancel is what keeps Radix from toggling on the press; the browser
    // still scrolls, because only a `touchstart` cancel could stop that.
    expect(touchDown(trigger(), 'pen')).toBe(true)
    expect(menuOpen()).toBe(false)
    tapRelease(trigger())
    expect(menuOpen()).toBe(true)
  })

  it('a press the browser turned into a scroll (pointercancel) is spent, so a later click does not open the menu', () => {
    render(<Harness />)
    touchDown(trigger(), 'touch', 1_000)
    scrollAway(trigger(), 1_000)
    clickAt(trigger(), 1_050)
    expect(menuOpen()).toBe(false)
  })

  it('opens on a tap whose click carries detail 0, as a synthesized tap click can', () => {
    render(<Harness />)
    touchDown(trigger())
    fireEvent.pointerUp(trigger(), { pointerType: 'touch', button: 0 })
    fireEvent.click(trigger(), { button: 0, detail: 0 })
    expect(menuOpen()).toBe(true)
  })

  it('still runs the consumer\'s own click handler, once, on the tap', () => {
    const onTriggerClick = vi.fn()
    render(<Harness onTriggerClick={onTriggerClick} />)
    touchDown(trigger())
    tapRelease(trigger())
    expect(onTriggerClick).toHaveBeenCalledTimes(1)
    expect(menuOpen()).toBe(true)
  })

  it('a disabled trigger stays closed on a tap', () => {
    render(<Harness disabled />)
    touchDown(trigger())
    tapRelease(trigger())
    expect(menuOpen()).toBe(false)
  })
})

describe('DropdownMenuTrigger on a mouse', () => {
  it('opens on the press, as Radix does, and the release click does not close it again', () => {
    const onOpenChange = vi.fn()
    render(<Harness onOpenChange={onOpenChange} />)

    const pointerDown = new PointerEvent('pointerdown', {
      bubbles: true, cancelable: true, pointerType: 'mouse', button: 0,
    })
    Object.defineProperty(pointerDown, 'timeStamp', { value: 1_000 })
    act(() => { trigger().dispatchEvent(pointerDown) })
    expect(menuOpen()).toBe(true)

    const click = createEvent.click(trigger(), { button: 0, detail: 1 })
    Object.defineProperty(click, 'timeStamp', { value: 1_050 })
    fireEvent(trigger(), click)
    expect(menuOpen()).toBe(true)
    expect(onOpenChange).toHaveBeenCalledTimes(1)
  })
})

// A screen reader's activation, Switch Control and a scripted `element.click()`
// send a `click` with no pointer event before it. Radix listens for
// `pointerdown` and `keydown` only, so that click has to toggle the menu itself.
describe('DropdownMenuTrigger on a click with no pointer event', () => {
  it('opens the closed menu, and the next such click closes it', () => {
    const onOpenChange = vi.fn()
    render(<Harness onOpenChange={onOpenChange} />)

    fireEvent.click(trigger(), { detail: 0 })
    expect(menuOpen()).toBe(true)
    expect(onOpenChange).toHaveBeenLastCalledWith(true)

    fireEvent.click(trigger(), { detail: 0 })
    expect(menuOpen()).toBe(false)
    expect(onOpenChange).toHaveBeenLastCalledWith(false)
    expect(onOpenChange).toHaveBeenCalledTimes(2)
  })

  it('still runs the consumer\'s own click handler, once', () => {
    const onTriggerClick = vi.fn()
    render(<Harness onTriggerClick={onTriggerClick} />)
    fireEvent.click(trigger(), { detail: 0 })
    expect(onTriggerClick).toHaveBeenCalledTimes(1)
    expect(menuOpen()).toBe(true)
  })

  it('a disabled trigger stays closed', () => {
    render(<Harness disabled />)
    fireEvent.click(trigger(), { detail: 0 })
    expect(menuOpen()).toBe(false)
  })

  it('a trigger marked aria-disabled stays closed, and opens once the mark is gone', () => {
    function Busy({ busy }: { busy: boolean }) {
      return (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button type="button" data-testid="trigger" aria-disabled={busy}>more</button>
          </DropdownMenuTrigger>
          <DropdownMenuContent data-testid="content">
            <DropdownMenuItem>Rename</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      )
    }
    const { rerender } = render(<Busy busy />)
    fireEvent.click(trigger(), { detail: 0 })
    expect(menuOpen()).toBe(false)

    rerender(<Busy busy={false} />)
    fireEvent.click(trigger(), { detail: 0 })
    expect(menuOpen()).toBe(true)
  })

  it.each([
    ['a press that became a scroll', (el: HTMLElement) => { touchDown(el, 'touch', 1_000); scrollAway(el, 1_000) }],
    ['a mouse press released somewhere else', (el: HTMLElement) => {
      const pointerDown = new PointerEvent('pointerdown', {
        bubbles: true, cancelable: true, pointerType: 'mouse', button: 0,
      })
      Object.defineProperty(pointerDown, 'timeStamp', { value: 1_000 })
      act(() => { el.dispatchEvent(pointerDown) })
      // Radix opened the menu on the press; close it again so the click below starts from closed.
      act(() => { fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' }) })
    }],
  ])('%s does not swallow a later click with no pointer event', (_name, gesture) => {
    render(<Harness />)
    gesture(trigger())
    expect(menuOpen()).toBe(false)

    clickAt(trigger(), 1_000 + 5_000)
    expect(menuOpen()).toBe(true)
  })

  it('a click right after a press that became a scroll still belongs to that press', () => {
    render(<Harness />)
    touchDown(trigger(), 'touch', 1_000)
    scrollAway(trigger(), 1_000)
    clickAt(trigger(), 1_050)
    expect(menuOpen()).toBe(false)
  })

  it('a finger tap still changes the menu once: open on the first tap, closed on the second', () => {
    const onOpenChange = vi.fn()
    render(<Harness onOpenChange={onOpenChange} />)

    touchDown(trigger(), 'touch', 1_000)
    tapRelease(trigger(), 1_000)
    expect(menuOpen()).toBe(true)
    expect(onOpenChange.mock.calls).toEqual([[true]])

    touchDown(trigger(), 'touch', 2_000)
    tapRelease(trigger(), 2_000)
    expect(menuOpen()).toBe(false)
    expect(onOpenChange.mock.calls).toEqual([[true], [false]])
  })

  it('a mouse press still changes the menu once: its release click does not close it', () => {
    const onOpenChange = vi.fn()
    render(<Harness onOpenChange={onOpenChange} />)
    const pointerDown = new PointerEvent('pointerdown', {
      bubbles: true, cancelable: true, pointerType: 'mouse', button: 0,
    })
    Object.defineProperty(pointerDown, 'timeStamp', { value: 1_000 })
    act(() => { trigger().dispatchEvent(pointerDown) })
    const pointerUp = createEvent.pointerUp(trigger(), { pointerType: 'mouse', button: 0 })
    Object.defineProperty(pointerUp, 'timeStamp', { value: 1_000 })
    fireEvent(trigger(), pointerUp)
    const click = createEvent.click(trigger(), { button: 0, detail: 1 })
    Object.defineProperty(click, 'timeStamp', { value: 1_050 })
    fireEvent(trigger(), click)
    expect(menuOpen()).toBe(true)
    expect(onOpenChange.mock.calls).toEqual([[true]])
  })
})
