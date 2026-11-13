/**
 * Range-matching helpers for anchored artifact comments on a markdown / text
 * body: locate an anchor's quote (disambiguated by prefix/suffix) inside a
 * rendered container and return a DOM `Range`.
 *
 * The artifact page renders markdown highlights with the DOM-rect overlay
 * (`InlineCommentOverlay`), which consumes these helpers — one reliable
 * mechanism everywhere. A DOM-rect overlay is used rather than the CSS Custom
 * Highlight API because that API does not paint reliably in the dashboard
 * browser and can't do the overlay's box-shadow / persistent active state.
 */

export interface AnchoredComment {
  id: string
  quote: string
  prefix: string
  suffix: string
  /** Rendered-text char offset of the selection start when the comment was
   *  created (anchor `start_offset`). When present it pinpoints the exact
   *  occurrence the user selected, so a repeated quote highlights the right
   *  copy even when prefix/suffix don't disambiguate. Absent for legacy
   *  comments — matching falls back to prefix/suffix + first occurrence. */
  startOffset?: number
}

/** Concatenate the container's text nodes, recording each node's start offset
 *  so a character index in the joined text maps back to a (node, offset).
 *  Exported for tests. */
export function indexTextNodes(root: Node): { text: string; nodes: { node: Text; start: number }[] } {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  const nodes: { node: Text; start: number }[] = []
  let text = ''
  let n: Node | null
  while ((n = walker.nextNode())) {
    const t = n as Text
    nodes.push({ node: t, start: text.length })
    text += t.nodeValue ?? ''
  }
  return { text, nodes }
}

function locate(
  nodes: readonly { node: Text; start: number }[],
  off: number,
): { node: Text; offset: number } | null {
  for (const nd of nodes) {
    if (off <= nd.start + (nd.node.nodeValue?.length ?? 0)) {
      return { node: nd.node, offset: off - nd.start }
    }
  }
  return null
}

/** The DOM ranges to paint for an anchor's quote, empty when it isn't found.
 *
 *  When the quote appears more than once, occurrences are ranked
 *  lexicographically: prefix/suffix match first, then — when the comment
 *  stored a `startOffset` — the occurrence nearest that offset. The offset pins
 *  the exact copy the user selected, which fixes repeats that prefix/suffix
 *  can't tell apart (identical surrounding context, or no prefix/suffix stored
 *  at all). Without an offset, prefix/suffix score decides and ties resolve to
 *  the first occurrence. Mirrors the in-iframe bridge's `findRange`.
 *
 *  The occurrence is then cut around `skip` (sorted spans a reader can't
 *  select, from `unselectableSpans`) by `rangesForSpan`. A quote comes from
 *  `Range.toString()`, which holds the text under `user-select: none` that the
 *  reader dragged across, such as the Copy buttons under a table; one range
 *  over it would draw a box on those buttons. */
export function rangesForAnchor(
  idx: { text: string; nodes: { node: Text; start: number }[] },
  a: AnchoredComment,
  skip: readonly TextSpan[],
): Range[] {
  const best = anchorOffset(idx.text, a)
  return best < 0 ? [] : rangesForSpan(idx, best, best + a.quote.length, skip)
}

/** DOM ranges over `[start, end)` of `idx`'s joined text, one per piece left
 *  once `skip` (sorted unselectable spans) is cut out (see `spanPieces`). A span
 *  that no skipped span touches comes back whole, exactly as given. Each range
 *  starts in the text node holding its first character and ends in the node
 *  holding its last: two table cells join with nothing between them, so the
 *  offset where one cell's text ends is also where the next one's begins, and a
 *  range started at the close of the earlier cell would count a hover over that
 *  cell as on the comment. Shared by the file viewer's highlights and the
 *  artifact comment overlay. */
export function rangesForSpan(
  idx: { text: string; nodes: readonly { node: Text; start: number }[] },
  start: number,
  end: number,
  skip: readonly TextSpan[],
): Range[] {
  const cut = skip.some(([s, e]) => s < end && e > start)
  const out: Range[] = []
  for (const [from, to] of cut ? spanPieces(idx.text, start, end, skip) : [[start, end] as const]) {
    const s = idx.nodes.find(n => from < n.start + (n.node.nodeValue?.length ?? 0))
    const e = locate(idx.nodes, to)
    if (!s || !e) continue
    try {
      const r = document.createRange()
      r.setStart(s.node, from - s.start)
      r.setEnd(e.node, e.offset)
      out.push(r)
    } catch { /* skip */ }
  }
  return out
}

/** Where `rangesForAnchor` finds the anchor's quote in `text`, or -1. */
function anchorOffset(text: string, a: AnchoredComment): number {
  if (!a.quote) return -1
  let from = 0
  let best = -1
  let bestScore = -1
  let bestDist = Infinity
  for (;;) {
    const i = text.indexOf(a.quote, from)
    if (i < 0) break
    const pre = text.slice(Math.max(0, i - (a.prefix?.length ?? 0)), i)
    const suf = text.slice(i + a.quote.length, i + a.quote.length + (a.suffix?.length ?? 0))
    let score = 0
    if (a.prefix && pre.includes(a.prefix)) score++
    if (a.suffix && suf.includes(a.suffix)) score++
    // Distance to the stored selection offset (0 for all occurrences when no
    // offset was recorded, so the prefix/suffix score alone decides and ties
    // keep the first occurrence).
    const dist = a.startOffset == null ? 0 : Math.abs(i - a.startOffset)
    if (score > bestScore || (score === bestScore && dist < bestDist)) {
      best = i
      bestScore = score
      bestDist = dist
    }
    from = i + 1
  }
  return best
}

/** Find the character offset of the best-matching occurrence of `anchor` in
 *  `text`. When `startOffset` is present, picks the occurrence nearest that
 *  offset (handles repeated identical text). Without an offset, falls back to
 *  the first match. Returns -1 when not found. Exported for unit tests +
 *  consumed by MarkdownPanel's CSS Highlight apply() loop. */
export function findBestOccurrence(text: string, anchor: string, startOffset?: number): number {
  if (!anchor) return -1
  let bestIdx = -1
  if (startOffset != null) {
    let from = 0
    let bestDist = Infinity
    for (;;) {
      const i = text.indexOf(anchor, from)
      if (i < 0) break
      const dist = Math.abs(i - startOffset)
      if (dist < bestDist) { bestDist = dist; bestIdx = i }
      else break // occurrences are in increasing order; distance is V-shaped past the nearest
      from = i + 1
    }
  } else {
    bestIdx = text.indexOf(anchor)
  }
  return bestIdx
}


/** A `[start, end)` span of a page's text. */
export type TextSpan = readonly [number, number]

/** A page's text with every whitespace character and every `skip`ped span
 *  removed (`chars`), and where each kept character sits in the page (`at`). */
export interface PageIndex { chars: string; at: number[] }

const WHITESPACE = /\s/

/** Index `text` for `findLooseSpan`. One pass over the text, about 11 ms for
 *  232,000 characters, so a host builds it at most once per highlight pass and
 *  only when a comment needs it. */
export function indexPage(text: string, skip: readonly TextSpan[] = []): PageIndex {
  let chars = ''
  const at: number[] = []
  let k = 0
  for (let i = 0; i < text.length; i++) {
    while (k < skip.length && skip[k][1] <= i) k++
    if (k < skip.length && skip[k][0] <= i) continue
    const ch = text[i]
    if (WHITESPACE.test(ch)) continue
    chars += ch
    at.push(i)
  }
  return { chars, at }
}

/** Where `anchor` sits in the page `page` indexes, as the span `[start, end)`
 *  of the page's text, when the two differ only in whitespace and in text a
 *  reader can't select, or null when it isn't there.
 *
 *  For a comment that quotes the browser's selection string and has no
 *  `renderedQuote` to search by: one saved before that field existed, or one
 *  made on the toolbar's fallback path. A browser writes whitespace into a
 *  selection string that the page's text nodes don't hold (a tab between table
 *  cells, a newline between rows or at a `<br>`, a blank line between
 *  paragraphs, a space for a soft line break) and leaves out text that can't be
 *  selected, such as the Copy buttons under a table. So the quote is compared
 *  with `page` (`indexPage` with `unselectableSpans` skipped), whitespace
 *  removed from both, and every other character has to match in order.
 *
 *  The caller tries an exact match first. With `startOffset`, where the
 *  selection began, the match nearest it wins. Without one, only a match the
 *  quote has nowhere else counts: with nothing to say which copy was selected,
 *  no underline is better than one on text the reader didn't pick. */
export function findLooseSpan(page: PageIndex, anchor: string, startOffset?: number): { start: number; end: number } | null {
  const quote = indexPage(anchor).chars
  if (!quote) return null
  const { chars, at } = page
  const spanAt = (i: number) => ({ start: at[i], end: at[i + quote.length - 1] + 1 })
  if (startOffset == null) {
    const i = chars.indexOf(quote)
    return i >= 0 && chars.indexOf(quote, i + 1) < 0 ? spanAt(i) : null
  }
  let best: { start: number; end: number } | null = null
  let bestDist = Infinity
  for (let from = 0; ;) {
    const i = chars.indexOf(quote, from)
    if (i < 0) break
    const span = spanAt(i)
    const dist = Math.abs(span.start - startOffset)
    if (dist < bestDist) { best = span; bestDist = dist }
    else break // matches come in increasing order; distance is V-shaped past the nearest
    from = i + 1
  }
  return best
}

/** The spans of `nodes` (the text nodes under `root` in document order, each
 *  with its offset in their joined text) that a reader can't select, under an
 *  element with the `select-none` class: the row of Copy buttons under a table
 *  (`MarkdownTable`). Sorted and non-overlapping, as `spanPieces` needs them.
 *  One pass over `nodes`, however many such elements the page holds. */
export function unselectableSpans(root: Element, nodes: readonly { node: Text; start: number }[]): TextSpan[] {
  const spans: TextSpan[] = []
  let k = 0
  let last: Element | null = null
  for (const off of root.querySelectorAll('.select-none')) {
    // A select-none element inside one already taken holds no new text.
    if (last && last.contains(off)) continue
    last = off
    const walker = document.createTreeWalker(off, NodeFilter.SHOW_TEXT)
    for (let t = walker.nextNode(); t; t = walker.nextNode()) {
      // Both lists are in document order, so the search only moves forward.
      while (k < nodes.length && nodes[k].node !== t) k++
      if (k === nodes.length) return spans
      const { node, start } = nodes[k]
      if (node.data.length) spans.push([start, start + node.data.length])
    }
  }
  return spans
}

/** `[start, end)` of `text` with every span in `skip` (sorted, as
 *  `unselectableSpans` returns them) cut out, as the pieces left over, each
 *  trimmed of whitespace and none empty. A comment that runs out of a table
 *  into the next paragraph covers the table's Copy buttons in the page text,
 *  though the reader couldn't select them; painting it in pieces keeps the
 *  buttons from being underlined. */
export function spanPieces(text: string, start: number, end: number, skip: readonly TextSpan[]): TextSpan[] {
  const raw: TextSpan[] = []
  let from = start
  for (const [s, e] of skip) {
    if (e <= from) continue
    if (s >= end) break
    if (s > from) raw.push([from, s])
    from = Math.max(from, e)
  }
  if (from < end) raw.push([from, end])
  const pieces: TextSpan[] = []
  for (let [s, e] of raw) {
    while (s < e && WHITESPACE.test(text[s])) s++
    while (e > s && WHITESPACE.test(text[e - 1])) e--
    if (s < e) pieces.push([s, e])
  }
  return pieces
}
