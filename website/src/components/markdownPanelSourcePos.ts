/**
 * Where a selection in the rendered file sits in its SOURCE: `resolveSourcePos`
 * walks a markdown-rendered DOM range back to the `data-sourcepos` spans
 * `rehypeSourcepos` emits, and `findCoords` is the plain-text fallback. Pure
 * DOM-and-string functions, no React; `MarkdownPanel` anchors inline comments
 * with them and re-exports both for their tests.
 */

/**
 * Locate the first char of `selected` in the raw source `content` and return
 * 1-based (line, column). Works perfectly for code files where rendered text
 * equals source text. For markdown, used as a fallback when DOM-based
 * `resolveSourcePos` can't resolve coordinates (rare). Exported for tests.
 */
export function findCoords(content: string, selected: string): { line: number; column: number } | undefined {
  if (!selected) return undefined
  const idx = content.indexOf(selected)
  return idx < 0 ? undefined : coordsAt(content, idx)
}

/** 1-based (line, column) of character `idx` of `content`. */
function coordsAt(content: string, idx: number): { line: number; column: number } {
  const before = content.slice(0, idx)
  const nl = before.lastIndexOf('\n')
  const line = (before.match(/\n/g)?.length ?? 0) + 1
  const column = (nl < 0 ? idx : idx - nl - 1) + 1
  return { line, column }
}

/** `needle`'s position in `content` when it occurs once. */
function uniqueCoords(content: string, needle: string): { line: number; column: number } | undefined {
  const at = content.indexOf(needle)
  if (at < 0 || at !== content.lastIndexOf(needle)) return undefined
  return coordsAt(content, at)
}

/**
 * Where the first visible character of `selected` sits in `content`, or undefined when
 * its first row is absent or occurs more than once. Only the first row is looked up
 * because rendered line separators and source prefixes can differ.
 */
export function findUniqueCoords(content: string, selected: string): { line: number; column: number } | undefined {
  const text = selected.trimStart()
  const row = text.split('\n')[0].trimEnd()
  return uniqueCoords(content, row)
}

/**
 * Resolve a selection `Range` inside a markdown-rendered `root` to 1-based
 * (line, column) in the source `content`, using the `data-sourcepos`
 * attributes emitted by the `rehypeSourcepos` plugin.
 *
 * Strategy: walk up from the selection start to the nearest ancestor element
 * carrying `data-sourcepos`, compute the rendered-text offset from that
 * element's start to the selection, then locate the corresponding char in
 * the element's source span by substring search scoped to that tight window
 * (duplicate-text ambiguity drops to near-zero vs global search). Returns
 * undefined when no ancestor carries a position (should not happen when the
 * renderer is built with `sourcePos`) — caller falls back to `findCoords`.
 * Exported for tests.
 */
export function resolveSourcePos(range: Range, root: HTMLElement, content: string): { line: number; column: number } | undefined {
  let el: HTMLElement | null = range.startContainer.nodeType === Node.ELEMENT_NODE
    ? range.startContainer as HTMLElement
    : range.startContainer.parentElement
  while (el && el !== root && !el.hasAttribute('data-sourcepos')) el = el.parentElement
  if (!el || !el.hasAttribute('data-sourcepos')) return undefined
  const m = /^(\d+):(\d+)-(\d+):(\d+)$/.exec(el.getAttribute('data-sourcepos') || '')
  if (!m) return undefined
  // Block offset: useBlockAssembler splits raw content into separate
  // MarkdownBlocks, so data-sourcepos line numbers are relative to the
  // block's own text. The enclosing `[data-block-start]` wrapper carries
  // the 1-based line of that block within the full source.
  let blockEl: HTMLElement | null = el
  while (blockEl && blockEl !== root && !blockEl.hasAttribute('data-block-start')) blockEl = blockEl.parentElement
  const blockStart = blockEl?.hasAttribute('data-block-start') ? +(blockEl.getAttribute('data-block-start') || '1') : 1
  const lineOffset = blockStart - 1
  const sLine = +m[1] + lineOffset, sCol = +m[2], eLine = +m[3] + lineOffset, eCol = +m[4]
  // Rendered-text offset from element start to selection start
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
  let offset = 0
  let node: Node | null
  while ((node = walker.nextNode())) {
    if (node === range.startContainer) { offset += range.startOffset; break }
    offset += (node as Text).data.length
  }
  // Extract the element's source span from `content`
  const lines = content.split('\n')
  if (sLine < 1 || eLine > lines.length) return { line: sLine, column: sCol }
  let span: string
  if (sLine === eLine) span = lines[sLine - 1].slice(sCol - 1, eCol - 1)
  else {
    const parts = [lines[sLine - 1].slice(sCol - 1)]
    for (let i = sLine; i < eLine - 1; i++) parts.push(lines[i])
    parts.push(lines[eLine - 1].slice(0, eCol - 1))
    span = parts.join('\n')
  }
  // Align rendered text to source span char-by-char: walk `span`, advancing
  // a rendered cursor whenever they match. When the rendered cursor equals
  // `offset`, the current span index is the source position of the selection.
  // Handles `**bold**`, `*em*`, `` `code` ``, `# Heading`, list `- ` / `> `,
  // and similar leading / wrapping / trailing syntax without enumerating
  // syntax characters (anything in span that isn't in rendered is syntax).
  const rendered = el.textContent || ''
  if (offset >= rendered.length) return { line: sLine, column: sCol }
  let spanIdx = 0
  let renderedIdx = 0
  while (spanIdx < span.length && renderedIdx < offset) {
    if (span[spanIdx] === rendered[renderedIdx]) renderedIdx++
    spanIdx++
  }
  // spanIdx now points at the position in span for rendered[renderedIdx] —
  // but may sit on leading syntax between the previous match and the target
  // rendered char. Advance past any such syntax to land on the target.
  while (spanIdx < span.length && span[spanIdx] !== rendered[offset]) spanIdx++
  // Exhausted the span without finding rendered[offset] — happens when
  // rendered text can't be aligned to source char-by-char (HTML entities
  // like `&amp;` → `&`, raw HTML like `<br>` → newline). Returning
  // element-start would silently mislead the agent; return undefined so
  // the caller falls back to `findCoords`.
  if (spanIdx >= span.length) return undefined
  // Convert spanIdx back to (line, column) in source
  let ln = sLine, cl = sCol
  for (let i = 0; i < spanIdx; i++) {
    if (span[i] === '\n') { ln++; cl = 1 } else cl++
  }
  return { line: ln, column: cl }
}
