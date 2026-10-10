/**
 * Markdown -> clean HTML for "Copy as rich text".
 *
 * Pasting into a rich-text editor (Outlook, Word, Google Docs) wants HTML, but
 * the HTML the chat pane renders carries the dashboard's own chrome: utility
 * classes, inline styles, path chips, code-block toolbars and bordered
 * containers. Copying that selection drags every bit of it into the email.
 *
 * So this converts the reply's Markdown SOURCE afresh, with no dashboard
 * components in the way, and keeps only semantic tags: headings, paragraphs,
 * lists, tables, links, emphasis, quotes and code. Every class, style, data
 * attribute and event handler is dropped, because the receiving editor
 * supplies its own typography and must not inherit ours.
 *
 * Safety: raw HTML in the Markdown is never passed through as markup. It is
 * kept as literal text, the way the chat pane shows a tag it does not render,
 * so `List<String>` survives the paste. A link keeps its URL only when it is
 * absolute and on an allowed scheme; a relative link points into this
 * dashboard and means nothing in an email, so it is unwrapped to its text.
 * No `<img>` is ever emitted: the chat pane loads a remote image only after
 * the user approves it, and an editor (and every recipient of the email) would
 * fetch it on paste with no such approval. An image becomes a link to it, or
 * its alt text when its URL is not one worth pasting.
 */
import { unified } from 'unified'
import remarkParse from 'remark-parse'
import { toHast } from 'mdast-util-to-hast'
import { toHtml } from 'hast-util-to-html'
import type { Element, ElementContent, Properties, Root as HastRoot, RootContent } from 'hast'
import type { Nodes as MdastNodes, Parent as MdastParent, Root as MdastRoot } from 'mdast'
import { capWhitespaceRuns } from '../../utils/markdownDepthBound'
import { REMARK_BASE_PLUGINS } from './remarkBase'

/** Tags kept as themselves. Anything else is unwrapped to its children. */
const KEEP_TAGS = new Set([
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'br', 'hr',
  'ul', 'ol', 'li', 'blockquote', 'pre', 'code',
  'strong', 'em', 'del', 'a',
  'table', 'thead', 'tbody', 'tr', 'th', 'td',
  'sup', 'section', 'input',
])

/** Attributes kept per tag; every other property is dropped. */
const KEEP_PROPS: Record<string, readonly string[]> = {
  a: ['href', 'id'],
  ol: ['start'],
  th: ['align'],
  td: ['align'],
  li: ['id'],
  sup: [],
  input: ['type', 'checked', 'disabled'],
}

const SAFE_SCHEMES = /^(https?|mailto):/i

/** Absolute URL on an allowed scheme, or a same-document fragment (GFM
 *  footnote back-references). Anything else is not a link worth pasting. */
function keepUrl(url: unknown, allowFragment: boolean): url is string {
  if (typeof url !== 'string') return false
  const trimmed = url.trim()
  if (allowFragment && trimmed.startsWith('#')) return true
  return SAFE_SCHEMES.test(trimmed)
}

function cleanProps(tag: string, props: Properties | undefined): Properties {
  const out: Properties = {}
  const allowed = KEEP_PROPS[tag]
  if (!allowed || !props) return out
  for (const key of allowed) {
    if (props[key] !== undefined && props[key] !== null) out[key] = props[key]
  }
  return out
}

function cleanChildren(children: readonly (RootContent | ElementContent)[], inLink = false): ElementContent[] {
  const out: ElementContent[] = []
  // Iterative over siblings, recursive over depth: the tree's depth is already
  // bounded by `remarkBoundDepth`, so recursion here cannot overflow.
  for (const child of children) {
    if (child.type === 'text') { out.push(child); continue }
    if (child.type !== 'element') continue  // comments, doctype, raw
    out.push(...cleanElement(child, inLink))
  }
  return out
}

/** Visually hidden in the source tree (the GFM footnote section's "Footnotes"
 *  heading): a paste has no such class, so it would show up as English prose. */
function isScreenReaderOnly(el: Element): boolean {
  const cls = el.properties?.className
  return Array.isArray(cls) && cls.includes('sr-only')
}

function cleanElement(el: Element, inLink: boolean): ElementContent[] {
  const tag = el.tagName
  if (isScreenReaderOnly(el)) return []
  const kids = cleanChildren(el.children, inLink || tag === 'a')
  if (tag === 'img') {
    const src = el.properties?.src
    const alt = typeof el.properties?.alt === 'string' ? el.properties.alt : ''
    // Inside a link the image is that link's label: a second link would nest.
    if (inLink || !keepUrl(src, false)) return alt ? [{ type: 'text', value: alt }] : []
    return [{ type: 'element', tagName: 'a', properties: { href: src }, children: [{ type: 'text', value: alt || src }] }]
  }
  if (!KEEP_TAGS.has(tag)) return kids
  if (tag === 'a') {
    if (!keepUrl(el.properties?.href, true)) return kids
  }
  if (tag === 'input' && el.properties?.type !== 'checkbox') return []
  return [{ type: 'element', tagName: tag, properties: cleanProps(tag, el.properties), children: kids }]
}

const BR = /^<br\s*\/?>$/i

/** Raw HTML becomes literal text (a `<br>` a line break, a comment nothing),
 *  so its source reaches the paste the way the chat pane shows it. */
function rawHtmlToText(tree: MdastRoot): void {
  const stack: MdastParent[] = [tree]
  while (stack.length) {
    const parent = stack.pop()!
    const kids = parent.children as MdastNodes[]
    for (let i = kids.length - 1; i >= 0; i--) {
      const node = kids[i]
      if (node.type === 'html') {
        const value = node.value.trim()
        if (value.startsWith('<!--')) kids.splice(i, 1)
        else if (BR.test(value)) kids[i] = { type: 'break' }
        else kids[i] = { type: 'text', value: node.value }
      } else if ('children' in node) {
        stack.push(node as MdastParent)
      }
    }
  }
}

const processor = unified().use(remarkParse).use(REMARK_BASE_PLUGINS)

/** Convert Markdown to an HTML fragment of semantic tags only. */
export function markdownToCleanHtml(markdown: string): string {
  // The same whitespace cap the chat renderer applies before parsing, so a
  // pathologically indented reply costs no more to copy than to display.
  const mdast = processor.runSync(processor.parse(capWhitespaceRuns(markdown))) as MdastRoot
  rawHtmlToText(mdast)
  const hast = toHast(mdast) as HastRoot
  const root: HastRoot = { type: 'root', children: cleanChildren(hast.children) }
  return toHtml(root)
}
