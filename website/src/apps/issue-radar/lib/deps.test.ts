import { describe, expect, it } from 'vitest'

import type { DepEdge, DepsResponse, Issue, PullRequest } from '../api'
import {
  buildNodes, layoutGraph, readySet, tracePath, lineage, edgeKey, LAYOUT,
  reduceInferred, componentOf, componentModel, satisfiedBlockers,
  frontierReady, frontierWaiting, openItems, defaultRoot,
  type GraphNode,
} from './deps'

/** A minimal deps payload: 5190 (PR) blocks 5191 & 5192 (issues); 5191 blocks
 * 5193 (issue). So 5193 is two layers deep and blocked until 5191 lands. */
function fixture(): DepsResponse {
  return {
    schema: 1,
    fetched_at: '2026-08-23T00:00:00Z',
    edges: [
      { blocker: 5190, blocked: 5191, source: 'native' },
      { blocker: 5190, blocked: 5192, source: 'native' },
      { blocker: 5191, blocked: 5193, source: 'inferred' },
    ],
    nodes: {
      '5190': { kind: 'pull', state: 'open', title: 'deps cache + sync' },
      '5191': { kind: 'issue', state: 'open', title: 'GET /deps route' },
      '5192': { kind: 'issue', state: 'open', title: 'detail-pane section' },
      '5193': { kind: 'issue', state: 'open', title: 'graph tab' },
    },
  }
}

describe('deps.buildNodes — state derivation', () => {
  it('holds a node whose blocker is still open', () => {
    const nodes = buildNodes(fixture(), [], [], new Set())
    // 5191 is blocked by the open PR 5190 → hold.
    expect(nodes.get(5191)!.state).toBe('hold')
    expect(nodes.get(5192)!.state).toBe('hold')
    // 5190 is an open PR with no blockers → plain open source.
    expect(nodes.get(5190)!.state).toBe('open')
    // 5193 blocked by still-open 5191 → hold.
    expect(nodes.get(5193)!.state).toBe('hold')
  })

  it('marks a node ready once every blocker is merged/closed', () => {
    const deps = fixture()
    // 5190 merged (a PR row with merged_at wins over the node map).
    const pulls: PullRequest[] = [{
      number: 5190, title: 'deps cache', url: '', state: 'closed', draft: false,
      labels: [], updated_at: '', merged_at: '2026-08-23T01:00:00Z',
    }]
    const nodes = buildNodes(deps, [], pulls, new Set())
    expect(nodes.get(5190)!.lifecycle).toBe('merged')
    expect(nodes.get(5190)!.state).toBe('done')
    // Its two direct dependents are now unblocked.
    expect(nodes.get(5191)!.state).toBe('ready')
    expect(nodes.get(5192)!.state).toBe('ready')
    // 5193 still waits on 5191 (which is ready, not closed) → hold.
    expect(nodes.get(5193)!.state).toBe('hold')
  })

  it('fires a one-shot unlock only on the transition INTO ready', () => {
    const deps = fixture()
    const pulls: PullRequest[] = [{
      number: 5190, title: '', url: '', state: 'closed', draft: false,
      labels: [], updated_at: '', merged_at: '2026-08-23T01:00:00Z',
    }]
    // prevReady empty → newly-ready nodes pulse.
    const first = buildNodes(deps, [], pulls, new Set())
    expect(first.get(5191)!.unlocked).toBe(true)
    // Feed the ready set back in → no longer a transition, no pulse.
    const second = buildNodes(deps, [], pulls, readySet(first))
    expect(second.get(5191)!.unlocked).toBe(false)
    expect(second.get(5191)!.state).toBe('ready')
  })

  it('prefers live issue/PR titles over the node map', () => {
    const deps = fixture()
    const issues: Issue[] = [{
      number: 5191, title: 'LIVE TITLE', url: '', labels: [], comments: 0, updated_at: '',
    }]
    const nodes = buildNodes(deps, issues, [], new Set())
    expect(nodes.get(5191)!.title).toBe('LIVE TITLE')
  })

  it('assigns topological layers (0 = unblocked source)', () => {
    const nodes = buildNodes(fixture(), [], [], new Set())
    expect(nodes.get(5190)!.layer).toBe(0)
    expect(nodes.get(5191)!.layer).toBe(1)
    expect(nodes.get(5193)!.layer).toBe(2)
  })

  it('does not hang on a dependency cycle', () => {
    const deps: DepsResponse = {
      schema: 1,
      edges: [
        { blocker: 1, blocked: 2, source: 'native' },
        { blocker: 2, blocked: 1, source: 'native' },
      ],
      nodes: {
        '1': { kind: 'issue', state: 'open', title: 'a' },
        '2': { kind: 'issue', state: 'open', title: 'b' },
      },
    }
    const nodes = buildNodes(deps, [], [], new Set())
    // Both resolve to a finite layer (cycle guard returns 0 for the back-edge).
    expect(Number.isFinite(nodes.get(1)!.layer)).toBe(true)
    expect(Number.isFinite(nodes.get(2)!.layer)).toBe(true)
  })

  it('still draws a node referenced only by an edge (partial payload)', () => {
    const deps: DepsResponse = {
      schema: 1,
      edges: [{ blocker: 99, blocked: 100, source: 'native' }],
      nodes: {}, // node map empty — both numbers come from the edge
    }
    const nodes = buildNodes(deps, [], [], new Set())
    expect(nodes.has(99)).toBe(true)
    expect(nodes.has(100)).toBe(true)
  })
})

describe('deps.layoutGraph — overlap-free channel routing', () => {
  it('places each layer in its own column and sizes chips by fan-out', () => {
    const nodes = buildNodes(fixture(), [], [], new Set())
    const layout = layoutGraph(nodes)
    const b5190 = layout.nodes.get(5190)!
    const b5191 = layout.nodes.get(5191)!
    // Layer 1 sits one COL to the right of layer 0.
    expect(b5191.x - b5190.x).toBe(LAYOUT.COL)
    // 5190 has 2 outgoing edges → height driven by fan-out (>= 2 pins).
    expect(b5190.h).toBeGreaterThanOrEqual(LAYOUT.MIN_H)
  })

  it('gives every edge in a column gap a DISTINCT channel X (zero overlap)', () => {
    const nodes = buildNodes(fixture(), [], [], new Set())
    const layout = layoutGraph(nodes)
    // The two edges out of 5190 share a column gap; their channels must differ.
    const e1 = layout.edges.find((e) => e.key === edgeKey({ blocker: 5190, blocked: 5191, source: 'native' }))!
    const e2 = layout.edges.find((e) => e.key === edgeKey({ blocker: 5190, blocked: 5192, source: 'native' }))!
    expect(e1.cx).not.toBe(e2.cx)
  })

  it('emits an H-V-H Manhattan trace path', () => {
    const nodes = buildNodes(fixture(), [], [], new Set())
    const layout = layoutGraph(nodes)
    const g = layout.edges[0]
    // M<sx> <sy> H<cx> V<ty> H<tx> — horizontal, vertical, horizontal.
    expect(tracePath(g)).toMatch(/^M[\d.]+ [\d.]+ H[\d.]+ V[\d.]+ H[\d.]+$/)
  })
})

describe('deps.lineage', () => {
  it('walks the full transitive net of a node', () => {
    const deps = fixture()
    // 5190 reaches 5191, 5192, and (through 5191) 5193.
    expect(lineage(5190, deps.edges)).toEqual(new Set([5190, 5191, 5192, 5193]))
  })
})

describe('componentOf / componentModel', () => {
  const mk = (id: number, state: 'hold' | 'ready' | 'done' | 'open' = 'open') =>
    [id, { id, kind: 'issue', title: `t${id}`, lifecycle: state === 'done' ? 'closed' : 'open', state, layer: 0, ins: [], outs: [], ciFailed: false, unlocked: false } as GraphNode] as const
  const edges = [
    { blocked: 2, blocker: 1, source: 'native' },
    { blocked: 3, blocker: 2, source: 'native' },
    { blocked: 11, blocker: 10, source: 'inferred' }, // separate component
  ] as DepEdge[]

  it('walks the whole connected component of a seed', () => {
    expect(componentOf(2, edges)).toEqual(new Set([1, 2, 3]))
    expect(componentOf(11, edges)).toEqual(new Set([10, 11]))
  })

  it('componentModel recomputes layers locally from the component edges', () => {
    const nodes = new Map([mk(1), mk(2), mk(3)])
    const ids = componentOf(3, edges)
    const compEdges = edges.filter((e) => ids.has(e.blocker) && ids.has(e.blocked))
    const sub = componentModel(ids, compEdges, nodes)
    expect(sub.nodes.get(1)!.layer).toBe(0)
    expect(sub.nodes.get(2)!.layer).toBe(1)
    expect(sub.nodes.get(3)!.layer).toBe(2)
    // source model untouched
    expect(nodes.get(3)!.layer).toBe(0)
  })

  it('exposes the root satisfied direct blockers as ghosts', () => {
    const nodes = new Map([mk(1, 'done'), mk(2, 'ready'), mk(3, 'hold')])
    // 2 is blocked by done #1
    nodes.get(2)!.ins = [{ blocked: 2, blocker: 1, source: 'native' }]
    expect(satisfiedBlockers(2, nodes).map((n) => n.id)).toEqual([1])
    expect(satisfiedBlockers(3, nodes)).toEqual([])
  })
})

describe('frontier', () => {
  const deps = fixture()
  const mergedPull: PullRequest[] = [{
    number: 5190, title: '', url: '', state: 'closed', draft: false,
    labels: [], updated_at: '', merged_at: '2026-08-23T01:00:00Z',
  }]

  it('lists newly unlocked = open, >=1 blocker on record, all satisfied', () => {
    const nodes = buildNodes(deps, [], mergedPull, new Set())
    // 5191 and 5192 both had blocker 5190 (now merged) → newly unlocked.
    expect(frontierReady(nodes).map((f) => f.node.id)).toEqual([5191, 5192])
  })

  it('excludes zero-blocker ready nodes from newly unlocked', () => {
    // A lone open issue with no blockers is buildNodes-'ready' but NOT frontier.
    const bare: DepsResponse = {
      schema: 1, edges: [], nodes: { '7': { kind: 'issue', state: 'open', title: 'lone' } },
    }
    const nodes = buildNodes(bare, [], [], new Set())
    expect(nodes.get(7)!.state).toBe('ready')
    expect(frontierReady(nodes)).toHaveLength(0)
  })

  it('lists waiting = open items with >=1 still-open blocker, with waitingOn', () => {
    const nodes = buildNodes(deps, [], [], new Set())
    const waiting = frontierWaiting(nodes)
    // all three dependents wait while 5190 is still open
    expect(waiting.map((f) => f.node.id)).toEqual([5191, 5192, 5193])
    const f5191 = waiting.find((f) => f.node.id === 5191)!
    expect(f5191.waitingOn.map((n) => n.id)).toEqual([5190])
  })

  it('openItems and defaultRoot pick a sensible starting root', () => {
    const nodes = buildNodes(deps, [], mergedPull, new Set())
    expect(openItems(nodes).map((n) => n.id)).toEqual([5191, 5192, 5193])
    // a newly-unlocked item is preferred as the default root
    expect(defaultRoot(nodes)).toBe(5191)
  })
})

describe('reduceInferred', () => {
  it('drops an inferred edge already implied by a longer path, keeps native', () => {
    const edges = [
      { blocked: 2, blocker: 1, source: 'inferred' },
      { blocked: 3, blocker: 2, source: 'inferred' },
      { blocked: 3, blocker: 1, source: 'inferred' }, // implied by 1->2->3
      { blocked: 5, blocker: 4, source: 'native' },
      { blocked: 5, blocker: 1, source: 'native' }, // native never drops
    ] as DepEdge[]
    const out = reduceInferred(edges)
    expect(out).toHaveLength(4)
    expect(out.find((e) => e.blocked === 3 && e.blocker === 1)).toBeUndefined()
    expect(out.filter((e) => e.source === 'native')).toHaveLength(2)
  })
})

describe('barycenter ordering', () => {
  it('keeps parallel chains in consistent row bands (no full-column crossings)', () => {
    // Two parallel chains: 1->10->100 and 2->20->200. Plain id-order interleaves
    // them; barycenter keeps each chain on its own side of the column.
    const mkN = (id: number, layer: number, ins: DepEdge[], outs: DepEdge[]) =>
      [id, { id, kind: 'issue', title: `t${id}`, state: 'open', lifecycle: 'open', layer, ins, outs } as unknown as GraphNode] as const
    const e = (blocker: number, blocked: number) => ({ blocker, blocked, source: 'native' }) as DepEdge
    const e1 = e(1, 10), e2 = e(10, 100), e3 = e(2, 20), e4 = e(20, 200)
    const nodes = new Map([
      mkN(2, 0, [], [e3]), mkN(1, 0, [], [e1]),
      mkN(20, 1, [e3], [e4]), mkN(10, 1, [e1], [e2]),
      mkN(200, 2, [e4], []), mkN(100, 2, [e2], []),
    ])
    const L = layoutGraph(nodes)
    const y = (id: number) => L.nodes.get(id)!.y
    expect(Math.sign(y(10) - y(20))).toBe(Math.sign(y(1) - y(2)))
    expect(Math.sign(y(100) - y(200))).toBe(Math.sign(y(10) - y(20)))
  })
})
