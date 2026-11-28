import { describe, expect, it } from 'vitest'
import { LINK_DIM_SHARE, LINK_LIT_ALPHA, LINK_MAX_ALPHA, LINK_OVERVIEW_ALPHA, drawnLinks, linkAlpha, linksInView, LABEL_GAP, LABEL_PAD, MARK_GAP, MAX_LABELS, NODE_MAX_PX, NODE_MIN_PX, canvasLabel, memoryLines, dotPxAt, MIN_INK_ALPHA, NODE_BUDGET, OVERVIEW_LABELS, inkAlpha, labelBudget, placeLabels, showsNodes, PHI, LEVEL_STEP, linkLevel, linkRule, panBy, topicLinks, topicLines, topicOrder, TOPIC_LINE_PX, labelGrid, topicLinePx } from '../pages/overview/memoryGraph/graphView'
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

  it.each([{ width: 316, height: 418 }, size])('keeps every placed box inside all edges and out of control corners at $width px', viewport => {
    const avoid = [
      { x: viewport.width - 60, y: viewport.height - 160, width: 60, height: 160 },
      { x: 0, y: viewport.height - 44, width: 200, height: 44 },
    ]
    const edges = [
      { x: 0, y: 0 }, { x: viewport.width, y: 0 },
      { x: 0, y: viewport.height }, { x: viewport.width, y: viewport.height },
      { x: 1, y: viewport.height / 2 }, { x: viewport.width - 1, y: viewport.height / 2 },
      { x: viewport.width / 2, y: 1 }, { x: viewport.width / 2, y: viewport.height - 1 },
    ]
    let placed = 0, omitted = 0
    for (const at of edges) for (const shape of [{ centred: true }, { mark: 12 }, {}]) {
      const labels = placeLabels([{ id: 'a', at, text: 'whole title', lines: ['whole', 'title'], rank: 1, ...shape }], viewport, 1, () => 90, [], undefined, avoid)
      if (!labels.length) omitted++
      for (const { box: b } of labels) {
        placed++
        expect(b.x).toBeGreaterThanOrEqual(0)
        expect(b.y).toBeGreaterThanOrEqual(0)
        expect(b.x + b.width).toBeLessThanOrEqual(viewport.width)
        expect(b.y + b.height).toBeLessThanOrEqual(viewport.height)
        for (const a of avoid) expect(b.x < a.x + a.width && a.x < b.x + b.width && b.y < a.y + a.height && a.y < b.y + b.height).toBe(false)
      }
    }
    expect(placed).toBeGreaterThan(0)
    expect(omitted).toBeGreaterThan(0)
  })

  it('does not place ellipsized text, including a shortened individual line', () => {
    const at = { x: 200, y: 100 }
    for (const candidate of [
      { text: 'After the ACP repai…' },
      { text: 'After the ACP repair', lines: ['After the ACP repai…'] },
    ]) expect(placeLabels([{ id: 'a', at, rank: 1, ...candidate }], size, 1, width)).toEqual([])
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
  // 7 px a character: a 160 px line holds 22 characters.
  const w = (line: string) => line.length * 7
  it('names a fact by its key and an experience by its first clause, never cut', () => {
    expect(canvasLabel({ kind: 'fact', label: 'pref.kirocrew.cli_help_probe_is_policy_blocked = true', record: { id: 'pref.kirocrew.cli_help_probe_is_policy_blocked' } }))
      .toBe('cli help probe is policy blocked')
    expect(canvasLabel({ kind: 'episode', label: 'Inside the sandbox, a curl upload is blocked by the exfil rule', record: { id: 'e1' } }))
      .toBe('Inside the sandbox')
    expect(canvasLabel({ kind: 'directive', label: 'Run the gate first. Then push.', record: { id: 'l1' } })).toBe('Run the gate first')
    expect(canvasLabel({ kind: 'episode', label: '2026-08-07: rebased the PR onto main', record: { id: 'e3' } })).toBe('rebased the PR onto main')
    expect(canvasLabel({ kind: 'episode', label: '2026-08-13 14:05 PDT: blocker on the release pick', record: { id: 'e4' } })).toBe('blocker on the release pick')
    expect(canvasLabel({ kind: 'episode', label: 'SessionMap. It keys by chat', record: { id: 'e5' } })).toBe('SessionMap')
    expect(canvasLabel({ kind: 'episode', label: 'pr3519 status: green', record: { id: 'e6' } })).toBe('pr3519 status')
    expect(canvasLabel({ kind: 'episode', label: 'A shell command blocked with "unresolved shell variable"', record: { id: 'e2' } }))
      .toBe('A shell command blocked with "unresolved shell variable"')
  })

  it('wraps a title over two lines of whole words, or leaves it unnamed rather than cut it', () => {
    expect(memoryLines('cli help probe is policy blocked', w)).toEqual(['cli help probe is', 'policy blocked'])
    expect(memoryLines('Inside the sandbox', w)).toEqual(['Inside the sandbox'])
    expect(memoryLines('A shell command blocked with "unresolved shell variable"', w)).toBeNull()
    for (const t of ['cli help probe is policy blocked', 'Inside the sandbox']) expect(memoryLines(t, w)!.join(' ')).toBe(t)
  })

  it('does not disguise an already cut source as a whole title', () => {
    expect(memoryLines(canvasLabel({ kind: 'episode', label: 'After the ACP repai…' }), w)).toBeNull()
    // A complete clause before the shortened remainder is still usable.
    expect(memoryLines(canvasLabel({ kind: 'episode', label: 'After the ACP repair, the rest…' }), w)).toEqual(['After the ACP repair'])
  })

  it('breaks text with no spaces between characters', () => {
    expect(memoryLines('记'.repeat(30), w)).toEqual(['记'.repeat(22), '记'.repeat(8)])
    expect(memoryLines('记'.repeat(50), w)).toBeNull()
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
    // Closest first, as drawnLinks hands them over.
    const links = [l(2, 3, 0.99), l(1, 2, 0.9), l(0, 2, 0.7), l(0, 1, 0.6)]
    expect(linksInView(links, items, [-1, -1, 3, 3])).toEqual([l(1, 2, 0.9), l(0, 2, 0.7), l(0, 1, 0.6)])
    expect(linksInView(links, items, [-1, -1, 3, 3], 2)).toEqual([l(1, 2, 0.9), l(0, 2, 0.7)])
  })

  it('hands links over closest first', () => {
    const items = [
      { id: 'a', cluster: 0, links: [['b', 0.6], ['c', 0.9]] as [string, number][] },
      { id: 'b', cluster: 0, links: [['c', 0.95]] as [string, number][] },
      { id: 'c', cluster: 0 },
    ]
    expect(drawnLinks(items).map(x => x.sim)).toEqual([0.95, 0.9, 0.6])
  })
})

describe('the golden-ratio levels', () => {
  it('steps the links at PHI ** 2 of zoom and grows a dot PHI times a level', () => {
    expect(LEVEL_STEP).toBeCloseTo(2.618, 3)
    expect(linkLevel(1)).toBe('topics')
    expect(linkLevel(LEVEL_STEP * 0.99)).toBe('topics')
    expect(linkLevel(LEVEL_STEP)).toBe('within')
    expect(linkLevel(LEVEL_STEP ** 2 * 0.99)).toBe('within')
    expect(linkLevel(LEVEL_STEP ** 2)).toBe('near')
    expect(dotPxAt(LEVEL_STEP) / dotPxAt(1)).toBeCloseTo(PHI, 5)
  })

  it('keeps each memory to its two closest links, inside its topic until the last level', () => {
    const items = [
      { x: 0, y: 0, cluster: 0 }, { x: 1, y: 0, cluster: 0 }, { x: 0, y: 1, cluster: 0 }, { x: 1, y: 1, cluster: 0 },
      { x: 3, y: 0, cluster: 1 }, { x: 90, y: 0, cluster: 1 },
    ]
    const l = (a: number, b: number, sim: number) => ({ a, b, sim, cluster: 0 })
    // Closest first, as drawnLinks hands them over.
    const links = [l(0, 1, 0.99), l(0, 2, 0.98), l(0, 3, 0.97), l(1, 4, 0.96), l(1, 5, 0.95), l(2, 3, 0.9)]
    const box = [-1, -1, 100, 100] as [number, number, number, number]
    // Inside a topic: 0 keeps its two closest (0-1, 0-2), never a third (0-3).
    expect(linksInView(links, items, box, 99, linkRule('within', 1, 10))).toEqual([l(0, 1, 0.99), l(0, 2, 0.98), l(2, 3, 0.9)])
    // The last level adds a short link to the next topic (1-4), not a long one (1-5).
    expect(linksInView(links, items, box, 99, linkRule('near', 1, 10))).toEqual([l(0, 1, 0.99), l(0, 2, 0.98), l(1, 4, 0.96), l(2, 3, 0.9)])
  })

  it('joins only topics that sit side by side, each to its two busiest neighbours', () => {
    const clusters = [
      { id: 0, x: 0, y: 0, r: 5, count: 1 }, { id: 1, x: 12, y: 0, r: 5, count: 1 },
      { id: 2, x: 0, y: 12, r: 5, count: 1 }, { id: 3, x: 200, y: 0, r: 5, count: 1 },
    ]
    const items = clusters.map(c => ({ x: c.x, y: c.y, cluster: c.id }))
    const l = (a: number, b: number) => ({ a, b, sim: 0.9, cluster: 0 })
    const out = topicLinks([l(0, 1), l(0, 1), l(0, 2), l(0, 3), l(0, 3), l(0, 3)], items, clusters)
    // 0-3 shares the most links but sits far away; 0-1 is the busiest neighbour.
    expect(out.map(x => [x.a, x.b, x.sim])).toEqual([[0, 1, 1], [0, 2, 0.5]])
    expect(out.every(x => x.hub)).toBe(true)
  })

  it('keeps a pan over the layout', () => {
    const v = panBy({ cx: 0, cy: 0, k: 1 }, { x: 1000, y: -1000 }, [-10, -10, 10, 10])
    expect([v.cx, v.cy]).toEqual([10, 10])
  })
})

describe('topic names', () => {
  // 8 px a character: a 150 px line holds 18 characters.
  const w = (line: string) => line.length * 8
  it('wraps a name to two lines of whole words, never cut or marked with an ellipsis', () => {
    expect(topicLines('Rebase & Conflict Resolution', w)).toEqual(['Rebase & Conflict', 'Resolution'])
    expect(topicLines('Git & Rebase', w)).toEqual(['Git & Rebase'])
    for (const name of ['Recovery & Tool Cancellation', 'Verification & Inherited Red', 'A B C D E F G H I J K L M N O P Q R S T']) {
      const lines = topicLines(name, w)
      expect(lines.length).toBeLessThanOrEqual(2)
      expect(lines.join(' ')).not.toContain('…')
      expect(name.startsWith(lines.join(' '))).toBe(true)
      for (const line of lines) expect(w(line) <= TOPIC_LINE_PX || !line.includes(' ')).toBe(true)
    }
  })

  it('shows the shorter name when the whole one does not fit, dropping a dangling "&"', () => {
    expect(topicLines('Userscriptkit & Kirocrewteamcapabilities', w)).toEqual(['Userscriptkit'])
    expect(topicLines('Supercalifragilisticexpialidocious', w)).toEqual(['Supercalifragilisticexpialidocious'])
  })

  it('names topics in spiral order: from the middle of the map at the overview, from the one in view zoomed in', () => {
    const size = { width: 400, height: 400 }
    const clusters = [{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: -20, y: 0 }, { x: 40, y: 0 }]
    const bounds = [-50, -50, 50, 50] as [number, number, number, number]
    // Overview: walking out from the layout's middle.
    expect(topicOrder(clusters, { cx: 0, cy: 0, k: 4 }, size, bounds, 1)).toEqual([0, 1, 2, 3])
    // Zoomed in over x = 40: that topic first, then outward from it.
    expect(topicOrder(clusters, { cx: 38, cy: 0, k: 40 }, size, bounds, LEVEL_STEP)).toEqual([2, 1, 3, 0])
  })

  it('spreads names over the whole phone view, one per grid cell, each on its own disc', () => {
    const size = { width: 316, height: 418 }
    // A 7 x 9 lattice of discs over the view, walked from the middle outward.
    const discs = Array.from({ length: 63 }, (_, i) => ({ x: 20 + (i % 7) * 46, y: 20 + Math.floor(i / 7) * 47 }))
    const order = topicOrder(discs, { cx: 158, cy: -209, k: 1 }, size, [0, -418, 316, 0], 1)
    const w = (line: string) => line.length * 8
    const cands = discs.map((at, i) => {
      const lines = topicLines(`Topic ${i} & More`, w, topicLinePx(size.width))
      return { id: `c${i}`, at, text: lines.join(' '), lines, rank: -order[i], centred: true }
    })
    const grid = labelGrid(size, topicLinePx(size.width), 37)
    const placed = placeLabels(cands, size, 12, w, [], grid)
    const cell = (p: { x: number; y: number }) => Math.floor(p.y / (size.height / grid.rows)) * grid.cols + Math.floor(p.x / (size.width / grid.cols))
    expect(new Set(placed.map(l => cell(l.at))).size).toBe(placed.length)
    expect(placed.some(l => l.at.x < size.width / 3)).toBe(true)
    expect(placed.some(l => l.at.x > (2 * size.width) / 3)).toBe(true)
    // No name under the zoom controls' corner.
    const controls = { x: 256, y: 258, width: 60, height: 160 }
    const under = placeLabels(cands, size, 12, w, [], grid, [controls])
    expect(under.some(l => l.box.x + l.box.width > controls.x && l.box.y + l.box.height > controls.y)).toBe(false)
    // Each name sits on its own disc: the box holds its anchor.
    for (const l of placed) expect(l.at.x >= l.box.x && l.at.x <= l.box.x + l.box.width).toBe(true)
  })

  it('skips a name that would touch one placed before it, keeping the walk order', () => {
    const size = { width: 400, height: 400 }
    const cands = [200, 210, 330].map((x, i) => ({ id: `c${i}`, at: { x, y: 200 }, text: `T${i}`, lines: [`T${i}`], rank: -i, centred: true }))
    expect(placeLabels(cands, size, 12, () => 60).map(l => l.id)).toEqual(['c0', 'c2'])
  })
})
