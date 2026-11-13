// On a phone, scrolling the chat list kept opening a row's ⋮ menu. Radix opens
// a DropdownMenu trigger on `pointerdown` for every pointer type, so the menu
// opened the moment a finger landed on the trigger (or its enlarged touch-hit
// area), before the browser could tell a tap from a scroll. The shared
// `DropdownMenuTrigger` defers a touch or pen press to the tap's `click`, which
// a scroll never produces. A mouse press still opens on `pointerdown`.
import { act, fireEvent, render, screen } from '@testing-library/react'
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
function touchDown(el: HTMLElement, pointerType: 'touch' | 'pen' = 'touch'): boolean {
  let notCancelled = true
  act(() => {
    notCancelled = el.dispatchEvent(new PointerEvent('pointerdown', {
      bubbles: true, cancelable: true, pointerType, button: 0,
    }))
  })
  return !notCancelled
}

/** The finger lifts without moving: the browser fires the tap's click (detail 1). */
function tapRelease(el: HTMLElement): void {
  fireEvent.pointerUp(el, { pointerType: 'touch', button: 0 })
  fireEvent.click(el, { button: 0, detail: 1 })
}

/** The finger moves and lifts elsewhere: a scroll, so the browser fires no click. */
function scrollAway(el: HTMLElement): void {
  fireEvent.pointerMove(el, { pointerType: 'touch', clientY: 80 })
  fireEvent.pointerCancel(el, { pointerType: 'touch' })
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

    touchDown(trigger())
    tapRelease(trigger())
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

    touchDown(trigger())
    expect(menuOpen()).toBe(false)
    // The menu's own outside-dismiss may run between the press and its click;
    // either way the click must not open the menu again.
    tapRelease(trigger())
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
    touchDown(trigger())
    scrollAway(trigger())
    fireEvent.click(trigger(), { detail: 0 })
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

    act(() => {
      trigger().dispatchEvent(new PointerEvent('pointerdown', {
        bubbles: true, cancelable: true, pointerType: 'mouse', button: 0,
      }))
    })
    expect(menuOpen()).toBe(true)

    fireEvent.click(trigger(), { button: 0, detail: 1 })
    expect(menuOpen()).toBe(true)
    expect(onOpenChange).toHaveBeenCalledTimes(1)
  })
})
