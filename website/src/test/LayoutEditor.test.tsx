/**
 * Layout editor (core pass) component behavior — the interactions that do NOT
 * depend on real pixel layout (jsdom has none): the palette renders every
 * content element, a placed pane's close button removes it through `onChange`,
 * and the dimension steppers respect the occupied-edge minimum and reset tracks
 * on grow. The pure drag/drop GEOMETRY is covered by grid.test.ts on the model;
 * here we prove the editor wires the model ops to the DOM.
 *
 * Plus the harness smoke test: the standalone dev page mounts the editor over
 * its seed spec.
 *
 * Resize, track dividers, and the tabs container land in follow-up PRs and are
 * tested there.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import LayoutEditor from '../components/crew/layout/LayoutEditor'
import { trackIndexAtFraction } from '../components/crew/layout/LayoutEditor'
import LayoutEditorHarnessPage from '../pages/LayoutEditorHarnessPage'
import type { GridSpec } from '../components/crew/layout/grid'

function baseSpec(): GridSpec {
  return {
    cols: 2,
    rows: 2,
    colSizes: [3, 2],
    items: [
      { id: 'a', element: 'chat', x: 0, y: 0, w: 1, h: 2 },
      { id: 'b', element: 'sidePanel', x: 1, y: 0, w: 1, h: 1 },
    ],
  }
}

describe('LayoutEditor (core)', () => {
  it('renders the palette with every content element', () => {
    render(<LayoutEditor spec={baseSpec()} onChange={() => {}} />)
    for (const label of ['Chat', 'Side panel', 'Files', 'Git', 'Changes', 'Subagents', 'Terminal', 'Notes', 'Work log']) {
      expect(screen.getByTitle(`Drag ${label} onto the grid`)).toBeTruthy()
    }
  })

  it('renders each placed item as a card with its label', () => {
    render(<LayoutEditor spec={baseSpec()} onChange={() => {}} />)
    const editor = screen.getByTestId('layout-editor')
    expect(within(editor).getAllByText('Chat').length).toBeGreaterThan(0)
    expect(within(editor).getAllByText('Side panel').length).toBeGreaterThan(0)
  })

  it('removes a pane through onChange when its close button is clicked', () => {
    const onChange = vi.fn()
    render(<LayoutEditor spec={baseSpec()} onChange={onChange} />)
    fireEvent.click(screen.getByLabelText('Remove Side panel'))
    expect(onChange).toHaveBeenCalledTimes(1)
    const next: GridSpec = onChange.mock.calls[0][0]
    expect(next.items.map((i) => i.id)).toEqual(['a'])
  })

  it('does not shrink columns below the occupied edge (stepper min)', () => {
    // An item spanning both columns forces min cols = 2, so the "fewer cols"
    // stepper button is disabled and cannot drop a track through the pane.
    const spec: GridSpec = {
      cols: 2,
      rows: 1,
      items: [{ id: 'wide', element: 'chat', x: 0, y: 0, w: 2, h: 1 }],
    }
    const onChange = vi.fn()
    render(<LayoutEditor spec={spec} onChange={onChange} />)
    const fewer = screen.getByLabelText('fewer cols') as HTMLButtonElement
    expect(fewer.disabled).toBe(true)
    fireEvent.click(fewer)
    expect(onChange).not.toHaveBeenCalled()
  })

  it('resets only the resized axis and preserves the other axis weights', () => {
    // baseSpec has colSizes [3,2] and no rowSizes. Growing cols to 3 resets
    // colSizes to equal (the old 2-length array no longer fits), but must NOT
    // touch the row axis.
    const onChange = vi.fn()
    render(<LayoutEditor spec={baseSpec()} onChange={onChange} />)
    fireEvent.click(screen.getByLabelText('more cols'))
    expect(onChange).toHaveBeenCalledTimes(1)
    const next: GridSpec = onChange.mock.calls[0][0]
    expect(next.cols).toBe(3)
    expect(next.colSizes).toEqual([1, 1, 1])
    // Row axis untouched — no rowSizes was set, and growing cols must not add one.
    expect(next.rowSizes).toBeUndefined()
  })

  it('preserves custom column weights when only the row count changes', () => {
    const spec: GridSpec = {
      cols: 2,
      rows: 1,
      colSizes: [3, 2],
      items: [{ id: 'a', element: 'chat', x: 0, y: 0, w: 1, h: 1 }],
    }
    const onChange = vi.fn()
    render(<LayoutEditor spec={spec} onChange={onChange} />)
    fireEvent.click(screen.getByLabelText('more rows'))
    const next: GridSpec = onChange.mock.calls[0][0]
    expect(next.rows).toBe(2)
    expect(next.rowSizes).toEqual([1, 1])
    // Column split survives a row-only resize.
    expect(next.colSizes).toEqual([3, 2])
  })

  it('places into the first free cell on keyboard/click activation (no drag)', () => {
    // A plain click (keyboard Enter dispatches click) on a palette tile adds the
    // element to the first free cell — the editor's non-pointer add path.
    const spec: GridSpec = { cols: 2, rows: 1, items: [{ id: 'a', element: 'chat', x: 0, y: 0, w: 1, h: 1 }] }
    const onChange = vi.fn()
    render(<LayoutEditor spec={spec} onChange={onChange} />)
    fireEvent.click(screen.getByTitle('Drag Files onto the grid'))
    expect(onChange).toHaveBeenCalledTimes(1)
    const next: GridSpec = onChange.mock.calls[0][0]
    const added = next.items.find((i) => i.element === 'files')
    expect(added).toBeTruthy()
    // First free cell in a 2×1 with chat at (0,0) is (1,0).
    expect({ x: added!.x, y: added!.y }).toEqual({ x: 1, y: 0 })
  })

  it('moves a placed pane one cell with an arrow key (keyboard arrange path)', () => {
    // sidePanel 'b' is at (1,0) in a 2×2; the cell to its left-down is free
    // enough for a one-cell down move (b is 1×1, so (1,1) is free).
    const onChange = vi.fn()
    render(<LayoutEditor spec={baseSpec()} onChange={onChange} />)
    const bar = screen.getByLabelText('Move Side panel')
    fireEvent.keyDown(bar, { key: 'ArrowDown' })
    expect(onChange).toHaveBeenCalledTimes(1)
    const next: GridSpec = onChange.mock.calls[0][0]
    const moved = next.items.find((i) => i.id === 'b')!
    expect({ x: moved.x, y: moved.y }).toEqual({ x: 1, y: 1 })
  })

  it('does not move a pane when the arrow-key destination is occupied', () => {
    // In baseSpec, chat 'a' fills column 0 (0,0)-(0,1); moving sidePanel 'b'
    // left would collide, so the move is refused (no onChange).
    const onChange = vi.fn()
    render(<LayoutEditor spec={baseSpec()} onChange={onChange} />)
    fireEvent.keyDown(screen.getByLabelText('Move Side panel'), { key: 'ArrowLeft' })
    expect(onChange).not.toHaveBeenCalled()
  })

  it('ignores a non-arrow key on the move bar', () => {
    const onChange = vi.fn()
    render(<LayoutEditor spec={baseSpec()} onChange={onChange} />)
    fireEvent.keyDown(screen.getByLabelText('Move Side panel'), { key: 'Enter' })
    expect(onChange).not.toHaveBeenCalled()
  })

  it('clamps a pane at the grid edge instead of moving off it', () => {
    // sidePanel 'b' at (1,0) cannot move right (col 1 is the last) or up (row 0).
    const onChange = vi.fn()
    render(<LayoutEditor spec={baseSpec()} onChange={onChange} />)
    const bar = screen.getByLabelText('Move Side panel')
    fireEvent.keyDown(bar, { key: 'ArrowRight' })
    fireEvent.keyDown(bar, { key: 'ArrowUp' })
    expect(onChange).not.toHaveBeenCalled()
  })
})

describe('LayoutEditor pointer drag lifecycle', () => {
  // jsdom has no layout, so cellAt() reads a STUBBED canvas rect. With a 200×200
  // canvas at the origin, client coords map onto cells: for an unweighted 2×2,
  // x=50/y=50 → cell (0,0), x=150/y=150 → cell (1,1).
  const RECT = { left: 0, top: 0, right: 200, bottom: 200, width: 200, height: 200, x: 0, y: 0, toJSON: () => ({}) }
  function stubCanvasRect() {
    vi.spyOn(HTMLDivElement.prototype, 'getBoundingClientRect').mockReturnValue(RECT as DOMRect)
  }
  function grid2x2(): GridSpec {
    return { cols: 2, rows: 2, items: [{ id: 'a', element: 'chat', x: 0, y: 0, w: 1, h: 1 }] }
  }

  it('drops a dragged palette tile onto the targeted free cell', () => {
    stubCanvasRect()
    const onChange = vi.fn()
    render(<LayoutEditor spec={grid2x2()} onChange={onChange} />)
    const tile = screen.getByTitle('Drag Files onto the grid')
    fireEvent.pointerDown(tile, { button: 0, clientX: 0, clientY: 0 })
    // Move well past the 5px threshold, over cell (1,0): x=150,y=50.
    fireEvent.pointerMove(window, { clientX: 150, clientY: 50 })
    fireEvent.pointerUp(window, { clientX: 150, clientY: 50 })
    expect(onChange).toHaveBeenCalled()
    const next: GridSpec = onChange.mock.calls.at(-1)![0]
    const added = next.items.find((i) => i.element === 'files')!
    expect({ x: added.x, y: added.y }).toEqual({ x: 1, y: 0 })
  })

  it('suppresses the synthetic click that follows a pointer gesture (no double-add)', () => {
    stubCanvasRect()
    const onChange = vi.fn()
    render(<LayoutEditor spec={grid2x2()} onChange={onChange} />)
    const tile = screen.getByTitle('Drag Files onto the grid')
    fireEvent.pointerDown(tile, { button: 0, clientX: 0, clientY: 0 })
    fireEvent.pointerMove(window, { clientX: 150, clientY: 50 })
    fireEvent.pointerUp(window, { clientX: 150, clientY: 50 })
    const afterPointer = onChange.mock.calls.length
    // The browser's compatibility click after a pointer sequence carries
    // detail>=1; the tile's onClick gates on detail===0, so it must NOT add a
    // second pane.
    fireEvent.click(tile, { detail: 1 })
    expect(onChange.mock.calls.length).toBe(afterPointer)
    // A genuine keyboard activation (detail===0) DOES place.
    fireEvent.click(tile, { detail: 0 })
    expect(onChange.mock.calls.length).toBe(afterPointer + 1)
  })

  it('moves a placed pane by dragging its title bar to another cell', () => {
    stubCanvasRect()
    const onChange = vi.fn()
    render(<LayoutEditor spec={grid2x2()} onChange={onChange} />)
    const bar = screen.getByLabelText('Move Chat')
    fireEvent.pointerDown(bar, { button: 0, clientX: 50, clientY: 50 })
    fireEvent.pointerMove(window, { clientX: 150, clientY: 150 })
    fireEvent.pointerUp(window, { clientX: 150, clientY: 150 })
    expect(onChange).toHaveBeenCalled()
    const next: GridSpec = onChange.mock.calls.at(-1)![0]
    const moved = next.items.find((i) => i.id === 'a')!
    expect({ x: moved.x, y: moved.y }).toEqual({ x: 1, y: 1 })
  })

  it('removes a pane dragged off the grid', () => {
    stubCanvasRect()
    const onChange = vi.fn()
    render(<LayoutEditor spec={grid2x2()} onChange={onChange} />)
    const bar = screen.getByLabelText('Move Chat')
    fireEvent.pointerDown(bar, { button: 0, clientX: 50, clientY: 50 })
    // Drag outside the 200×200 canvas → off-grid.
    fireEvent.pointerMove(window, { clientX: 500, clientY: 500 })
    fireEvent.pointerUp(window, { clientX: 500, clientY: 500 })
    expect(onChange).toHaveBeenCalled()
    const next: GridSpec = onChange.mock.calls.at(-1)![0]
    expect(next.items.find((i) => i.id === 'a')).toBeUndefined()
  })

  it('ignores a non-primary (right) button on a palette tile', () => {
    stubCanvasRect()
    const onChange = vi.fn()
    render(<LayoutEditor spec={grid2x2()} onChange={onChange} />)
    fireEvent.pointerDown(screen.getByTitle('Drag Files onto the grid'), { button: 2, clientX: 0, clientY: 0 })
    fireEvent.pointerUp(window, { clientX: 0, clientY: 0 })
    expect(onChange).not.toHaveBeenCalled()
  })

  it('a cancelled drag clears without committing, and a later stray pointerup is inert', () => {
    stubCanvasRect()
    const onChange = vi.fn()
    render(<LayoutEditor spec={grid2x2()} onChange={onChange} />)
    const bar = screen.getByLabelText('Move Chat')
    fireEvent.pointerDown(bar, { button: 0, clientX: 50, clientY: 50 })
    // Browser/device takes the gesture away → the drag must clear, NOT commit.
    fireEvent.pointerCancel(window, { clientX: 50, clientY: 50 })
    expect(onChange).not.toHaveBeenCalled()
    // A subsequent unrelated pointerup must not run the (now-disarmed) off-grid
    // removal branch on the pane.
    fireEvent.pointerUp(window, { clientX: 500, clientY: 500 })
    expect(onChange).not.toHaveBeenCalled()
  })
})

describe('trackIndexAtFraction (weighted-track hit-test)', () => {
  it('splits equal tracks at even boundaries', () => {
    expect(trackIndexAtFraction([1, 1], 0.25)).toBe(0)
    expect(trackIndexAtFraction([1, 1], 0.75)).toBe(1)
  })

  it('honours unequal fr weights — a 3:2 split boundary is at 0.6, not 0.5', () => {
    // The whole point of the fix: uniform division would put 0.55 in cell 1;
    // with [3,2] the first track spans 0..0.6, so 0.55 is still cell 0.
    expect(trackIndexAtFraction([3, 2], 0.55)).toBe(0)
    expect(trackIndexAtFraction([3, 2], 0.65)).toBe(1)
    expect(trackIndexAtFraction([3, 2], 0.0)).toBe(0)
    expect(trackIndexAtFraction([3, 2], 1.0)).toBe(1)
  })

  it('clamps a degenerate (all-zero) weight array to the first track', () => {
    expect(trackIndexAtFraction([0, 0], 0.5)).toBe(0)
  })
})

describe('LayoutEditorHarnessPage', () => {
  it('mounts the editor over its seed spec', () => {
    render(<LayoutEditorHarnessPage />)
    const editor = screen.getByTestId('layout-editor')
    expect(editor).toBeTruthy()
    // The seed places chat + a side panel, so both labels render in the grid.
    expect(within(editor).getAllByText('Chat').length).toBeGreaterThan(0)
    expect(within(editor).getAllByText('Side panel').length).toBeGreaterThan(0)
  })
})
