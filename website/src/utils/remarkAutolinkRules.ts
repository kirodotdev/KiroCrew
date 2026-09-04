/**
 * Bare-token autolinking, as a remark plugin over `text` nodes.
 *
 * Working on the tree rather than the source is what keeps this short: `code`,
 * `inlineCode`, math, `link` and image nodes are separate types the walk never
 * enters, and a `footnoteReference` label is a property, not children.
 *
 * Two checks the tree cannot express structurally remain: text between paired
 * raw inline HTML tags is a SIBLING of those tags, and a backslash surviving in
 * a text value is the author asking for the token to stay literal.
 *
 * Contract and ordering: `website/docs/extension-seams.md`.
 */
import { autolinkHref, getAutolinkRules } from './autolinkRules'

type MdNode = {
  type: string
  value?: string
  url?: string
  children?: MdNode[]
}

/** Types whose descendants are never prose, so the walk stops at them. */
const OPAQUE = new Set([
  'code',
  'inlineCode',
  'math',
  'inlineMath',
  'html',
  'link',
  'linkReference',
  'image',
  'imageReference',
  'definition',
  'footnoteReference',
])

/** Tags that never close, so they open no enclosed region. */
const VOID_TAGS = new Set([
  'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
  'link', 'meta', 'param', 'source', 'track', 'wbr',
])

type TagKind = 'open' | 'close' | undefined

/**
 * Classify a raw inline HTML node. remark has already tokenized the tag, so
 * this reads the node's own value rather than re-scanning prose for `<`.
 */
function tagKind(value: string): TagKind {
  const m = /^<(\/?)([a-zA-Z][\w:-]*)/.exec(value)
  if (!m) return undefined
  if (m[1]) return 'close'
  if (value.endsWith('/>') || VOID_TAGS.has(m[2].toLowerCase())) return undefined
  return 'open'
}

type Hit = { start: number; end: number; text: string; href: string }

/** Matches accepted in registration order, then ordered by position. */
function hitsIn(value: string): Hit[] {
  const hits: Hit[] = []
  for (const rule of getAutolinkRules()) {
    rule.pattern.lastIndex = 0
    let m: RegExpExecArray | null
    while ((m = rule.pattern.exec(value)) !== null) {
      const text = m[0]
      // A zero-width match cannot advance the scan: under `u` a lastIndex bump
      // lands mid-surrogate and the engine re-matches forever. Abandon the rule.
      if (!text) break
      const start = m.index
      if (value[start - 1] === '\\') continue
      const href = autolinkHref(rule, text)
      hits.push({ start, end: start + text.length, text, href })
    }
  }
  const accepted: Hit[] = []
  for (const h of hits) {
    if (accepted.some(a => h.start < a.end && a.start < h.end)) continue
    accepted.push(h)
  }
  return accepted.sort((a, b) => a.start - b.start)
}

/** Split one text node into alternating text and link nodes. */
function splitText(value: string, hits: Hit[]): MdNode[] {
  const out: MdNode[] = []
  let pos = 0
  for (const h of hits) {
    if (h.start > pos) out.push({ type: 'text', value: value.slice(pos, h.start) })
    out.push({ type: 'link', url: h.href, children: [{ type: 'text', value: h.text }] })
    pos = h.end
  }
  if (pos < value.length) out.push({ type: 'text', value: value.slice(pos) })
  return out
}

function transformChildren(parent: MdNode, enclosingDepth = 0): void {
  const kids = parent.children
  if (!kids?.length) return
  const next: MdNode[] = []
  let htmlDepth = enclosingDepth
  for (const node of kids) {
    if (node.type === 'html') {
      const kind = tagKind(node.value ?? '')
      if (kind === 'open') htmlDepth++
      else if (kind === 'close' && htmlDepth > 0) htmlDepth--
      next.push(node)
      continue
    }
    if (node.type === 'text' && htmlDepth === 0 && node.value) {
      const hits = hitsIn(node.value)
      if (hits.length) {
        next.push(...splitText(node.value, hits))
        continue
      }
    }
    if (!OPAQUE.has(node.type)) transformChildren(node, htmlDepth)
    next.push(node)
  }
  parent.children = next
}

/**
 * Remark plugin. Inert with an empty registry, which is the core's own state:
 * an edition registers the vocabulary.
 */
export default function remarkAutolinkRules() {
  return (tree: MdNode): void => {
    if (!getAutolinkRules().length) return
    transformChildren(tree)
  }
}
