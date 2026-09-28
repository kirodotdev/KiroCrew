import { act, renderHook } from '@testing-library/react'
import type React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useFilteredDropdown } from '../hooks/useFilteredDropdown'

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
})
