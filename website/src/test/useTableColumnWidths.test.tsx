// User-resizable TABLE columns: the `useTableColumnWidths` + `ColumnResizer`
// pair. The property every case below protects is that the table's min-width
// and its column widths stay ONE statement: a fixed-layout table does not
// shrink content to fit, so a column that grows without the table's min-width
// growing by the same amount takes its pixels out of the residual column and
// draws that column's content over its neighbour.
import { describe, it, expect, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/react'

import ColumnResizer from '../components/ColumnResizer'
import {
  loadTableColumnWidths, useTableColumnWidths, type TableColumnSpec,
} from '../hooks/useTableColumnWidths'

const KEY = 'kc:test:table-column-widths'
const BASE_MIN_WIDTH = 500

const COLUMNS = {
  id: { base: 68, min: 48, max: 360 },
  name: { base: 160, min: 80, max: 640 },
} satisfies Record<string, TableColumnSpec>

function Harness() {
  const cols = useTableColumnWidths(KEY, COLUMNS)
  return (
    <div>
      <table data-testid="table" style={cols.extra ? { minWidth: BASE_MIN_WIDTH + cols.extra } : undefined}>
        <thead>
          <tr>
            <th data-testid="th-id" className="relative" style={cols.style('id')}>
              ID<ColumnResizer column="ID" {...cols.resizer('id')} />
            </th>
            <th data-testid="th-name" className="relative" style={cols.style('name')}>
              Name<ColumnResizer column="Name" {...cols.resizer('name')} />
            </th>
          </tr>
        </thead>
      </table>
      <button type="button" data-testid="reset-all" disabled={!cols.customized} onClick={cols.reset}>reset</button>
    </div>
  )
}

function drag(handle: HTMLElement, from: number, to: number, id = 1) {
  fireEvent.pointerDown(handle, { clientX: from, pointerId: id })
  fireEvent.pointerMove(handle, { clientX: to, pointerId: id })
  fireEvent.pointerUp(handle, { clientX: to, pointerId: id })
}

const stored = () => JSON.parse(localStorage.getItem(KEY) ?? 'null')

describe('useTableColumnWidths', () => {
  beforeEach(() => { localStorage.clear() })

  it('leaves an untouched table to its declared classes', () => {
    const { getByTestId } = render(<Harness />)
    // No inline width and no inline min-width: the `w-[Npx]` / `min-w-[Npx]`
    // classes stay the single source of the defaults.
    expect(getByTestId('th-id').style.width).toBe('')
    expect(getByTestId('table').style.minWidth).toBe('')
    expect((getByTestId('reset-all') as HTMLButtonElement).disabled).toBe(true)
    expect(localStorage.getItem(KEY)).toBeNull()
  })

  it('a drag widens the column, moves the table min-width by the same amount, and persists', () => {
    const { getByTestId, getAllByRole } = render(<Harness />)
    const [idGrip] = getAllByRole('separator')
    drag(idGrip, 100, 172)
    expect(getByTestId('th-id').style.width).toBe('140px')
    expect(getByTestId('table').style.minWidth).toBe(`${BASE_MIN_WIDTH + 72}px`)
    expect(getByTestId('th-name').style.width).toBe('')
    expect(stored()).toEqual({ id: 140 })
  })

  it('narrowing a column gives the pixels back to the table', () => {
    const { getByTestId, getAllByRole } = render(<Harness />)
    drag(getAllByRole('separator')[1], 300, 260)
    expect(getByTestId('th-name').style.width).toBe('120px')
    expect(getByTestId('table').style.minWidth).toBe(`${BASE_MIN_WIDTH - 40}px`)
  })

  it('clamps to the column bounds in both directions', () => {
    const { getByTestId, getAllByRole } = render(<Harness />)
    const [idGrip] = getAllByRole('separator')
    drag(idGrip, 100, 9000)
    expect(getByTestId('th-id').style.width).toBe('360px')
    drag(idGrip, 100, -9000, 2)
    expect(getByTestId('th-id').style.width).toBe('48px')
    expect(stored()).toEqual({ id: 48 })
  })

  it('does not persist mid-drag, only on release', () => {
    const { getByTestId, getAllByRole } = render(<Harness />)
    const [idGrip] = getAllByRole('separator')
    fireEvent.pointerDown(idGrip, { clientX: 100, pointerId: 1 })
    fireEvent.pointerMove(idGrip, { clientX: 150, pointerId: 1 })
    expect(getByTestId('th-id').style.width).toBe('118px')
    expect(localStorage.getItem(KEY)).toBeNull()
    fireEvent.pointerUp(idGrip, { clientX: 150, pointerId: 1 })
    expect(stored()).toEqual({ id: 118 })
  })

  it('successive drags accumulate from the current width, not the base', () => {
    const { getByTestId, getAllByRole } = render(<Harness />)
    const [idGrip] = getAllByRole('separator')
    drag(idGrip, 100, 130)
    drag(idGrip, 130, 160, 2)
    expect(getByTestId('th-id').style.width).toBe('128px')
  })

  it('landing back on the base drops the override instead of storing it', () => {
    const { getByTestId, getAllByRole } = render(<Harness />)
    const [idGrip] = getAllByRole('separator')
    drag(idGrip, 100, 130)
    drag(idGrip, 130, 100, 2)
    expect(getByTestId('th-id').style.width).toBe('')
    expect(getByTestId('table').style.minWidth).toBe('')
    expect(stored()).toEqual({})
  })

  it('arrow keys resize and persist; Shift takes the coarse step', () => {
    const { getByTestId, getAllByRole } = render(<Harness />)
    const [idGrip] = getAllByRole('separator')
    fireEvent.keyDown(idGrip, { key: 'ArrowRight' })
    expect(getByTestId('th-id').style.width).toBe('84px')
    fireEvent.keyDown(idGrip, { key: 'ArrowRight', shiftKey: true })
    expect(getByTestId('th-id').style.width).toBe('148px')
    fireEvent.keyDown(idGrip, { key: 'ArrowLeft' })
    expect(stored()).toEqual({ id: 132 })
  })

  it('leaves Up/Down alone so the page still scrolls from a focused grip', () => {
    const { getAllByRole } = render(<Harness />)
    // fireEvent returns false when the handler called preventDefault.
    expect(fireEvent.keyDown(getAllByRole('separator')[0], { key: 'ArrowDown' })).toBe(true)
  })

  it('a double-click and Enter both return one column to its base', () => {
    const { getByTestId, getAllByRole } = render(<Harness />)
    const [idGrip, nameGrip] = getAllByRole('separator')
    drag(idGrip, 100, 150)
    drag(nameGrip, 300, 350, 2)
    fireEvent.doubleClick(idGrip)
    expect(getByTestId('th-id').style.width).toBe('')
    expect(getByTestId('th-name').style.width).toBe('210px')
    expect(stored()).toEqual({ name: 210 })
    fireEvent.keyDown(nameGrip, { key: 'Enter' })
    expect(getByTestId('th-name').style.width).toBe('')
    expect(stored()).toEqual({})
  })

  it('reset() drops every override', () => {
    const { getByTestId, getAllByRole } = render(<Harness />)
    const [idGrip, nameGrip] = getAllByRole('separator')
    drag(idGrip, 100, 150)
    drag(nameGrip, 300, 350, 2)
    fireEvent.click(getByTestId('reset-all'))
    expect(getByTestId('th-id').style.width).toBe('')
    expect(getByTestId('th-name').style.width).toBe('')
    expect(getByTestId('table').style.minWidth).toBe('')
  })

  it('restores persisted widths on mount', () => {
    localStorage.setItem(KEY, JSON.stringify({ id: 120, name: 300 }))
    const { getByTestId } = render(<Harness />)
    expect(getByTestId('th-id').style.width).toBe('120px')
    expect(getByTestId('th-name').style.width).toBe('300px')
    expect(getByTestId('table').style.minWidth).toBe(`${BASE_MIN_WIDTH + 52 + 140}px`)
  })

  it('a grip click never reaches the header cell it sits in', () => {
    let clicks = 0
    function Clickable() {
      const cols = useTableColumnWidths(KEY, COLUMNS)
      return (
        <table><thead><tr>
          <th onClick={() => { clicks += 1 }}>ID<ColumnResizer column="ID" {...cols.resizer('id')} /></th>
        </tr></thead></table>
      )
    }
    const { getByRole } = render(<Clickable />)
    fireEvent.click(getByRole('separator'))
    fireEvent.doubleClick(getByRole('separator'))
    expect(clicks).toBe(0)
  })

  it('reports its position as the splitter widget does', () => {
    const { getAllByRole } = render(<Harness />)
    const [idGrip] = getAllByRole('separator')
    expect(idGrip.getAttribute('aria-valuenow')).toBe('68')
    expect(idGrip.getAttribute('aria-valuemin')).toBe('48')
    expect(idGrip.getAttribute('aria-valuemax')).toBe('360')
    expect(idGrip.getAttribute('aria-orientation')).toBe('vertical')
    expect(idGrip.tabIndex).toBe(0)
  })
})

describe('loadTableColumnWidths', () => {
  beforeEach(() => { localStorage.clear() })

  it('discards what no longer makes sense instead of clamping it', () => {
    localStorage.setItem(KEY, JSON.stringify({
      id: 9999,          // above max: written under different bounds
      name: 200,         // fine
      gone: 120,         // a column this table no longer has
      toString: 120,     // an inherited name must not read as a column
    }))
    expect(loadTableColumnWidths(KEY, COLUMNS)).toEqual({ name: 200 })
  })

  it('ignores non-numeric and non-finite values', () => {
    localStorage.setItem(KEY, JSON.stringify({ id: '120', name: null }))
    expect(loadTableColumnWidths(KEY, COLUMNS)).toEqual({})
  })

  it.each(['not json', '[]', '42', 'null'])('falls back to no overrides on %s', (raw) => {
    localStorage.setItem(KEY, raw)
    expect(loadTableColumnWidths(KEY, COLUMNS)).toEqual({})
  })

  it('treats a stored base as no override', () => {
    localStorage.setItem(KEY, JSON.stringify({ id: 68 }))
    expect(loadTableColumnWidths(KEY, COLUMNS)).toEqual({})
  })
})
