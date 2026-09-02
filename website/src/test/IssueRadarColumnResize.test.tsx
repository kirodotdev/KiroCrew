import { describe, it, expect, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import {
  loadRailWidth, loadRailCollapsed, RAIL_WIDTH_KEY, RAIL_COLLAPSED_KEY,
  DEFAULT_RAIL_WIDTH, MIN_RAIL_WIDTH, MAX_RAIL_WIDTH, COLLAPSED_RAIL_WIDTH,
} from '../apps/issue-radar/lib/format'
import { useColumnResize, type CollapseConfig } from '../hooks/useColumnResize'
import ResizeHandle from '../components/ResizeHandle'

const COLLAPSE: CollapseConfig = { width: COLLAPSED_RAIL_WIDTH, storageKey: RAIL_COLLAPSED_KEY }
// The default overshoot the hook requires before snapping (see DEFAULT_SLOP).
const SLOP = 48

function Harness({ collapse }: { collapse?: CollapseConfig }) {
  const col = useColumnResize(
    RAIL_WIDTH_KEY, loadRailWidth, MIN_RAIL_WIDTH, MAX_RAIL_WIDTH, collapse, loadRailCollapsed,
  )
  return (
    <div>
      <aside data-testid="col" style={{ width: col.width }} />
      <button data-testid="expand" onClick={col.expand}>{col.collapsed ? 'collapsed' : 'open'}</button>
      <span data-testid="dragging">{col.dragging ? 'dragging' : 'idle'}</span>
      <ResizeHandle handleProps={col.handleProps} label="Resize sidebar" />
    </div>
  )
}

function renderHarness(collapse?: CollapseConfig) {
  const utils = render(<Harness collapse={collapse} />)
  return {
    ...utils,
    col: utils.getByTestId('col'),
    state: utils.getByTestId('expand'),
    dragging: utils.getByTestId('dragging'),
    handle: utils.getByRole('separator', { name: 'Resize sidebar' }),
  }
}

/** One complete pointer drag of `dx` px starting from clientX 0. */
function drag(handle: HTMLElement, dx: number, id = 1) {
  fireEvent.pointerDown(handle, { clientX: 0, pointerId: id })
  fireEvent.pointerMove(handle, { clientX: dx, pointerId: id })
  fireEvent.pointerUp(handle, { clientX: dx, pointerId: id })
}

beforeEach(() => localStorage.clear())

describe('loadRailWidth / loadRailCollapsed', () => {
  it('falls back to the default when nothing is stored', () => {
    expect(loadRailWidth()).toBe(DEFAULT_RAIL_WIDTH)
    expect(loadRailCollapsed()).toBe(false)
  })

  it('honours a stored width inside the allowed range', () => {
    localStorage.setItem(RAIL_WIDTH_KEY, String(MIN_RAIL_WIDTH + 40))
    expect(loadRailWidth()).toBe(MIN_RAIL_WIDTH + 40)
  })

  it('ignores an out-of-range or unparseable stored width', () => {
    // A value persisted under an older min/max — or hand-edited — must not
    // resurrect a rail that is 4px or half the screen wide.
    localStorage.setItem(RAIL_WIDTH_KEY, String(MAX_RAIL_WIDTH + 500))
    expect(loadRailWidth()).toBe(DEFAULT_RAIL_WIDTH)
    localStorage.setItem(RAIL_WIDTH_KEY, 'wide')
    expect(loadRailWidth()).toBe(DEFAULT_RAIL_WIDTH)
  })

  it('reads the collapsed flag independently of the width', () => {
    localStorage.setItem(RAIL_COLLAPSED_KEY, '1')
    expect(loadRailCollapsed()).toBe(true)
    // The width the user had before collapsing survives the collapse.
    localStorage.setItem(RAIL_WIDTH_KEY, '400')
    expect(loadRailWidth()).toBe(400)
  })
})

describe('useColumnResize', () => {
  it('exposes the handle as an accessible vertical separator', () => {
    const { handle } = renderHarness()
    expect(handle.getAttribute('aria-orientation')).toBe('vertical')
    // touch-action:none lets a touch drag resize instead of scrolling the page.
    expect(handle.style.touchAction).toBe('none')
  })

  it('a pointer drag widens the column by the pointer delta and persists it', () => {
    const { col, handle } = renderHarness()
    drag(handle, 60)
    expect(col.style.width).toBe(`${DEFAULT_RAIL_WIDTH + 60}px`)
    expect(localStorage.getItem(RAIL_WIDTH_KEY)).toBe(String(DEFAULT_RAIL_WIDTH + 60))
  })

  it('clamps to the min and max bounds', () => {
    const { col, handle } = renderHarness()
    fireEvent.pointerDown(handle, { clientX: 0, pointerId: 1 })
    fireEvent.pointerMove(handle, { clientX: -5000, pointerId: 1 })
    expect(col.style.width).toBe(`${MIN_RAIL_WIDTH}px`)
    fireEvent.pointerMove(handle, { clientX: 5000, pointerId: 1 })
    fireEvent.pointerUp(handle, { clientX: 5000, pointerId: 1 })
    expect(col.style.width).toBe(`${MAX_RAIL_WIDTH}px`)
    expect(localStorage.getItem(RAIL_WIDTH_KEY)).toBe(String(MAX_RAIL_WIDTH))
  })

  it('resumes a second drag from the new width, not the initial one', () => {
    const { col, handle } = renderHarness()
    drag(handle, 40, 1)
    drag(handle, 30, 2)
    expect(col.style.width).toBe(`${DEFAULT_RAIL_WIDTH + 70}px`)
  })

  it('restores the body cursor and text selection after the drag', () => {
    const { handle } = renderHarness()
    fireEvent.pointerDown(handle, { clientX: 0, pointerId: 1 })
    expect(document.body.style.cursor).toBe('col-resize')
    expect(document.body.style.userSelect).toBe('none')
    fireEvent.pointerUp(handle, { clientX: 0, pointerId: 1 })
    expect(document.body.style.cursor).toBe('')
    expect(document.body.style.userSelect).toBe('')
  })

  it('restores the body styles when unmounted mid-drag', () => {
    const { handle, unmount } = renderHarness()
    fireEvent.pointerDown(handle, { clientX: 0, pointerId: 1 })
    unmount()
    expect(document.body.style.cursor).toBe('')
    expect(document.body.style.userSelect).toBe('')
  })

  it('reports dragging only for the duration of the gesture', () => {
    // Consumers switch off layout animation while this is true — a card layout
    // animation scale-distorts its text on every width change.
    const { handle, dragging } = renderHarness()
    expect(dragging.textContent).toBe('idle')
    fireEvent.pointerDown(handle, { clientX: 0, pointerId: 1 })
    expect(dragging.textContent).toBe('dragging')
    fireEvent.pointerMove(handle, { clientX: 30, pointerId: 1 })
    expect(dragging.textContent).toBe('dragging')
    fireEvent.pointerUp(handle, { clientX: 30, pointerId: 1 })
    expect(dragging.textContent).toBe('idle')
  })
})

describe('useColumnResize — collapsing', () => {
  const toMin = MIN_RAIL_WIDTH - DEFAULT_RAIL_WIDTH // dx that lands exactly on min

  it('holds at the minimum until the pointer overshoots it', () => {
    const { col, state, handle } = renderHarness(COLLAPSE)
    fireEvent.pointerDown(handle, { clientX: 0, pointerId: 1 })
    fireEvent.pointerMove(handle, { clientX: toMin - (SLOP - 1), pointerId: 1 })
    expect(col.style.width).toBe(`${MIN_RAIL_WIDTH}px`)
    expect(state.textContent).toBe('open')
  })

  it('snaps to the collapsed strip when dragged past the minimum', () => {
    const { col, state, handle } = renderHarness(COLLAPSE)
    drag(handle, toMin - SLOP)
    expect(col.style.width).toBe(`${COLLAPSED_RAIL_WIDTH}px`)
    expect(state.textContent).toBe('collapsed')
    expect(localStorage.getItem(RAIL_COLLAPSED_KEY)).toBe('1')
    // The collapsed strip width must never be persisted as a real column width.
    expect(localStorage.getItem(RAIL_WIDTH_KEY)).not.toBe(String(COLLAPSED_RAIL_WIDTH))
  })

  it('does not re-expand on a small outward nudge (hysteresis)', () => {
    localStorage.setItem(RAIL_COLLAPSED_KEY, '1')
    const { col, handle } = renderHarness(COLLAPSE)
    expect(col.style.width).toBe(`${COLLAPSED_RAIL_WIDTH}px`)
    drag(handle, SLOP - 1)
    expect(col.style.width).toBe(`${COLLAPSED_RAIL_WIDTH}px`)
    expect(localStorage.getItem(RAIL_COLLAPSED_KEY)).toBe('1')
  })

  it('re-expands to the remembered width once pulled clear of the strip', () => {
    localStorage.setItem(RAIL_COLLAPSED_KEY, '1')
    const { col, handle } = renderHarness(COLLAPSE)
    drag(handle, SLOP)
    expect(col.style.width).toBe(`${DEFAULT_RAIL_WIDTH}px`)
    expect(localStorage.getItem(RAIL_COLLAPSED_KEY)).toBe('0')
  })

  it('a drag-expand restores the saved width and grows from there', () => {
    // The reopen must land ON the saved width: resolving to the raw pointer
    // position would reopen at the clamped minimum and bank it, so a 400px rail
    // would come back as 220px.
    localStorage.setItem(RAIL_WIDTH_KEY, '400')
    localStorage.setItem(RAIL_COLLAPSED_KEY, '1')
    const { col, handle } = renderHarness(COLLAPSE)
    expect(col.style.width).toBe(`${COLLAPSED_RAIL_WIDTH}px`)
    fireEvent.pointerDown(handle, { clientX: 0, pointerId: 1 })
    fireEvent.pointerMove(handle, { clientX: SLOP, pointerId: 1 })
    expect(col.style.width).toBe('400px')
    fireEvent.pointerMove(handle, { clientX: SLOP + 30, pointerId: 1 })
    fireEvent.pointerUp(handle, { clientX: SLOP + 30, pointerId: 1 })
    expect(col.style.width).toBe('430px')
    expect(localStorage.getItem(RAIL_WIDTH_KEY)).toBe('430')
  })

  it('a slow collapse still reopens at the pre-drag width', () => {
    // A drag that ends collapsed must not bank what it swept past: if onMove
    // banked every intermediate width, dragging a 400px rail slowly THROUGH the
    // minimum would leave 220 remembered and reopening would lose the 400.
    localStorage.setItem(RAIL_WIDTH_KEY, '400')
    const { col, state, handle } = renderHarness(COLLAPSE)
    expect(col.style.width).toBe('400px')
    fireEvent.pointerDown(handle, { clientX: 0, pointerId: 1 })
    fireEvent.pointerMove(handle, { clientX: MIN_RAIL_WIDTH - 400, pointerId: 1 }) // rest on the min
    fireEvent.pointerMove(handle, { clientX: MIN_RAIL_WIDTH - 400 - SLOP, pointerId: 1 }) // then past it
    fireEvent.pointerUp(handle, { clientX: MIN_RAIL_WIDTH - 400 - SLOP, pointerId: 1 })
    expect(col.style.width).toBe(`${COLLAPSED_RAIL_WIDTH}px`)
    expect(localStorage.getItem(RAIL_WIDTH_KEY)).toBe('400')
    fireEvent.click(state)
    expect(col.style.width).toBe('400px')
  })

  it('expand() reopens at the width the user had chosen, not the default', () => {
    localStorage.setItem(RAIL_WIDTH_KEY, '400')
    localStorage.setItem(RAIL_COLLAPSED_KEY, '1')
    const { col, state } = renderHarness(COLLAPSE)
    expect(col.style.width).toBe(`${COLLAPSED_RAIL_WIDTH}px`)
    fireEvent.click(state)
    expect(col.style.width).toBe('400px')
    expect(localStorage.getItem(RAIL_WIDTH_KEY)).toBe('400')
    expect(localStorage.getItem(RAIL_COLLAPSED_KEY)).toBe('0')
  })

  it('never collapses a column that did not opt in', () => {
    const { col, handle } = renderHarness()
    drag(handle, -5000)
    expect(col.style.width).toBe(`${MIN_RAIL_WIDTH}px`)
    expect(localStorage.getItem(RAIL_COLLAPSED_KEY)).toBeNull()
  })
})
