/**
 * Copying part of a rendered chat reply keeps its markdown.
 *
 * The browser's own copy of a selection writes the rendered text, so a link
 * pastes as its label and bold pastes as plain words. On copy this module
 * rebuilds the selected DOM into markdown for `text/plain` and a sanitized copy
 * of the same fragment for `text/html`, so a paste into a markdown editor keeps
 * the syntax and a paste into a rich editor keeps the formatting.
 *
 * Markdown is written for paragraphs, headings, rules and inline formatting
 * (bold, italic, strikethrough, inline code, links, line breaks). Everything
 * else is flattened to the text the reader sees: an unknown element yields its
 * children's text, a list or table yields its rows as paragraphs, a code block
 * yields its lines, and a wrapper whose markdown would not be valid (an
 * unsafe link target, a same-kind nesting, a hard break at its edge, CSS that
 * removes its emphasis) yields its inner text. Prose is copied as-is, with no
 * escaping. Hidden content is left out. Only when nothing at all serializes
 * does the caller get null and leave the browser's copy alone. Pure DOM
 * functions: no React, no globals beyond the nodes they are handed.
 */

import { markdownCodeSpan } from '../../utils/tableClipboard'
import { containedSelectionRange } from '../../utils/selectionContainment'

export interface SelectionCopy {
  /** Markdown source for `text/plain`. */
  markdown: string
  /** Sanitized HTML for `text/html`: allow-listed tags, no attributes but `href`. */
  html: string
}

/** Output of the walk: inline text, a block boundary, or a block written
 *  as-is (a heading, a rule, a code block). */
const BREAK = Symbol('break')
type Tok = string | typeof BREAK | { raw: string }

interface Ctx {
  /** Page URL link destinations resolve against. */
  base: string
  /** Emphasis delimiters already open around the current node. */
  open: ReadonlySet<string>
  /** Block-shaped children of a link stay on one label line. */
  inLink: boolean
}

/** Inline wrappers and the delimiter they write. */
const WRAPPER_DELIMITER = new Map<string, string>([
  ['strong', '**'], ['b', '**'], ['em', '*'], ['i', '*'], ['del', '~~'], ['s', '~~'], ['strike', '~~'],
])
/** Elements whose markdown is restored around a selection made inside them. */
const RESTORED_TAGS = new Set([...WRAPPER_DELIMITER.keys(), 'a', 'code', 'pre'])
/** Elements written as a paragraph boundary; their text is a paragraph. */
const BLOCK_TAGS = new Set([
  'p', 'div', 'section', 'article', 'aside', 'header', 'footer', 'main', 'nav', 'figure', 'figcaption',
  'ul', 'ol', 'li', 'dl', 'dt', 'dd', 'blockquote', 'details', 'summary',
  'table', 'caption', 'thead', 'tbody', 'tfoot', 'tr',
])
/** Table cells: joined with a space on one row. */
const CELL_TAGS = new Set(['td', 'th'])
/** Content with no text form. `math` is the math renderer's hidden duplicate
 *  of the visible HTML it also writes. */
const TEXTLESS_TAGS = new Set([
  'img', 'picture', 'svg', 'video', 'audio', 'canvas', 'iframe', 'object', 'embed', 'math',
])
const SKIP_TAGS = new Set(['button', 'script', 'style', 'template', 'input', 'select', 'textarea', 'noscript'])
/** Classes whose content the reader does not see in place, or cannot select:
 *  screen-reader status text and action rows. Native copy leaves
 *  `user-select: none` content out too. Each maps to the variant utilities
 *  that undo it (`md:not-sr-only`, `md:select-text`), so an element carrying
 *  one is judged by what it renders at that width, not skipped on the token.
 *  `hidden` is not listed: it is a `display: none` utility, and the computed
 *  style check (`pruneHidden`) sees whether a responsive variant such as
 *  `sm:inline` shows it. */
const SKIP_CLASSES: ReadonlyArray<readonly [string, RegExp]> = [
  ['sr-only', /(^|:)not-sr-only$/],
  ['select-none', /(^|:)select-(text|auto|all)$/],
]

const SAFE_MD_HREF = /^[A-Za-z0-9\-._~:/?#@!$&'*+,;=%]+$/
const ENTITY_REFERENCE = /&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]*);/

function tagOf(el: Element): string {
  return el.tagName.toLowerCase()
}

/** Whether `el` carries a skip class with no variant utility undoing it. */
function hasSkipClass(el: Element): boolean {
  const list = el.classList
  if (!list) return false
  const tokens = Array.from(list)
  return SKIP_CLASSES.some(([cls, undo]) => list.contains(cls) && !tokens.some((t) => undo.test(t)))
}

/** Not part of what the reader copied: chrome, hidden helpers, textless
 *  content, and anything hidden from assistive technology. */
function isSkipped(el: Element): boolean {
  const tag = tagOf(el)
  return SKIP_TAGS.has(tag) || TEXTLESS_TAGS.has(tag) || el.getAttribute('aria-hidden') === 'true' ||
    el.hasAttribute('hidden') || hasSkipClass(el)
}

/** Whether an inline style on a semantic wrapper takes away the emphasis its
 *  markdown would write, so the wrapper is flattened to its text. */
function emphasisRemoved(el: Element): boolean {
  const style = (el as HTMLElement).style
  if (!style) return false
  switch (WRAPPER_DELIMITER.get(tagOf(el))) {
    case '**': return (style.fontWeight === 'normal' ? 400 : Number.parseFloat(style.fontWeight)) < 600
    case '*': return style.fontStyle === 'normal'
    case '~~': return (style.textDecorationLine || style.textDecoration) === 'none'
    default: return false
  }
}

/** Text a reader sees inside an element, skipping hidden helpers; no escaping. */
function visibleText(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return node.nodeValue ?? ''
  if (node.nodeType === Node.ELEMENT_NODE) {
    const el = node as Element
    if (isSkipped(el)) return ''
    if (tagOf(el) === 'br') return '\n'
  }
  let out = ''
  node.childNodes.forEach((child) => { out += visibleText(child) })
  return out
}

/** The text of tokens that hold no block boundary, or null when they do. */
function inlineOnly(toks: Tok[]): string | null {
  return toks.every((t): t is string => typeof t === 'string') ? toks.join('') : null
}

/** Wrap inline content in a delimiter pair, moving edge whitespace outside:
 *  `<b> bold </b>` becomes ` **bold** `, since `** bold **` is not emphasis.
 *  Content that spans a block boundary, or a hard break (`\` + newline, from
 *  `<br>`) at either edge, cannot sit inside delimiters and is left as-is. */
function wrap(toks: Tok[], open: string, close = open): Tok[] {
  const inner = inlineOnly(toks)
  if (inner === null || /^\s*\\\n/.test(inner) || /\\\n\s*$/.test(inner)) return toks
  const [, lead, body, trail] = /^(\s*)([\s\S]*?)(\s*)$/.exec(inner) ?? ['', '', inner, '']
  if (!body) return lead || trail ? [' '] : []
  return [`${lead}${open}${body}${close}${trail}`]
}

/** An autolink renders its own absolute address as the label, and pastes as
 *  that bare address. A relative link whose label equals its href
 *  (`[README.md](README.md)`) is not one: the bare text would drop the link. */
function isAutolink(label: string, href: string): boolean {
  return (label === href && /^https?:\/\//i.test(href)) || `mailto:${label}` === href
}

/** A code span as a markdown parser finds it: a backtick run closed by the
 *  next run of the same length. Brackets inside one cannot end a link label. */
const CODE_SPAN = /(?<!`)(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)/g

/** Escape the square brackets of a link label, the one place the copy
 *  escapes anything: an unescaped bracket there can close the label early or
 *  open a nested link, so the paste would point somewhere else. Brackets in a
 *  code span are left alone, since a backslash there is literal. Backslashes
 *  just before a bracket are doubled, so they stay literal and the escape
 *  holds. All other copied text is written as-is. */
function escapeLabelBrackets(label: string): string {
  const escape = (s: string) => s.replace(/(\\*)([[\]])/g, (_m, bs: string, br: string) => `${bs}${bs}\\${br}`)
  let out = ''
  let last = 0
  for (const m of label.matchAll(CODE_SPAN)) {
    out += escape(label.slice(last, m.index)) + m[0]
    last = m.index + m[0].length
  }
  return out + escape(label.slice(last))
}

/** Write `[label](destination)`, or the label alone when the destination
 *  cannot be written safely. Both flavours carry one destination: the href
 *  resolved against the page's origin and path, as the HTML flavour writes
 *  it, so a relative link does not resolve against whatever site it is
 *  pasted into. `safeHref` keeps only http(s) and mailto; the raw and the
 *  resolved forms are both checked, since the parser percent-encodes some
 *  characters and strips others (tabs, newlines). */
function link(toks: Tok[], href: string | null, base: string): Tok[] {
  const inner = inlineOnly(toks)
  if (!href || inner === null || !inner.trim()) return toks
  const destination = safeHref(href, base)
  const unsafe = (s: string) => !SAFE_MD_HREF.test(s) || ENTITY_REFERENCE.test(s)
  if (!destination || unsafe(href) || unsafe(destination) || isAutolink(inner.trim(), href)) return toks
  return wrap([escapeLabelBrackets(inner)], '[', `](${destination})`)
}

/** A heading is one line: a hard break inside it becomes a space. A heading
 *  holding a block is flattened to its blocks. */
function heading(toks: Tok[], level: number): Tok[] {
  const inner = inlineOnly(toks)
  if (inner === null) return [BREAK, ...toks, BREAK]
  const text = collapseInline(inner).replace(/\\?\n/g, ' ')
  return text ? [{ raw: `${'#'.repeat(level)} ${text}` }] : []
}

/** TeX source from a rendered equation's accessible branch. */
function equationSource(el: Element): string {
  return el.querySelector('annotation[encoding="application/x-tex"]')?.textContent ?? ''
}

/** Visible equation text when the renderer did not retain its TeX source. */
function equationFallback(el: Element): string {
  return el.querySelector('.katex-html')?.textContent ?? el.textContent ?? ''
}

/** A rendered equation is inline unless its renderer supplied a display wrapper. */
function equation(el: Element): Tok[] {
  const source = equationSource(el)
  if (source) return el.parentElement?.closest('.katex-display') ? [{ raw: `$$${source}$$` }] : [`$${source}$`]
  const fallback = equationFallback(el)
  return fallback ? [fallback] : []
}

/** Walk the children, merging adjacent inline text. Two adjacent nodes can
 *  each bring the space between them; keep one. Joined here rather than
 *  collapsed afterwards, so the spaces inside a code span are never touched. */
function children(node: Node, ctx: Ctx): Tok[] {
  const out: Tok[] = []
  node.childNodes.forEach((child) => {
    for (const tok of walk(child, ctx)) {
      const last = out[out.length - 1]
      if (typeof tok === 'string' && typeof last === 'string') {
        out[out.length - 1] = last + (last.endsWith(' ') && tok.startsWith(' ') ? tok.slice(1) : tok)
      } else {
        out.push(tok)
      }
    }
  })
  return out
}

function walk(node: Node, ctx: Ctx): Tok[] {
  // Layout whitespace (newlines between rendered blocks) collapses like HTML
  // does; runs of real spaces become one. Nothing is escaped.
  if (node.nodeType === Node.TEXT_NODE) return [(node.nodeValue ?? '').replace(/[ \t\n\r\f]+/g, ' ')]
  if (node.nodeType === Node.DOCUMENT_FRAGMENT_NODE) return children(node, ctx)
  if (node.nodeType !== Node.ELEMENT_NODE) return []

  const el = node as Element
  if (el.classList.contains('katex')) return equation(el)
  if (isSkipped(el)) return []
  const tag = tagOf(el)
  if (el.classList.contains('block')) {
    const inner = children(el, ctx)
    return ctx.inLink ? [' ', ...inner, ' '] : [BREAK, ...inner, BREAK]
  }
  const delimiter = WRAPPER_DELIMITER.get(tag)
  if (delimiter) {
    // A wrapper nested in its own kind (`<em>a<em>b</em></em>`) would fuse
    // delimiter runs that CommonMark pairs differently; the inner one is
    // flattened, as is one whose CSS removed the emphasis.
    if (ctx.open.has(delimiter) || emphasisRemoved(el)) return children(el, ctx)
    return wrap(children(el, { ...ctx, open: new Set(ctx.open).add(delimiter) }), delimiter)
  }
  switch (tag) {
    // A backslash before the newline is CommonMark's hard line break; a bare
    // newline would re-render as a space.
    case 'br': return ['\\\n']
    case 'hr': return [{ raw: '---' }]
    case 'code': {
      // Element children are read as their text; a rendered line break
      // inside inline code becomes a space, as a literal newline would.
      const text = visibleText(el).replace(/\s*\n\s*/g, ' ')
      return text ? [markdownCodeSpan(text)] : []
    }
    case 'pre': {
      // A code block keeps its lines; only the blank edges are dropped.
      const text = visibleText(el).replace(/^\n+|\s+$/g, '')
      return text ? [{ raw: text }] : []
    }
    case 'a': return link(children(el, { ...ctx, inLink: true }), el.getAttribute('href'), ctx.base)
    case 'h1': case 'h2': case 'h3': case 'h4': case 'h5': case 'h6':
      return heading(children(el, ctx), Number(tag[1]))
  }
  if (CELL_TAGS.has(tag)) return [' ', ...children(el, ctx), ' ']
  if (BLOCK_TAGS.has(tag)) return [BREAK, ...children(el, ctx), BREAK]
  // Anything else (`span`, `u`, `kbd`, `sub`, `mark`, ...) is its text.
  return children(el, ctx)
}

/** Drop spaces hugging a line break and trim the ends. Space runs are
 *  already one space: text nodes collapse their own and `children` joins
 *  neighbours without doubling. A hard break at either edge of a block has
 *  nothing to break; it is dropped with its backslash. */
function collapseInline(text: string): string {
  return text.replace(/ *(\\?\n) */g, '$1').replace(/^(?:\\?\n)+|(?:\\?\n)+$/g, '').trim()
}

/** Assemble the walk's tokens into blank-line separated blocks. */
function finish(toks: Tok[]): string {
  const blocks: string[] = []
  let inline = ''
  const flush = () => {
    const text = collapseInline(inline)
    if (text) blocks.push(text)
    inline = ''
  }
  for (const tok of toks) {
    if (typeof tok === 'string') {
      inline += tok
    } else {
      flush()
      if (tok !== BREAK) blocks.push(tok.raw)
    }
  }
  flush()
  return blocks.join('\n\n')
}

/** The page URL link destinations resolve against: the document's location,
 *  or a fixed origin for a document with no window. */
function pageBase(doc: Document | null): string {
  return doc?.defaultView?.location?.href ?? 'http://localhost/'
}

/** Convert a DOM node (usually a cloned selection fragment) to markdown.
 *  Link destinations are resolved against `base` (default: the node's page)
 *  the same way the HTML flavour resolves them, see `safeHref`. Returns null
 *  when nothing serializes. */
export function nodeToMarkdown(node: Node, base: string = pageBase(node.ownerDocument)): string | null {
  return finish(walk(node, { base, open: new Set(), inLink: false })) || null
}

const HTML_TAGS = new Set([
  'p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'b', 'em', 'i', 'del', 's', 'strike',
  'code', 'a', 'br', 'hr', 'section', 'article',
  'ul', 'ol', 'li', 'pre', 'blockquote', 'table', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th',
])

/** An href is carried into the HTML only when it resolves to http(s) or
 *  mailto; a relative in-app link is made absolute (without the page's query
 *  or hash) so it still works pasted into another application. */
function safeHref(href: string | null, base: string): string | null {
  if (!href) return null
  try {
    // Resolve against the page's origin and path only: the dashboard URL can
    // carry a `?token=` credential, and a relative or `#fragment` href
    // resolved against the full page URL would copy it into the clipboard.
    const page = new URL(base)
    const url = new URL(href, `${page.origin}${page.pathname}`)
    return url.protocol === 'http:' || url.protocol === 'https:' || url.protocol === 'mailto:' ? url.href : null
  } catch {
    return null
  }
}

function cleanInto(src: Node, dst: Node, doc: Document, base: string): void {
  src.childNodes.forEach((child) => {
    if (child.nodeType === Node.TEXT_NODE) {
      dst.appendChild(doc.createTextNode(child.nodeValue ?? ''))
      return
    }
    if (child.nodeType !== Node.ELEMENT_NODE) return
    const el = child as Element
    if (el.classList.contains('katex')) {
      const text = equationSource(el) || equationFallback(el)
      if (text) dst.appendChild(doc.createTextNode(text))
      return
    }
    if (isSkipped(el)) return
    const tag = tagOf(el)
    if (el.classList.contains('block')) {
      const out = doc.createElement('div')
      cleanInto(el, out, doc, base)
      dst.appendChild(out)
      return
    }
    // Anything else (a `span`, a chip wrapper) carries nothing once its
    // attributes are gone: keep its content, drop the element.
    if (!HTML_TAGS.has(tag)) {
      cleanInto(el, dst, doc, base)
      return
    }
    const out = doc.createElement(tag)
    if (tag === 'a') {
      const href = safeHref(el.getAttribute('href'), base)
      if (href) out.setAttribute('href', href)
    }
    cleanInto(el, out, doc, base)
    dst.appendChild(out)
  })
}

/** Serialize a node as sanitized HTML: allow-listed tags only, every attribute
 *  dropped except a safe `href`, skipped helper content removed. */
export function nodeToSafeHtml(node: Node, doc: Document, base: string): string {
  const container = doc.createElement('div')
  cleanInto(node, container, doc, base)
  return container.innerHTML
}

/** Whether the computed style hides the element's text from the reader. */
function isHiddenByStyle(style: CSSStyleDeclaration): boolean {
  const color = style.color.trim().toLowerCase()
  const zeroAlpha = '(?:0(?:\\.0*)?|\\.0+)(?:%)?'
  const transparentColor = color === 'transparent' ||
    new RegExp(`^rgba\\([^)]*(?:,|/)\\s*${zeroAlpha}\\s*\\)$`).test(color) ||
    new RegExp(`^color\\([^)]*/\\s*${zeroAlpha}\\s*\\)$`).test(color)
  return style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse' ||
    Number.parseFloat(style.opacity) === 0 || Number.parseFloat(style.fontSize) === 0 || transparentColor
}

/** Remove from the clone every element whose computed style hides it, so
 *  both flavours carry what the reader saw. Styles are read on the live DOM,
 *  since a clone has none. `Range.cloneContents` clones exactly the elements
 *  below the common ancestor that the range intersects, in document order,
 *  so the live and cloned element lists line up index for index. */
function pruneHidden(range: Range, clone: DocumentFragment): void {
  const top = range.commonAncestorContainer
  const doc = top.ownerDocument
  const view = doc?.defaultView
  if (!doc || !view) return
  const cloned = clone.querySelectorAll('*')
  const walker = doc.createTreeWalker(top, NodeFilter.SHOW_ELEMENT)
  let i = 0
  for (let n = walker.nextNode(); n && i < cloned.length; n = walker.nextNode()) {
    if (!range.intersectsNode(n)) continue
    const copy = cloned[i++]
    if (isHiddenByStyle(view.getComputedStyle(n as Element))) copy.remove()
  }
}

/** Whether the range reaches an element with a shadow root (a highlighted
 *  diff or code surface). Its content is not in `Range.cloneContents`, so a
 *  conversion would silently drop it; checked on the live DOM because a clone
 *  carries no shadow root. */
function touchesShadowRoot(range: Range): boolean {
  let top: Node | null = range.commonAncestorContainer
  if (top.nodeType !== Node.ELEMENT_NODE) top = top.parentNode
  if (!top) return false
  if ((top as Element).shadowRoot) return true
  const walker = top.ownerDocument!.createTreeWalker(top, NodeFilter.SHOW_ELEMENT)
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if ((n as Element).shadowRoot && range.intersectsNode(n)) return true
  }
  return false
}

/** Whether the range spans all of `el`'s visible text. */
function coversText(range: Range, el: Element): boolean {
  const part = range.cloneRange()
  if (!el.contains(range.startContainer)) part.setStart(el, 0)
  if (!el.contains(range.endContainer)) part.setEnd(el, el.childNodes.length)
  const text = (el.textContent ?? '').trim()
  return text !== '' && part.toString().trim() === text
}

/** The heading (below `root`) that contains `node`, if any. */
function headingAround(node: Node, root: Element): Element | null {
  for (let at: Node | null = node; at && at !== root; at = at.parentNode) {
    if (at.nodeType === Node.ELEMENT_NODE && /^h[1-6]$/.test(tagOf(at as Element))) return at as Element
  }
  return null
}

/** Replace a partially selected heading's clone with its content, so a drag
 *  that starts or ends mid-heading pastes that part as prose. */
function unwrap(el: Element): void {
  el.replaceWith(...Array.from(el.childNodes))
}

/** Whether a selection made inside `el` gets `el`'s markdown back around it.
 *  Inline wrappers, links and code do; a code span inside a code block does
 *  not, since the block is restored instead; a heading does only when the
 *  whole of it is selected, part of a heading's text pastes as prose. */
function restores(el: Element, range: Range): boolean {
  const tag = tagOf(el)
  if (tag === 'code') return !el.parentElement?.closest('pre')
  return RESTORED_TAGS.has(tag) || (/^h[1-6]$/.test(tag) && coversText(range, el))
}

/** The selection's contents plus the formatting it sits inside.
 *  `Range.cloneContents` drops ancestors above the common container, so a
 *  selection of two words inside a link would lose the link; they are cloned
 *  back around the fragment. Returns null when the selection sits inside a
 *  hidden helper or leaves `root`. */
function equationAround(node: Node, root: Element): Element | null {
  const el = node.nodeType === Node.ELEMENT_NODE ? node as Element : node.parentElement
  const equation = el?.closest('.katex') ?? null
  return equation && root.contains(equation) ? equation : null
}

/** Expand an endpoint inside rendered math to include the complete equation. */
function expandEquationEndpoints(range: Range, root: Element): Range {
  const expanded = range.cloneRange()
  const startEquation = equationAround(range.startContainer, root)
  const endEquation = equationAround(range.endContainer, root)
  if (startEquation) expanded.setStartBefore(startEquation)
  if (endEquation) expanded.setEndAfter(endEquation)
  return expanded
}

function selectedFragment(range: Range, root: Element): Node | null {
  range = expandEquationEndpoints(range, root)
  const doc = root.ownerDocument
  const clone = range.cloneContents()
  pruneHidden(range, clone)
  // A heading the range starts or ends inside arrives in the clone as a
  // partial heading. It is the first (or last) heading of the clone in
  // document order; keep it as a heading only when all of it is selected.
  const startHeading = headingAround(range.startContainer, root)
  const endHeading = headingAround(range.endContainer, root)
  // When both ends sit in one heading it is an ancestor, not in the clone,
  // and the ancestor walk below restores it only if fully covered.
  if (startHeading !== endHeading) {
    const cloned = clone.querySelectorAll('h1, h2, h3, h4, h5, h6')
    const first = cloned[0]
    const last = cloned[cloned.length - 1]
    if (startHeading && first && !coversText(range, startHeading)) unwrap(first)
    const lastIsEnd = last !== first || !startHeading
    if (endHeading && last && lastIsEnd && !coversText(range, endHeading)) unwrap(last)
  }
  let fragment: Node = clone
  let at: Node | null = range.commonAncestorContainer
  if (at.nodeType !== Node.ELEMENT_NODE) at = at.parentNode
  while (at && at !== root) {
    if (at.nodeType === Node.ELEMENT_NODE) {
      const el = at as Element
      if (isSkipped(el)) return null
      if (restores(el, range)) {
        const wrapper = el.cloneNode(false)
        wrapper.appendChild(fragment)
        const holder = doc.createDocumentFragment()
        holder.appendChild(wrapper)
        fragment = holder
      }
    }
    at = at.parentNode
  }
  return at === root ? fragment : null
}

/** Build both clipboard flavours for a selection inside `root`. Null means
 *  "leave the browser's copy alone": the selection is collapsed, leaves
 *  `root`, reaches a shadow root, or serializes to nothing. */
export function selectionToCopy(range: Range, root: Element): SelectionCopy | null {
  if (range.collapsed) return null
  // Judged by text, not ancestry: a triple-click of the reply's last block
  // ends just past the bubble, and must still convert.
  const contained = containedSelectionRange(range, root)
  if (!contained || touchesShadowRoot(contained)) return null
  const fragment = selectedFragment(contained, root)
  if (!fragment) return null
  const doc = root.ownerDocument
  const base = pageBase(doc)
  const markdown = nodeToMarkdown(fragment, base)
  if (!markdown) return null
  return { markdown, html: nodeToSafeHtml(fragment, doc, base) }
}

/** Minimal shape of a copy event, so React's synthetic event and a native
 *  `ClipboardEvent` both fit. */
export interface CopyEventLike {
  clipboardData: DataTransfer | null
  defaultPrevented: boolean
  preventDefault(): void
}

/** Copy handler for a rendered-markdown container. Writes markdown and
 *  sanitized HTML and cancels the browser's copy when the current selection
 *  converts; otherwise does nothing. Returns whether it took over. */
export function copySelectionAsMarkdown(event: CopyEventLike, root: Element): boolean {
  if (event.defaultPrevented || !event.clipboardData) return false
  const selection = root.ownerDocument.getSelection()
  if (!selection || selection.rangeCount !== 1) return false
  const copy = selectionToCopy(selection.getRangeAt(0), root)
  if (!copy) return false
  event.clipboardData.setData('text/plain', copy.markdown)
  event.clipboardData.setData('text/html', copy.html)
  event.preventDefault()
  return true
}
