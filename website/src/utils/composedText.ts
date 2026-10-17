/**
 * Rendered-text arithmetic that reaches into open shadow roots.
 *
 * Fenced code in a markdown body renders through Pierre, which draws its rows
 * inside an OPEN shadow root on a `<diffs-container>` host. The light-DOM
 * measures stop at that boundary: `Node.contains` is false for a node in the
 * shadow tree, a `TreeWalker` does not descend into it, and a `Range` cannot
 * have one end on each side. Chromium also reports a mouse selection made
 * inside the shadow tree to the document as a COLLAPSED range at the host's
 * light-DOM parent: `sel.isCollapsed` is true while `sel.toString()` returns
 * the selected code.
 *
 * The text space defined here is every light-DOM Text node in document order
 * (on a body without code that is its `textContent`) plus the code rows of each
 * open shadow root, spliced in where the host sits. Inside a shadow tree only
 * text under a `[data-line]` row counts: Pierre's line-number gutter, icon
 * sprite and `<style>` are not part of the code the reader sees. Consecutive
 * rows are joined by a virtual `\n` that has no Text node behind it, and the
 * `\n` Pierre puts in an EMPTY row is skipped, so the highlighted rows read the
 * same as the plain `<pre>` stand-in CodeBlock shows before highlighting lands:
 * an anchor taken in either state resolves in the other.
 *
 * Only shadow roots inside a fenced code block's surface (`CodeBlock`, marked
 * `data-anchorable-code`) take part. Pierre's other surfaces stay out: windowed
 * code views and the editor render only the rows on screen (offsets would move
 * with the scroll, and editor text is a scratch copy), and a patch's rows read
 * differently from its raw-text stand-in.
 */

export interface TextIndex {
  /** The joined text, including the virtual newlines between code rows. */
  text: string
  /** Each indexed Text node with the offset of its first character in `text`. */
  nodes: { node: Text; start: number }[]
}

/** Pierre's per-row element inside its shadow root. */
const CODE_ROW_ATTR = 'data-line'

/** Marks the element whose shadow-rendered code a comment may anchor in. */
export const ANCHORABLE_CODE_ATTR = 'data-anchorable-code'

/** What the comment layer observes, in the body and in each code block's shadow tree. */
export const COMPOSED_MUTATION_OPTS: MutationObserverInit = { childList: true, subtree: true, characterData: true }

/** The open shadow root of `el` when it hosts anchorable code, else null. */
function anchorableShadow(el: Element): ShadowRoot | null {
  const sr = el.shadowRoot
  return sr && el.closest(`[${ANCHORABLE_CODE_ATTR}]`) ? sr : null
}

/** True when `node` is `container` or sits under it, crossing open shadow
 *  boundaries on the way up (a shadow root's parent is its host). */
export function composedContains(container: Node, node: Node | null): boolean {
  let cur: Node | null = node
  while (cur) {
    if (cur === container) return true
    cur = cur.parentNode ?? (cur instanceof ShadowRoot ? cur.host : null)
  }
  return false
}

/** Pierre renders a blank source line as a row holding one `"\n"` Text node;
 *  the virtual separator already stands for that line break. */
function isEmptyRowFiller(t: Text): boolean {
  const row = t.parentElement
  return t.nodeValue === '\n' && !!row && row.hasAttribute(CODE_ROW_ATTR) && row.childNodes.length === 1
}

/** Build the composed text index of `root`. */
export function indexComposedText(root: Node): TextIndex {
  const nodes: { node: Text; start: number }[] = []
  let text = ''
  // `rows` counts the code rows already emitted in the current shadow tree, so
  // the separator goes BETWEEN rows (the fence's trailing newline is stripped
  // before CodeBlock ever sees the code).
  const visit = (node: Node, inShadow: boolean, inRow: boolean, rows: { n: number }) => {
    if (node.nodeType === Node.TEXT_NODE) {
      if (!inShadow || (inRow && !isEmptyRowFiller(node as Text))) {
        nodes.push({ node: node as Text, start: text.length })
        text += (node as Text).nodeValue ?? ''
      }
      return
    }
    let row = inRow
    if (inShadow && !inRow && node instanceof Element && node.hasAttribute(CODE_ROW_ATTR)) {
      if (rows.n > 0) text += '\n'
      rows.n++
      row = true
    }
    const shadow = node instanceof Element ? anchorableShadow(node) : null
    if (shadow) {
      const shadowRows = { n: 0 }
      for (let c = shadow.firstChild; c; c = c.nextSibling) visit(c, true, false, shadowRows)
    }
    for (let c = node.firstChild; c; c = c.nextSibling) visit(c, inShadow, row, rows)
  }
  visit(root, false, false, { n: 0 })
  return { text, nodes }
}

/** `node` lifted to the tree `target` lives in: itself, or the shadow host
 *  (repeatedly) that holds it. Null when `node` is not in that tree. */
function liftToTree(node: Node, target: Node): Node | null {
  const want = target.getRootNode()
  let cur: Node = node
  for (;;) {
    const r = cur.getRootNode()
    if (r === want) return cur
    if (!(r instanceof ShadowRoot)) return null
    cur = r.host
  }
}

/**
 * Offset of the boundary point (`container`, `offset`) in `idx.text`, or null
 * when the point is in a Text node the index skips (a line number, a style).
 * A point between nodes resolves to the start of the first indexed node at or
 * after it, so it matches what `Range.toString()` counts in the light DOM. A
 * point after the last row of a shadow tree resolves to the end of that row:
 * nothing past the host lifts into the shadow tree, so the end of the whole
 * text would be wrong there.
 */
export function composedTextOffset(idx: TextIndex, container: Node, offset: number): number | null {
  if (container.nodeType === Node.TEXT_NODE) {
    const hit = idx.nodes.find(n => n.node === container)
    if (hit) return hit.start + offset
    // A blank code row's filler is not indexed: measure from the row itself.
    if (!isEmptyRowFiller(container as Text)) return null
    return composedTextOffset(idx, container.parentNode!, 0)
  }
  const point = document.createRange()
  try {
    point.setStart(container, offset)
  } catch {
    return null
  }
  let lastEndInTree: number | null = null
  for (const n of idx.nodes) {
    const lifted = liftToTree(n.node, container)
    if (!lifted) continue
    try {
      // A host lifted into `container`'s tree may BE `container` (or hold the
      // point); its shadow text renders inside it, after a point at offset 0.
      if (lifted !== n.node && lifted === container) return n.start
      if (point.comparePoint(lifted, 0) >= 0) return n.start
      lastEndInTree = n.start + (n.node.nodeValue?.length ?? 0)
    } catch { /* different documents: keep looking */ }
  }
  if (container.getRootNode() instanceof ShadowRoot && lastEndInTree != null) return lastEndInTree
  return idx.text.length
}

/** The (node, offset) a character offset maps to. `bias: 'start'` lands on the
 *  node the character begins (a boundary between two nodes picks the LATER one,
 *  so a quote that opens a code block starts inside the code, not at the end
 *  of the prose node before it); `bias: 'end'` lands on the node it closes. An
 *  offset on a virtual newline clamps to the neighbouring node. */
export function locateOffset(
  idx: TextIndex,
  off: number,
  bias: 'start' | 'end',
): { node: Text; offset: number } | null {
  for (const nd of idx.nodes) {
    const len = nd.node.nodeValue?.length ?? 0
    if (bias === 'start' ? off < nd.start + len : off <= nd.start + len) {
      return { node: nd.node, offset: Math.max(0, off - nd.start) }
    }
  }
  // Nothing begins at or after `off` (an offset at the very end, or past it):
  // the last node's end is the only point a range can use.
  const last = idx.nodes[idx.nodes.length - 1]
  if (!last || off > last.start + (last.node.nodeValue?.length ?? 0)) return null
  return { node: last.node, offset: last.node.nodeValue?.length ?? 0 }
}

/** True when only whitespace of `host`'s text lies between `off` and the
 *  host's edge in direction `step` (`i` indexes a node inside the host). A
 *  stored quote is trimmed, so a block whose first row is indented, or whose
 *  last row ends in a space, still counts as covered. */
function reachesHostEdge(idx: TextIndex, i: number, off: number, step: 1 | -1, host: Node): boolean {
  let j = i
  while (idx.nodes[j + step] && composedContains(host, idx.nodes[j + step].node)) j += step
  const edge = step === 1 ? idx.nodes[j].start + (idx.nodes[j].node.nodeValue?.length ?? 0) : idx.nodes[j].start
  const gap = step === 1 ? idx.text.slice(off, edge) : idx.text.slice(edge, off)
  return !gap.trim()
}

/** A live Range over `idx.text[start, end)`, or null when it is out of the
 *  index. A Range cannot cross a shadow boundary, so a span whose end is in a
 *  code block is only representable when it covers that block to its last
 *  character (the Range then ends after the host), and likewise for a start;
 *  any other cross-tree span is null (`liveSelectionRange` never makes one). */
export function rangeForTextSpan(idx: TextIndex, start: number, end: number): Range | null {
  const s = locateOffset(idx, start, 'start')
  const e = locateOffset(idx, end, 'end')
  if (!s || !e) return null
  try {
    const r = document.createRange()
    if (s.node.getRootNode() === e.node.getRootNode()) {
      r.setStart(s.node, s.offset)
      r.setEnd(e.node, e.offset)
      return r
    }
    const endHost = liftToTree(e.node, s.node)
    const ei = idx.nodes.findIndex(n => n.node === e.node)
    if (endHost && ei >= 0 && reachesHostEdge(idx, ei, end, 1, endHost)) {
      r.setStart(s.node, s.offset)
      r.setEndAfter(endHost)
      return r
    }
    const startHost = liftToTree(s.node, e.node)
    const si = idx.nodes.findIndex(n => n.node === s.node)
    if (startHost && si >= 0 && reachesHostEdge(idx, si, start, -1, startHost)) {
      r.setStartBefore(startHost)
      r.setEnd(e.node, e.offset)
      return r
    }
    return null
  } catch {
    return null
  }
}

/**
 * A Range from (`sn`, `so`) to (`en`, `eo`) with both ends in one tree. A
 * Range cannot cross a shadow boundary, so when the ends are in different
 * trees the part inside a code block is dropped: an end inside a block stops
 * before that CodeBlock, a start inside a block begins after it. A selection
 * crossing a block's edge anchors only its part outside the block, as it did
 * before code was anchorable; when only whitespace lies outside, it anchors
 * its part inside the block instead (`insidePart`).
 */
function oneTreeRange(sn: Node, so: number, en: Node, eo: number): Range | null {
  try {
    const r = document.createRange()
    if (sn.getRootNode() === en.getRootNode()) {
      r.setStart(sn, so)
      r.setEnd(en, eo)
      return r
    }
    const endHost = liftToTree(en, sn)
    if (endHost) {
      r.setStart(sn, so)
      r.setEndBefore(codeBlockOf(endHost))
      return r.toString().trim() ? r : insidePart(sn, so, en, eo)
    }
    // The start is in a shadow tree: lift it to the lowest tree holding the
    // end and start after that block.
    for (let x: Node = sn; ;) {
      const root = x.getRootNode()
      if (!(root instanceof ShadowRoot)) return null
      x = root.host
      const end = liftToTree(en, x)
      if (end) {
        r.setStartAfter(codeBlockOf(x))
        if (end === en) r.setEnd(en, eo)
        else r.setEndBefore(codeBlockOf(end))
        return r.collapsed || !r.toString().trim() ? insidePart(sn, so, en, eo) : r
      }
    }
  } catch {
    return null
  }
}

/** The whole CodeBlock around a code host (its header label and footer are
 *  not part of the passage either), or the host itself outside a CodeBlock. */
function codeBlockOf(host: Node): Node {
  return (host instanceof Element && host.closest('.code-block')) || host
}

/** Code-row text nodes of one shadow tree, in order. */
function rowTexts(root: ShadowRoot): Text[] {
  const out: Text[] = []
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const t = n as Text
    if (t.parentElement?.closest(`[${CODE_ROW_ATTR}]`) && !isEmptyRowFiller(t)) out.push(t)
  }
  return out
}

/** The part of a cross-edge selection inside the block it starts (or, failing
 *  that, ends) in: used when nothing but whitespace lies outside the block, so
 *  a drag from mid-code past the block's edge anchors what was selected. */
function insidePart(sn: Node, so: number, en: Node, eo: number): Range | null {
  const usable = (r: Range) => (r.collapsed || !r.toString().trim() ? null : r)
  const sRoot = sn.getRootNode()
  if (sRoot instanceof ShadowRoot) {
    const last = rowTexts(sRoot).at(-1)
    if (last) {
      const r = document.createRange()
      r.setStart(sn, so)
      r.setEnd(last, last.length)
      if (usable(r)) return r
    }
  }
  const eRoot = en.getRootNode()
  const first = eRoot instanceof ShadowRoot && eRoot !== sRoot ? rowTexts(eRoot)[0] : undefined
  if (!first) return null
  const r = document.createRange()
  r.setStart(first, 0)
  r.setEnd(en, eo)
  return usable(r)
}

/** Anchorable shadow roots of `node` and its descendants. */
function openShadowRootsWithin(node: Node | null): ShadowRoot[] {
  const el = node instanceof Element ? node : node?.parentElement
  if (!el) return []
  const out: ShadowRoot[] = []
  for (const d of [el, ...Array.from(el.querySelectorAll(`[${ANCHORABLE_CODE_ATTR}] *`))]) {
    const sr = anchorableShadow(d)
    if (sr) out.push(sr)
  }
  return out
}

/** True for a node inside a shadow tree that is not anchorable code. */
function inOtherShadow(node: Node): boolean {
  const root = node.getRootNode()
  return root instanceof ShadowRoot && !anchorableShadow(root.host)
}

/** Anchorable roots right next to an element boundary of `r`: where Chromium
 *  retargets an endpoint that is really inside a code block. */
function rootsAtBoundaries(r: Range): ShadowRoot[] {
  const out: ShadowRoot[] = []
  for (const [c, o] of [[r.startContainer, r.startOffset], [r.endContainer, r.endOffset]] as const) {
    if (!(c instanceof Element)) continue
    // Chromium may also put the boundary ON the host itself, `(host, 0)`.
    const own = anchorableShadow(c)
    if (own) out.push(own)
    for (const kid of [c.childNodes[o - 1], c.childNodes[o]]) {
      if (kid instanceof Element) out.push(...openShadowRootsWithin(kid))
    }
  }
  return out
}

type ComposedRangesFn = (...args: unknown[]) => StaticRange[]

/**
 * The selection's first range as a live Range, seen through open shadow roots.
 *
 * An ordinary selection returns `getRangeAt(0)` untouched. Chromium retargets
 * an endpoint inside a code block's shadow tree to the host's parent: a drag
 * within the block arrives collapsed (with the code still in `toString()`), and
 * one that crosses the block's edge arrives covering the whole host. Then
 * `getComposedRanges` with the code roots next to the endpoints gives the real
 * ends, kept to one tree by `oneTreeRange`. Null when nothing is selected, the
 * selection is inside a shadow tree that is not anchorable code, or the engine
 * cannot see past the boundary of a selection made inside a block.
 */
export function liveSelectionRange(sel: Selection | null): Range | null {
  if (!sel || sel.rangeCount === 0) return null
  const r = sel.getRangeAt(0)
  if (inOtherShadow(r.startContainer) || inOtherShadow(r.endContainer)) return null
  if (r.collapsed && !sel.toString().trim()) return null
  const composed = (sel as unknown as { getComposedRanges?: ComposedRangesFn }).getComposedRanges
  const roots = typeof composed !== 'function' ? [] : [...new Set(r.collapsed
    ? [...openShadowRootsWithin(sel.anchorNode), ...openShadowRootsWithin(sel.focusNode)]
    : rootsAtBoundaries(r))]
  if (roots.length === 0) return r.collapsed ? null : r
  let ranges: StaticRange[]
  try {
    ranges = composed!.call(sel, { shadowRoots: roots })
  } catch {
    // The first shipped signature took the roots as rest arguments.
    try { ranges = composed!.call(sel, ...roots) } catch { return r.collapsed ? null : r }
  }
  const sr = ranges[0]
  if (!sr || sr.collapsed) return r.collapsed ? null : r
  // No anchorable part at all: fall back to the browser's own range, so Copy
  // and Quote still act on the selection as they did before.
  return oneTreeRange(sr.startContainer, sr.startOffset, sr.endContainer, sr.endOffset) ?? (r.collapsed ? null : r)
}

/** One Range per code-row text node of each anchorable code block `range`
 *  covers. A light-DOM Range over a whole block contains its host but not the
 *  rows inside the host's shadow tree, which a CSS highlight would skip. */
export function codeRangesWithin(range: Range): Range[] {
  const root = range.commonAncestorContainer
  const el = root instanceof Element ? root : root.parentElement
  if (!el) return []
  const out: Range[] = []
  for (const host of Array.from(el.querySelectorAll(`[${ANCHORABLE_CODE_ATTR}] *`))) {
    if (!anchorableShadow(host) || !range.intersectsNode(host)) continue
    for (const { node } of indexComposedText(host).nodes) {
      if (node.getRootNode() === document) continue
      const r = document.createRange()
      r.selectNodeContents(node)
      out.push(r)
    }
  }
  return out
}

/** Point `observer` at every open shadow root under `root` it is not watching
 *  yet (`seen` records them). A MutationObserver on the light DOM never hears
 *  a shadow tree change, and Pierre fills its rows after the host mounts. */
export function observeOpenShadowRoots(
  observer: MutationObserver,
  root: Node,
  seen: WeakSet<ShadowRoot>,
  options: MutationObserverInit,
): void {
  for (const sr of openShadowRootsWithin(root)) {
    if (seen.has(sr)) continue
    seen.add(sr)
    observer.observe(sr, options)
  }
}
