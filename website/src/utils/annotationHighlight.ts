/**
 * The stand-in highlight for the passage an open comment composer annotates.
 *
 * Focusing the composer's input collapses the document selection, so without
 * this nothing on the page marks what the open box is anchored to. The paint
 * goes through the browser-native CSS Custom Highlight API (`CSS.highlights` +
 * `Range`), never by wrapping text in `<mark>` elements: the previews are
 * React-reconciled (react-markdown), and splitting their text nodes would
 * crash React on the next re-render. Ranges live outside the DOM.
 *
 * One registry name (`mc-annotate`, styled in index.css) is shared by every
 * host that has a composer open — file tabs stay mounted while hidden and each
 * can retain one — so ranges are aggregated per OWNER: opening or closing a
 * composer in one host cannot erase another's paint.
 *
 * Feature-detected at runtime: where the API is absent the calls are no-ops
 * (no highlight, nothing else changes).
 */
type NativeHighlight = object
const HighlightCtor: (new (...ranges: Range[]) => NativeHighlight) | undefined =
  typeof window !== 'undefined'
    ? (window as unknown as { Highlight?: new (...r: Range[]) => NativeHighlight }).Highlight
    : undefined
const cssHighlights: { set(n: string, h: NativeHighlight): void; delete(n: string): boolean } | undefined =
  typeof CSS !== 'undefined'
    ? (CSS as unknown as { highlights?: { set(n: string, h: NativeHighlight): void; delete(n: string): boolean } }).highlights
    : undefined

const ANNOTATE_HL = 'mc-annotate'
const rangesByOwner = new Map<object, Range[]>()

/** Replace `owner`'s ranges (an empty list removes them) and repaint the union. */
function setAnnotationHighlightRanges(owner: object, ranges: Range[]): void {
  if (!HighlightCtor || !cssHighlights) return
  if (ranges.length > 0) rangesByOwner.set(owner, ranges)
  else rangesByOwner.delete(owner)
  const all = Array.from(rangesByOwner.values()).flat()
  if (all.length > 0) cssHighlights.set(ANNOTATE_HL, new HighlightCtor(...all))
  else cssHighlights.delete(ANNOTATE_HL)
}

/**
 * Paint `range` for `owner`, split per text node so a selection spanning
 * several nodes highlights each of them exactly (a single multi-node Range
 * renders unevenly in some engines).
 */
export function paintAnnotationHighlight(owner: object, range: Range): void {
  if (!HighlightCtor || !cssHighlights) return
  const walker = document.createTreeWalker(range.commonAncestorContainer, NodeFilter.SHOW_TEXT)
  const textNodes: Text[] = []
  let node: Node | null
  while ((node = walker.nextNode())) {
    if (range.intersectsNode(node)) textNodes.push(node as Text)
  }
  if (textNodes.length === 0 && range.startContainer.nodeType === Node.TEXT_NODE) {
    textNodes.push(range.startContainer as Text)
  }
  const ranges: Range[] = []
  for (const textNode of textNodes) {
    const start = textNode === range.startContainer ? range.startOffset : 0
    const end = textNode === range.endContainer ? range.endOffset : textNode.length
    if (start === end) continue
    const r = document.createRange()
    r.setStart(textNode, start)
    r.setEnd(textNode, end)
    ranges.push(r)
  }
  setAnnotationHighlightRanges(owner, ranges)
}

/** Remove `owner`'s paint. */
export function clearAnnotationHighlight(owner: object): void {
  setAnnotationHighlightRanges(owner, [])
}
