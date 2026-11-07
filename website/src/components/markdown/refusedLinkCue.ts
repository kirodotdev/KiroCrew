import type { Element } from 'hast'

/**
 * Whether an `<a>` that reached a chat renderer with no usable href should
 * wear the refused-link cue.
 *
 * An inert anchor arrives in two shapes: href `''` (react-markdown's
 * urlTransform sentinel for a custom scheme or an empty `[x]()` destination)
 * and no href at all (the sanitizer deleted a `javascript:`/`data:` one, or the
 * raw HTML never had one). Only a link someone was meant to follow gets the
 * cue. A raw in-page anchor (`<a name="install">`, `<a id="top">`, both kept
 * by the sanitizer) is a target, not a link, and an anchor with no content has
 * no label to mark, so both keep the plain span.
 */
export function wearsRefusedLinkCue(node: Element | undefined): boolean {
  if (!node) return true
  const props = node.properties ?? {}
  for (const key of ['name', 'id'] as const) {
    const v = props[key]
    if (v !== undefined && v !== null && String(v) !== '') return false
  }
  return node.children.length > 0
}
