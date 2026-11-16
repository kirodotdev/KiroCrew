import { describe, it, expect } from 'vitest'
import { unified } from 'unified'
import remarkParse from 'remark-parse'
import remarkGfm from 'remark-gfm'
import remarkRehype from 'remark-rehype'
import type { Element as HastElement, Root as HastRoot, RootContent } from 'hast'
import { rehypeRedactionMarkers, type BlockedLink } from '../components/RedactionCards'

// The card of a blocked link is keyed on its top-level block, and a held card
// reopens under the block that lists its id after Allow. Allow drops the
// host's records and leaves the text as it was, so each chip turns back into
// the inline link it stood for and a block keeps its place.

const HOST = 'reviews.example-sink.net'
const OTHER = 'other.example-sink.net'
const url = (h: string, n: number) => `https://${h}/p${n}?q=x`
const rec = (u: string): BlockedLink => ({ url: u, domain: new URL(u).hostname, rule: 'exfil_query_length', query_chars: 290, withheld: null })

function hast(md: string): HastRoot {
  const p = unified().use(remarkParse).use(remarkGfm).use(remarkRehype)
  return p.runSync(p.parse(md)) as HastRoot
}

function run(md: string, opts: { links: BlockedLink[]; held: string[]; block: number }): HastRoot {
  const tree = hast(md)
  rehypeRedactionMarkers({ ordinals: new Set(), links: opts.links, held: new Set(opts.held), base: 0, block: opts.block })(tree)
  return tree
}

function elements(node: HastRoot | HastElement, tag: string, out: HastElement[] = []): HastElement[] {
  for (const c of node.children as RootContent[]) {
    if (c.type !== 'element') continue
    if (c.tagName === tag) out.push(c)
    elements(c, tag, out)
  }
  return out
}

/** Per top-level block, the blocked-link card ids its slot lists for `host`. */
function slotIds(tree: HastRoot, host: string): string[][] {
  const out: string[][] = []
  const kids = tree.children as RootContent[]
  for (let i = 0; i < kids.length; i++) {
    const k = kids[i]
    if (k.type === 'element' && k.tagName === 'redaction-card-slot') continue
    if (k.type === 'text' && k.value.trim() === '') continue
    const next = kids[i + 1]
    const slot = next && next.type === 'element' && next.tagName === 'redaction-card-slot' ? next : null
    out.push(slot ? String(slot.properties?.ids ?? '').split(' ').filter(id => id.startsWith(`rx-link-${host}~`)) : [])
  }
  return out
}

describe('rehypeRedactionMarkers card keys', () => {
  it('names the block of every chip it makes, wherever the chip sits', () => {
    const md = [
      `# Head ${url(HOST, 1)}`,
      `Para *em ${url(HOST, 2)}* and [other](${url(OTHER, 1)})`,
      `- item ${url(HOST, 3)}\n- item ![img](${url(HOST, 4)})`,
      `| a | b |\n| - | - |\n| ${url(HOST, 5)} | x |`,
      `> quote ${url(OTHER, 2)}`,
    ].join('\n\n')
    const links = [1, 2, 3, 4, 5].map(n => rec(url(HOST, n))).concat([1, 2].map(n => rec(url(OTHER, n))))
    const tree = run(md, { links, held: [], block: 3 })
    const chips = elements(tree, 'blocked-link')
    // No anchor or image is left carrying a recorded address.
    expect(elements(tree, 'a')).toEqual([])
    expect(elements(tree, 'img')).toEqual([])
    expect(chips).toHaveLength(7)
    for (const c of chips) {
      expect(String(c.properties?.block)).toMatch(/^3\.\d+$/)
    }
    // The two list items share a block.
    const listBlocks = chips.filter(c => c.properties?.domain === HOST).map(c => String(c.properties?.block))
    expect(listBlocks[2]).toBe(listBlocks[3])
    expect(new Set(listBlocks).size).toBe(4)
  })

  function rng(seed: number): () => number {
    let s = seed >>> 0
    return () => {
      s = (s + 0x6d2b79f5) >>> 0
      let t = s
      t = Math.imul(t ^ (t >>> 15), t | 1)
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296
    }
  }

  // Seeded paragraphs and list items holding blocked links of the host (a
  // markdown link, a bare URL or an image) and other text. Allow drops the
  // records and leaves the text, so each chip turns back into its link. Every
  // block lists the same card ids after the reload as before it, so a held card
  // reopens where it was.
  it('lists the same card ids per block after Allow turns chips back into links (300 seeds)', () => {
    for (let seed = 1; seed <= 300; seed++) {
      const r = rng(seed)
      const pick = (n: number) => Math.floor(r() * n)
      const blocks: string[] = []
      const links: BlockedLink[] = []
      const count = 2 + pick(5)
      for (let b = 0; b < count; b++) {
        const pieces: string[] = []
        const count = pick(3)
        if (pick(4) === 0) pieces.push('plain words')
        for (let k = 0; k < count; k++) {
          const u = url(HOST, b * 10 + k)
          links.push(rec(u))
          pieces.push(`x ${[`[page](${u})`, u, `![img](${u})`][pick(3)]}`)
        }
        const list = pick(4) === 0
        const join = (xs: string[]) => list ? xs.map(x => `- ${x}`).join('\n') : xs.join(' ')
        blocks.push(join(pieces) || `filler ${b}`)
      }
      const md = blocks.join('\n\n')
      const pre = slotIds(run(md, { links, held: [], block: 0 }), HOST)
      const post = slotIds(run(md, { links: [], held: [HOST], block: 0 }), HOST)
      expect(post, `seed ${seed}`).toEqual(pre)
      // Every id names its own block: no two blocks share a card.
      const all = pre.flat()
      expect(new Set(all).size, `seed ${seed}`).toBe(all.length)
    }
  })
})
