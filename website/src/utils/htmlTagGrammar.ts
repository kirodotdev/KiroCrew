/**
 * The ONE grammar for "is this mdast `html` node a single tag, and which?".
 *
 * Two passes decide whether a span of siblings is shown as source rather than
 * prose: the renderer's verbatim-unknown-tags pass and the math plugin's
 * verbatim context (`remarkLatexDelimiters`). They must agree, or a tag the
 * renderer diverts to literal source can still get a KaTeX span converted in
 * the middle of it. Keeping one regex and one pairing walk here is what makes
 * them agree; neither consumer may grow its own copy.
 */

/** The minimal node shape the pairing walk needs; both consumers' node types satisfy it. */
export type TagNode = { type: string; value?: string }

/** A whole mdast `html` node that is exactly ONE tag: `<x>`, `</x>`, `<x a b>`,
 * `<x/>`. Attribute values are quote-aware, so a value may itself contain `>`
 * (`<x a="b>c">`); without that, such a tag misses this test and falls to the
 * lossy escapedNodeTree() path. A bare attribute may hold `/` (`<x a/b>`) so
 * this accepts everything the previous blanket `[^>]*` did. The leading
 * `[a-zA-Z]` excludes comments (`<!-- -->`) and doctypes, which keep their
 * existing handling. */
const SINGLE_TAG_RE =
  /^<\/?([a-zA-Z][a-zA-Z0-9-]*)((?:\s+[^\s=>]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]*))?)*)\s*\/?>$/

/** Tag name of a single-tag html node, or undefined when it is not one. */
export function singleTagName(value: string): string | undefined {
  return SINGLE_TAG_RE.exec(value)?.[1]?.toLowerCase()
}

/** True for `<x ...>` -- a single OPENING tag that is neither a close nor self-closing. */
export function isOpeningTag(value: string): boolean {
  return singleTagName(value) !== undefined && !value.startsWith('</') && !value.endsWith('/>')
}

/** For every OPENING single-tag html sibling in `kids` that is closed among
 * them, the index of its close. One linear pass with a stack per tag name, so
 * a run of unclosed openers costs O(n) in total -- a per-opener suffix scan
 * would make one math delimiter after thousands of `<code>` tags quadratic.
 * Pairing semantics: a close pairs with the most recent unpaired same-tag
 * opener (same-tag nesting tracked); self-closing tags pair with nothing; an
 * unclosed opener is simply absent from the map. */
export function pairedCloseIndices(kids: readonly TagNode[]): Map<number, number> {
  const open = new Map<string, number[]>()
  const out = new Map<number, number>()
  for (let j = 0; j < kids.length; j++) {
    const k = kids[j]
    if (k.type !== 'html' || typeof k.value !== 'string') continue
    const tag = singleTagName(k.value)
    if (tag === undefined) continue
    if (k.value.startsWith('</')) {
      const stack = open.get(tag)
      const opener = stack?.pop()
      if (opener !== undefined) out.set(opener, j)
    } else if (!k.value.endsWith('/>')) {
      let stack = open.get(tag)
      if (!stack) open.set(tag, (stack = []))
      stack.push(j)
    }
  }
  return out
}
