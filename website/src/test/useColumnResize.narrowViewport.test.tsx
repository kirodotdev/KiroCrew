// A fixed-width rail cannot share a phone viewport with the pane beside it, so
// `useColumnResize` renders a collapsible column as its strip while the viewport
// is narrow. What these tests pin is the part that is easy to get wrong: the
// forced strip is a RENDER decision, never a stored preference, and the user can
// still open the column from it.
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'

let mockIsMobile = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mockIsMobile }))

import { useColumnResize, type CollapseConfig } from '../hooks/useColumnResize'
import ResizeHandle from '../components/ResizeHandle'

const WIDTH_KEY = 'kc:test:col-width'
const COLLAPSED_KEY = 'kc:test:col-collapsed'
const MIN = 220
const MAX = 460
const STRIP = 48
const STORED_OPEN = 300

const COLLAPSE: CollapseConfig = { width: STRIP, storageKey: COLLAPSED_KEY, whenNarrow: true }
/** A collapsible column that did NOT opt in — it must be left alone when narrow. */
const COLLAPSE_NO_OPT_IN: CollapseConfig = { width: STRIP, storageKey: COLLAPSED_KEY }

const loadWidth = () => {
  const raw = Number(localStorage.getItem(WIDTH_KEY))
  return Number.isFinite(raw) && raw > 0 ? Math.min(MAX, Math.max(MIN, raw)) : STORED_OPEN
}
const loadCollapsed = () => localStorage.getItem(COLLAPSED_KEY) === '1'

function Harness({ collapse }: { collapse?: CollapseConfig }) {
  const col = useColumnResize(WIDTH_KEY, loadWidth, MIN, MAX, collapse, loadCollapsed)
  return (
    <div>
      <aside data-testid="col" style={{ width: col.width }} />
      <span data-testid="state">{col.collapsed ? 'collapsed' : 'open'}</span>
      <button data-testid="expand" onClick={col.expand}>expand</button>
      <button data-testid="collapse" onClick={col.collapse}>collapse</button>
      <ResizeHandle handleProps={col.handleProps} label="Resize sidebar" onNudge={col.nudge} />
    </div>
  )
}

/** A pointer press that never moves — the shape of a stray tap on the handle. */
function tap(handle: HTMLElement, id = 1) {
  fireEvent.pointerDown(handle, { clientX: 0, pointerId: id })
  fireEvent.pointerUp(handle, { clientX: 0, pointerId: id })
}

const widthOf = (el: HTMLElement) => el.style.width

describe('useColumnResize on a narrow viewport', () => {
  beforeEach(() => {
    localStorage.clear()
    localStorage.setItem(WIDTH_KEY, String(STORED_OPEN))
    localStorage.setItem(COLLAPSED_KEY, '0')
    mockIsMobile = false
  })

  it('renders the strip even though the stored flag says expanded', () => {
    mockIsMobile = true
    const { getByTestId } = render(<Harness collapse={COLLAPSE} />)
    expect(getByTestId('state').textContent).toBe('collapsed')
    expect(widthOf(getByTestId('col'))).toBe(`${STRIP}px`)
  })

  it('leaves the stored collapse preference untouched', () => {
    mockIsMobile = true
    const { getByTestId } = render(<Harness collapse={COLLAPSE} />)
    // Precondition: the forced strip is what we are asserting about. Without it
    // the storage assertions below would pass trivially.
    expect(getByTestId('state').textContent).toBe('collapsed')
    // The desktop preference must survive a phone visit: persisting the forced
    // strip would come back as a collapsed rail on the next desktop session.
    expect(localStorage.getItem(COLLAPSED_KEY)).toBe('0')
    expect(localStorage.getItem(WIDTH_KEY)).toBe(String(STORED_OPEN))
  })

  it('still opens from the strip when the user asks', () => {
    mockIsMobile = true
    const { getByTestId } = render(<Harness collapse={COLLAPSE} />)
    expect(getByTestId('state').textContent).toBe('collapsed')
    fireEvent.click(getByTestId('expand'))
    expect(getByTestId('state').textContent).toBe('open')
    expect(widthOf(getByTestId('col'))).toBe(`${STORED_OPEN}px`)
  })

  it('re-collapses when the user explicitly collapses again', () => {
    mockIsMobile = true
    const { getByTestId } = render(<Harness collapse={COLLAPSE} />)
    expect(getByTestId('state').textContent).toBe('collapsed')
    fireEvent.click(getByTestId('expand'))
    expect(getByTestId('state').textContent).toBe('open')
    fireEvent.click(getByTestId('collapse'))
    expect(getByTestId('state').textContent).toBe('collapsed')
  })

  it('does nothing to a column that has no collapsed form', () => {
    mockIsMobile = true
    const { getByTestId } = render(<Harness />)
    expect(getByTestId('state').textContent).toBe('open')
    expect(widthOf(getByTestId('col'))).toBe(`${STORED_OPEN}px`)
  })

  it('does nothing to a collapsible column that did not opt in', () => {
    // The strip alone is half the behaviour: a page without the drill-down would
    // get an expand button that leads straight back into the squeeze. So pages
    // opt in, and one that has not is left exactly as it is on desktop.
    mockIsMobile = true
    const { getByTestId } = render(<Harness collapse={COLLAPSE_NO_OPT_IN} />)
    expect(getByTestId('state').textContent).toBe('open')
    expect(widthOf(getByTestId('col'))).toBe(`${STORED_OPEN}px`)
  })

  it('leaves a desktop viewport exactly as it was', () => {
    const { getByTestId } = render(<Harness collapse={COLLAPSE} />)
    expect(getByTestId('state').textContent).toBe('open')
    expect(widthOf(getByTestId('col'))).toBe(`${STORED_OPEN}px`)
  })

  it('honours a stored collapsed flag on desktop', () => {
    localStorage.setItem(COLLAPSED_KEY, '1')
    const { getByTestId } = render(<Harness collapse={COLLAPSE} />)
    expect(getByTestId('state').textContent).toBe('collapsed')
    expect(widthOf(getByTestId('col'))).toBe(`${STRIP}px`)
  })

  it('a stored collapsed flag does not freeze the column on a phone', () => {
    // The stored flag says collapsed AND the viewport is narrow. The mobile paths
    // deliberately never touch that flag, so if the effective state also read it
    // the column could never be opened again — expand, drag and keyboard would
    // all be inert.
    localStorage.setItem(COLLAPSED_KEY, '1')
    mockIsMobile = true
    const { getByTestId } = render(<Harness collapse={COLLAPSE} />)
    expect(getByTestId('state').textContent).toBe('collapsed')
    fireEvent.click(getByTestId('expand'))
    expect(getByTestId('state').textContent).toBe('open')
    expect(widthOf(getByTestId('col'))).toBe(`${STORED_OPEN}px`)
    // ...and the desktop preference is still intact.
    expect(localStorage.getItem(COLLAPSED_KEY)).toBe('1')
  })

  it('a tap on the drag handle does not persist the forced strip', () => {
    // The forced strip is the state a drag resolves FROM, so a press that never
    // moves resolves right back to it. If that result reached storage, one stray
    // tap on a phone would collapse the rail on the next desktop session.
    mockIsMobile = true
    const { getByTestId, getByLabelText } = render(<Harness collapse={COLLAPSE} />)
    expect(getByTestId('state').textContent).toBe('collapsed')
    tap(getByLabelText('Resize sidebar'))
    expect(localStorage.getItem(COLLAPSED_KEY)).toBe('0')
  })

  it('a desktop drag does not disarm the strip for a later narrowing', () => {
    const { getByTestId, getByLabelText, rerender } = render(<Harness collapse={COLLAPSE} />)
    const handle = getByLabelText('Resize sidebar')
    fireEvent.pointerDown(handle, { clientX: 0, pointerId: 1 })
    fireEvent.pointerMove(handle, { clientX: 40, pointerId: 1 })
    fireEvent.pointerUp(handle, { clientX: 40, pointerId: 1 })
    expect(getByTestId('state').textContent).toBe('open')
    // Same mount, viewport now narrow: the rail must still fall back to its
    // strip. A desktop drag banking the narrow override would leave it full
    // width on a phone.
    mockIsMobile = true
    rerender(<Harness collapse={COLLAPSE} />)
    expect(getByTestId('state').textContent).toBe('collapsed')
  })
})
