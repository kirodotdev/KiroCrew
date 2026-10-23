/**
 * Text and selections inside Pierre's code blocks, read from outside them.
 *
 * Pierre draws a code block into an OPEN shadow root on a `diffs-container`
 * host. Two tools that work on the document tree stop at that host: a
 * `TreeWalker` never enters the root, and a selection made wholly inside it is
 * reported with both ends moved onto the host's parent — collapsed — while
 * `toString()` still returns the text. This module reads both from the root
 * itself.
 *
 * One source line is one `[data-line]` element holding a text node per token
 * (`mycli`, ` rm`, ` NAME`), so a phrase crosses node boundaries. The gutter's
 * line numbers and the root's `<style>` carry text too, outside `[data-line]`,
 * and are never searched.
 *
 * A block is not final when it first appears. `WarmSwap` parks the highlighted
 * copy in an `aria-hidden` box beside the plain copy, fills it, then removes the
 * plain copy and shows the box. A parked block is not searched (its text would
 * count twice), and `watchText` tells a find when the swap has left it holding
 * ranges on nodes that are gone.
 */

const CODE_HOST = 'diffs-container'
const CODE_LINE = '[data-line]'
const CODE_BLOCK = '.code-block'
const DIFF_SECTION = '[data-diff-section]'
// Producing components mark every unit in every rendering state: `.code-block` for a code fence and one
// diff section per file, because source row numbers restart in each file. A bare host is its own unit.
const CODE_UNIT = [CODE_BLOCK, DIFF_SECTION].join(',')
// Columns stay below CODE_LINE_SPAN and rows below CODE_BLOCK_SPAN / CODE_LINE_SPAN.
// Positions outside those ranges or without a safe packed integer are not placed; their drafts use
// the shared fallback slot, so two such passages with the same text share one slot.
const CODE_LINE_SPAN = 1e5
const CODE_BLOCK_SPAN = 1e12

function packedCodePosition(ordinal: number, line: number, column: number): number | null {
  if (column >= CODE_LINE_SPAN || line >= CODE_BLOCK_SPAN / CODE_LINE_SPAN) return null
  const packed = ordinal * CODE_BLOCK_SPAN + line * CODE_LINE_SPAN + column
  return Number.isSafeInteger(packed) ? packed : null
}

/**
 * Whether `host` sits in an `aria-hidden` box below `root`: a block parked until it
 * paints. Only boxes inside `root` count, so an app hidden behind a dialog is not parked.
 */
function isParked(host: Element, root: Element): boolean {
  for (let el: Element | null = host; el && el !== root; el = el.parentElement) if (el.getAttribute('aria-hidden') === 'true') return true
  return false
}

/** The open shadow roots of the shown code blocks under `root`, in document order. */
export function codeShadowRoots(root: Element): ShadowRoot[] {
  const roots: ShadowRoot[] = []
  for (const host of root.querySelectorAll(CODE_HOST)) if (host.shadowRoot && !isParked(host, root)) roots.push(host.shadowRoot)
  return roots
}

export function plainCodeFenceAtRangeStart(range: Range): HTMLPreElement | null {
  const start = range.startContainer
  const startElement = start.nodeType === Node.ELEMENT_NODE ? start as Element : start.parentElement
  return startElement?.closest<HTMLPreElement>(`${CODE_BLOCK} pre.pierre-plain`) ?? null
}

/**
 * Advances a selection start past its leading whitespace and returns the source
 * row and column where visible text begins. Shadow rows have no text separator;
 * a plain fallback keeps one newline character between rows.
 */
function firstVisibleCodePosition(
  rows: Array<{ line: number; text: string }>,
  startRow: number,
  startColumn: number,
  selectionText: string,
  rowSeparatorLength: number,
): { line: number; column: number } | null {
  let rowIndex = startRow
  let column = startColumn
  let whitespace = selectionText.length - selectionText.trimStart().length
  while (true) {
    const row = rows[rowIndex]
    if (!row || !Number.isInteger(row.line) || row.line <= 0) return null
    if (whitespace <= 0) return { line: row.line, column }
    const remainingInRow = row.text.length - column
    const crossesRow = rowIndex < rows.length - 1 && whitespace >= remainingInRow
    if (!crossesRow) {
      column += Math.min(whitespace, remainingInRow)
      return { line: row.line, column }
    }
    whitespace -= remainingInRow + rowSeparatorLength
    rowIndex += 1
    column = 0
  }
}

/**
 * The code units under `root`, in document order. Each producer-marked unit remains present whether its
 * body is plain, parked, or shown. A code fence contributes one `.code-block`; a diff fence contributes
 * one section per file; a bare host contributes itself. A unit's index is the ordinal in a code position.
 */
function codeUnits(root: Element): Element[] {
  return [...root.querySelectorAll(`${CODE_UNIT}, ${CODE_HOST}`)]
    .filter(unit => unit.matches(CODE_UNIT) || !unit.closest(CODE_UNIT))
}

/**
 * Position of the first visible character of `range`, encoded by its code-block ordinal,
 * absolute source line and column. Null when the range starts outside a shown source line
 * or plain code-fence fallback, its line number is invalid, or its block is not under `root`.
 */
export function codePassagePosition(root: Element, range: Range): number | null {
  const units = codeUnits(root)
  const plain = plainCodeFenceAtRangeStart(range)
  const plainWrapper = plain?.closest(CODE_BLOCK)
  if (plain && plainWrapper) {
    const ordinal = units.indexOf(plainWrapper)
    if (ordinal < 0) return null
    const source = plain.textContent ?? ''
    const lines = source.split('\n')
    if (source.endsWith('\n')) lines.pop()
    const before = document.createRange()
    before.selectNodeContents(plain)
    before.setEnd(range.startContainer, range.startOffset)
    const prefix = before.toString()
    const rowIndex = prefix.split('\n').length - 1
    const lastBreak = prefix.lastIndexOf('\n')
    const column = prefix.length - lastBreak - 1
    const position = firstVisibleCodePosition(
      lines.map((text, index) => ({ line: index + 1, text })),
      rowIndex,
      column,
      range.toString(),
      1,
    )
    return position == null ? null : packedCodePosition(ordinal, position.line, position.column)
  }

  for (const shadowRoot of codeShadowRoots(root)) {
    const rowElements = [...shadowRoot.querySelectorAll<HTMLElement>(CODE_LINE)]
    const rows = rowElements.map(row => ({ line: Number(row.getAttribute('data-line')), text: row.textContent ?? '' }))
    for (let rowIndex = 0; rowIndex < rowElements.length; rowIndex += 1) {
      const row = rowElements[rowIndex]
      if (!row.contains(range.startContainer)) continue
      const ordinal = units.indexOf(shadowRoot.host.closest(CODE_UNIT) ?? shadowRoot.host)
      if (ordinal < 0) return null
      const before = document.createRange()
      before.selectNodeContents(row)
      before.setEnd(range.startContainer, range.startOffset)
      const position = firstVisibleCodePosition(rows, rowIndex, before.toString().length, range.toString(), 0)
      return position == null ? null : packedCodePosition(ordinal, position.line, position.column)
    }
  }
  return null
}

/** The text nodes of `el`, whitespace-only ones included: inside a code line they are the spaces between tokens. */
function textNodesOf(el: Element): Text[] {
  const nodes: Text[] = []
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
  for (let n = walker.nextNode(); n; n = walker.nextNode()) nodes.push(n as Text)
  return nodes
}

/**
 * The runs of text a find searches, in document order: every non-blank text
 * node of the light DOM on its own, and each source line of a shown code block as
 * one run, so a phrase can span the line's tokens.
 */
export function searchRuns(root: Element): Text[][] {
  const runs: Text[][] = []
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => {
      if (n.nodeType === Node.TEXT_NODE) return n.nodeValue?.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT
      if ((n as Element).localName !== CODE_HOST) return NodeFilter.FILTER_SKIP
      return isParked(n as Element, root) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT
    },
  })
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (n.nodeType === Node.TEXT_NODE) runs.push([n as Text])
    else for (const line of (n as Element).shadowRoot?.querySelectorAll(CODE_LINE) ?? []) runs.push(textNodesOf(line))
  }
  return runs
}

/**
 * The text node and offset that character `offset` of the joined run falls on. A
 * start sits on the node it begins in, an end on the node it finishes in.
 */
function boundary(nodes: Text[], offset: number, isEnd: boolean): [Text, number] {
  let from = 0
  for (const node of nodes) {
    const to = from + node.data.length
    if (isEnd ? offset <= to : offset < to) return [node, offset - from]
    from = to
  }
  const last = nodes[nodes.length - 1]
  return [last, last.data.length]
}

/**
 * A Range for every match of `re` in each run. `re` is global and matches a non-empty
 * literal: an empty match would never advance. A match may span the run's text nodes.
 */
export function matchRanges(runs: Text[][], re: RegExp): Range[] {
  const ranges: Range[] = []
  for (const nodes of runs) {
    const text = nodes.map((n) => n.data).join('')
    for (let m = re.exec(text); m; m = re.exec(text)) {
      const range = document.createRange()
      range.setStart(...boundary(nodes, m.index, false))
      range.setEnd(...boundary(nodes, m.index + m[0].length, true))
      ranges.push(range)
    }
  }
  return ranges
}

/** A shown block's source lines: their text nodes, `data-line` numbers, and offsets in `text`, which joins them with `\n`. */
interface ShownBlock { rows: Text[][]; lines: number[]; starts: number[]; text: string }

/** The shown (not parked) code block of `unit`, or null while only its plain copy is up. */
function shownBlock(unit: Element, root: Element): ShownBlock | null {
  const hosts = unit.localName === CODE_HOST ? [unit] : [...unit.querySelectorAll(CODE_HOST)]
  const shadowRoot = hosts.find(host => host.shadowRoot && !isParked(host, root))?.shadowRoot
  if (!shadowRoot) return null
  const rowElements = [...shadowRoot.querySelectorAll(CODE_LINE)]
  const rows = rowElements.map(textNodesOf)
  const texts = rows.map(nodes => nodes.map(n => n.data).join(''))
  const starts: number[] = []
  let from = 0
  for (const text of texts) { starts.push(from); from += text.length + 1 }
  return { rows, lines: rowElements.map(row => Number(row.getAttribute('data-line'))), starts, text: texts.join('\n') }
}

/** A Range over characters `start`..`start + length` of `block.text`; a start sits on the row it begins, an end on the row it finishes. */
function blockRange(block: ShownBlock, start: number, length: number): Range | null {
  const point = (offset: number, isEnd: boolean): [Text, number] | null => {
    for (let i = 0; i < block.rows.length; i += 1) {
      const from = block.starts[i]
      const to = from + block.rows[i].reduce((n, node) => n + node.data.length, 0)
      const inside = isEnd ? offset > from && offset <= to : offset >= from && offset < to
      if (inside && block.rows[i].length) return boundary(block.rows[i], offset - from, isEnd)
    }
    return null
  }
  const s = point(start, false)
  const e = point(start + length, true)
  if (!s || !e) return null
  const range = document.createRange()
  range.setStart(...s)
  range.setEnd(...e)
  return range
}

/**
 * The range a comment's `quote` covers in a shown code block under `root`, or null. Prose is never searched.
 *
 * `position` (from `codePassagePosition` on the same root) names the block, line and column the quote was
 * selected at. While that block shows only its plain copy, nothing is placed: the swap to the highlighted
 * copy is the moment to look again. Without a position, or when the text there no longer starts with the
 * quote, the quote is placed only if it occurs once across every code block, plain copies included.
 */
export function codeQuoteRange(root: Element, quote: string, position?: number): Range | null {
  if (!quote) return null
  const units = codeUnits(root)
  if (position != null && Number.isSafeInteger(position) && position >= 0) {
    const unit = units[Math.floor(position / CODE_BLOCK_SPAN)]
    const block = unit ? shownBlock(unit, root) : null
    if (unit && !block) return null
    const row = block ? block.lines.indexOf(Math.floor((position % CODE_BLOCK_SPAN) / CODE_LINE_SPAN)) : -1
    const column = position % CODE_LINE_SPAN
    const rowLength = block && row >= 0 ? block.rows[row].reduce((n, node) => n + node.data.length, 0) : 0
    const start = block && row >= 0 && column < rowLength ? block.starts[row] + column : -1
    if (block && start >= 0 && block.text.startsWith(quote, start)) return blockRange(block, start, quote.length)
  }
  let found: { block: ShownBlock; start: number } | null = null
  let count = 0
  for (const unit of units) {
    const block = shownBlock(unit, root)
    const text = block?.text ?? unit.querySelector('pre')?.textContent ?? ''
    for (let i = text.indexOf(quote); i >= 0; i = text.indexOf(quote, i + 1)) {
      count += 1
      if (block) found = { block, start: i }
    }
  }
  return count === 1 && found ? blockRange(found.block, found.start, quote.length) : null
}

/**
 * The range a selection really covers when it lies wholly inside a code block
 * hosted under `container`, or null. For such a selection `getRangeAt` and
 * `isCollapsed` describe the host's parent, so only `getComposedRanges` — absent
 * in older engines, which then behave as before — names the text.
 */
export function shadowSelectionRange(sel: Selection, container: Element): Range | null {
  if (typeof sel.getComposedRanges !== 'function') return null
  const roots = codeShadowRoots(container)
  if (!roots.length) return null
  try {
    const [composed] = sel.getComposedRanges({ shadowRoots: roots })
    const root = composed?.startContainer.getRootNode()
    if (!(root instanceof ShadowRoot) || !roots.includes(root)) return null
    const range = document.createRange()
    range.setStart(composed.startContainer, composed.startOffset)
    range.setEnd(composed.endContainer, composed.endOffset) // into another root, this collapses the range
    return range.collapsed ? null : range
  } catch {
    return null // an engine that implements the older call shape rejects this one
  }
}

/**
 * Calls `onChange`, at most once a frame, after the text under `root` may have
 * changed, until the returned function stops it. A tree observer sees a block
 * swapped in but not the fill of a shadow root, so every shown root is watched
 * too, and the set is taken again after each change: a new block brings a new root.
 */
export function watchText(root: Element, onChange: () => void): () => void {
  const observers: MutationObserver[] = []
  let frame = 0
  function attach() {
    for (const target of [root, ...codeShadowRoots(root)]) {
      const observer = new MutationObserver(schedule)
      observer.observe(target, { childList: true, subtree: true })
      observers.push(observer)
    }
  }
  function detach() {
    for (const observer of observers.splice(0)) observer.disconnect()
  }
  function schedule() {
    if (frame) return
    frame = requestAnimationFrame(() => { frame = 0; detach(); attach(); onChange() })
  }
  attach()
  return () => { cancelAnimationFrame(frame); detach() }
}
