/**
 * Beside a reported fold the fold owns two shell states: the sessions list ends
 * at it, and the rail stays compact where an expanded one would push the list
 * across the fold. Neither offers a control that
 * would do nothing on screen and still rewrite the saved preference.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { RailBrandToggle } from '../shell/nav/railChrome'
import { i18nT } from '../i18n/t'

const segmentsMock = vi.hoisted(() => ({ value: null as null | { x: number; width: number }[] }))
vi.mock('../hooks/useViewportSegments', async (orig) => ({
  ...(await orig<typeof import('../hooks/useViewportSegments')>()),
  useViewportSegments: () => segmentsMock.value,
}))

import { useSidebarResize } from '../pages/chat-sidebar/resize'
import { SIDEBAR_LS_KEY } from '../pages/chat-sidebar/persistence'

afterEach(cleanup)

describe('the rail toggle while a fold holds the rail compact', () => {
  it('is disabled, so a click cannot flip the saved rail state', () => {
    const toggleNav = vi.fn()
    render(<RailBrandToggle effectiveCollapsed locked toggleNav={toggleNav} avatar="" branding={undefined as never} botName="Kiro Crew" />)
    const button = screen.getByRole('button', { name: i18nT('app.expand_sidebar') })
    expect(button).toBeDisabled()
    fireEvent.click(button)
    expect(toggleNav).not.toHaveBeenCalled()
  })
})

describe('the sessions list beside a reported fold', () => {
  beforeEach(() => { localStorage.setItem(SIDEBAR_LS_KEY, '300') })
  afterEach(() => { segmentsMock.value = null; localStorage.clear() })

  it('paints to the fold and reports the lock that hides the resize handle', () => {
    segmentsMock.value = [{ x: 0, width: 475.5 }, { x: 475.5, width: 475.5 }]
    const { result } = renderHook(() => useSidebarResize({ onWidthChange: undefined, onDragChange: undefined, boardActive: false }))
    expect(result.current.foldLocked).toBe(true)
    expect(result.current.paintedSidebarWidth).toBe(475.5 - 236)
    expect(localStorage.getItem(SIDEBAR_LS_KEY)).toBe('300')
  })

  it('a board opened while folded does not rewrite the saved width', () => {
    segmentsMock.value = [{ x: 0, width: 475.5 }, { x: 475.5, width: 475.5 }]
    const { result } = renderHook(() => useSidebarResize({ onWidthChange: undefined, onDragChange: undefined, boardActive: false }))
    act(() => { result.current.widenForBoard(900) })
    expect(localStorage.getItem(SIDEBAR_LS_KEY)).toBe('300')
  })

  it('folding mid-drag ends the drag, and the release saves nothing', () => {
    const onDragChange = vi.fn()
    const { result, rerender } = renderHook(() => useSidebarResize({ onWidthChange: undefined, onDragChange, boardActive: false }))
    const handle = { setPointerCapture() {}, releasePointerCapture() {} }
    const ev = (clientX: number, type = 'pointermove') =>
      ({ type, pointerType: 'mouse', button: 0, pointerId: 1, clientX, clientY: 0, currentTarget: handle, preventDefault() {} }) as unknown as React.PointerEvent
    act(() => { result.current.sidebarResize.onPointerDown(ev(300, 'pointerdown')) })
    act(() => { result.current.sidebarResize.onPointerMove(ev(380)) })
    expect(document.body.style.userSelect).toBe('none')
    expect(onDragChange).toHaveBeenLastCalledWith(true)

    segmentsMock.value = [{ x: 0, width: 475.5 }, { x: 475.5, width: 475.5 }]
    rerender()
    expect(result.current.foldLocked).toBe(true)
    expect(document.body.style.userSelect).toBe('')
    expect(document.body.style.cursor).toBe('')
    expect(onDragChange).toHaveBeenLastCalledWith(false)

    // A release that still reaches the hook (the handle came back on unfolding) is inert.
    segmentsMock.value = null
    rerender()
    const painted = result.current.paintedSidebarWidth
    act(() => { result.current.sidebarResize.onPointerMove(ev(500)) })
    act(() => { result.current.sidebarResize.onPointerUp(ev(500, 'pointerup')) })
    expect(result.current.paintedSidebarWidth).toBe(painted)
    expect(localStorage.getItem(SIDEBAR_LS_KEY)).toBe('300')
  })

  it('without a fold, paints the saved width and offers the handle', () => {
    const { result } = renderHook(() => useSidebarResize({ onWidthChange: undefined, onDragChange: undefined, boardActive: false }))
    expect(result.current.foldLocked).toBe(false)
    expect(result.current.paintedSidebarWidth).not.toBe(475.5 - 236)
  })
})
