/**
 * The text a reader sees in a rendered element, as one string.
 *
 * `innerText` is close but not usable as is: jsdom has none, it reads a code
 * block's line numbers and copy-button labels, it reads KaTeX twice (the MathML
 * copy and the drawn glyphs), and it cannot see into a shadow root, which is
 * where the code highlighter draws. So this walks the light DOM itself:
 *
 * - text that is hidden from the reader is left out: `hidden`, `aria-hidden`,
 *   `sr-only`, `display: none`, `visibility: hidden`, and a closed `<details>`
 *   past its summary;
 * - host controls and drawn media give nothing: buttons, inputs, SVG (rendered
 *   diagrams), canvas, iframes, images, audio and video;
 * - a KaTeX formula gives its TeX source once;
 * - a code block registered with {@link holdVisibleCodeText} gives exactly its
 *   code: no language label, no line numbers, no button text;
 * - a `<textarea>` gives its current value, which is what it shows;
 * - `<pre>` and `white-space: pre`/`pre-wrap`/`pre-line` text keeps its spacing.
 *
 * Text clipped by a scroll box or a max-height is still in the element, so it is
 * read; anything not rendered at all (an unmounted block) is not.
 *
 * The walk emits tokens, never in-band marker characters, so every character of
 * the content, whatever it is, comes out unchanged.
 *
 * {@link visibleHast} reads a selected part by the same rules, as markup, for a
 * rich-text copy of a selection.
 *
 * @module utils/visibleText
 */
import type { ElementContent, Properties } from 'hast'

const codeText = new WeakMap<Element, string>()

/** Register the code a code-block element shows; returns the release. */
export function holdVisibleCodeText(el: Element, code: string): () => void {
  codeText.set(el, code)
  return () => { if (codeText.get(el) === code) codeText.delete(el) }
}

/** Elements that never give text: host controls and drawn media. */
const SKIP = new Set([
  'button', 'input', 'select', 'option', 'svg', 'canvas', 'iframe', 'img', 'picture', 'video', 'audio',
  'script', 'style', 'template', 'noscript', 'object', 'embed',
])
/** Elements that end a line of their own. */
const BLOCKS = new Set([
  'p', 'div', 'section', 'article', 'aside', 'header', 'footer', 'nav', 'main', 'figure', 'figcaption',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'pre', 'ul', 'ol', 'li', 'dl', 'dt', 'dd',
  'table', 'thead', 'tbody', 'tfoot', 'tr', 'caption', 'hr', 'details', 'summary', 'textarea',
])

/** `flow` text has its layout whitespace collapsed; `kept` text is verbatim. */
type Token = { kind: 'flow'; text: string } | { kind: 'kept'; text: string } | { kind: 'break' } | { kind: 'cell' }
const BREAK: Token = { kind: 'break' }
/** A tab between table cells; none before a row's first cell. */
const CELL: Token = { kind: 'cell' }

function styleOf(el: Element): CSSStyleDeclaration | null {
  const view = el.ownerDocument.defaultView
  return view ? view.getComputedStyle(el) : null
}

/**
 * Marks host-only content: shown to the person (and to assistive technology)
 * but never part of the text a message action, app or copy of the message
 * reads. The owner's reveal of a removed credential carries it, so a revealed
 * value cannot ride a message action into an app.
 */
export const HOST_ONLY_ATTR = 'data-host-only'

/**
 * Whether any range of `sel` reaches host-only content anywhere in the page.
 * The whole document is searched, not just `container`: a multi-range
 * selection (Firefox Ctrl-drag) can start in one reply and add a range in
 * another, and every range feeds the selection's text.
 */
export function selectionTouchesHostOnly(sel: Selection, container: Element): boolean {
  const marked = container.ownerDocument.querySelectorAll(`[${HOST_ONLY_ATTR}]`)
  if (marked.length === 0) return false
  for (let i = 0; i < sel.rangeCount; i++) {
    const range = sel.getRangeAt(i)
    for (const el of Array.from(marked)) if (range.intersectsNode(el)) return true
  }
  return false
}

function hiddenFromReader(el: Element, style: CSSStyleDeclaration | null): boolean {
  if (el.hasAttribute('hidden') || el.getAttribute('aria-hidden') === 'true' || el.classList.contains('sr-only')) return true
  return !!style && (style.display === 'none' || style.visibility === 'hidden')
}

function keepsSpacing(tag: string, style: CSSStyleDeclaration | null): boolean {
  if (tag === 'pre') return true
  const ws = style?.whiteSpace ?? ''
  return ws === 'pre' || ws === 'pre-wrap' || ws === 'pre-line' || ws === 'break-spaces'
}

/** A KaTeX formula's TeX source: KaTeX marks its own drawn copy aria-hidden and
 *  keeps the MathML copy, which carries the source, for screen readers. */
function texOf(el: Element): string {
  return el.querySelector('annotation[encoding="application/x-tex"]')?.textContent ?? ''
}

/** The children a reader sees: a closed `<details>` shows only its summary. */
function shownChildren(el: Element): Node[] {
  if (el.localName !== 'details' || (el as HTMLDetailsElement).open) return Array.from(el.childNodes)
  const summary = Array.from(el.children).find((c) => c.localName === 'summary')
  return summary ? [summary] : []
}

function walk(node: Node, out: Token[], pre: boolean): void {
  if (node.nodeType === 3) {
    const value = node.nodeValue ?? ''
    if (value) out.push(pre ? { kind: 'kept', text: value } : { kind: 'flow', text: value.replace(/\s+/g, ' ') })
    return
  }
  if (node.nodeType !== 1) return
  const el = node as Element
  if (el.hasAttribute(HOST_ONLY_ATTR)) return
  const code = codeText.get(el)
  if (code !== undefined) {
    out.push(BREAK, { kind: 'kept', text: code }, BREAK)
    return
  }
  const style = styleOf(el)
  if (hiddenFromReader(el, style)) return
  // A visible formula: its TeX once, taken from the root.
  if (el.classList.contains('katex')) {
    const tex = texOf(el)
    if (tex) out.push({ kind: 'kept', text: tex })
    return
  }
  const tag = el.localName
  if (SKIP.has(tag)) return
  if (tag === 'br') {
    out.push(BREAK)
    return
  }
  if (tag === 'textarea') {
    out.push(BREAK, { kind: 'kept', text: (el as HTMLTextAreaElement).value }, BREAK)
    return
  }
  if (tag === 'td' || tag === 'th') out.push(CELL)
  const block = BLOCKS.has(tag) || el.classList.contains('katex-display')
  if (block) out.push(BREAK)
  const inner = pre || keepsSpacing(tag, style)
  for (const child of shownChildren(el)) walk(child, out, inner)
  if (block) out.push(BREAK)
}

/** Joins tokens: one line break per run of block edges, no layout spaces at a
 *  line's ends, verbatim text untouched. */
function join(tokens: readonly Token[]): string {
  type Part = { flow: boolean; text: string }
  const lines: Part[][] = [[]]
  for (const t of tokens) {
    if (t.kind === 'break') {
      if (lines[lines.length - 1].length) lines.push([])
      continue
    }
    const line = lines[lines.length - 1]
    if (t.kind === 'cell') {
      if (line.length) line.push({ flow: false, text: '\t' })
      continue
    }
    if (t.kind === 'kept') {
      line.push({ flow: false, text: t.text })
      continue
    }
    // Layout space at the start of a line, or doubled with a layout space before it, is dropped.
    const prev = line[line.length - 1]
    const text = !prev || (prev.flow && prev.text.endsWith(' ')) ? t.text.replace(/^ +/, '') : t.text
    if (text) line.push({ flow: true, text })
  }
  return lines
    .map((parts) => {
      const last = parts[parts.length - 1]
      if (last?.flow) parts[parts.length - 1] = { flow: true, text: last.text.replace(/ +$/, '') }
      return parts.map((p) => p.text).join('')
    })
    .filter((line) => line.length > 0)
    .join('\n')
}

/** The visible text of `root`, with block ends as line breaks. */
export function visibleText(root: Element): string {
  const tokens: Token[] = []
  for (const child of Array.from(root.childNodes)) walk(child, tokens, keepsSpacing(root.localName, styleOf(root)))
  return join(tokens)
}

const KEPT_EMPTY = new Set(['br', 'hr', 'td', 'th'])

function insideHeldCode(node: Node | null): boolean {
  for (; node; node = node.parentNode) if (node.nodeType === 1 && codeText.has(node as Element)) return true
  return false
}

/**
 * The part of `root` inside `range` as hast, by the rules `visibleText` reads
 * by: what the reader cannot see gives nothing, a held code block its code, a
 * formula its TeX, a closed `<details>` its summary. Elements keep their tag and
 * every attribute, under its attribute name; the caller decides which survive.
 *
 * Nothing at all when the range starts or ends inside a held code block: the
 * highlighter draws the code in a shadow root, so the selected part of it is
 * not in the light DOM to read.
 */
export function visibleHast(root: Element, range: Range): ElementContent[] {
  if (insideHeldCode(range.startContainer) || insideHeldCode(range.endContainer)) return []
  return Array.from(root.childNodes).flatMap((child) => hastOf(child, range))
}

function hastOf(node: Node, range: Range): ElementContent[] {
  if (!range.intersectsNode(node)) return []
  if (node.nodeType === 3) {
    const value = node.nodeValue ?? ''
    const text = value.slice(node === range.startContainer ? range.startOffset : 0, node === range.endContainer ? range.endOffset : value.length)
    return text ? [{ type: 'text', value: text }] : []
  }
  if (node.nodeType !== 1) return []
  const el = node as Element
  const tag = el.localName
  if (el.hasAttribute(HOST_ONLY_ATTR) || hiddenFromReader(el, styleOf(el)) || SKIP.has(tag)) return []
  const code = codeText.get(el)
  if (code !== undefined) {
    return [{ type: 'element', tagName: 'pre', properties: {}, children: [{ type: 'element', tagName: 'code', properties: {}, children: [{ type: 'text', value: code }] }] }]
  }
  // An unfurled link chip or card copies as its URL, as the plain-text copy does,
  // never as the fetched title and description it shows.
  const unfurled = tag === 'a' ? el.getAttribute('data-unfurl-url') : null
  if (unfurled) return [{ type: 'element', tagName: 'a', properties: { href: unfurled }, children: [{ type: 'text', value: unfurled }] }]
  if (el.classList.contains('katex')) {
    const tex = texOf(el)
    return tex ? [{ type: 'text', value: tex }] : []
  }
  const shown = shownChildren(el)
  const parts = shown.map((child) => hastOf(child, range))
  // A selection that starts mid-row and runs into later rows gets a blank cell
  // for each cell before it, so the selected cells stay under their headers.
  // One that stays inside the row pads nothing, so one cell still pastes inline.
  if (tag === 'tr' && !el.contains(range.endContainer) && parts.some((part) => part.length)) {
    for (let i = 0; !parts[i].length; i++) {
      const cell = shown[i]
      if (cell.nodeType === 1 && KEPT_EMPTY.has((cell as Element).localName)) parts[i] = [{ type: 'element', tagName: (cell as Element).localName, properties: attributesOf(cell as Element), children: [] }]
    }
  }
  const children = parts.flat()
  // A block the range only touches at its edge (a triple-click ends at the next
  // paragraph's start) holds nothing. A break or a rule is its own content, and
  // a cell that is empty in the reply keeps the cells after it in their columns.
  if (!children.length && !(KEPT_EMPTY.has(tag) && !el.textContent?.trim())) return []
  const properties = attributesOf(el)
  // A numbered list the selection enters past its first item keeps the numbers
  // the reader saw.
  if (tag === 'ol') {
    const first = parts.findIndex((part) => part.length)
    const skipped = shown.slice(0, first).filter((child) => child.nodeType === 1 && (child as Element).localName === 'li').length
    const start = parseInt(el.getAttribute('start') ?? '', 10)
    if (skipped) properties.start = String((Number.isNaN(start) ? 1 : start) + skipped)
  }
  return [{ type: 'element', tagName: tag, properties, children }]
}

function attributesOf(el: Element): Properties {
  const properties: Properties = {}
  for (const attr of Array.from(el.attributes)) properties[attr.name] = attr.value
  return properties
}
