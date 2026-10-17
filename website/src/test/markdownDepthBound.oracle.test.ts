import { describe, it, expect } from 'vitest'
import { unified } from 'unified'
import remarkParse from 'remark-parse'
import remarkGfm from 'remark-gfm'
import remarkRehype from 'remark-rehype'
import rehypeRaw from 'rehype-raw'
import type { Root as HastRoot } from 'hast'
import { MAX_TREE_DEPTH, boundRawHtmlDepth } from '../utils/markdownDepthBound'

// `rehypeBoundRawDepth` decides from an ORACLE parse -- parse5 over the
// serialized tree -- whether the raw HTML a message carries would nest past
// the bound once `rehype-raw` rebuilds it. That decision is only sound while
// the oracle reads the same tree `rehype-raw` (hast-util-raw) actually builds:
// same parser, same options, same handling of every construct that changes
// nesting. This file runs both on the same inputs and compares.
//
// It is the drift detector: a `parse5` or `hast-util-raw` upgrade that changes
// either side's tree, or a lockfile that ever resolves the two to different
// `parse5` instances, turns a case here red instead of quietly re-opening the
// stack overflow. Inputs sit just past the bound (not thousands deep) so the
// real `rehype-raw` pass can run without overflowing.

const N = MAX_TREE_DEPTH + 60

/** Every construct known to change nesting between naive and real parsing. */
const INPUTS: Record<string, string> = {
  'plain div block': '<div>\n' + '<div>'.repeat(N),
  'plain div inline': 'x <div>' + '<div>'.repeat(N),
  'divs across paragraphs': 'x <div>\n\n'.repeat(N),
  'colon tag names': '<div>\n' + '<x:y>'.repeat(N),
  'form-feed terminated': '<div>\n' + '<span\f>'.repeat(N),
  'NUL in name': '<div>\n' + '<span\0>'.repeat(N),
  'svg foreign voids': '<svg>\n' + '<base>'.repeat(N),
  'svg foreign voids inline': 'x <svg>' + '<base>'.repeat(N),
  'noscript body': '<noscript>\n' + '<div>'.repeat(N),
  'noscript inline': 'x <noscript>' + '<div>'.repeat(N),
  'template body': '<template>\n' + '<div>'.repeat(N),
  'nested templates': '<template>'.repeat(N),
  'comment fake close': 'x ' + '<div><!-- </div> -->'.repeat(N),
  'cdata fake close': 'x ' + '<div><![CDATA[ </div> ]]>'.repeat(N),
  'attr fake close': 'x ' + '<div data-x="</div>">'.repeat(N),
  'code-span fake close': 'x ' + '<div>`</div>`'.repeat(N),
  'self-closing spelling': 'x ' + '<div/>'.repeat(N),
  'implied-end siblings': 'x ' + '<p><p>'.repeat(N) + '<div>'.repeat(N),
  'mixed with blockquotes': '> '.repeat(40) + '<div>'.repeat(N),
  'mixed with list': '- <div>\n  - <div>\n    - ' + '<div>'.repeat(N),
  'raw + math passthrough': '$$\nx\n$$\n\n<div>\n' + '<div>'.repeat(N),
}

function iterDepth(root: unknown): number {
  let max = 0
  const stack: Array<[{ children?: unknown[] }, number]> = [[root as { children?: unknown[] }, 0]]
  while (stack.length) {
    const [n, d] = stack.pop()!
    if (d > max) max = d
    for (const c of n.children ?? []) stack.push([c as { children?: unknown[] }, d + 1])
  }
  return max
}

const toHast = unified().use(remarkParse).use(remarkGfm).use(remarkRehype, { allowDangerousHtml: true })
const realRaw = unified().use(rehypeRaw, { passThrough: ['math', 'inlineMath'] })

describe('raw-HTML depth oracle agrees with what rehype-raw builds', () => {
  for (const [name, src] of Object.entries(INPUTS)) {
    it(name, () => {
      const parsed = toHast.runSync(toHast.parse(src)) as HastRoot
      // The real tree, on an untouched copy.
      const real = realRaw.runSync(structuredClone(parsed)) as HastRoot
      const realDepth = iterDepth(real)
      // The oracle's decision, on the original.
      const downgraded = boundRawHtmlDepth(parsed)
      // Soundness: whenever the real tree exceeds the bound, the oracle must
      // have downgraded. (The converse is allowed -- over-count is safe.)
      if (realDepth > MAX_TREE_DEPTH) expect(downgraded).toBe(true)
    })
  }

  it('leaves ordinary raw HTML alone (no false downgrade on shallow input)', () => {
    const parsed = toHast.runSync(toHast.parse('<div class="x"><b>bold</b> and <i>italic</i></div>\n\ntext <span>inline</span>')) as HastRoot
    expect(boundRawHtmlDepth(parsed)).toBe(false)
  })
})
