import { pairedCloseIndices, singleTagName } from '../../utils/htmlTagGrammar'

/**
 * Rehype plugin: ALLOWLIST-based HTML sanitization of the HAST tree.
 * Unknown/unrecognized tags are converted to escaped text (renders literally)
 * rather than passed to React as elements -- prevents React error #290 crashes
 * from bare XML tags like `<dynamoDBClient>` in agent output.
 */
export const ALLOWED_TAGS = new Set([
  // Block structure
  'div', 'span', 'p', 'br', 'hr',
  // Headings
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  // Lists
  'ul', 'ol', 'li',
  // Inline formatting
  'strong', 'b', 'em', 'i', 'del', 's', 'u', 'mark', 'small',
  'sup', 'sub', 'kbd', 'abbr', 'cite', 'q', 'var', 'samp',
  // Code
  'code', 'pre',
  // Links & media
  'a', 'img', 'picture', 'source', 'video', 'audio',
  // Tables
  'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'caption', 'colgroup', 'col',
  // Semantic blocks
  'blockquote', 'details', 'summary', 'figure', 'figcaption',
  // Semantic HTML5 structure (remark-gfm emits <section> for footnotes)
  'section', 'article', 'header', 'footer', 'nav', 'aside', 'time',
  // Forms (only checkbox for GFM task lists -- further constrained below)
  'input',
  // Misc safe elements
  'dl', 'dt', 'dd', 'ruby', 'rt', 'rp', 'wbr',
  // SVG (inline diagrams)
  'svg', 'path', 'circle', 'rect', 'line', 'polyline', 'polygon', 'text', 'g', 'defs', 'use',
  'tspan', 'ellipse', 'lineargradient', 'radialgradient', 'stop', 'title', 'desc', 'clippath', 'marker',
  // Math (rehypeKatex pipeline -- pass through rehypeRaw)
  'math', 'inlinemath',
])
const DANGEROUS_PROTOCOLS = ['javascript:', 'data:', 'vbscript:']
const cleanUrl = (url: string) => url.replace(/[\x00-\x1f\x7f]/g, '').trim().toLowerCase()

/**
 * Attribute ALLOWLIST (replaces the former on-handler / protocol denylist).
 *
 * frontend-security: for an allowlisted element we now KEEP only the attributes
 * explicitly permitted for it and DROP everything else — so `style`,
 * `formaction`, `srcset`-on-the-wrong-tag, unknown `on*` handlers, etc. are all
 * removed by default rather than only the handful we remembered to block.
 *
 * Matching is case-insensitive because hast camelCases some property names
 * (`viewBox`, `colSpan`, `ariaHidden`, `data-*` → `dataSourcepos`); we always
 * compare on the lowercased key. `aria*`/`data*` prefixes are allowed wholesale
 * (inert, a11y/metadata only) — except the transcript's own `data-message-*`
 * hooks, which are reserved (see isAllowedAttr).
 */
const GLOBAL_ATTRS = new Set([
  'classname', 'class', 'id', 'title', 'dir', 'lang', 'role', 'align',
])
export const TAG_ATTRS: Record<string, Set<string>> = {
  a: new Set(['href', 'name', 'target', 'rel']),
  img: new Set(['src', 'alt', 'width', 'height', 'loading']),
  input: new Set(['type', 'checked', 'disabled']),
  ol: new Set(['start', 'type', 'reversed']),
  li: new Set(['value']),
  td: new Set(['colspan', 'rowspan', 'headers']),
  th: new Set(['colspan', 'rowspan', 'scope', 'headers']),
  col: new Set(['span', 'width']),
  colgroup: new Set(['span', 'width']),
  source: new Set(['src', 'srcset', 'type', 'media', 'sizes']),
  video: new Set(['src', 'controls', 'width', 'height', 'poster', 'loop', 'muted', 'preload']),
  audio: new Set(['src', 'controls', 'loop', 'muted', 'preload']),
  details: new Set(['open']),
  time: new Set(['datetime']),
}
// SVG-family elements share a pool of inert presentation/geometry attributes.
const SVG_TAGS = new Set([
  'svg', 'path', 'circle', 'rect', 'line', 'polyline', 'polygon', 'text', 'g',
  'defs', 'use', 'tspan', 'ellipse', 'lineargradient', 'radialgradient', 'stop',
  'clippath', 'marker',
])
const SVG_ATTRS = new Set([
  'viewbox', 'xmlns', 'fill', 'stroke', 'strokewidth', 'strokelinecap',
  'strokelinejoin', 'strokedasharray', 'strokeopacity', 'fillopacity',
  'fillrule', 'cliprule', 'clippath', 'opacity', 'transform', 'd', 'points',
  'x', 'y', 'x1', 'y1', 'x2', 'y2', 'cx', 'cy', 'r', 'rx', 'ry', 'width',
  'height', 'offset', 'stopcolor', 'stopopacity', 'gradientunits',
  'gradienttransform', 'preserveaspectratio', 'markerwidth', 'markerheight',
  'refx', 'refy', 'orient',
])
/** True when `key` is a permitted attribute for element `tag` (both lowercased). */
function isAllowedAttr(tag: string, key: string): boolean {
  const k = key.toLowerCase()
  // `data-message-*` is reserved for the transcript's own UI hooks — the
  // `data-message-actions` strip, `data-message-edit` pencil and
  // `data-message-editing` root that UserMessage renders AROUND a message, never
  // inside one. usePinnedPrompt measures the strip's rect and reads the editing
  // marker off the row it is about to hide, and index.css re-shows
  // `[data-message-actions]` inside that hidden row; a body that could mint one
  // of them (a prompt typed, or relayed from a connected channel) would be
  // measured as the strip, drop the banner for its whole scroll region, or paint
  // itself visible inside the hidden row. Same reservation rehypeMarkFencedCode
  // makes for `data-fenced`, and for the same reason by NORMALIZED key: the HTML
  // parser lowercases attribute names and hast camelCases `data-*`, so
  // `data-message-edit`, `data-Message-Edit` and `dataMessageEdit` all reach
  // here as some casing of `datamessageedit`. Every other `data-*` stays admitted.
  if (k.replace(/-/g, '').startsWith('datamessage')) return false
  if (k.startsWith('aria') || k.startsWith('data')) return true
  if (GLOBAL_ATTRS.has(k)) return true
  if (TAG_ATTRS[tag]?.has(k)) return true
  if (SVG_TAGS.has(tag) && SVG_ATTRS.has(k)) return true
  return false
}

/** Elements that cannot have children per HTML spec (used by escapedNodeTree). */
const VOID_ELEMENTS = new Set(['img', 'br', 'hr', 'input', 'source', 'wbr', 'col'])

/** HAST element node shape (subset used by sanitize pipeline). */
interface HastNode {
  type: string
  tagName?: string
  value?: string
  properties?: Record<string, unknown>
  children?: HastNode[]
}

/** Tags never reconstructed — even as escaped text, faithful reconstruction of
 * executable elements is a liability. They collapse to an [unsupported:] marker. */
const UNSAFE_RECONSTRUCT_TAGS = new Set([
  'script', 'style', 'iframe', 'object', 'embed', 'form', 'link', 'meta', 'base', 'noscript',
])

const textNode = (value: string): HastNode => ({ type: 'text', value })

/** Convert a non-allowlisted element into a SAFE HAST element tree for display.
 *
 * frontend-security: no HTML string is ever materialized from untrusted content.
 * The node's source form is represented as a `<span class="escaped-tag">` whose
 * children are discrete TEXT fragments — the `<` / `>` delimiters live in their
 * own text nodes, separate from the tag/attribute content — so no single string
 * anywhere in the tree contains parseable markup, and React renders text nodes
 * safely by construction. Filters retained from the sanitizer: `on*` handler
 * attributes dropped, tag/attr names restricted to a safe charset, and
 * dangerous-protocol attribute values (javascript:/data:/vbscript:) dropped.
 */
function escapedNodeTree(node: HastNode): HastNode {
  const tag = (node.tagName ?? '').replace(/[^a-zA-Z0-9-]/g, '')
  const wrap = (children: HastNode[]): HastNode => ({
    type: 'element',
    tagName: 'span',
    properties: { className: ['escaped-tag'] },
    children,
  })
  if (UNSAFE_RECONSTRUCT_TAGS.has(tag.toLowerCase())) {
    return wrap([textNode(`[unsupported: ${tag}]`)])
  }
  const attrs = node.properties
    ? Object.entries(node.properties)
        .filter(([k]) => k !== 'className' && !/^on/i.test(k) && /^[a-zA-Z0-9_:-]+$/.test(k))
        .filter(([, v]) => typeof v !== 'string' || !DANGEROUS_PROTOCOLS.some(p => cleanUrl(v).startsWith(p)))
        .map(([k, v]) => (v === true ? k : `${k}="${String(v)}"`))
        .join(' ')
    : ''
  const children: HastNode[] = [textNode('<'), textNode(attrs ? `${tag} ${attrs}` : tag), textNode('>')]
  for (const c of node.children || []) {
    if (c.type === 'text') children.push(textNode(c.value ?? ''))
    else if (c.type === 'element') children.push(escapedNodeTree(c))
  }
  if (!VOID_ELEMENTS.has(tag)) {
    children.push(textNode('</'), textNode(tag), textNode('>'))
  }
  return wrap(children)
}

/**
 * Exported so every markdown surface in the product shares ONE sanitize policy.
 *
 * Any renderer that admits raw HTML (`rehype-raw`) needs this immediately after
 * it, and a second surface must never carry its own copy of the allowlist: the
 * policy is security-relevant, so a fork would silently drift out of step with
 * this one. The plugin is pure (no React, no styling), so a surface that cannot
 * reuse the component itself can still reuse the policy.
 */
export function rehypeSanitize() {
  return (tree: HastNode) => {
    const walk = (node: HastNode, parent: HastNode, index: number) => {
      // TS strict-null: HastNode.children is `HastNode[] | undefined`. Callers only
      // recurse into nodes whose children array they are iterating, so this cannot
      // happen for a well-formed HAST tree — guard defensively and move on.
      if (!parent.children) return index + 1
      if (node.type === 'element') {
        const tagLower = (node.tagName || '').toLowerCase()

        // Allowlist check: unknown tags become a safe element tree of text
        // fragments (no HTML string is ever built from untrusted content)
        if (!ALLOWED_TAGS.has(tagLower)) {
          parent.children.splice(index, 1, escapedNodeTree(node))
          return index + 1  // skip past the replacement (already safe)
        }

        // input: only allow GFM task-list checkboxes
        if (tagLower === 'input') {
          if (node.properties?.type === 'checkbox') {
            node.properties = { type: 'checkbox', checked: !!node.properties.checked, disabled: true }
          } else {
            parent.children.splice(index, 1)
            return index
          }
        }

        // Attribute ALLOWLIST: keep only attributes permitted for this element;
        // drop everything else (was: a denylist that stripped on*/protocol/srcdoc
        // and kept the rest). Retained URL-bearing attrs still get the
        // dangerous-protocol check below.
        if (node.properties) {
          for (const [key, val] of Object.entries(node.properties)) {
            if (!isAllowedAttr(tagLower, key)) {
              delete node.properties[key]
              continue
            }
            if (typeof val === 'string') {
              const cleaned = cleanUrl(val)
              if (DANGEROUS_PROTOCOLS.some(p => cleaned.startsWith(p))) {
                // Allow data:image/* on img src (inline base64 images)
                if (node.tagName === 'img' && key === 'src' && cleaned.startsWith('data:image/')) {
                  continue
                }
                delete node.properties[key]
              }
            }
          }
        }
      }
      if (node.children) {
        for (let i = 0; i < node.children.length; i++) {
          const result = walk(node.children[i], node, i)
          if (typeof result === 'number') i = result - 1  // re-check after splice
        }
      }
    }
    if (tree.children) {
      for (let i = 0; i < tree.children.length; i++) {
        const result = walk(tree.children[i], tree, i)
        if (typeof result === 'number') i = result - 1
      }
    }
  }
}

/** Showable verbatim. Executable tags keep their `[unsupported: x]` marker; every
 * other unknown tag diverts, because a text node is inert wherever it lands. */
function divertibleTag(tag: string): boolean {
  return !UNSAFE_RECONSTRUCT_TAGS.has(tag)
}

/** Render non-allowlisted single tags VERBATIM instead of reconstructing them.
 *
 * Runs at the remark (mdast) stage, before rehypeRaw reaches the HTML parser. An
 * mdast `html` node's `value` IS the author's original source substring, so
 * converting it to `text` reproduces exactly what was typed: original case,
 * original spacing, and no closing tag the author never wrote.
 *
 * Deliberately narrow — two things keep existing escapedNodeTree() handling:
 * multi-tag raw HTML blocks, and UNSAFE_RECONSTRUCT_TAGS (script/style/iframe
 * still collapse to `[unsupported: x]`). Everything else diverts, including a
 * tag whose attribute value is a dangerous protocol — see frontend-security.
 *
 * Exported so every markdown surface that admits raw HTML shares this pass; a
 * surface wiring rehypeSanitize without it keeps the lossy reconstruction.
 *
 * frontend-security: the tag never becomes an element and never reaches the HTML
 * parser — it ends up a text node, which React escapes on render, so the React
 * #290 guard still holds.
 */
/** Allowlisted tags whose text content is verbatim, never prose (see remarkLatexDelimiters). */
export const VERBATIM_CONTENT_TAGS = new Set(['code', 'pre', 'kbd', 'samp', 'var', 'tt', 'textarea', 'svg', 'math'])

export function remarkVerbatimUnknownTags() {
  return (tree: MdastNode) => {
    const walk = (node: MdastNode) => {
      const kids = node.children
      if (!kids) return
      // Pairing is computed ONCE per sibling list (linear), never per opener:
      // a run of unclosed unknown openers must not cost a suffix scan each.
      let pairs: Map<number, number> | null = null
      for (let i = 0; i < kids.length; i++) {
        const child = kids[i]
        if (child.type === 'html' && typeof child.value === 'string') {
          const tag = singleTagName(child.value)
          if (tag && !ALLOWED_TAGS.has(tag) && divertibleTag(tag)) {
            let paired = -1
            if (!child.value.startsWith('</') && !child.value.endsWith('/>')) {
              pairs ??= pairedCloseIndices(kids)
              paired = pairs.get(i) ?? -1
            }
            if (paired > i) {
              // A closed container: divert the whole span, so allowlisted tags
              // inside it stay literal instead of rendering as live elements.
              for (let j = i; j <= paired; j++) {
                const k = kids[j]
                if (k.type !== 'html' || typeof k.value !== 'string') continue
                const kt = singleTagName(k.value)
                if (kt && divertibleTag(kt)) k.type = 'text'
              }
            } else {
              // Verbatim source text — no HTML string is built or re-parsed.
              child.type = 'text'
            }
          }
        }
        walk(child)
      }
    }
    walk(tree)
  }
}

export type MdastNode = {
  type: string
  url?: string
  value?: string
  children?: MdastNode[]
  position?: { start: { offset?: number }; end: { offset?: number } }
}
