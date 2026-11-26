import { describe, expect, it } from 'vitest'
import { LINK_DIM_SHARE, LINK_LIT_ALPHA, LINK_MAX_ALPHA, LINK_OVERVIEW_ALPHA, drawnLinks, linkAlpha, linksInView, LABEL_GAP, LABEL_PAD, MARK_GAP, MAX_LABELS, NODE_LABEL_CHARS, NODE_MAX_PX, NODE_MIN_PX, canvasLabel, cutAtWord, dotPxAt, MIN_INK_ALPHA, NODE_BUDGET, OVERVIEW_LABELS, inkAlpha, labelBudget, placeLabels, showsNodes } from '../pages/overview/memoryGraph/graphView'
import { KIND_SIZE, oklchToRgb, topicColors } from '../pages/overview/memoryGraph/graphColors'

const size = { width: 800, height: 600 }
const big = [{ id: 0, x: 0, y: 0, r: 100, count: NODE_BUDGET * 3, label: null }]

describe('when the memory graph opens a cluster into memories', () => {
  it('shows a cluster larger than the node budget as a cluster while it is in view whole', () => {
    expect(showsNodes({ cx: 0, cy: 0, k: 2 }, size, big)).toBe(false)
  })

  it('opens that cluster into memories once the view holds a small part of it', () => {
    expect(showsNodes({ cx: 0, cy: 0, k: 40 }, size, big)).toBe(true)
  })
})

describe('memory graph labels', () => {
  const width = () => 120
  it('never draws two labels that touch, nor the same text twice', () => {
    const cands = [
      { id: 'a', at: { x: 200, y: 100 }, text: 'mutation-testing', rank: 9 },
      { id: 'b', at: { x: 200, y: 118 }, text: 'ci · flaky', rank: 8 },
      { id: 'c', at: { x: 600, y: 300 }, text: 'mutation-testing', rank: 7 },
      { id: 'd', at: { x: 600, y: 400 }, text: 'release', rank: 6 },
    ]
    const out = placeLabels(cands, size, 10, width)
    expect(out.map(l => l.id)).toEqual(['a', 'd'])
    const [p, q] = out.map(l => l.box)
    const apart = p.x + p.width + LABEL_GAP <= q.x || q.x + q.width + LABEL_GAP <= p.x || p.y + p.height + LABEL_GAP <= q.y || q.y + q.height + LABEL_GAP <= p.y
    expect(apart).toBe(true)
  })

  it('names a few topics with the whole store in view and more as the view zooms in', () => {
    expect(labelBudget(1)).toBe(OVERVIEW_LABELS)
    expect(labelBudget(4)).toBeGreaterThan(OVERVIEW_LABELS)
    expect(labelBudget(1e6)).toBe(MAX_LABELS)
  })
})

describe('memory graph topic colours and ink', () => {
  const cluster = (id: number, x: number, y: number) => ({ id, x, y, r: 5, count: 50, label: `t${id}` })
  const dist = (a: number[], b: number[]) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2])

  it('gives neighbouring topics related colours and opposite topics different ones', () => {
    const { dot } = topicColors([cluster(0, 100, 0), cluster(1, 100, 10), cluster(2, -100, 0), cluster(-1, 0, 120)], [-100, -100, 100, 100], false)
    expect(dist(dot.get(0)!, dot.get(1)!)).toBeLessThan(dist(dot.get(0)!, dot.get(2)!) / 3)
    expect(dot.has(-1)).toBe(false)
  })

  it('draws dark-theme dots lighter than light-theme dots', () => {
    const c = [cluster(0, 50, 50)]
    const lum = (rgb: number[]) => rgb[0] + rgb[1] + rgb[2]
    expect(lum(topicColors(c, [-100, -100, 100, 100], true).dot.get(0)!)).toBeGreaterThan(lum(topicColors(c, [-100, -100, 100, 100], false).dot.get(0)!))
  })

  it('converts OKLCH to sRGB', () => {
    expect(oklchToRgb(1, 0, 0).map(x => Math.round(x * 255))).toEqual([255, 255, 255])
    expect(oklchToRgb(0, 0, 0)).toEqual([0, 0, 0])
  })

  it('lightens dots that pile up and keeps spread-out dots solid', () => {
    expect(inkAlpha(5000, 3, 20)).toBe(MIN_INK_ALPHA)
    expect(inkAlpha(10, 3, 200)).toBe(1)
    const mid = inkAlpha(1000, 3, 40)
    expect(mid).toBeGreaterThan(MIN_INK_ALPHA)
    expect(mid).toBeLessThan(1)
  })
})

describe('memory graph dot size', () => {
  it('keeps overview dots small and grows them to a readable shape zoomed in', () => {
    expect(dotPxAt(1)).toBe(NODE_MIN_PX)
    expect(dotPxAt(16)).toBeGreaterThanOrEqual(8)
    expect(dotPxAt(64)).toBe(NODE_MAX_PX)
    expect(NODE_MAX_PX).toBeGreaterThanOrEqual(10)
    expect(dotPxAt(1e6)).toBe(NODE_MAX_PX)
  })
})

describe('memory names on the canvas', () => {
  it('cuts a long name at a word with an ellipsis, never mid-word', () => {
    const cut = cutAtWord('Inside the sandbox, a curl upload is blocked by the exfil rule')
    expect(cut).toBe('Inside the sandbox, a curl…')
    expect(Array.from(cut).length).toBeLessThanOrEqual(NODE_LABEL_CHARS)
    expect(cutAtWord('short name')).toBe('short name')
  })

  it('cuts text with no space to break at at the limit', () => {
    const cut = cutAtWord('记'.repeat(50))
    expect(Array.from(cut).length).toBe(NODE_LABEL_CHARS)
    expect(cut.endsWith('…')).toBe(true)
  })

  it('names a fact by its key and an experience by its text', () => {
    expect(canvasLabel({ kind: 'fact', label: 'pref.kirocrew.cli_help_probe_is_policy_blocked = true', record: { id: 'pref.kirocrew.cli_help_probe_is_policy_blocked' } }))
      .toBe('cli help probe is policy blocked')
    expect(canvasLabel({ kind: 'episode', label: 'A shell command blocked with "unresolved shell variable"', record: { id: 'e1' } }))
      .toBe('A shell command blocked with…')
  })
})

describe('a memory name beside its mark', () => {
  const width = () => 100
  const at = { x: 300, y: 200 }
  it('sits right of its mark, a gap from its edge, centred on it', () => {
    const [l] = placeLabels([{ id: 'a', at, text: 'a', rank: 1, mark: 10 }], size, 5, width, [{ id: 'a', at, px: 10 }])
    expect(l.box.x + LABEL_PAD).toBe(at.x + 5 + MARK_GAP)
    expect(l.box.y + l.box.height / 2).toBe(at.y)
  })

  it('moves left when the right side covers other marks, and is left out when both do', () => {
    const right = [0, 1, 2].map(i => ({ id: `r${i}`, at: { x: 340 + i * 25, y: 200 }, px: 10 }))
    const [l] = placeLabels([{ id: 'a', at, text: 'a', rank: 1, mark: 10 }], size, 5, width, right)
    expect(l.box.x + l.box.width).toBeLessThanOrEqual(at.x)
    const left = [0, 1, 2].map(i => ({ id: `l${i}`, at: { x: 260 - i * 25, y: 200 }, px: 10 }))
    expect(placeLabels([{ id: 'a', at, text: 'a', rank: 1, mark: 10 }], size, 5, width, [...right, ...left])).toEqual([])
  })
})

describe('mark sizes by kind', () => {
  it('gives a square and a triangle about the area of a circle', () => {
    const circle = Math.PI / 4
    const square = (0.8 * KIND_SIZE.lesson) ** 2
    const triangle = (Math.sqrt(3) / 4) * (0.9 * KIND_SIZE.episode) ** 2
    for (const area of [square, triangle]) expect(Math.abs(area - circle) / circle).toBeLessThan(0.15)
    expect(KIND_SIZE.fact).toBe(1)
  })
})

describe('links between related memories', () => {
  it('stay faint with the whole store in view and fade in as the view zooms', () => {
    expect(linkAlpha(1, 1)).toBeCloseTo(LINK_OVERVIEW_ALPHA)
    expect(linkAlpha(4, 1)).toBeGreaterThan(linkAlpha(1, 1))
    expect(linkAlpha(16, 1)).toBeCloseTo(LINK_MAX_ALPHA)
    expect(linkAlpha(1000, 1)).toBeCloseTo(LINK_MAX_ALPHA)
  })

  it('show a closer pair stronger, light a hovered memory\'s own and dim the rest', () => {
    expect(linkAlpha(16, 0.95)).toBeGreaterThan(linkAlpha(16, 0.6))
    expect(linkAlpha(1, 0.6, 'lit')).toBe(LINK_LIT_ALPHA)
    expect(linkAlpha(16, 1, 'dim')).toBeCloseTo(LINK_MAX_ALPHA * LINK_DIM_SHARE)
  })

  it('pairs each drawn link once and skips memories not drawn', () => {
    const items = [
      { id: 'a', cluster: 1, links: [['b', 0.8], ['c', 0.7]] as [string, number][] },
      { id: 'b', cluster: 1, links: [['a', 0.8], ['gone', 0.9]] as [string, number][] },
      { id: 'c', cluster: 2, links: [['a', 0.7]] as [string, number][] },
    ]
    expect(drawnLinks(items)).toEqual([{ a: 0, b: 1, sim: 0.8, cluster: 1 }, { a: 0, b: 2, sim: 0.7, cluster: 1 }])
  })
})

describe('links drawn for a view', () => {
  const items = [{ x: 0, y: 0 }, { x: 1, y: 1 }, { x: 2, y: 2 }, { x: 50, y: 50 }]
  const l = (a: number, b: number, sim: number) => ({ a, b, sim, cluster: 0 })
  it('keeps links with both memories in view, the closest pairs first, up to the budget', () => {
    const links = [l(0, 1, 0.6), l(1, 2, 0.9), l(0, 2, 0.7), l(2, 3, 0.99)]
    expect(linksInView(links, items, [-1, -1, 3, 3])).toEqual([l(0, 1, 0.6), l(1, 2, 0.9), l(0, 2, 0.7)])
    expect(linksInView(links, items, [-1, -1, 3, 3], 2)).toEqual([l(1, 2, 0.9), l(0, 2, 0.7)])
  })
})
