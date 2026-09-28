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
  nodes: { node: Text; start: number }[],
  off: number,
): { node: Text; offset: number } | null {
  for (const nd of nodes) {
    if (off <= nd.start + (nd.node.nodeValue?.length ?? 0)) {
      return { node: nd.node, offset: off - nd.start }
    }
  }
  return null
}

/** Best DOM Range for an anchor's quote when it appears multiple times.
 *  Occurrences are ranked lexicographically: prefix/suffix match first, then —
 *  when the comment stored a `startOffset` — the occurrence nearest that offset.
 *  The offset pins the exact copy the user selected, which fixes repeats that
 *  prefix/suffix can't tell apart (identical surrounding context, or no
 *  prefix/suffix stored at all). Without an offset, prefix/suffix score decides
 *  and ties resolve to the first occurrence. Mirrors the
 *  in-iframe bridge's `findRange`. Exported for tests. */
export function rangeForAnchor(
  idx: { text: string; nodes: { node: Text; start: number }[] },
  a: AnchoredComment,
): Range | null {
  const { text, nodes } = idx
  if (!a.quote) return null
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
  if (best < 0) return null
  const s = locate(nodes, best)
  const e = locate(nodes, best + a.quote.length)
  if (!s || !e) return null
  try {
    const r = document.createRange()
    r.setStart(s.node, s.offset)
    r.setEnd(e.node, e.offset)
    return r
  } catch {
    return null
  }
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

