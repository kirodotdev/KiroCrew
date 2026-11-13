/**
 * Remark plugin: wrap plain text an app's `textPatterns` match in a hoverable span.
 *
 * Walks the same regions `remarkAutolinkRules` does and nothing else: code, inline
 * code, math, raw HTML, links and images are opaque node types, and text between
 * paired raw inline HTML tags is skipped. Runs after the autolink pass, so a token
 * an edition already turned into a link stays a link.
 *
 * The span is `<span class="link-preview-text" data-link-preview-text>`; it carries
 * no URL (see `textPatterns.ts`). Inert when no pattern is active; a text node the
 * worker has not answered for yet renders as plain text until it has.
 */
import { OPAQUE, tagKind, type MdNode } from '../utils/remarkAutolinkRules'
import { hasTextPatterns, textPatternHits } from './textPatterns'

type SpanNode = MdNode & { data?: { hName: string; hProperties: Record<string, unknown> } }

function span(value: string): SpanNode {
  return {
    type: 'linkPreviewText',
    data: { hName: 'span', hProperties: { className: ['link-preview-text'], dataLinkPreviewText: '' } },
    children: [{ type: 'text', value }],
  }
}

function transform(parent: MdNode, enclosingDepth = 0): void {
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
      // The worker's answer for this exact text, or none yet (see textPatterns.ts).
      const hits = textPatternHits(node.value)
      if (hits.length) {
        let pos = 0
        for (const h of hits) {
          if (h.start > pos) next.push({ type: 'text', value: node.value.slice(pos, h.start) })
          next.push(span(node.value.slice(h.start, h.end)))
          pos = h.end
        }
        if (pos < node.value.length) next.push({ type: 'text', value: node.value.slice(pos) })
        continue
      }
    }
    if (!OPAQUE.has(node.type)) transform(node, htmlDepth)
    next.push(node)
  }
  parent.children = next
}

export default function remarkLinkPreviewText() {
  return (tree: MdNode): void => {
    if (!hasTextPatterns()) return
    transform(tree)
  }
}
