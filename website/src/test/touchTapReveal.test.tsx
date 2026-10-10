/**
 * A touch tap must not reveal hover content (#15341).
 *
 * On iOS a tap replays mouseover / mouseenter before Safari decides to send
 * the click. Content that appears from that replay -- synchronously or from a
 * timer of 400ms or less -- makes WebKit read the tap as a hover and drop the
 * click, so the user has to tap twice. Each site below reveals from
 * `onMouseEnter`; each now reads the tap's own pointer type and skips the
 * reveal for `touch`, while a mouse (even on a touch device) still hovers.
 * RefLink's coverage lives with its other tests in IssueRadarRefSheet.test.tsx.
 */
import { useRef } from 'react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { useHoverIntent, HOVER_OPEN_MS } from '../hooks/useHoverIntent'
import { useNavTip } from '../hooks/useNavTip'
import MarkdownOutlineRail from '../components/MarkdownToc'

/** One finger tap, in the order a browser sends it: the touch pointer's own
 *  events, then the mouse events it replays once the finger lifts (mousedown
 *  moves focus), then the click. */
function tap(el: HTMLElement) {
  fireEvent.pointerEnter(el, { pointerType: 'touch' })
  fireEvent.pointerDown(el, { pointerType: 'touch' })
  fireEvent.pointerUp(el, { pointerType: 'touch' })
  fireEvent.mouseEnter(el)
  fireEvent.mouseDown(el)
  fireEvent.focus(el)
  fireEvent.mouseUp(el)
  fireEvent.click(el)
}

/** A mouse arriving on `el`: a mouse pointer's enter, then the mouse enter. */
function mouseIn(el: HTMLElement) {
  fireEvent.pointerEnter(el, { pointerType: 'mouse' })
  fireEvent.mouseEnter(el)
}

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

describe('useHoverIntent trigger on touch', () => {
  function Harness({ onClick }: { onClick: () => void }) {
    const hover = useHoverIntent()
    return (
      <>
        <button type="button" onClick={onClick} {...hover.triggerProps}>trigger</button>
        {hover.open && <div role="menu" {...hover.surfaceProps}>surface</div>}
      </>
    )
  }

  it('a touch tap runs the click and never opens the surface', () => {
    const onClick = vi.fn()
    render(<Harness onClick={onClick} />)
    tap(screen.getByRole('button', { name: 'trigger' }))
    expect(onClick).toHaveBeenCalledTimes(1)
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_MS * 5) })
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('a mouse on a touch device still opens after the hover delay', () => {
    render(<Harness onClick={() => {}} />)
    mouseIn(screen.getByRole('button', { name: 'trigger' }))
    expect(screen.queryByRole('menu')).toBeNull()
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_MS) })
    expect(screen.getByRole('menu')).toBeInTheDocument()
  })

  it('ArrowDown still opens the surface right after a tap', () => {
    render(<Harness onClick={() => {}} />)
    const trigger = screen.getByRole('button', { name: 'trigger' })
    tap(trigger)
    fireEvent.keyDown(trigger, { key: 'ArrowDown' })
    expect(screen.getByRole('menu')).toBeInTheDocument()
  })
})

describe('useNavTip on touch', () => {
  function Row({ onClick }: { onClick: () => void }) {
    const { tip, rowRef, showTip, hideTip, pointerProps } = useNavTip<HTMLButtonElement>(true)
    return (
      <>
        <button
          ref={rowRef}
          type="button"
          onClick={onClick}
          {...pointerProps}
          onMouseEnter={showTip}
          onMouseLeave={hideTip}
          onFocus={showTip}
          onBlur={hideTip}
        >
          row
        </button>
        {tip && <div data-testid="nav-tip">label</div>}
      </>
    )
  }

  it('a touch tap runs the row\'s click and never mounts the label', () => {
    const onClick = vi.fn()
    render(<Row onClick={onClick} />)
    tap(screen.getByRole('button', { name: 'row' }))
    expect(onClick).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('nav-tip')).toBeNull()
  })

  it('a mouse on a touch device still mounts the label on enter', () => {
    render(<Row onClick={() => {}} />)
    mouseIn(screen.getByRole('button', { name: 'row' }))
    expect(screen.getByTestId('nav-tip')).toBeInTheDocument()
  })

  it('keyboard focus after the tap has settled still mounts the label', () => {
    render(<Row onClick={() => {}} />)
    const row = screen.getByRole('button', { name: 'row' })
    tap(row)
    fireEvent.blur(row)
    act(() => { vi.advanceTimersByTime(1000) })
    fireEvent.focus(row)
    expect(screen.getByTestId('nav-tip')).toBeInTheDocument()
  })
})

describe('MarkdownToc rail on touch', () => {
  class InertObserver { observe() {} unobserve() {} disconnect() {} }
  beforeEach(() => {
    vi.stubGlobal('IntersectionObserver', InertObserver)
    vi.stubGlobal('ResizeObserver', InertObserver)
  })
  afterEach(() => { vi.unstubAllGlobals() })

  function Doc() {
    const ref = useRef<HTMLDivElement>(null)
    return (
      <div style={{ position: 'relative' }}>
        <div ref={ref}>
          <h2>First</h2>
          <h2>Second</h2>
          <h2>Third</h2>
        </div>
        <MarkdownOutlineRail containerRef={ref} />
      </div>
    )
  }

  /** The labelled flyout: always mounted, revealed by its classes. */
  const flyout = (container: HTMLElement) => container.querySelector('nav > div[aria-hidden]') as HTMLElement
  const expanded = (container: HTMLElement) => flyout(container).className.includes('opacity-100')
  /** Whether tick `name` is the active (lit) one -- what a tick's click sets. */
  const lit = (name: string) =>
    (screen.getByRole('button', { name }).firstElementChild as HTMLElement).style.background === 'var(--accent)'

  it('a touch tap on a tick runs its click and never expands the flyout', () => {
    const { container } = render(<Doc />)
    const tick = screen.getByRole('button', { name: 'Second' })
    expect(lit('Second')).toBe(false)
    tap(tick)
    expect(lit('Second')).toBe(true)
    expect(expanded(container)).toBe(false)
  })

  it('a mouse on a touch device still expands the flyout on enter', () => {
    const { container } = render(<Doc />)
    mouseIn(screen.getByRole('button', { name: 'Second' }))
    expect(expanded(container)).toBe(true)
  })
})
