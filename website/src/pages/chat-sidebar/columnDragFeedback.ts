/** What a board column shows while another column is dragged over the strip.
 *
 *  Column reorder uses native HTML5 drag from the header's grip, and a native
 *  drag's `dataTransfer` withholds its data until `drop`: a `dragover` handler
 *  can see that a column is moving but not which one. So the drag start records
 *  the dragged column on the strip as a data attribute, and each column's
 *  `dragover` reads it back to decide where a release would put the column.
 *
 *  The marks are DOM data attributes rather than React state. The board lane is
 *  a render helper of the sidebar shell, and a `dragover` fires many times a
 *  second; holding the marks in shell state would re-render the whole sidebar
 *  on every one of them. The column's own classes style the attributes
 *  (`COLUMN_DRAG_CLASSES`), so no stylesheet outside the column is involved. */
import type { DragEvent } from 'react'

/** Where a release over a column lands the dragged column. */
export type ColumnDropSide = 'before' | 'after'

/** Classes a board column carries so the drag marks below have a look. The
 *  source column dims; the target column grows an accent bar in the strip's
 *  gap on the side the dragged column will take. A box shadow, not a border, so
 *  the marks never shift the columns' layout under the pointer. Each mark is a
 *  bare data attribute (one per side) rather than one attribute with a value,
 *  so each variant reads `data-<mark>:` with no `=` selector. */
export const COLUMN_DRAG_CLASSES =
  'data-col-drag:opacity-50 data-col-drop-before:shadow-[-4px_0_0_0_var(--accent)] data-col-drop-after:shadow-[4px_0_0_0_var(--accent)]'

/** The side of `targetId` a dropped `draggedId` ends up on, or null when the
 *  drop would not move it. Mirrors the reorder the column's `drop` runs: the
 *  dragged id is removed and re-inserted at the target's index, which puts it
 *  after a target to its right and before a target to its left. */
export function columnDropSide(order: readonly string[], draggedId: string, targetId: string): ColumnDropSide | null {
  if (draggedId === targetId) return null
  const from = order.indexOf(draggedId)
  const to = order.indexOf(targetId)
  if (from < 0 || to < 0) return null
  return from < to ? 'after' : 'before'
}

/** Mark a column drag as started from the grip inside `column`, and make the
 *  whole column the drag image so the pointer carries what it moves. */
export function startColumnDrag(e: DragEvent<HTMLElement>, column: HTMLElement | null, columnId: string): void {
  if (!column) return
  column.dataset.colDrag = 'source'
  const strip = column.parentElement
  if (strip) strip.dataset.colDragging = columnId
  // The image is a nicety: where the engine lacks or refuses `setDragImage` (an
  // unimplemented DOM, a webview), the drag goes on with the default image, the
  // grip, rather than being cancelled by a throw inside `dragstart`.
  try {
    const rect = column.getBoundingClientRect()
    e.dataTransfer.setDragImage(column, e.clientX - rect.left, e.clientY - rect.top)
  } catch {
    // Default drag image.
  }
}

/** The id of the column being dragged across the strip `column` sits in. */
export function draggedColumnId(column: HTMLElement): string | null {
  return column.parentElement?.dataset.colDragging ?? null
}

/** Show (or clear, with null) where a release over `column` would land. */
export function markColumnDropSide(column: HTMLElement, side: ColumnDropSide | null): void {
  if (side === 'before') column.dataset.colDropBefore = ''
  else delete column.dataset.colDropBefore
  if (side === 'after') column.dataset.colDropAfter = ''
  else delete column.dataset.colDropAfter
}

/** The landing side `column` currently shows, or null. */
export function columnDropMark(column: HTMLElement): ColumnDropSide | null {
  if (column.dataset.colDropBefore !== undefined) return 'before'
  if (column.dataset.colDropAfter !== undefined) return 'after'
  return null
}

/** Clear every drag mark in the strip `column` sits in. Runs on `dragend`,
 *  which fires on the source whether the drop landed, missed or was cancelled. */
export function endColumnDrag(column: HTMLElement | null): void {
  const strip = column?.parentElement
  if (!strip) return
  delete strip.dataset.colDragging
  strip.querySelectorAll<HTMLElement>('[data-col-drag], [data-col-drop-before], [data-col-drop-after]').forEach(el => {
    delete el.dataset.colDrag
    markColumnDropSide(el, null)
  })
}
