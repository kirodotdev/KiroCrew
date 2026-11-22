/** Selection helpers for virtualized transcript rows. */

import type { RetainedVirtualRange } from '../hooks/virtualizer/types'

/** The transcript row holding `node`, or null when it sits on none. */
function rowOf(container: HTMLElement, node: Node | null): HTMLElement | null {
  const element = node instanceof Element ? node : node?.parentElement
  const row = element?.closest<HTMLElement>('[data-display-index]')
  return row && container.contains(row) ? row : null
}

function displayIndexOf(row: HTMLElement): number | null {
  const index = Number(row.dataset.displayIndex)
  return Number.isInteger(index) && index >= 0 ? index : null
}

function rowIndexFor(container: HTMLElement, node: Node | null): number | null {
  const row = rowOf(container, node)
  return row ? displayIndexOf(row) : null
}

/** True when either native selection endpoint still belongs to `container`. */
export function selectionTouchesContainer(container: HTMLElement, selection: Selection): boolean {
  return container.contains(selection.anchorNode) || container.contains(selection.focusNode)
}

/** One selection endpoint, by node and by row + character offset.
 *
 * The row is held as its element, with the display index it carried. A page
 * of older history renumbers every row, so the number alone would name an
 * older message; the element survives a landing for every row but one, since
 * rows are keyed by message identity (re-rendered, not remounted). */
interface RowPoint {
  node: Node
  offset: number
  el: HTMLElement
  row: number
  /** Characters of row text before the point; survives a re-render. */
  chars: number
  /** Characters of row text after the point. A row drawn again as a new
   *  element grew at its HEAD (the opening prompt landed in front of the
   *  reply), so its tail is what still measures the point there. */
  charsAfter: number
}

/** Where both endpoints of a selection last sat on transcript rows. */
export interface SelectionEndpoints {
  anchor: RowPoint
  focus: RowPoint
}

function rowElement(container: HTMLElement, row: number): HTMLElement | null {
  return container.querySelector<HTMLElement>(`[data-display-index="${row}"]`)
}

function rowPoint(container: HTMLElement, node: Node, offset: number): RowPoint | null {
  const el = rowOf(container, node)
  const row = el ? displayIndexOf(el) : null
  if (!el || row === null) return null
  const before = document.createRange()
  const after = document.createRange()
  try {
    before.setStart(el, 0)
    before.setEnd(node, offset)
    after.setStart(node, offset)
    after.setEnd(el, el.childNodes.length)
  } catch {
    return null
  }
  return { node, offset, el, row, chars: before.toString().length, charsAfter: after.toString().length }
}

/** The row of `p` once its element is gone: drawn again as a new element (the
 * topmost turn, when its opening prompt lands), it is found by number, moved
 * by as far as `witness`, the selection's other row, moved. A landing moves
 * both rows by the same count, a row redrawn in place by none. With no mounted
 * witness there is nothing to measure by, and no row is chosen. */
function redrawnRow(container: HTMLElement, p: RowPoint, witness: RowPoint): HTMLElement | null {
  if (!container.contains(witness.el)) return null
  const at = displayIndexOf(witness.el)
  return at === null ? null : rowElement(container, p.row + (at - witness.row))
}

/** The point `chars` characters into the text of `el`. A point on the
 * boundary between two text nodes keeps the side the endpoint was on: the
 * start of the later node when it sat at the start of its own (`atStart`),
 * else the end of the earlier one. A start at a reply's first character never
 * lands at the end of the prompt before it. */
function pointAtChars(el: HTMLElement, chars: number, atStart: boolean): [Node, number] {
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
  let seen = 0
  let last: Text | null = null
  for (let n = walker.nextNode() as Text | null; n; n = walker.nextNode() as Text | null) {
    const end = seen + n.data.length
    if (atStart ? end > chars : end >= chars) return [n, chars - seen]
    seen = end
    last = n
  }
  return last ? [last, last.data.length] : [el, 0]
}

/** The live position for `p`: its own node if still mounted, else the same
 * character offset in its row's current text. A row drawn again as a new
 * element is measured from its tail: it grew at its head. */
function resolvePoint(container: HTMLElement, p: RowPoint, witness: RowPoint): [Node, number] | null {
  if (p.node.isConnected && container.contains(p.node)) return [p.node, p.offset]
  if (container.contains(p.el)) return pointAtChars(p.el, p.chars, p.offset === 0)
  const el = redrawnRow(container, p, witness)
  if (!el) return null
  return pointAtChars(el, Math.max(0, (el.textContent ?? '').length - p.charsAfter), p.offset === 0)
}

/** Snapshot the endpoints when both sit on rows, else null. */
export function rowEndpoints(container: HTMLElement, selection: Selection): SelectionEndpoints | null {
  if (selection.isCollapsed || !selectedRowRange(container, selection)) return null
  const { anchorNode, anchorOffset, focusNode, focusOffset } = selection
  if (!anchorNode || !focusNode) return null
  const anchor = rowPoint(container, anchorNode, anchorOffset)
  const focus = rowPoint(container, focusNode, focusOffset)
  return anchor && focus ? { anchor, focus } : null
}

/** Put an endpoint that lost its transcript row back where it last sat.
 *
 * Two ways an endpoint leaves its row while the other stays in the transcript:
 * - a touch handle dragged over the title or the composer lands in their text;
 * - the row holding the START is re-rendered while it is scrolled away. The
 *   browser then parks the start between rows, and every row mounted or
 *   unmounted above it walks it up to offset 0 of the scroller, so the
 *   selection grew to everything from the top of the chat.
 * The endpoint goes back to its own last position, by character offset in its
 * row if the old node is gone: never a row's start or the transcript's edge,
 * and never another message's row. A page of older history landing above
 * renumbers the rows, so the row is its element while that is mounted, and
 * otherwise its number re-based by the other endpoint's row (`redrawnRow`).
 * Only a selection that has already sat on rows (`last`) is touched, so a
 * fresh long-press is never rewritten. Returns whether the selection changed.
 */
export function restoreEndpointToTranscript(
  container: HTMLElement,
  selection: Selection,
  last: SelectionEndpoints | null,
): boolean {
  if (!last || selection.isCollapsed) return false
  const { anchorNode, anchorOffset, focusNode, focusOffset } = selection
  if (!anchorNode || !focusNode) return false
  const anchorOnRow = rowIndexFor(container, anchorNode) !== null
  const focusOnRow = rowIndexFor(container, focusNode) !== null
  if (!anchorOnRow && focusOnRow) {
    const at = resolvePoint(container, last.anchor, last.focus)
    if (!at) return false
    selection.setBaseAndExtent(at[0], at[1], focusNode, focusOffset)
    return true
  }
  if (anchorOnRow && !container.contains(focusNode)) {
    const at = resolvePoint(container, last.focus, last.anchor)
    if (!at) return false
    selection.setBaseAndExtent(anchorNode, anchorOffset, at[0], at[1])
    return true
  }
  return false
}

/** Return the exclusive row span containing both selection endpoints.
 *
 * A range is intentionally returned only when both endpoints are transcript
 * rows. A transient WebKit endpoint outside the scroller leaves the last safe
 * retained span in place instead of replacing it with a document-wide range.
 */
export function selectedRowRange(
  container: HTMLElement,
  selection: Selection,
): RetainedVirtualRange | null {
  if (selection.isCollapsed) return null
  const anchor = rowIndexFor(container, selection.anchorNode)
  const focus = rowIndexFor(container, selection.focusNode)
  if (anchor === null || focus === null) return null
  return { start: Math.min(anchor, focus), end: Math.max(anchor, focus) + 1 }
}

/** What the transcript's retained range should do after a selection change.
 *
 * `null` releases the retention; `'keep'` leaves the last retained span in
 * place; a range replaces it. An endpoint that is inside the transcript but on
 * no row (the scroller's padding under the composer, a spacer, a sentinel) is
 * a transient handle position, not the end of the selection: releasing there
 * let the virtualizer unmount the row holding the selection's start, and the
 * browser then re-rooted the selection at the top of the transcript.
 *
 * Read-only: the selection is never rewritten. Snapping an off-row endpoint to
 * a transcript edge stretched a fresh Android long-press to the whole chat, or
 * cancelled it outright. The overlays are inert while a selection is held
 * (`useSelectionInertOverlays`), so a handle no longer lands in the chrome.
 */
export function nextRetainedRange(
  container: HTMLElement,
  selection: Selection | null,
): RetainedVirtualRange | 'keep' | null {
  if (!selection || selection.isCollapsed) return null
  if (!selectionTouchesContainer(container, selection)) return null
  return selectedRowRange(container, selection) ?? 'keep'
}
