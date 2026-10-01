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
    // An item spanning both columns forces min cols = 2, so the "fewer columns"
    // stepper button is disabled and cannot drop a track through the pane.
    const spec: GridSpec = {
      cols: 2,
      rows: 1,
      items: [{ id: 'wide', element: 'chat', x: 0, y: 0, w: 2, h: 1 }],
    }
    const onChange = vi.fn()
    render(<LayoutEditor spec={spec} onChange={onChange} />)
    const fewer = screen.getByLabelText('fewer columns') as HTMLButtonElement
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
    fireEvent.click(screen.getByLabelText('more columns'))
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

  it('shows a danger replace cue (not the safe accent) when dragging over an occupied cell', () => {
    stubCanvasRect()
    const onChange = vi.fn()
    const { container } = render(<LayoutEditor spec={grid2x2()} onChange={onChange} />)
    const tile = screen.getByTitle('Drag Files onto the grid')
    fireEvent.pointerDown(tile, { button: 0, clientX: 0, clientY: 0 })
    // Move over the OCCUPIED cell (0,0), where item 'a' (Chat) sits.
    fireEvent.pointerMove(window, { clientX: 50, clientY: 50 })
    // The covering cell and the target pane both carry the danger replace class,
    // never the affirmative accent — a destructive replace must not look like a
    // safe empty-cell drop.
    expect(container.querySelector('.le-cell.le-replace')).not.toBeNull()
    expect(container.querySelector('.le-item.le-replace')).not.toBeNull()
    expect(container.querySelector('.le-cell.le-on-target')).toBeNull()
    // The ghost names what will be deleted.
    expect(screen.getByText('Replace Chat')).toBeInTheDocument()
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

  it('preserves the dragged pane\u2019s own dimensions on a replace-drop (no silent resize)', () => {
    stubCanvasRect()
    // A 2\u00d72 with a 1\u00d72 Chat filling column 0 and a 1\u00d71 Side panel at (1,0).
    // Dragging Chat (1\u00d72) onto Side panel must NOT shrink Chat to 1\u00d71: it keeps
    // w:1 h:2, anchored at the victim's origin (clamped in-bounds), replacing it.
    const spec: GridSpec = {
      cols: 2,
      rows: 2,
      items: [
        { id: 'a', element: 'chat', x: 0, y: 0, w: 1, h: 2 },
        { id: 'b', element: 'sidePanel', x: 1, y: 0, w: 1, h: 1 },
      ],
    }
    const onChange = vi.fn()
    render(<LayoutEditor spec={spec} onChange={onChange} />)
    const bar = screen.getByLabelText('Move Chat')
    fireEvent.pointerDown(bar, { button: 0, clientX: 50, clientY: 50 })
    // Move over the occupied top-right cell (1,0) where Side panel sits.
    fireEvent.pointerMove(window, { clientX: 150, clientY: 50 })
    fireEvent.pointerUp(window, { clientX: 150, clientY: 50 })
    expect(onChange).toHaveBeenCalled()
    const next: GridSpec = onChange.mock.calls.at(-1)![0]
    // Side panel is replaced (gone); Chat survives with its ORIGINAL 1\u00d72 span.
    expect(next.items.find((i) => i.id === 'b')).toBeUndefined()
    const moved2 = next.items.find((i) => i.id === 'a')!
    expect({ w: moved2.w, h: moved2.h }).toEqual({ w: 1, h: 2 })
  })

  it('shows the MUTED invalid cue (not danger) when the dragged pane cannot fit', () => {
    stubCanvasRect()
    // A 2\u00d72 with a 1\u00d72 Chat in column 0 and a 1\u00d71 Side panel at (1,0). Grabbing
    // Chat near its bottom (row 1) and hovering the bottom-right cell (1,1) would
    // seat the 1\u00d72 pane across rows 1\u20132 \u2014 off the grid \u2014 so neither a plain
    // placement nor a victim-excluded replace fits: the preview is a no-op, shown
    // as the MUTED le-invalid cue, never danger (le-replace) or accent (le-on-target).
    const spec: GridSpec = {
      cols: 2,
      rows: 2,
      items: [
        { id: 'a', element: 'chat', x: 0, y: 0, w: 1, h: 2 },
        { id: 'b', element: 'sidePanel', x: 1, y: 0, w: 1, h: 1 },
      ],
    }
    const onChange = vi.fn()
    const { container } = render(<LayoutEditor spec={spec} onChange={onChange} />)
    const bar = screen.getByLabelText('Move Chat')
    fireEvent.pointerDown(bar, { button: 0, clientX: 50, clientY: 150 })
    fireEvent.pointerMove(window, { clientX: 150, clientY: 150 })
    expect(container.querySelector('.le-cell.le-invalid')).not.toBeNull()
    expect(container.querySelector('.le-cell.le-replace')).toBeNull()
    expect(container.querySelector('.le-cell.le-on-target')).toBeNull()
    // The no-op cue is not color-only: the ghost says "Doesn't fit", never the
    // plain element label (which would read as a successful "Chat will land here").
    expect(screen.getByText("Doesn't fit")).toBeInTheDocument()
    expect(container.querySelector('.le-ghost')?.textContent).not.toContain('Chat')
    // A no-op release commits nothing.
    fireEvent.pointerUp(window, { clientX: 150, clientY: 150 })
    expect(onChange).not.toHaveBeenCalled()
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

  it('window blur disarms a drag stranded by an off-window release — a later stray pointerup cannot delete the pane', () => {
    stubCanvasRect()
    const onChange = vi.fn()
    render(<LayoutEditor spec={grid2x2()} onChange={onChange} />)
    const bar = screen.getByLabelText('Move Chat')
    fireEvent.pointerDown(bar, { button: 0, clientX: 50, clientY: 50 })
    // Mouse released OUTSIDE the window: the page gets neither pointerup nor
    // pointercancel. The window loses focus instead — that blur must clear the
    // armed drag so it can never be committed against a later interaction.
    fireEvent.blur(window)
    expect(onChange).not.toHaveBeenCalled()
    // The stale drag is gone: an unrelated off-grid pointerup is inert.
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

  it('opens over a ?seed= override when a valid GridSpec is supplied', () => {
    // The dev-only seed override lets a capture harness open a specific editor
    // state (e.g. the invalid "Doesn't fit" drop, which needs a wide pane this
    // PR cannot otherwise create). A Files+Terminal seed must render those, not
    // the default Chat/Side panel arrangement.
    const seed = { cols: 2, rows: 1, items: [
      { id: 's1', element: 'files', x: 0, y: 0, w: 1, h: 1 },
      { id: 's2', element: 'terminal', x: 1, y: 0, w: 1, h: 1 },
    ] }
    const prev = window.location.search
    window.history.replaceState({}, '', `/developer/layout-editor?seed=${encodeURIComponent(JSON.stringify(seed))}`)
    try {
      render(<LayoutEditorHarnessPage />)
      const editor = screen.getByTestId('layout-editor')
      // Assert against PLACED panes (title bars), not palette tiles — every
      // element label appears in the palette regardless of the seed.
      const placed = () => Array.from(editor.querySelectorAll('.le-item .le-bar-title')).map((n) => n.textContent)
      expect(placed()).toContain('Files')
      expect(placed()).toContain('Terminal')
      // The default seed's Chat/Side panel are NOT placed — the override replaced them.
      expect(placed()).not.toContain('Side panel')
      expect(placed()).not.toContain('Chat')
    } finally {
      window.history.replaceState({}, '', `/developer/layout-editor${prev}`)
    }
  })

  it('falls back to the default seed when ?seed= is malformed', () => {
    const prev = window.location.search
    window.history.replaceState({}, '', '/developer/layout-editor?seed=not-json')
    try {
      render(<LayoutEditorHarnessPage />)
      const editor = screen.getByTestId('layout-editor')
      // Malformed seed → default arrangement (Chat + Side panel) is PLACED.
      const placed = Array.from(editor.querySelectorAll('.le-item .le-bar-title')).map((n) => n.textContent)
      expect(placed).toContain('Chat')
      expect(placed).toContain('Side panel')
    } finally {
      window.history.replaceState({}, '', `/developer/layout-editor${prev}`)
    }
  })

  it('rejects an out-of-bounds or non-positive ?seed= without crashing (falls back)', () => {
    // A seed that PARSES and has the right field TYPES but holds an invalid
    // dimension/geometry must not reach the editor: a negative dim would make
    // trackSizes call Array(-1) and throw, error-boundarying the route. Each of
    // these must silently fall back to the default Chat/Side panel arrangement.
    const bad = [
      { cols: -1, rows: 1, items: [] }, // negative dim → Array(-1) throws
      { cols: 0, rows: 2, items: [] }, // zero dim
      { cols: 2.5, rows: 2, items: [] }, // non-integer dim
      { cols: 99, rows: 1, items: [] }, // over MAX_DIM
      { cols: 2, rows: 2, items: [{ id: 'x', element: 'chat', x: 5, y: 0, w: 1, h: 1 }] }, // item off-grid
      { cols: 2, rows: 2, items: [{ id: 'x', element: 'chat', x: 0, y: 0, w: 3, h: 1 }] }, // span runs off-grid
    ]
    const prev = window.location.search
    for (const seed of bad) {
      window.history.replaceState({}, '', `/developer/layout-editor?seed=${encodeURIComponent(JSON.stringify(seed))}`)
      const { unmount } = render(<LayoutEditorHarnessPage />)
      const editor = screen.getByTestId('layout-editor')
      const placed = Array.from(editor.querySelectorAll('.le-item .le-bar-title')).map((n) => n.textContent)
      expect(placed).toContain('Chat')
      expect(placed).toContain('Side panel')
      unmount()
    }
    window.history.replaceState({}, '', `/developer/layout-editor${prev}`)
  })

  it('rejects a well-typed but unsafe ?seed= (unknown element, bad tracks, containers, overlap) without crashing', () => {
    // GPT 5.6 blocking finding + the full field audit: a seed can PARSE and pass
    // a bare typeof check yet still reach an unchecked lookup or render wrong.
    // isValidSeed now rejects EVERY such case, so each falls back to the default.
    const bad = [
      // The GPT block: an unknown `element` string passes typeof but crashes
      // ELEMENT_META[element] (undefined.labelKey).
      { cols: 2, rows: 2, items: [{ id: 'x', element: 'bogus', x: 0, y: 0, w: 1, h: 1 }] },
      { cols: 2, rows: 2, items: [{ id: 'x', element: '', x: 0, y: 0, w: 1, h: 1 }] },
      // colSizes/rowSizes: wrong length, or non-positive / non-finite entries.
      { cols: 2, rows: 1, colSizes: [1], items: [] }, // length ≠ cols
      { cols: 2, rows: 1, colSizes: [1, 0], items: [] }, // non-positive track
      { cols: 2, rows: 1, colSizes: [1, 'x'], items: [] }, // non-number track
      // Container / nested fields are rejected — leaves only on a harness seed.
      { cols: 2, rows: 2, items: [{ id: 'g', element: 'group', x: 0, y: 0, w: 1, h: 1, grid: { cols: 1, rows: 1, items: [] } }] },
      { cols: 2, rows: 2, items: [{ id: 't', element: 'tabs', x: 0, y: 0, w: 1, h: 1, tabs: [] }] },
      { cols: 2, rows: 2, items: [{ id: 'c', element: 'chat', x: 0, y: 0, w: 1, h: 1, config: {} }] },
      // Two items sharing a cell — a seed bypasses the editor's move-time guard.
      { cols: 2, rows: 2, items: [
        { id: 'a', element: 'chat', x: 0, y: 0, w: 1, h: 1 },
        { id: 'b', element: 'files', x: 0, y: 0, w: 1, h: 1 },
      ] },
      // GPT 5.6 blocking finding (round 2): two items sharing an id — closing
      // either makes removeItemById (which filters EVERY match) delete both.
      { cols: 2, rows: 2, items: [
        { id: 'dup', element: 'chat', x: 0, y: 0, w: 1, h: 1 },
        { id: 'dup', element: 'files', x: 1, y: 0, w: 1, h: 1 },
      ] },
      // An empty id collides the same way (two empty ids are duplicates).
      { cols: 2, rows: 2, items: [
        { id: '', element: 'chat', x: 0, y: 0, w: 1, h: 1 },
        { id: '', element: 'files', x: 1, y: 0, w: 1, h: 1 },
      ] },
    ]
    const prev = window.location.search
    for (const seed of bad) {
      window.history.replaceState({}, '', `/developer/layout-editor?seed=${encodeURIComponent(JSON.stringify(seed))}`)
      const { unmount } = render(<LayoutEditorHarnessPage />)
      const editor = screen.getByTestId('layout-editor')
      const placed = Array.from(editor.querySelectorAll('.le-item .le-bar-title')).map((n) => n.textContent)
      // Fell back to the default Chat + Side panel arrangement — no crash, no
      // partial render of the unsafe seed.
      expect(placed).toContain('Chat')
      expect(placed).toContain('Side panel')
      unmount()
    }
    window.history.replaceState({}, '', `/developer/layout-editor${prev}`)
  })

  it('accepts a valid ?seed= carrying colSizes/rowSizes (the tracks a real seed uses)', () => {
    // The floor seed carries non-equal tracks; a valid override may too. This
    // guards that the new track-array validation does not reject a legitimate seed.
    const seed = {
      cols: 2,
      rows: 1,
      colSizes: [3, 2],
      items: [
        { id: 's1', element: 'files', x: 0, y: 0, w: 1, h: 1 },
        { id: 's2', element: 'git', x: 1, y: 0, w: 1, h: 1 },
      ],
    }
    const prev = window.location.search
    window.history.replaceState({}, '', `/developer/layout-editor?seed=${encodeURIComponent(JSON.stringify(seed))}`)
    try {
      render(<LayoutEditorHarnessPage />)
      const editor = screen.getByTestId('layout-editor')
      const placed = Array.from(editor.querySelectorAll('.le-item .le-bar-title')).map((n) => n.textContent)
      expect(placed).toContain('Files')
      expect(placed).toContain('Git')
      expect(placed).not.toContain('Chat')
    } finally {
      window.history.replaceState({}, '', `/developer/layout-editor${prev}`)
    }
  })
})
