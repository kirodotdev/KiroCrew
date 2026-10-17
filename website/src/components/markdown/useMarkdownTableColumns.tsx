import React, { useCallback, useLayoutEffect, useRef, useState } from 'react'
import ColumnResizer from '../ColumnResizer'
import { useIsTouchDevice } from '../../hooks/useIsTouchDevice'

/** Narrowest a column may be dragged to: the grip plus a couple of glyphs. */
const MIN_COL = 48
/** Widest a single column may be dragged to, so its grip stays findable. */
const MAX_COL = 720
/** The grip's hit strip (ColumnResizer's `w-1.5`). */
const GRIP_PX = 6

interface HeaderEdge { right: number; top: number; height: number; width: number; label: string }

/** A column's drag bounds always contain its laid-out width, so auto layout
 *  handing a column more than MAX_COL (or less than MIN_COL) can never make a
 *  widen gesture narrow it, or a narrow gesture widen it. */
const boundsFor = (laidOut: number) => ({
  min: Math.min(MIN_COL, laidOut),
  max: Math.max(MAX_COL, laidOut),
})

const clamp = (w: number, laidOut: number) => {
  const { min, max } = boundsFor(laidOut)
  return Math.round(Math.min(max, Math.max(min, w)))
}

function headerCells(table: HTMLTableElement): HTMLTableCellElement[] {
  return Array.from(table.querySelectorAll<HTMLTableCellElement>(':scope > thead > tr:first-child > th'))
}

/** A cell's declared span, read from the attribute: `rowspan="0"` (span to
 *  the end of the section) is a span too, and the DOM property may clamp it
 *  to 1. An absent or unparsable attribute is a span of 1. */
function spanOf(cell: HTMLTableCellElement, attr: 'colspan' | 'rowspan'): number {
  const n = Number.parseInt(cell.getAttribute(attr) ?? '1', 10)
  return Number.isNaN(n) ? 1 : n
}

/** One header cell per logical column holds only for a plain grid: a
 *  raw-HTML table with a spanned cell or a multi-row header would map grips
 *  and `<col>`s onto the wrong columns, so such a table keeps auto layout. */
function isPlainGrid(table: HTMLTableElement): boolean {
  if (table.querySelectorAll(':scope > thead > tr').length > 1) return false
  // Only cells that DECLARE a span are visited, so a long GFM table (which
  // never carries one) costs a selector match, not a walk over every cell.
  return !Array.from(table.querySelectorAll<HTMLTableCellElement>('th[colspan], td[colspan], th[rowspan], td[rowspan]'))
    .some(cell => cell.closest('table') === table && (spanOf(cell, 'colspan') > 1 || spanOf(cell, 'rowspan') !== 1))
}

/** Where each header cell's right edge sits inside the table's scroll wrapper.
 *  A cell's offsetParent is its table, so the table's own offset is added. */
function measureEdges(table: HTMLTableElement): HeaderEdge[] {
  const thead = table.tHead
  if (!thead || !isPlainGrid(table)) return []
  const top = table.offsetTop + thead.offsetTop
  return headerCells(table).map(th => ({
    right: table.offsetLeft + th.offsetLeft + th.offsetWidth,
    top,
    height: thead.offsetHeight,
    // The laid-out width, rounded UP: offsetWidth rounds, and freezing a
    // column even a fraction narrower than auto layout gave it clips (and
    // ellipsizes) a header label that fitted exactly.
    width: Math.ceil(th.getBoundingClientRect().width) || th.offsetWidth,
    label: th.textContent?.trim() ?? '',
  }))
}

/**
 * User-resizable columns for an auto-layout markdown table.
 *
 * `useTableColumnWidths` assumes columns with declared px defaults; a markdown
 * table has none, its widths come out of auto layout. So nothing changes until
 * the first drag: that drag snapshots the widths the browser laid out as the
 * baseline, and from then on the table is `table-layout: fixed` with one
 * `<col>` per column. Widening a column widens the table (the wrapper's own
 * horizontal scroll absorbs it) instead of taking pixels from a neighbour.
 *
 * The grips are an overlay inside the scroll wrapper, one per header cell at
 * its right edge, rather than children of the `th` override: the header cell
 * markup (and every renderer golden that pins it) stays as it was. They render
 * only once real layout exists, so server output, jsdom and a zero-width host
 * carry no grips at all. A touch device carries none either.
 *
 * All of a table's grips are one tab stop; ArrowUp/ArrowDown move between
 * them and ArrowLeft/ArrowRight resize the focused column.
 *
 * Widths live in component state only: they last as long as the rendered
 * message, not across reloads. Enter or a double-click on a grip returns that
 * column to its laid-out width; when every column is back, the table returns
 * to auto layout.
 */
export function useMarkdownTableColumns(tableRef: React.RefObject<HTMLTableElement | null>) {
  const [edges, setEdges] = useState<HeaderEdge[]>([])
  const [baseline, setBaseline] = useState<number[] | null>(null)
  const [cols, setCols] = useState<number[] | null>(null)
  // The table's grips are ONE roving tab stop: Tab lands on this grip, and
  // ArrowUp/ArrowDown move along the row, so a transcript of many tables
  // does not cost a Tab press per column.
  const [focusIndex, setFocusIndex] = useState(0)
  const overlayRefs = useRef<(HTMLDivElement | null)[]>([])
  // A touch pointer gets no grips: a strip narrow enough to sit on a cell
  // edge is too narrow to hit with a finger, and its `touch-action: none`
  // would swallow a horizontal swipe that starts there. The wrapper's own
  // horizontal scroll is what a touch reader uses on a wide table.
  const touch = useIsTouchDevice()

  useLayoutEffect(() => {
    const table = tableRef.current
    if (!table) return
    // Re-attached on every measurement (observe() is idempotent), so a header
    // cell the renderer swapped in is watched too.
    let observeHeader = () => {}
    const measure = () => {
      observeHeader()
      const next = measureEdges(table)
      setEdges(prev => (prev.length === next.length && prev.every((e, i) =>
        e.right === next[i].right && e.top === next[i].top && e.height === next[i].height
        && e.width === next[i].width && e.label === next[i].label)
        ? prev : next))
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    // Header boxes only: rows streaming into the body resize the TABLE every
    // frame without moving a header edge, so the table itself is not watched.
    // A body cell that does widen a column resizes that column's `th`. Bursts
    // of observer callbacks collapse into one measurement per frame.
    let frame = 0
    const schedule = () => {
      if (frame) return
      frame = requestAnimationFrame(() => { frame = 0; measure() })
    }
    const observer = new ResizeObserver(schedule)
    if (table.tHead) observer.observe(table.tHead)
    for (const th of headerCells(table)) observer.observe(th)
    observeHeader = () => { for (const th of headerCells(table)) observer.observe(th) }
    return () => { observer.disconnect(); if (frame) cancelAnimationFrame(frame) }
  }, [tableRef])

  // A table whose header changed shape (a streamed reply) drops stale widths.
  const active = cols && cols.length === edges.length ? cols : null

  const resize = useCallback((index: number, width: number) => {
    const base = baseline && baseline.length === edges.length ? baseline : edges.map(e => e.width)
    const prev = cols && cols.length === base.length ? cols : null
    const nextWidth = clamp(width, base[index])
    // A width equal to the laid-out one changes nothing; it must not freeze an
    // auto-layout table into fixed layout (a motionless click commits a drag).
    if (!prev && nextWidth === base[index]) return
    if (base !== baseline) setBaseline(base)
    const next = (prev ?? base).slice()
    next[index] = nextWidth
    setCols(next)
  }, [baseline, cols, edges])

  const reset = useCallback((index: number) => {
    if (!baseline || !cols) return
    const next = cols.slice()
    next[index] = baseline[index]
    if (next.every((w, i) => w === baseline[i])) {
      // Back to auto layout: the next resize measures fresh widths instead of
      // re-adopting ones the table has since outgrown.
      setCols(null)
      setBaseline(null)
    } else {
      setCols(next)
    }
  }, [baseline, cols])

  const tableStyle: React.CSSProperties | undefined = active
    ? { tableLayout: 'fixed', width: active.reduce((a, b) => a + b, 0), minWidth: 0 }
    : undefined

  const colgroup = active
    ? <colgroup>{active.map((w, i) => <col key={i} style={{ width: w }} />)}</colgroup>
    : null

  const current = Math.min(focusIndex, Math.max(0, edges.length - 1))
  const navigate = (from: number, dir: -1 | 1) => {
    const to = from + dir
    if (to < 0 || to >= edges.length) return
    setFocusIndex(to)
    overlayRefs.current[to]?.querySelector<HTMLElement>('[role="separator"]')?.focus()
  }

  const grips = !touch && edges.length > 0 && edges.every(e => e.width > 0)
    ? edges.map((e, i) => {
      const { min, max } = boundsFor(baseline?.[i] ?? e.width)
      return (
      <div key={i} ref={el => { overlayRefs.current[i] = el }} className="absolute" data-testid="table-column-grip"
        style={{ left: e.right - GRIP_PX, top: e.top, width: GRIP_PX, height: e.height }}>
        <ColumnResizer column={e.label} value={active?.[i] ?? e.width} min={min} max={max}
          tabIndex={i === current ? 0 : -1} onFocus={() => setFocusIndex(i)} onNavigate={dir => navigate(i, dir)}
          onResize={w => resize(i, w)} onReset={() => reset(i)} />
      </div>
      )
    })
    : null

  return { resized: active !== null, tableStyle, colgroup, grips }
}
