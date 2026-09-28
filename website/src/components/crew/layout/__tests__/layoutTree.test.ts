/**
 * The layout-tree model: round-trip, versioning, and the drop-node BC rule.
 *
 * The contract under test is RFC §5.5: a stored layout survives a serialize ⇄
 * parse round-trip; an unknown envelope version is refused (so the caller falls
 * through to the floor); and a node referencing an element kind that no longer
 * exists is DROPPED rather than crashing the parse.
 */
import { describe, it, expect } from 'vitest'
import {
  LAYOUT_VERSION,
  parseLayout,
  serializeLayout,
  isKnownElement,
  type LayoutTree,
} from '../layoutTree'

let n = 0
const newId = () => `id-${n++}`

const sample: LayoutTree = {
  version: LAYOUT_VERSION,
  root: {
    kind: 'group',
    id: 'root',
    dir: 'row',
    sizes: [1, 3],
    children: [
      { kind: 'cell', id: 'thread', element: 'chat', config: { agentLocked: true } },
      { kind: 'cell', id: 'panel', element: 'sidePanel' },
    ],
  },
}

describe('layoutTree', () => {
  it('round-trips through serialize → parse', () => {
    const res = parseLayout(serializeLayout(sample), newId)
    expect(res.ok).toBe(true)
    if (res.ok) expect(res.tree).toEqual(sample)
  })

  it('round-trips a grid root with a spanning cell', () => {
    const grid: LayoutTree = {
      version: LAYOUT_VERSION,
      root: {
        kind: 'grid',
        id: 'root',
        cols: 3,
        rows: 2,
        colSizes: [2, 1, 1],
        rowSizes: [1, 1],
        children: [
          { x: 0, y: 0, w: 2, h: 2, node: { kind: 'cell', id: 'a', element: 'chat' } },
          { x: 2, y: 0, w: 1, h: 1, node: { kind: 'cell', id: 'b', element: 'sidePanel' } },
          { x: 2, y: 1, w: 1, h: 1, node: { kind: 'cell', id: 'c', element: 'files' } },
        ],
      },
    }
    const res = parseLayout(serializeLayout(grid), newId)
    expect(res.ok).toBe(true)
    if (res.ok) expect(res.tree).toEqual(grid)
  })

  it('refuses an unknown envelope version', () => {
    const raw = JSON.stringify({ version: 999, root: sample.root })
    const res = parseLayout(raw, newId)
    expect(res.ok).toBe(false)
  })

  it('drops a cell with an unknown element kind, keeping its siblings', () => {
    const raw = JSON.stringify({
      version: LAYOUT_VERSION,
      root: {
        kind: 'group',
        id: 'root',
        dir: 'row',
        children: [
          { kind: 'cell', id: 'a', element: 'chat' },
          { kind: 'cell', id: 'b', element: 'no-such-element' },
          { kind: 'cell', id: 'c', element: 'sidePanel' },
        ],
      },
    })
    const res = parseLayout(raw, newId)
    expect(res.ok).toBe(true)
    if (res.ok && res.tree.root.kind === 'group') {
      expect(res.tree.root.children.map((c) => (c.kind === 'cell' ? c.element : c.kind))).toEqual([
        'chat',
        'sidePanel',
      ])
    }
  })

  it('rejects malformed JSON', () => {
    expect(parseLayout('{not json', newId).ok).toBe(false)
  })

  it('recognizes only the known element kinds', () => {
    expect(isKnownElement('chat')).toBe(true)
    expect(isKnownElement('sidePanel')).toBe(true)
    expect(isKnownElement('git')).toBe(true)
    expect(isKnownElement('terminal')).toBe(true)
    expect(isKnownElement('roster')).toBe(false)
    expect(isKnownElement('no-such-view')).toBe(false)
  })
})

/**
 * Characterization tests for `normalizeNode` / `normalizePlaced` (private, so
 * exercised through `parseLayout`). These pin the DEFENSIVE normalization
 * branches — bad node kind, bad group dir, id-minting, tabs recursion, grid
 * clamping, rect defaults, junk-child filtering — that the round-trip and
 * drop-unknown-element tests above never reach. RFC §5.5: a hand-edited or
 * drifted stored layout normalizes or drops, never crashes.
 */
describe('normalizeNode (via parseLayout)', () => {
  const parse = (root: unknown, version = LAYOUT_VERSION, mkId = newId) =>
    parseLayout(JSON.stringify({ version, root }), mkId)

  it('drops a node with an unknown kind', () => {
    const res = parse({ kind: 'splitter', id: 'x' })
    expect(res.ok).toBe(false) // root dropped → whole parse fails to the floor
  })

  it('drops a container child whose node kind is unknown, keeping valid siblings', () => {
    const res = parse({
      kind: 'group',
      id: 'root',
      dir: 'row',
      children: [
        { kind: 'cell', id: 'a', element: 'chat' },
        { kind: 'splitter', id: 'bad' },
        { kind: 'cell', id: 'c', element: 'files' },
      ],
    })
    expect(res.ok).toBe(true)
    if (res.ok && res.tree.root.kind === 'group') {
      expect(res.tree.root.children.map((c) => (c.kind === 'cell' ? c.element : c.kind))).toEqual([
        'chat',
        'files',
      ])
    }
  })

  it('drops a group whose dir is neither row nor col', () => {
    const res = parse({ kind: 'group', id: 'root', dir: 'diagonal', children: [] })
    expect(res.ok).toBe(false)
  })

  it('mints a fresh id when a node omits one', () => {
    let minted = 0
    const mkId = () => `minted-${minted++}`
    const res = parse({ kind: 'cell', element: 'chat' }, LAYOUT_VERSION, mkId)
    expect(res.ok).toBe(true)
    if (res.ok && res.tree.root.kind === 'cell') {
      expect(res.tree.root.id).toBe('minted-0')
    }
    expect(minted).toBe(1)
  })

  it('mints a fresh id when a node has a blank id', () => {
    const mkId = () => 'minted-blank'
    const res = parse({ kind: 'cell', id: '', element: 'chat' }, LAYOUT_VERSION, mkId)
    expect(res.ok).toBe(true)
    if (res.ok && res.tree.root.kind === 'cell') expect(res.tree.root.id).toBe('minted-blank')
  })

  it('normalizes a tabs node, recursing into children and keeping active', () => {
    const res = parse({
      kind: 'tabs',
      id: 'root',
      active: 1,
      children: [
        { kind: 'cell', id: 't0', element: 'notes' },
        { kind: 'cell', id: 't1', element: 'workLog' },
      ],
    })
    expect(res.ok).toBe(true)
    if (res.ok && res.tree.root.kind === 'tabs') {
      expect(res.tree.root.active).toBe(1)
      expect(res.tree.root.children.map((c) => (c.kind === 'cell' ? c.element : c.kind))).toEqual([
        'notes',
        'workLog',
      ])
    }
  })

  it('drops a tabs child with an unknown element, keeping the rest', () => {
    const res = parse({
      kind: 'tabs',
      id: 'root',
      children: [
        { kind: 'cell', id: 't0', element: 'chat' },
        { kind: 'cell', id: 't1', element: 'no-such-element' },
      ],
    })
    expect(res.ok).toBe(true)
    if (res.ok && res.tree.root.kind === 'tabs') {
      expect(res.tree.root.children.map((c) => (c.kind === 'cell' ? c.element : c.kind))).toEqual([
        'chat',
      ])
    }
  })

  it('clamps grid cols/rows below 1 (or missing) up to 1', () => {
    const res = parse({ kind: 'grid', id: 'root', cols: 0, rows: -2, children: [] })
    expect(res.ok).toBe(true)
    if (res.ok && res.tree.root.kind === 'grid') {
      expect(res.tree.root.cols).toBe(1)
      expect(res.tree.root.rows).toBe(1)
    }
  })

  it('defaults a placed rect (x=0,y=0,w=1,h=1) when fields are missing', () => {
    const res = parse({
      kind: 'grid',
      id: 'root',
      cols: 2,
      rows: 2,
      children: [{ node: { kind: 'cell', id: 'a', element: 'chat' } }],
    })
    expect(res.ok).toBe(true)
    if (res.ok && res.tree.root.kind === 'grid') {
      expect(res.tree.root.children[0]).toMatchObject({ x: 0, y: 0, w: 1, h: 1 })
    }
  })

  it('drops a placed child whose nested node is malformed', () => {
    const res = parse({
      kind: 'grid',
      id: 'root',
      cols: 2,
      rows: 1,
      children: [
        { x: 0, y: 0, w: 1, h: 1, node: { kind: 'cell', id: 'a', element: 'chat' } },
        { x: 1, y: 0, w: 1, h: 1, node: { kind: 'splitter', id: 'bad' } },
      ],
    })
    expect(res.ok).toBe(true)
    if (res.ok && res.tree.root.kind === 'grid') {
      expect(res.tree.root.children).toHaveLength(1)
    }
  })

  it('filters non-object junk out of a container children array', () => {
    const res = parse({
      kind: 'group',
      id: 'root',
      dir: 'col',
      children: [{ kind: 'cell', id: 'a', element: 'chat' }, null, 42, 'nope'],
    })
    expect(res.ok).toBe(true)
    if (res.ok && res.tree.root.kind === 'group') {
      expect(res.tree.root.children).toHaveLength(1)
    }
  })

  it('drops a sizes array containing a non-finite number', () => {
    const res = parse({
      kind: 'group',
      id: 'root',
      dir: 'row',
      sizes: [1, null, 3],
      children: [{ kind: 'cell', id: 'a', element: 'chat' }],
    })
    expect(res.ok).toBe(true)
    if (res.ok && res.tree.root.kind === 'group') expect(res.tree.root.sizes).toBeUndefined()
  })

  it('floors a fractional grid dimension to an integer', () => {
    const res = parse({ kind: 'grid', id: 'root', cols: 2.9, rows: 1, children: [] })
    expect(res.ok).toBe(true)
    if (res.ok && res.tree.root.kind === 'grid') expect(res.tree.root.cols).toBe(2)
  })
})

/**
 * Regression tests for three normalization gaps flagged in review (all
 * `crash-data-loss-corruption` class): a stale tabs `active` index surviving a
 * dropped child (F1), a non-finite / out-of-range placed rect corrupting
 * geometry (F2), and duplicate stored ids letting one edit hit several panes
 * (F3). Each asserts the SPECIFIC corruption is neutralized, not merely that
 * parse succeeds.
 */
describe('normalizeNode — review findings (data-loss guards)', () => {
  const parse = (root: unknown, version = LAYOUT_VERSION, mkId = newId) =>
    parseLayout(JSON.stringify({ version, root }), mkId)

  // ── F1: tabs `active` must stay a valid index after children are dropped ──

  it('remaps tabs active to the selected surviving child after an earlier child is dropped', () => {
    const res = parse({
      kind: 'tabs',
      id: 'root',
      active: 1,
      children: [
        { kind: 'cell', id: 't0', element: 'no-such-element' }, // dropped
        { kind: 'cell', id: 't1', element: 'notes' },
        { kind: 'cell', id: 't2', element: 'chat' },
      ],
    })
    expect(res.ok).toBe(true)
    if (res.ok && res.tree.root.kind === 'tabs') {
      expect(res.tree.root.children[res.tree.root.active ?? -1]?.id).toBe('t1')
      expect(res.tree.root.active).toBe(0)
    }
  })

  it('drops tabs active when the selected child was dropped', () => {
    const res = parse({
      kind: 'tabs',
      id: 'root',
      active: 1,
      children: [
        { kind: 'cell', id: 't0', element: 'chat' },
        { kind: 'cell', id: 't1', element: 'no-such-element' }, // dropped
        { kind: 'cell', id: 't2', element: 'notes' },
      ],
    })
    expect(res.ok).toBe(true)
    if (res.ok && res.tree.root.kind === 'tabs') {
      expect(res.tree.root.active).toBeUndefined()
    }
  })

  it('filters group sizes to the children that survived normalization', () => {
    const res = parse({
      kind: 'group',
      id: 'root',
      dir: 'row',
      sizes: [1, 2, 3],
      children: [
        { kind: 'cell', id: 'g0', element: 'no-such-element' }, // dropped
        { kind: 'cell', id: 'g1', element: 'chat' },
        { kind: 'cell', id: 'g2', element: 'notes' },
      ],
    })
    expect(res.ok).toBe(true)
    if (res.ok && res.tree.root.kind === 'group') {
      expect(res.tree.root.children.map((child) => child.id)).toEqual(['g1', 'g2'])
      expect(res.tree.root.sizes).toEqual([2, 3])
    }
  })

  it('clamps a negative / fractional / NaN tabs active into range', () => {
    for (const bad of [-1, 1.9, Number.NaN, Number.POSITIVE_INFINITY]) {
      const res = parse({
        kind: 'tabs',
        id: 'root',
        active: bad,
        children: [
          { kind: 'cell', id: 't0', element: 'chat' },
          { kind: 'cell', id: 't1', element: 'notes' },
        ],
      })
      expect(res.ok).toBe(true)
      if (res.ok && res.tree.root.kind === 'tabs') {
        const a = res.tree.root.active
        expect(a === undefined || (Number.isInteger(a) && a >= 0 && a <= 1)).toBe(true)
      }
    }
  })

  it('drops tabs active entirely when every child was dropped', () => {
    const res = parse({
      kind: 'tabs',
      id: 'root',
      active: 0,
      children: [{ kind: 'cell', id: 't0', element: 'no-such-element' }],
    })
    expect(res.ok).toBe(true)
    if (res.ok && res.tree.root.kind === 'tabs') {
      expect(res.tree.root.children).toHaveLength(0)
      expect(res.tree.root.active).toBeUndefined()
    }
  })

  // ── F2: a placed rect must be finite integers; dims ≥ 1 ──

  it('replaces a non-finite placed rect with finite defaults', () => {
    const res = parse({
      kind: 'grid',
      id: 'root',
      cols: 4,
      rows: 4,
      children: [
        {
          x: Number.POSITIVE_INFINITY,
          y: Number.NaN,
          w: Number.POSITIVE_INFINITY,
          h: 0,
          node: { kind: 'cell', id: 'a', element: 'chat' },
        },
      ],
    })
    expect(res.ok).toBe(true)
    if (res.ok && res.tree.root.kind === 'grid') {
      const p = res.tree.root.children[0]
      for (const v of [p.x, p.y, p.w, p.h]) expect(Number.isFinite(v)).toBe(true)
      expect(p.x).toBe(0)
      expect(p.y).toBe(0)
      expect(p.w).toBeGreaterThanOrEqual(1)
      expect(p.h).toBeGreaterThanOrEqual(1)
    }
  })

  it('floors a fractional rect and forces non-negative coords / ≥1 dims', () => {
    const res = parse({
      kind: 'grid',
      id: 'root',
      cols: 4,
      rows: 4,
      children: [{ x: -3, y: 1.9, w: 2.7, h: 0, node: { kind: 'cell', id: 'a', element: 'chat' } }],
    })
    expect(res.ok).toBe(true)
    if (res.ok && res.tree.root.kind === 'grid') {
      expect(res.tree.root.children[0]).toMatchObject({ x: 0, y: 1, w: 2, h: 1 })
    }
  })

  // ── F3: duplicate stored ids must be made unique ──

  it('rewrites a duplicate stored id so no two nodes share one', () => {
    let m = 0
    const mkId = () => `fresh-${m++}`
    const res = parse(
      {
        kind: 'grid',
        id: 'root',
        cols: 2,
        rows: 1,
        children: [
          { x: 0, y: 0, w: 1, h: 1, node: { kind: 'cell', id: 'dupe', element: 'chat' } },
          { x: 1, y: 0, w: 1, h: 1, node: { kind: 'cell', id: 'dupe', element: 'notes' } },
        ],
      },
      LAYOUT_VERSION,
      mkId,
    )
    expect(res.ok).toBe(true)
    if (res.ok && res.tree.root.kind === 'grid') {
      const ids = res.tree.root.children.map((c) => c.node.id)
      expect(new Set(ids).size).toBe(ids.length) // all unique
      expect(ids).toContain('dupe') // first keeps its id
      expect(ids.filter((i) => i === 'dupe')).toHaveLength(1) // second was rewritten
    }
  })

  it('does not reserve the id of a node dropped during normalization', () => {
    let m = 0
    const mkId = () => `fresh-${m++}`
    const res = parse(
      {
        kind: 'group',
        id: 'root',
        dir: 'row',
        children: [
          { kind: 'cell', id: 'panel', element: 'no-such-element' }, // dropped
          { kind: 'cell', id: 'panel', element: 'sidePanel' },
        ],
      },
      LAYOUT_VERSION,
      mkId,
    )
    expect(res.ok).toBe(true)
    if (res.ok && res.tree.root.kind === 'group') {
      expect(res.tree.root.children).toHaveLength(1)
      expect(res.tree.root.children[0].id).toBe('panel')
    }
  })

  it('rewrites a duplicate id even against a nested container', () => {
    let m = 0
    const mkId = () => `fresh-${m++}`
    const res = parse(
      {
        kind: 'group',
        id: 'root',
        dir: 'row',
        children: [
          { kind: 'cell', id: 'shared', element: 'chat' },
          {
            kind: 'tabs',
            id: 'shared', // collides with the sibling cell
            children: [{ kind: 'cell', id: 'shared', element: 'notes' }], // and its own child
          },
        ],
      },
      LAYOUT_VERSION,
      mkId,
    )
    expect(res.ok).toBe(true)
    if (res.ok && res.tree.root.kind === 'group') {
      const ids: string[] = []
      const walk = (n: import('../layoutTree').LayoutNode) => {
        ids.push(n.id)
        if (n.kind === 'group' || n.kind === 'tabs') n.children.forEach(walk)
        if (n.kind === 'grid') n.children.forEach((p) => walk(p.node))
      }
      walk(res.tree.root)
      expect(new Set(ids).size).toBe(ids.length)
    }
  })
})
