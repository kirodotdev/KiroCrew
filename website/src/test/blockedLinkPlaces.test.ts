import { describe, it, expect } from 'vitest'
import { unified } from 'unified'
import remarkParse from 'remark-parse'
import remarkGfm from 'remark-gfm'
import remarkRehype from 'remark-rehype'
import type { Element as HastElement, Root as HastRoot, RootContent } from 'hast'
import { rehypeRedactionMarkers } from '../components/RedactionCards'

// The card of a blocked link is keyed on where its block's run of the host's
// links starts, and a held card reopens under the block whose run holds the
// clicked link's number. That needs two things from `rehypeRedactionMarkers`:
// every chip it makes names its block, and a link keeps its number when Allow
// turns each placeholder into its address, however the reload regroups the
// blocks around it.

const HOST = 'reviews.example-sink.net'
const OTHER = 'other.example-sink.net'
const PH = (h: string) => `[REDACTED: suspicious URL to ${h}]`
const url = (h: string, n: number) => `https://${h}/p${n}?q=x`

function hast(md: string): HastRoot {
  const p = unified().use(remarkParse).use(remarkGfm).use(remarkRehype)
  return p.runSync(p.parse(md)) as HastRoot
}

function run(md: string, opts: { domains: string[]; held: string[]; block: number }): HastRoot {
  const tree = hast(md)
  rehypeRedactionMarkers({ ordinals: new Set(), domains: new Set(opts.domains), held: new Set(opts.held), base: 0, block: opts.block })(tree)
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

function text(node: RootContent): string {
  if (node.type === 'text') return node.value
  if (node.type === 'element') return node.children.map(c => text(c as RootContent)).join('')
  return ''
}

/** Per top-level block: its text, and the host's run from its slot. */
function runs(tree: HastRoot, host: string): Array<{ text: string; first: number; end: number } | { text: string; first: null; end: null }> {
  const out: Array<{ text: string; first: number; end: number } | { text: string; first: null; end: null }> = []
  const kids = tree.children as RootContent[]
  for (let i = 0; i < kids.length; i++) {
    const k = kids[i]
    if (k.type === 'element' && k.tagName === 'redaction-card-slot') continue
    const next = kids[i + 1]
    const slot = next && next.type === 'element' && next.tagName === 'redaction-card-slot' ? next : null
    const pair = slot ? String(slot.properties?.ends ?? '').split(' ').find(p => p.startsWith(`rx-link-${host}~`)) : undefined
    if (!pair) { out.push({ text: text(k), first: null, end: null }); continue }
    const m = /~\d+\.(\d+)\|(\d+)$/.exec(pair)!
    out.push({ text: text(k), first: Number(m[1]), end: Number(m[2]) })
  }
  return out
}

describe('rehypeRedactionMarkers card keys', () => {
  it('names the block of every chip it makes, wherever the chip sits', () => {
    const md = [
      `# Head ${PH(HOST)}`,
      `Para *em ${PH(HOST)}* and ${PH(OTHER)}`,
      `- item ${PH(HOST)}\n- item ${PH(HOST)}`,
      `| a | b |\n| - | - |\n| ${PH(HOST)} | x |`,
      `> quote ${PH(OTHER)}`,
    ].join('\n\n')
    const chips = elements(run(md, { domains: [HOST, OTHER], held: [], block: 3 }), 'blocked-link')
    expect(chips).toHaveLength(7)
    for (const c of chips) {
      expect(String(c.properties?.block)).toMatch(/^3\.\d+$/)
      expect(String(c.properties?.chip)).toMatch(/^\d+$/)
    }
    // Numbers run per host through the rendered block, in document order.
    expect(chips.filter(c => c.properties?.domain === HOST).map(c => Number(c.properties?.chip))).toEqual([0, 1, 2, 3, 4])
    expect(chips.filter(c => c.properties?.domain === OTHER).map(c => Number(c.properties?.chip))).toEqual([0, 1])
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

  // Seeded paragraphs and list items holding labelled links of the host (as
  // chips before the reload, as markdown links, bare URLs or images after),
  // other text, and a plain link to the host the redactor never removed. The
  // reload joins random neighbouring blocks. For every link, the block that
  // holds its label after the reload has a run that holds its number.
  it('keeps every link inside its block\'s run across a reload that joins blocks (300 seeds)', () => {
    for (let seed = 1; seed <= 300; seed++) {
      const r = rng(seed)
      const pick = (n: number) => Math.floor(r() * n)
      let n = 0
      const before: string[] = []
      const after: string[] = []
      const labels: Array<{ label: string; n: number }> = []
      const count = 2 + pick(5)
      for (let b = 0; b < count; b++) {
        const pieces: Array<[string, string]> = []
        const links = pick(3)
        if (pick(4) === 0) pieces.push(['plain words', 'plain words'])
        for (let k = 0; k < links; k++) {
          const label = `L${seed}x${n}`
          labels.push({ label, n })
          const u = url(HOST, n)
          const restored = [`[page](${u})`, u, `![img](${u})`][pick(3)]
          pieces.push([`${label} ${PH(HOST)}`, `${label} ${restored}`])
          n++
        }
        if (pick(5) === 0) {
          // A link to the host the redactor kept: a link in both phases.
          pieces.push([`kept [ok](${url(HOST, 900 + b)})`, `kept [ok](${url(HOST, 900 + b)})`])
          n++
        }
        const list = pick(4) === 0
        const join = (xs: string[]) => list ? xs.map(x => `- ${x}`).join('\n') : xs.join(' ')
        before.push(join(pieces.map(p => p[0])) || `filler ${b}`)
        after.push(join(pieces.map(p => p[1])) || `filler ${b}`)
      }
      const glue = after.slice(1).map(() => (pick(3) === 0 ? '\n' : '\n\n'))
      const pre = run(before.join('\n\n'), { domains: [HOST], held: [], block: 0 })
      const post = run(after.map((a, i) => (i ? glue[i - 1] : '') + a).join(''), { domains: [], held: [HOST], block: 0 })
      // Before the reload: the chips carry the numbers the labels were given.
      for (const c of elements(pre, 'blocked-link')) expect(c.properties?.domain).toBe(HOST)
      const blocks = runs(post, HOST)
      for (const { label, n: num } of labels) {
        const holder = blocks.filter(b => b.text.includes(`${label} `))
        expect(holder, `seed ${seed} ${label}`).toHaveLength(1)
        const b = holder[0]
        expect(b.first, `seed ${seed} ${label}`).not.toBeNull()
        expect(b.first! <= num && num < b.end!, `seed ${seed} ${label} in [${b.first}, ${b.end})`).toBe(true)
      }
      // No two blocks' runs overlap, so exactly one block can hold a link.
      const spans = blocks.filter(b => b.first !== null).map(b => [b.first!, b.end!])
      for (let i = 1; i < spans.length; i++) expect(spans[i][0]).toBeGreaterThanOrEqual(spans[i - 1][1])
    }
  })
})
