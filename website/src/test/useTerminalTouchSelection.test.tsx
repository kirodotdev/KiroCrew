import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { renderHook, act, cleanup } from '@testing-library/react'
import type { Terminal } from '@xterm/xterm'
import { useTerminalTouchSelection, clientYToBufferRow } from '../hooks/useTerminalTouchSelection'

afterEach(cleanup)

/**
 * clientYToBufferRow is the only geometry math the touch feature does, and
 * xterm exposes no point→row API — so it is a pure exported function derived
 * from the rendered cell height, unit-tested here without a real canvas.
 */
describe('clientYToBufferRow', () => {
  // A 240px screen, 10 rows → 24px per row. Screen top at y=100.
  const rect = { top: 100, height: 240 }

  it('maps a Y inside the first row to the top visible row (+ viewportY)', () => {
    // 12px below the screen top → row 0 of the viewport; viewportY=0 → buffer 0.
    expect(clientYToBufferRow(112, rect, 10, 0)).toBe(0)
  })

  it('adds the scrollback offset (viewportY) to the visible row', () => {
    // Same click, but scrolled 50 rows into scrollback → absolute buffer row 50.
    expect(clientYToBufferRow(112, rect, 10, 50)).toBe(50)
  })

  it('floors to the row the pixel falls in', () => {
    // y=100+24*3+5 = 177 → viewport row 3.
    expect(clientYToBufferRow(177, rect, 10, 0)).toBe(3)
  })

  it('clamps a Y below the last row to the last visible row', () => {
    // Way past the bottom → clamps to row 9 (rows-1), not beyond.
    expect(clientYToBufferRow(9999, rect, 10, 0)).toBe(9)
  })

  it('clamps a Y above the screen top to the first row', () => {
    expect(clientYToBufferRow(-500, rect, 10, 0)).toBe(0)
  })

  it('returns null when geometry is unusable (no rows / zero height)', () => {
    expect(clientYToBufferRow(112, rect, 0, 0)).toBeNull()
    expect(clientYToBufferRow(112, { top: 100, height: 0 }, 10, 0)).toBeNull()
  })
})

/**
 * The endpoint state machine. A fake terminal models the buffer geometry
 * (rows, viewportY, isWrapped) and the .xterm-screen element getBoundingClientRect
 * so a synthetic touch at a Y resolves to a known row, and records
 * selectLines(a,b) calls so the anchor→focus span can be asserted end to end.
 */
type Row = string | { text: string; isWrapped?: boolean }

function makeTouchTerm(rows: Row[], opts?: { viewportY?: number; visibleRows?: number; screenTop?: number; screenHeight?: number; cursorY?: number }) {
  const norm = rows.map(r => (typeof r === 'string' ? { text: r, isWrapped: false } : { text: r.text, isWrapped: r.isWrapped ?? false }))
  const visibleRows = opts?.visibleRows ?? norm.length
  const screenTop = opts?.screenTop ?? 0
  const screenHeight = opts?.screenHeight ?? visibleRows * 20 // 20px/row
  let sel: { start: number; end: number } | null = null
  const selectionListeners = new Set<() => void>()
  const scrollListeners = new Set<() => void>()
  const emitSelection = () => { for (const l of [...selectionListeners]) l() }
  const emitScroll = () => { for (const l of [...scrollListeners]) l() }

  // .xterm-screen stub with a fixed bounding rect. `contains` answers the
  // target gate (a touch on the grid vs on an overlaid control).
  const screenEl = {
    getBoundingClientRect: () => ({ top: screenTop, height: screenHeight, left: 0, width: 300, right: 300, bottom: screenTop + screenHeight, x: 0, y: screenTop, toJSON() {} }),
    contains: (n: unknown) => n === screenEl || n === undefined || n === null ? n === screenEl : true,
  } as unknown as HTMLElement & { contains: (n: unknown) => boolean }
  const element = {
    offsetParent: {} as unknown, // laid out / visible
    querySelector: (s: string) => (s === '.xterm-screen' ? screenEl : null),
  }

  // Markers registered on the buffer. Each remembers its absolute line; a
  // content shift (shiftContent) remaps every marker's line by the same delta,
  // and a trim past a marker disposes it (line -> -1). Models xterm's IMarker.
  const markers: { line: number; isDisposed: boolean; dispose: () => void }[] = []

  const selectLines = vi.fn((a: number, b: number) => { sel = { start: a, end: b }; emitSelection() })
  // Mutable content signature so a test can simulate NEW output (baseY/length
  // change) vs a pure pan (only viewportY changes).
  const bufSig = { baseY: opts?.viewportY ?? 0, length: norm.length }
  const cursorY = opts?.cursorY ?? (visibleRows - 1)
  const term = {
    element,
    rows: visibleRows,
    registerMarker: (cursorYOffset: number) => {
      const line = bufSig.baseY + cursorY + (cursorYOffset ?? 0)
      const m = { line, isDisposed: false, dispose() { this.isDisposed = true } }
      markers.push(m)
      return m
    },
    buffer: {
      active: {
        get length() { return bufSig.length },
        get baseY() { return bufSig.baseY },
        cursorY,
        viewportY: opts?.viewportY ?? 0,
        getLine: (row: number) =>
          row >= 0 && row < norm.length
            ? { translateToString: () => norm[row].text, isWrapped: norm[row].isWrapped }
            : undefined,
      },
    },
    selectLines,
    getSelection: () => (sel ? norm.slice(sel.start, sel.end + 1).map(r => r.text).join('\n') : ''),
    clearSelection: () => { sel = null; emitSelection() },
    onSelectionChange: (cb: () => void) => { selectionListeners.add(cb); return { dispose: () => selectionListeners.delete(cb) } },
    onScroll: (cb: () => void) => { scrollListeners.add(cb); return { dispose: () => scrollListeners.delete(cb) } },
  } as unknown as Terminal & { selectLines: typeof selectLines }

  // A helper to fabricate a single-touch React.TouchEvent at a viewport row.
  // Row r → clientY at the vertical centre of that row. Carries a preventDefault
  // spy + cancelable so the synthetic-click-suppression path is observable.
  // `target` defaults to the grid (screenEl) so the terminal-screen gate passes;
  // pass target=null to model a touch on an overlaid control (off the grid).
  const cellH = screenHeight / visibleRows
  const touchAtRow = (r: number, count = 1, target: unknown = screenEl) => {
    const clientY = screenTop + r * cellH + cellH / 2
    const touch = { clientX: 10, clientY }
    const touches = Array.from({ length: count }, () => touch)
    return { touches, target, cancelable: true, preventDefault: vi.fn() } as unknown as React.TouchEvent & { preventDefault: ReturnType<typeof vi.fn> }
  }
  // A touch on an OVERLAID control (not the grid): target is some other node.
  const touchOffScreen = (r: number) => touchAtRow(r, 1, { nodeName: 'BUTTON' })
  // A bare touchend event (no active touches) with a preventDefault spy.
  const touchEnd = () => ({ touches: [], target: screenEl, cancelable: true, preventDefault: vi.fn() } as unknown as React.TouchEvent & { preventDefault: ReturnType<typeof vi.fn> })

  // Simulate NEW output: shift baseY/length and remap every live marker by the
  // same delta; a marker whose line falls below 0 is trimmed away (disposed).
  const shiftContent = (dBaseY: number, dLen: number) => {
    bufSig.baseY += dBaseY
    bufSig.length += dLen
    for (const m of markers) {
      if (m.isDisposed) continue
      m.line += dBaseY
      if (m.line < 0) { m.line = -1; m.isDisposed = true }
    }
    emitScroll()
  }

  // Off-screen contains() for touchOffScreen targets.
  ;(screenEl as unknown as { contains: (n: unknown) => boolean }).contains = (n: unknown) => n === screenEl

  // Model xterm's at-capacity trim: baseY and length hold constant, but every
  // absolute row remaps down by `n` — so each live marker's line drops by `n`
  // (disposing at < 0). Then a scroll fires. This is the case a {baseY,length}
  // signature is blind to.
  const trimAtCapacity = (n = 1) => {
    for (const m of markers) {
      if (m.isDisposed) continue
      m.line -= n
      if (m.line < 0) { m.line = -1; m.isDisposed = true }
    }
    emitScroll()
  }

  return { term, selectLines, touchAtRow, touchOffScreen, touchEnd, emitScroll, shiftContent, trimAtCapacity, screenEl, clearSelection: () => term.clearSelection(), element }
}

describe('useTerminalTouchSelection', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('sets the anchor line on a long-press and highlights just that line', () => {
    const { term, selectLines, touchAtRow } = makeTouchTerm(['a', 'b', 'c'])
    const { result } = renderHook(() => useTerminalTouchSelection(term, true))

    act(() => { result.current.onTouchStart(touchAtRow(1)) })
    act(() => { vi.advanceTimersByTime(500) }) // long-press fires

    expect(selectLines).toHaveBeenCalledTimes(1)
    expect(selectLines).toHaveBeenCalledWith(1, 1)
    expect(result.current.status).toBe('range_anchor')
  })

  it('selects the inclusive span on a second tap (anchor → focus)', () => {
    const { term, selectLines, touchAtRow, touchEnd } = makeTouchTerm(['a', 'b', 'c', 'd', 'e'])
    const { result } = renderHook(() => useTerminalTouchSelection(term, true))

    // Long-press row 1 → anchor.
    act(() => { result.current.onTouchStart(touchAtRow(1)) })
    act(() => { vi.advanceTimersByTime(500) })
    expect(selectLines).toHaveBeenLastCalledWith(1, 1)

    // Quick tap row 3 → focus; span 1..3.
    act(() => { result.current.onTouchStart(touchAtRow(3)) })
    act(() => { result.current.onTouchEnd(touchEnd()) }) // lift before long-press threshold = tap
    expect(selectLines).toHaveBeenLastCalledWith(1, 3)
    expect(result.current.status).toBe('range_selected')
  })

  /**
   * Regression for the GPT/Design/UX finding on #8070: after a touchend the
   * browser synthesizes mousedown/click into the terminal, and xterm's
   * selection service clears the selection on mousedown — which would wipe the
   * range the completing tap just built, leaving Copy nothing to read. The
   * completing tap must preventDefault() to suppress that synthetic sequence.
   */
  it('preventDefaults the completing tap so the synthetic click cannot clear the new range', () => {
    const { term, touchAtRow, touchEnd } = makeTouchTerm(['a', 'b', 'c', 'd', 'e'])
    const { result } = renderHook(() => useTerminalTouchSelection(term, true))

    act(() => { result.current.onTouchStart(touchAtRow(1)) })
    act(() => { vi.advanceTimersByTime(500) }) // anchor
    act(() => { result.current.onTouchStart(touchAtRow(3)) })
    const end = touchEnd()
    act(() => { result.current.onTouchEnd(end) }) // completing tap
    expect((end as unknown as { preventDefault: ReturnType<typeof vi.fn> }).preventDefault).toHaveBeenCalledTimes(1)
  })

  /**
   * A bare tap with NO gesture in progress must fall through to xterm untouched
   * (it may focus / place the cursor) — the hook must NOT preventDefault it.
   */
  it('does not preventDefault a tap when no gesture is in progress', () => {
    const { term, touchAtRow, touchEnd } = makeTouchTerm(['a', 'b', 'c'])
    const { result } = renderHook(() => useTerminalTouchSelection(term, true))

    act(() => { result.current.onTouchStart(touchAtRow(1)) })
    const end = touchEnd()
    act(() => { result.current.onTouchEnd(end) }) // quick tap, no anchor set
    expect((end as unknown as { preventDefault: ReturnType<typeof vi.fn> }).preventDefault).not.toHaveBeenCalled()
  })

  it('orders endpoints regardless of tap direction (focus above anchor)', () => {
    const { term, selectLines, touchAtRow, touchEnd } = makeTouchTerm(['a', 'b', 'c', 'd', 'e'])
    const { result } = renderHook(() => useTerminalTouchSelection(term, true))

    act(() => { result.current.onTouchStart(touchAtRow(4)) })
    act(() => { vi.advanceTimersByTime(500) }) // anchor at row 4
    act(() => { result.current.onTouchStart(touchAtRow(1)) })
    act(() => { result.current.onTouchEnd(touchEnd()) }) // tap focus at row 1

    // min..max, not anchor..focus.
    expect(selectLines).toHaveBeenLastCalledWith(1, 4)
  })

  it('expands both endpoints to their wrapped logical-line bounds', () => {
    // Rows: 0 unwrapped, 1-2 a wrapped logical line, 3 unwrapped, 4-5 wrapped.
    const { term, selectLines, touchAtRow, touchEnd } = makeTouchTerm([
      'header',
      'a long line that wraps ',
      { text: 'onto row two', isWrapped: true },
      'middle',
      'another wrapped line ',
      { text: 'continuing here', isWrapped: true },
    ])
    const { result } = renderHook(() => useTerminalTouchSelection(term, true))

    // Anchor on the continuation of the first wrapped line (row 2) → expands up to 1.
    act(() => { result.current.onTouchStart(touchAtRow(2)) })
    act(() => { vi.advanceTimersByTime(500) })
    expect(selectLines).toHaveBeenLastCalledWith(1, 2)

    // Focus on the START of the second wrapped line (row 4) → expands down to 5.
    act(() => { result.current.onTouchStart(touchAtRow(4)) })
    act(() => { result.current.onTouchEnd(touchEnd()) })
    // Span: min(1,4)=1 .. max(2,5)=5.
    expect(selectLines).toHaveBeenLastCalledWith(1, 5)
  })

  it('adds the scrollback offset so a tap addresses the visible line', () => {
    // 4 visible rows, scrolled 10 into scrollback (viewportY=10).
    const rows = Array.from({ length: 20 }, (_, i) => `r${i}`)
    const { term, selectLines, touchAtRow } = makeTouchTerm(rows, { viewportY: 10, visibleRows: 4, screenHeight: 80 })
    const { result } = renderHook(() => useTerminalTouchSelection(term, true))

    // Long-press visible row 2 → absolute buffer row 12.
    act(() => { result.current.onTouchStart(touchAtRow(2)) })
    act(() => { vi.advanceTimersByTime(500) })
    expect(selectLines).toHaveBeenLastCalledWith(12, 12)
  })

  it('cancels the pending long-press when the finger pans past tolerance (scroll, not press)', () => {
    const { term, selectLines, touchAtRow } = makeTouchTerm(['a', 'b', 'c'], { screenHeight: 300, visibleRows: 3 })
    const { result } = renderHook(() => useTerminalTouchSelection(term, true))

    act(() => { result.current.onTouchStart(touchAtRow(1)) })
    // Move far (a scroll): a touch 50px away in Y.
    const moved = { touches: [{ clientX: 10, clientY: touchAtRow(1).touches[0].clientY + 50 }] } as unknown as React.TouchEvent
    act(() => { result.current.onTouchMove(moved) })
    act(() => { vi.advanceTimersByTime(500) })

    expect(selectLines).not.toHaveBeenCalled()
    expect(result.current.status).toBeNull()
  })

  /**
   * A pure PAN through existing scrollback (only viewportY moves; buffer
   * content unchanged) must KEEP the pending anchor — the anchor is an absolute
   * buffer row, so a second tap after panning completes the span correctly.
   * This is what makes a range taller than one viewport selectable (the whole
   * point of #6834); dropping it on every scroll capped ranges at one screen
   * (Design review #8070).
   */
  it('keeps the anchor across a pure pan so ranges can exceed one viewport', () => {
    const { term, selectLines, touchAtRow, touchEnd, emitScroll } = makeTouchTerm(
      ['r0', 'r1', 'r2', 'r3', 'r4', 'r5', 'r6', 'r7'],
      { visibleRows: 4, screenHeight: 80 },
    )
    const { result } = renderHook(() => useTerminalTouchSelection(term, true))

    act(() => { result.current.onTouchStart(touchAtRow(1)) })
    act(() => { vi.advanceTimersByTime(500) }) // anchor at row 1
    expect(selectLines).toHaveBeenLastCalledWith(1, 1)

    // User pans (no new output). Anchor must survive.
    act(() => { emitScroll() })
    expect(result.current.status).toBe('range_anchor')

    // Second tap after the pan completes the span — a range wider than a screen.
    act(() => { result.current.onTouchStart(touchAtRow(3)) })
    act(() => { result.current.onTouchEnd(touchEnd()) })
    expect(selectLines).toHaveBeenLastCalledWith(1, 3)
    expect(result.current.status).toBe('range_selected')
  })

  /**
   * NEW output (baseY / length change) genuinely moves the highlighted row
   * under the user — abandon the half-built gesture so the next tap starts
   * fresh instead of completing a stale span.
   */
  it('drops the anchor when new output shifts the buffer mid-gesture', () => {
    const { term, selectLines, touchAtRow, shiftContent } = makeTouchTerm(['a', 'b', 'c', 'd'])
    const { result } = renderHook(() => useTerminalTouchSelection(term, true))

    act(() => { result.current.onTouchStart(touchAtRow(1)) })
    act(() => { vi.advanceTimersByTime(500) }) // anchor set
    expect(selectLines).toHaveBeenCalledTimes(1)

    // New output arrives: baseY + length change (not a pure pan).
    act(() => { shiftContent(2, 2) })
    // A subsequent tap must NOT complete a stale range — it starts fresh.
    act(() => { result.current.onTouchStart(touchAtRow(3)) })
    act(() => { vi.advanceTimersByTime(500) })
    expect(selectLines).toHaveBeenLastCalledWith(3, 3) // a NEW anchor, not a 1..3 span
    expect(result.current.status).toBe('range_anchor')
  })

  /**
   * At scrollback capacity xterm trims one line per new line, so baseY and
   * length stay constant while every absolute row remaps — a {baseY,length}
   * check is blind to it. The anchor marker is not: its line moves (or disposes
   * to -1), so the gesture is abandoned. This is the exact long-scrollback case
   * #6834 targets (Design review #8070).
   */
  it('drops the anchor on a capacity trim even when baseY and length are unchanged', () => {
    const { term, selectLines, touchAtRow, trimAtCapacity } = makeTouchTerm(['a', 'b', 'c', 'd'])
    const { result } = renderHook(() => useTerminalTouchSelection(term, true))

    act(() => { result.current.onTouchStart(touchAtRow(1)) })
    act(() => { vi.advanceTimersByTime(500) }) // anchor set + marker registered
    expect(selectLines).toHaveBeenCalledTimes(1)
    expect(result.current.status).toBe('range_anchor')

    // Capacity trim: marker line moves, baseY/length hold — a signature check
    // would miss it, the marker does not.
    act(() => { trimAtCapacity(1) })
    // The next tap starts a FRESH anchor, not a stale span.
    act(() => { result.current.onTouchStart(touchAtRow(3)) })
    act(() => { vi.advanceTimersByTime(500) })
    expect(selectLines).toHaveBeenLastCalledWith(3, 3)
    expect(result.current.status).toBe('range_anchor')
  })

  /**
   * The handlers sit on a div that also hosts overlaid interactive children
   * (Reconnect button, completion menu). A touch that starts OFF the grid must
   * NOT arm the gesture, so those taps aren't swallowed (Design review #8070).
   */
  it('does not arm the gesture when the touch starts off the terminal grid', () => {
    const { term, selectLines, touchOffScreen } = makeTouchTerm(['a', 'b', 'c'])
    const { result } = renderHook(() => useTerminalTouchSelection(term, true))

    act(() => { result.current.onTouchStart(touchOffScreen(1)) })
    act(() => { vi.advanceTimersByTime(500) })

    expect(selectLines).not.toHaveBeenCalled()
    expect(result.current.status).toBeNull()
  })

  /**
   * A FOREIGN non-empty selection change (e.g. the Select soft key building its
   * own selection) while our anchor is pending must abandon the anchor + chip —
   * otherwise they linger contradicting the key bar's stage label, because
   * getSelection() is not '' (UX review #8070).
   */
  it('abandons the pending anchor when a foreign selection replaces it', () => {
    const { term, touchAtRow } = makeTouchTerm(['a', 'b', 'c', 'd'])
    const { result } = renderHook(() => useTerminalTouchSelection(term, true))

    act(() => { result.current.onTouchStart(touchAtRow(1)) })
    act(() => { vi.advanceTimersByTime(500) }) // our anchor at row 1
    expect(result.current.status).toBe('range_anchor')

    // A foreign surface (the Select soft key) sets its OWN selection: emulate by
    // driving selectLines directly (bypassing the hook), which fires
    // onSelectionChange with a non-empty selection the hook did not cause.
    act(() => { (term as unknown as { selectLines: (a: number, b: number) => void }).selectLines(3, 3) })

    // The pending anchor + chip are abandoned.
    expect(result.current.status).toBeNull()
  })

  it('resets when the selection is cleared out from under it (onSelectionChange)', () => {
    const { term, selectLines, touchAtRow, clearSelection } = makeTouchTerm(['a', 'b', 'c'])
    const { result } = renderHook(() => useTerminalTouchSelection(term, true))

    act(() => { result.current.onTouchStart(touchAtRow(1)) })
    act(() => { vi.advanceTimersByTime(500) })
    expect(result.current.status).toBe('range_anchor')

    act(() => { clearSelection() }) // ESC / viewport tap
    expect(result.current.status).toBeNull()

    // Next tap starts a fresh anchor, not a span from the cleared row.
    act(() => { result.current.onTouchStart(touchAtRow(2)) })
    act(() => { vi.advanceTimersByTime(500) })
    expect(selectLines).toHaveBeenLastCalledWith(2, 2)
  })

  it('does not mutate a hidden pane (offsetParent null — staleness gate)', () => {
    const { term, selectLines, touchAtRow, element } = makeTouchTerm(['a', 'b', 'c'])
    const { result } = renderHook(() => useTerminalTouchSelection(term, true))

    element.offsetParent = null // tab/instance switch hid the pane
    act(() => { result.current.onTouchStart(touchAtRow(1)) })
    act(() => { vi.advanceTimersByTime(500) })

    expect(selectLines).not.toHaveBeenCalled()
    expect(result.current.status).toBeNull()
  })

  it('ignores multi-touch (pinch-zoom is never an endpoint gesture)', () => {
    const { term, selectLines, touchAtRow } = makeTouchTerm(['a', 'b', 'c'])
    const { result } = renderHook(() => useTerminalTouchSelection(term, true))

    act(() => { result.current.onTouchStart(touchAtRow(1, 2)) }) // 2 touches
    act(() => { vi.advanceTimersByTime(500) })

    expect(selectLines).not.toHaveBeenCalled()
  })

  it('is inert on a mouse device (enabled=false)', () => {
    const { term, selectLines, touchAtRow } = makeTouchTerm(['a', 'b', 'c'])
    const { result } = renderHook(() => useTerminalTouchSelection(term, false))

    act(() => { result.current.onTouchStart(touchAtRow(1)) })
    act(() => { vi.advanceTimersByTime(500) })

    expect(selectLines).not.toHaveBeenCalled()
    expect(result.current.status).toBeNull()
  })
})
