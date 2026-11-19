/**
 * Beside a reported fold the fold owns two shell states: the sessions list ends
 * at it, and the rail stays compact where an expanded one would push the list
 * across the fold. Neither offers a control that
 * would do nothing on screen and still rewrite the saved preference.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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
  it('is aria-disabled, so a click cannot flip the saved rail state', () => {
    const toggleNav = vi.fn()
    render(<RailBrandToggle effectiveCollapsed locked toggleNav={toggleNav} avatar="" branding={undefined as never} botName="Kiro Crew" />)
    const button = screen.getByRole('button', { name: i18nT('app.sidebar_compact_while_folded') })
    expect(button).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(button)
    expect(toggleNav).not.toHaveBeenCalled()
  })

  it('shows a visible locked cue on the mark', () => {
    render(<RailBrandToggle effectiveCollapsed locked toggleNav={vi.fn()} avatar="" branding={undefined as never} botName="Kiro Crew" />)
    const button = screen.getByRole('button')
    fireEvent.pointerDown(button, { pointerType: 'touch' })
    fireEvent.focus(button)
    fireEvent.click(button)
    // The hover tip never shows on touch; the cue is on the mark either way.
    const cue = screen.getByTestId('rail-locked-cue')
    expect(cue).toBeVisible()
    expect(cue).toHaveAttribute('aria-hidden', 'true')
  })

  describe('a press on the locked toggle says why below the mark', () => {
    const note = i18nT('app.sidebar_compact_while_folded')
    afterEach(() => { segmentsMock.value = null })
    const renderLocked = () => render(<RailBrandToggle effectiveCollapsed locked toggleNav={vi.fn()} avatar="" branding={undefined as never} botName="Kiro Crew" />)
    afterEach(() => { vi.useRealTimers() })

    it('a tap shows the reason, the cue stays, and it clears after its time', () => {
      vi.useFakeTimers()
      renderLocked()
      const button = screen.getByRole('button')
      fireEvent.pointerDown(button, { pointerType: 'touch' })
      fireEvent.click(button)
      expect(screen.getByTestId('rail-lock-note')).toHaveTextContent(note)
      expect(screen.getByTestId('rail-locked-cue')).toBeInTheDocument()
      act(() => { vi.advanceTimersByTime(2000) })
      expect(screen.queryByTestId('rail-lock-note')).toBeNull()
    })

    it('renders the reason in the overlay root, outside the rail, so the rail cannot clip it', () => {
      const { container } = renderLocked()
      fireEvent.click(screen.getByRole('button'))
      const shown = screen.getByTestId('rail-lock-note')
      expect(container.contains(shown)).toBe(false)
    })

    it('sits below the mark, so it cannot cover the sessions header beside the rail', () => {
      renderLocked()
      const button = screen.getByRole('button')
      vi.spyOn(button, 'getBoundingClientRect').mockReturnValue({ top: 10, left: 8, width: 58, height: 44, right: 66, bottom: 54, x: 8, y: 10, toJSON: () => ({}) } as DOMRect)
      fireEvent.click(button)
      const style = screen.getByTestId('rail-lock-note').style
      // The sessions header row is level with the mark (its top to its bottom),
      // so a note whose top is below the mark's bottom cannot intersect it.
      expect(parseFloat(style.top)).toBeGreaterThanOrEqual(54)
    })

    it('beside a fold, ends before the hinge: its width is bounded and it wraps', () => {
      segmentsMock.value = [{ x: 0, width: 384 }, { x: 384, width: 384 }]
      renderLocked()
      const button = screen.getByRole('button')
      vi.spyOn(button, 'getBoundingClientRect').mockReturnValue({ top: 10, left: 17, width: 58, height: 44, right: 75, bottom: 54, x: 17, y: 10, toJSON: () => ({}) } as DOMRect)
      fireEvent.click(button)
      const note = screen.getByTestId('rail-lock-note')
      expect(parseFloat(note.style.left) + parseFloat(note.style.maxWidth)).toBeLessThanOrEqual(384)
      expect(note.className).toContain('whitespace-normal')
    })

    it('the hover/focus tip on the locked toggle also sits below the mark and ends at the fold', () => {
      segmentsMock.value = [{ x: 0, width: 384 }, { x: 384, width: 384 }]
      renderLocked()
      const button = screen.getByRole('button')
      vi.spyOn(button, 'getBoundingClientRect').mockReturnValue({ top: 10, left: 17, width: 58, height: 44, right: 75, bottom: 54, x: 17, y: 10, toJSON: () => ({}) } as DOMRect)
      fireEvent.mouseEnter(button)
      const tip = screen.getByTestId('rail-expand-tip')
      expect(parseFloat(tip.style.top)).toBeGreaterThanOrEqual(54)
      expect(parseFloat(tip.style.left) + parseFloat(tip.style.maxWidth)).toBeLessThanOrEqual(384)
    })

    for (const key of ['{Enter}', ' '] as const) {
      it(`a keyboard press (${key === ' ' ? 'Space' : 'Enter'}) shows it, and blur clears it`, async () => {
        const user = userEvent.setup()
        renderLocked()
        await user.tab()
        await user.keyboard(key)
        expect(screen.getByTestId('rail-lock-note')).toHaveTextContent(note)
        await user.tab()
        expect(screen.queryByTestId('rail-lock-note')).toBeNull()
      })
    }
  })

  it('an unlocked toggle shows no locked cue and still toggles', () => {
    const toggleNav = vi.fn()
    render(<RailBrandToggle effectiveCollapsed toggleNav={toggleNav} avatar="" branding={undefined as never} botName="Kiro Crew" />)
    expect(screen.queryByTestId('rail-locked-cue')).toBeNull()
    fireEvent.click(screen.getByRole('button'))
    expect(toggleNav).toHaveBeenCalledTimes(1)
  })

  it('stays in the tab order, so a keyboard user reaches the reason', () => {
    render(<RailBrandToggle effectiveCollapsed locked toggleNav={vi.fn()} avatar="" branding={undefined as never} botName="Kiro Crew" />)
    const button = screen.getByRole('button', { name: i18nT('app.sidebar_compact_while_folded') })
    // Focusable and in the tab order (a disabled button would be neither).
    expect(button).not.toBeDisabled()
    expect(button.tabIndex).toBe(0)
  })

  it('says why in its name and title, not that it would expand', () => {
    render(<RailBrandToggle effectiveCollapsed locked toggleNav={vi.fn()} avatar="" branding={undefined as never} botName="Kiro Crew" />)
    const button = screen.getByRole('button')
    expect(button).toHaveAttribute('title', i18nT('app.sidebar_compact_while_folded'))
    expect(button).toHaveAccessibleName(i18nT('app.sidebar_compact_while_folded'))
    expect(button.getAttribute('title')).not.toBe(i18nT('app.expand_sidebar'))
  })

  // The title and the accessible name are two copies of one expression (the
  // ui-index reader cannot follow a shared value), so this pins them together.
  for (const [state, props] of [
    ['locked', { effectiveCollapsed: true, locked: true }],
    ['collapsed', { effectiveCollapsed: true, locked: false }],
    ['expanded', { effectiveCollapsed: false, locked: false }],
  ] as const) {
    it(`the title equals the accessible name when ${state}`, () => {
      render(<RailBrandToggle {...props} toggleNav={vi.fn()} avatar="" branding={undefined as never} botName="Kiro Crew" />)
      const button = screen.getByRole('button')
      expect(button.getAttribute('title')).toBeTruthy()
      expect(button).toHaveAccessibleName(button.getAttribute('title')!)
    })
  }

  it('keeps the expand name when the rail is free', () => {
    render(<RailBrandToggle effectiveCollapsed toggleNav={vi.fn()} avatar="" branding={undefined as never} botName="Kiro Crew" />)
    const button = screen.getByRole('button', { name: i18nT('app.expand_sidebar') })
    expect(button).toBeEnabled()
    expect(button).not.toHaveAttribute('aria-disabled')
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

  it('an embedded chat page (the App Kit ChatPanel) keeps its own width beside a fold', () => {
    localStorage.removeItem(SIDEBAR_LS_KEY)
    segmentsMock.value = [{ x: 0, width: 475.5 }, { x: 475.5, width: 475.5 }]
    const { result } = renderHook(() => useSidebarResize({ onWidthChange: undefined, onDragChange: undefined, embedded: true, boardActive: false }))
    expect(result.current.foldLocked).toBe(false)
    expect(result.current.paintedSidebarWidth).toBe(260)
    act(() => { result.current.widenForBoard(900) })
    expect(localStorage.getItem(SIDEBAR_LS_KEY)).toBe('900')
  })

  it("the dashboard's own chat page beside the same fold still locks", () => {
    localStorage.removeItem(SIDEBAR_LS_KEY)
    segmentsMock.value = [{ x: 0, width: 475.5 }, { x: 475.5, width: 475.5 }]
    const { result } = renderHook(() => useSidebarResize({ onWidthChange: undefined, onDragChange: undefined, embedded: false, boardActive: false }))
    expect(result.current.foldLocked).toBe(true)
    act(() => { result.current.widenForBoard(900) })
    expect(localStorage.getItem(SIDEBAR_LS_KEY)).toBeNull()
  })

  it('without a fold, paints the saved width and offers the handle', () => {
    const { result } = renderHook(() => useSidebarResize({ onWidthChange: undefined, onDragChange: undefined, boardActive: false }))
    expect(result.current.foldLocked).toBe(false)
    expect(result.current.paintedSidebarWidth).not.toBe(475.5 - 236)
  })
})
