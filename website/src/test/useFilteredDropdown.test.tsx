import { act, render, renderHook, screen } from '@testing-library/react'
import type React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useFilteredDropdown } from '../hooks/useFilteredDropdown'
import { usePressActivation } from '../hooks/usePressActivation'

/** Click-outside dismissal of the shared filtered dropdown. The listener is
 *  attached on a zero timeout after open, so every case advances fake timers
 *  before clicking. */
describe('useFilteredDropdown click-outside', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers(); document.body.replaceChildren() })

  function openDropdown() {
    const hook = renderHook(() => useFilteredDropdown([{ name: 'a' }, { name: 'b' }]))
    const panel = document.createElement('div')
    document.body.appendChild(panel)
    act(() => {
      // The component would pass this ref to its panel; the hook test hands
      // it the element directly.
      (hook.result.current.dropdownRef as React.MutableRefObject<HTMLDivElement | null>).current = panel
      hook.result.current.setOpen(true)
    })
    act(() => { vi.runOnlyPendingTimers() })
    expect(hook.result.current.open).toBe(true)
    return { hook, panel }
  }

  it('closes on a click outside the dropdown', () => {
    const { hook } = openDropdown()
    const outside = document.createElement('div')
    document.body.appendChild(outside)
    act(() => { outside.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
    expect(hook.result.current.open).toBe(false)
  })

  it('stays open on a click inside the dropdown', () => {
    const { hook, panel } = openDropdown()
    act(() => { panel.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
    expect(hook.result.current.open).toBe(true)
  })

  it('stays open on a click inside a portaled help tooltip', () => {
    // InfoTip portals its role="tooltip" body to document.body, outside
    // dropdownRef. A click on the help text must not dismiss the picker that
    // opened it.
    const { hook } = openDropdown()
    const tooltip = document.createElement('div')
    tooltip.setAttribute('role', 'tooltip')
    const text = document.createElement('span')
    tooltip.appendChild(text)
    document.body.appendChild(tooltip)
    act(() => { text.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
    expect(hook.result.current.open).toBe(true)
  })

  it('closes on a mouse press outside, before the release', () => {
    // Matches triggers that open on the press: pressing another picker's chip
    // closes this list at the same instant the other one opens.
    const { hook } = openDropdown()
    const outside = document.createElement('div')
    document.body.appendChild(outside)
    act(() => { outside.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerType: 'mouse' })) })
    expect(hook.result.current.open).toBe(false)
  })

  it('stays open on a touch press outside, so a scroll does not dismiss it', () => {
    const { hook } = openDropdown()
    const outside = document.createElement('div')
    document.body.appendChild(outside)
    act(() => { outside.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerType: 'touch' })) })
    expect(hook.result.current.open).toBe(true)
  })

  it('leaves right-button and modified presses outside to the click path', () => {
    // Same split as the triggers: a shift-click on a chip acts on click, so
    // dismissing on its press would close the list and the click reopen it.
    const { hook } = openDropdown()
    const outside = document.createElement('div')
    document.body.appendChild(outside)
    for (const init of [{ button: 2 }, { button: 1 }, { button: 0, shiftKey: true }, { button: 0, ctrlKey: true }]) {
      act(() => { outside.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerType: 'mouse', ...init })) })
      expect(hook.result.current.open).toBe(true)
    }
  })

  it('ignores the release of the press that opened it', () => {
    // The trigger opens on pointer-down, so its own click arrives after this
    // listener is attached and lands outside the panel. That click is the
    // tail of the opening press, not a click outside.
    const { hook } = openDropdown()
    function Trigger() {
      const bind = usePressActivation()
      return <button {...bind(() => {})}>chip</button>
    }
    render(<Trigger />)
    const trigger = screen.getByRole('button', { name: 'chip' })
    act(() => { trigger.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerType: 'mouse', button: 0 })) })
    // The press is also a press outside this list; reopen as the trigger would.
    act(() => { hook.result.current.setOpen(true) })
    act(() => { vi.runOnlyPendingTimers() })
    act(() => { trigger.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 })) })
    expect(hook.result.current.open).toBe(true)
  })

  it('closes on a click that no press-activated trigger consumed', () => {
    // A click that no press-activated trigger consumed is a plain click outside.
    const { hook } = openDropdown()
    render(<button>plain</button>)
    act(() => { screen.getByRole('button', { name: 'plain' }).dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 })) })
    expect(hook.result.current.open).toBe(false)
  })
})
