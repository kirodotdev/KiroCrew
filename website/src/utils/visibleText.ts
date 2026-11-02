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
 * @module utils/visibleText
 */

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

function hiddenFromReader(el: Element, style: CSSStyleDeclaration | null): boolean {
  if (el.hasAttribute('hidden') || el.getAttribute('aria-hidden') === 'true' || el.classList.contains('sr-only')) return true
  return !!style && (style.display === 'none' || style.visibility === 'hidden')
}

function keepsSpacing(tag: string, style: CSSStyleDeclaration | null): boolean {
  if (tag === 'pre') return true
  const ws = style?.whiteSpace ?? ''
  return ws === 'pre' || ws === 'pre-wrap' || ws === 'pre-line' || ws === 'break-spaces'
}

function walk(node: Node, out: Token[], pre: boolean): void {
  if (node.nodeType === 3) {
    const value = node.nodeValue ?? ''
    if (value) out.push(pre ? { kind: 'kept', text: value } : { kind: 'flow', text: value.replace(/\s+/g, ' ') })
    return
  }
  if (node.nodeType !== 1) return
  const el = node as Element
  const code = codeText.get(el)
  if (code !== undefined) {
    out.push(BREAK, { kind: 'kept', text: code }, BREAK)
    return
  }
  const style = styleOf(el)
  if (hiddenFromReader(el, style)) return
  // A visible formula: its TeX once. Taken from the root, because KaTeX marks
  // its own drawn copy aria-hidden and keeps the MathML copy for screen readers.
  if (el.classList.contains('katex')) {
    const tex = el.querySelector('annotation[encoding="application/x-tex"]')?.textContent ?? ''
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
  if (tag === 'details' && !(el as HTMLDetailsElement).open) {
    const summary = Array.from(el.children).find((c) => c.localName === 'summary')
    if (summary) walk(summary, out, pre)
  } else {
    const inner = pre || keepsSpacing(tag, style)
    for (const child of Array.from(el.childNodes)) walk(child, out, inner)
  }
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
