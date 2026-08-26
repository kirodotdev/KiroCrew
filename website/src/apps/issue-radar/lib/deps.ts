// Dependency graph — the pure half of the Graph dashboard tab (#5187 M2).
//
// Everything here is dependency-free (no React, no DOM, no theme) so the layout
// algorithm and the node-state derivation are directly unit-testable, exactly
// like `refLinks.ts`. The view (`views/GraphView.tsx`) is the impure half: it
// reads this module's output and paints it as the PCB "dependency fabric".
//
// The layout is ported faithfully from the approved "Circuit rev B" mockup:
//   - topological layering (a node's layer = 1 + max blocker layer),
//   - one PIN per edge on each chip, at a fixed pitch, sorted by counterpart Y
//     to minimise crossings,
//   - one EXCLUSIVE vertical CHANNEL per edge in each column gap, evenly
//     distributed and sorted by source-pin Y,
//   - H-V-H Manhattan traces (out-pin -> channel -> in-pin).
// Overlap-free by construction: no two traces share a vertical segment because
// every edge owns its own channel lane.

import type { DepEdge, DepsResponse, Issue, PullRequest } from '../api'

/** The derived state of a node in the fabric, in the mockup's vocabulary:
 *  - `hold`   — blocked: at least one blocker is still open/unmerged.
 *  - `ready`  — unblocked and open: every blocker is closed/merged (the "go" hue).
 *  - `running`— an open item a crew is actively working (reserved; the graph
 *               only knows crew ownership if a caller supplies it, so today this
 *               is produced only when `crew` is set on the node).
 *  - `done`   — the item itself is closed/merged (an etched "MERGED" ghost).
 *  - `open`   — an open PR that is neither a blocker-satisfied target nor blocked
 *               (a plain open source with no unmet constraint above it). */
export type NodeState = 'hold' | 'ready' | 'running' | 'done' | 'open'

/** A node kind, mirroring the deps payload's `nodes[].kind`. */
export type NodeKind = 'issue' | 'pull'

/** A fully-resolved graph node: the payload's node joined with live list state
 * and the derived fabric state, plus its topological position metadata. */
export interface GraphNode {
  id: number
  kind: NodeKind
  title: string
  /** Raw lifecycle state from the join: 'open' | 'closed' | 'merged'. */
  lifecycle: 'open' | 'closed' | 'merged'
  /** Derived fabric state (drives colour + LED label). */
  state: NodeState
  /** CI rollup for a PR, when the live row carries one — drives the CI FAIL badge. */
  ciFailed: boolean
  /** Topological layer (0 = an unblocked source at the far left). */
  layer: number
  /** Incoming edges (this node is `blocked`; the edge's `blocker` is upstream). */
  ins: DepEdge[]
  /** Outgoing edges (this node is a `blocker`; the edge's `blocked` is downstream). */
  outs: DepEdge[]
  /** True when this node's state flipped TO `ready` since the previous fetch —
   * fires the one-shot unlock pulse. */
  unlocked: boolean
}

/** A laid-out edge: its endpoints (pin coordinates) and its exclusive channel X. */
export interface EdgeGeometry {
  key: string
  edge: DepEdge
  /** Source out-pin (right edge of the blocker chip). */
  sx: number
  sy: number
  /** Target in-pin (left edge of the blocked chip). */
  tx: number
  ty: number
  /** The edge's own vertical channel X in the column gap. */
  cx: number
}

/** A laid-out node box. */
export interface NodeGeometry {
  x: number
  y: number
  w: number
  h: number
}

/** The complete layout: node boxes, edge traces, and the overall canvas size. */
export interface GraphLayout {
  nodes: Map<number, NodeGeometry>
  edges: EdgeGeometry[]
  width: number
  height: number
}

/** Layout constants, ported from the mockup. Exported so the view and the tests
 * agree on the exact geometry. */
export const LAYOUT = {
  CHIP_W: 176,
  PIN: 14,
  COL: 372,
  X0: 72,
  Y0: 96,
  ROWGAP: 30,
  /** Minimum chip height (a chip with <=2 pins still gets this). */
  MIN_H: 52,
} as const

/** Stable key for an edge (also the React key and the channel-map key). */
export function edgeKey(e: DepEdge): string {
  return `${e.blocker}>${e.blocked}`
}

/** True when a lifecycle state counts as "satisfied" for unblocking purposes:
 * a blocker is met once it is closed or merged. */
function blockerSatisfied(lifecycle: 'open' | 'closed' | 'merged'): boolean {
  return lifecycle === 'closed' || lifecycle === 'merged'
}

/** Resolve the lifecycle of one node number from the live list rows first
 * (authoritative, fresh), falling back to the deps payload's node map.
 *
 * A merged PR is 'closed' on GitHub with a merge timestamp, so the PR row's
 * `merged_at` is what distinguishes merged from closed-unmerged. */
function resolveLifecycle(
  n: number,
  deps: DepsResponse,
  issueByNumber: Map<number, Issue>,
  pullByNumber: Map<number, PullRequest>,
): { kind: NodeKind; title: string; lifecycle: 'open' | 'closed' | 'merged'; ciFailed: boolean } {
  const node = deps.nodes[String(n)]
  const pull = pullByNumber.get(n)
  if (pull) {
    const lifecycle: 'open' | 'closed' | 'merged' =
      pull.merged_at ? 'merged' : pull.state === 'closed' ? 'closed' : 'open'
    return {
      kind: 'pull',
      title: pull.title || node?.title || '',
      lifecycle,
      ciFailed: pull.checks_state === 'failure',
    }
  }
  const issue = issueByNumber.get(n)
  if (issue) {
    const lifecycle: 'open' | 'closed' | 'merged' = issue.state === 'closed' ? 'closed' : 'open'
    return { kind: 'issue', title: issue.title || node?.title || '', lifecycle, ciFailed: false }
  }
  // Fall back entirely to the payload's node map.
  if (node) {
    return { kind: node.kind, title: node.title, lifecycle: node.state, ciFailed: false }
  }
  // A number referenced by an edge but absent from the node map — treat as an
  // open issue so it is still drawn (the backend should always populate it, but
  // the view must not crash on a partial payload).
  return { kind: 'issue', title: '', lifecycle: 'open', ciFailed: false }
}

/** Every node number that appears in the payload (node map ∪ edge endpoints). */
function allNodeNumbers(deps: DepsResponse): number[] {
  const set = new Set<number>()
  for (const k of Object.keys(deps.nodes)) {
    const n = Number(k)
    if (Number.isSafeInteger(n)) set.add(n)
  }
  for (const e of deps.edges) {
    set.add(e.blocker)
    set.add(e.blocked)
  }
  return [...set]
}

/**
 * Build the resolved graph nodes with derived states.
 *
 * `prevReady` is the set of node numbers that were `ready` on the PREVIOUS
 * fetch; a node that is `ready` now but was NOT in `prevReady` is `unlocked`
 * (fires the one-shot pulse). Pass `null` on the very first observation —
 * an unknown previous state must seed silently, never pulse.
 */
export function buildNodes(
  deps: DepsResponse,
  issues: Issue[],
  pulls: PullRequest[],
  prevReady: ReadonlySet<number> | null,
): Map<number, GraphNode> {
  const issueByNumber = new Map(issues.map((i) => [i.number, i]))
  const pullByNumber = new Map(pulls.map((p) => [p.number, p]))

  const numbers = allNodeNumbers(deps)
  const insByNode = new Map<number, DepEdge[]>()
  const outsByNode = new Map<number, DepEdge[]>()
  for (const n of numbers) {
    insByNode.set(n, [])
    outsByNode.set(n, [])
  }
  for (const e of deps.edges) {
    insByNode.get(e.blocked)?.push(e)
    outsByNode.get(e.blocker)?.push(e)
  }

  // First pass: lifecycle for every node (needed to derive blocked/ready).
  const resolved = new Map<number, ReturnType<typeof resolveLifecycle>>()
  for (const n of numbers) {
    resolved.set(n, resolveLifecycle(n, deps, issueByNumber, pullByNumber))
  }

  // Second pass: derive fabric state. An open item is `hold` if any blocker is
  // still open, else `ready`; an open PR with no incoming edges is `open`.
  const nodes = new Map<number, GraphNode>()
  for (const n of numbers) {
    const r = resolved.get(n)!
    const ins = insByNode.get(n) ?? []
    const outs = outsByNode.get(n) ?? []
    let state: NodeState
    if (r.lifecycle !== 'open') {
      state = 'done'
    } else {
      const anyUnmet = ins.some((e) => !blockerSatisfied(resolved.get(e.blocker)!.lifecycle))
      if (ins.length === 0) {
        // An unblocked open source: a PR reads as a plain `open` source, an issue
        // with no blockers is `ready` (nothing gates it).
        state = r.kind === 'pull' ? 'open' : 'ready'
      } else {
        state = anyUnmet ? 'hold' : 'ready'
      }
    }
    nodes.set(n, {
      id: n,
      kind: r.kind,
      title: r.title,
      lifecycle: r.lifecycle,
      state,
      ciFailed: r.ciFailed,
      layer: 0, // filled below
      ins,
      outs,
      unlocked: state === 'ready' && prevReady != null && !prevReady.has(n),
    })
  }

  // Topological layer: 1 + max blocker layer (0 for an unblocked source).
  // Memoised with cycle protection — a dependency cycle should not hang the UI.
  const layerCache = new Map<number, number>()
  const computing = new Set<number>()
  const layerOf = (id: number): number => {
    const cached = layerCache.get(id)
    if (cached !== undefined) return cached
    if (computing.has(id)) return 0 // cycle guard
    computing.add(id)
    const ins = insByNode.get(id) ?? []
    const layer = ins.length ? 1 + Math.max(...ins.map((e) => layerOf(e.blocker))) : 0
    computing.delete(id)
    layerCache.set(id, layer)
    return layer
  }
  for (const node of nodes.values()) node.layer = layerOf(node.id)

  return nodes
}

/** The set of node numbers currently in the `ready` state — persist this between
 * fetches to drive the next fetch's unlock detection. */
export function readySet(nodes: Map<number, GraphNode>): Set<number> {
  const s = new Set<number>()
  for (const n of nodes.values()) if (n.state === 'ready') s.add(n.id)
  return s
}

/**
 * Lay the graph out: chip boxes, pins, exclusive channels, Manhattan traces.
 * Overlap-free by construction (see the module header).
 */
export function layoutGraph(nodes: Map<number, GraphNode>): GraphLayout {
  const { CHIP_W, PIN, COL, X0, Y0, ROWGAP, MIN_H } = LAYOUT

  // Group into layers. Order within a layer by BARYCENTER (mean neighbor
  // position in the adjacent layer), swept down then up, twice — the Sugiyama
  // crossing-minimization step. Plain id order put connected nodes at opposite
  // ends of their columns and drew every trace across the whole sheet. Ties
  // fall back to id so the layout stays deterministic.
  const layers: GraphNode[][] = []
  for (const n of nodes.values()) (layers[n.layer] ||= []).push(n)
  layers.forEach((l) => l && l.sort((a, b) => a.id - b.id))

  const orderOf = (l: GraphNode[] | undefined): Map<number, number> => {
    const m = new Map<number, number>()
    l?.forEach((n, i) => m.set(n.id, i))
    return m
  }
  const bary = (l: GraphNode[], neighbor: Map<number, number>, edgesOf: (n: GraphNode) => DepEdge[], otherEnd: (e: DepEdge) => number) => {
    const pos = orderOf(l)
    l.sort((a, b) => {
      const mean = (n: GraphNode): number => {
        const ps = edgesOf(n).map((e) => neighbor.get(otherEnd(e))).filter((p): p is number => p != null)
        return ps.length ? ps.reduce((s, p) => s + p, 0) / ps.length : pos.get(n.id)!
      }
      return mean(a) - mean(b) || a.id - b.id
    })
  }
  for (let pass = 0; pass < 2; pass++) {
    for (let li = 1; li < layers.length; li++) {
      if (!layers[li] || !layers[li - 1]) continue
      bary(layers[li], orderOf(layers[li - 1]), (n) => n.ins, (e) => e.blocker)
    }
    for (let li = layers.length - 2; li >= 0; li--) {
      if (!layers[li] || !layers[li + 1]) continue
      bary(layers[li], orderOf(layers[li + 1]), (n) => n.outs, (e) => e.blocked)
    }
  }

  const boxes = new Map<number, NodeGeometry>()
  let maxBottom = 0
  layers.forEach((ln, li) => {
    if (!ln) return
    let y = Y0
    for (const n of ln) {
      const h = Math.max(MIN_H, 24 + PIN * Math.max(n.ins.length, n.outs.length, 2))
      boxes.set(n.id, { x: X0 + li * COL, y, w: CHIP_W, h })
      y += h + ROWGAP
    }
    maxBottom = Math.max(maxBottom, y)
  })

  // Pins: one per edge, fixed pitch, sorted by counterpart Y to reduce crossings.
  const edgeGeom = new Map<string, EdgeGeometry>()
  const ensure = (e: DepEdge): EdgeGeometry => {
    const k = edgeKey(e)
    let g = edgeGeom.get(k)
    if (!g) {
      g = { key: k, edge: e, sx: 0, sy: 0, tx: 0, ty: 0, cx: 0 }
      edgeGeom.set(k, g)
    }
    return g
  }
  for (const n of nodes.values()) {
    const p = boxes.get(n.id)
    if (!p) continue
    ;[...n.ins]
      .sort((a, b) => (boxes.get(a.blocker)?.y ?? 0) - (boxes.get(b.blocker)?.y ?? 0))
      .forEach((e, i) => {
        const g = ensure(e)
        g.ty = p.y + 22 + i * PIN
        g.tx = p.x
      })
    ;[...n.outs]
      .sort((a, b) => (boxes.get(a.blocked)?.y ?? 0) - (boxes.get(b.blocked)?.y ?? 0))
      .forEach((e, i) => {
        const g = ensure(e)
        g.sy = p.y + 22 + i * PIN
        g.sx = p.x + CHIP_W
      })
  }

  // Channels: each edge owns a distinct vertical lane in its blocker's column gap.
  const gaps = new Map<number, DepEdge[]>()
  for (const e of edgeGeom.values()) {
    const li = nodes.get(e.edge.blocker)?.layer ?? 0
    ;(gaps.get(li) ?? gaps.set(li, []).get(li)!).push(e.edge)
  }
  for (const [li, list] of gaps) {
    list.sort((a, b) => (edgeGeom.get(edgeKey(a))?.sy ?? 0) - (edgeGeom.get(edgeKey(b))?.sy ?? 0))
    const gx0 = X0 + li * COL + CHIP_W + 26
    const span = COL - CHIP_W - 52
    list.forEach((e, i) => {
      const g = edgeGeom.get(edgeKey(e))!
      g.cx = gx0 + (list.length === 1 ? span / 2 : (i * span) / (list.length - 1))
    })
  }

  const layerCount = layers.length
  const width = X0 + layerCount * COL - (COL - CHIP_W) + 40
  const height = Math.max(maxBottom + 24, 320)
  return { nodes: boxes, edges: [...edgeGeom.values()], width, height }
}

/** The Manhattan trace path for one laid-out edge: out-pin -> channel -> in-pin. */
export function tracePath(g: EdgeGeometry): string {
  return `M${g.sx} ${g.sy} H${g.cx} V${g.ty} H${g.tx}`
}

/** The lineage (transitive blockers + dependents) of a node, for hover focus. */
export function lineage(id: number, edges: DepEdge[]): Set<number> {
  const keep = new Set<number>([id])
  let grew = true
  while (grew) {
    grew = false
    for (const e of edges) {
      if (keep.has(e.blocker) && !keep.has(e.blocked)) { keep.add(e.blocked); grew = true }
      if (keep.has(e.blocked) && !keep.has(e.blocker)) { keep.add(e.blocker); grew = true }
    }
  }
  return keep
}

/**
 * Transitive reduction over the INFERRED edges: drop an inferred edge when the
 * same dependency is already implied by a longer path. Cross-reference graphs
 * are full of redundant transitive mentions (A refs B refs C, and A also refs
 * C), and drawing all of them is what turns a large sheet into a hairball.
 * Reachability — the semantics of a dependency — is preserved by construction.
 * NATIVE edges are never dropped: they are user-authored statements, not
 * inferences, even when redundant.
 */
export function reduceInferred(edges: DepEdge[]): DepEdge[] {
  const out = new Map<number, number[]>()
  for (const e of edges) {
    const l = out.get(e.blocker)
    if (l) l.push(e.blocked)
    else out.set(e.blocker, [e.blocked])
  }
  const reaches = (from: number, to: number, skip: DepEdge): boolean => {
    const seen = new Set<number>([from])
    const stack = [from]
    while (stack.length) {
      const v = stack.pop()!
      for (const e of edges) {
        if (e === skip || e.blocker !== v) continue
        if (e.blocked === to) return true
        if (!seen.has(e.blocked)) { seen.add(e.blocked); stack.push(e.blocked) }
      }
    }
    return false
  }
  return edges.filter((e) => e.source === 'native' || !reaches(e.blocker, e.blocked, e))
}

/**
 * An edge is a LIVE constraint while its blocker is still open: a satisfied
 * dependency (blocker closed/merged) is history, not a constraint. FOCUS draws
 * live edges only; satisfied blockers of the root appear separately as struck
 * ghost chips.
 */
export function isLiveEdge(e: DepEdge, nodes: Map<number, GraphNode>): boolean {
  const b = nodes.get(e.blocker)
  return !!b && b.state !== 'done'
}

/**
 * The connected component containing `seed`, walked over the supplied edges.
 * FOCUS renders one whole component at a time (median 4 nodes, p90 16, max ~40
 * at real scale) so it never paginates — pass the LIVE edges to get the
 * live-constraint tree the design shows. Adapted from the union-find walk the
 * removed `partitionSheets` used, reduced to the single component we need.
 */
export function componentOf(seed: number, edges: DepEdge[]): Set<number> {
  const keep = new Set<number>([seed])
  let grew = true
  while (grew) {
    grew = false
    for (const e of edges) {
      if (keep.has(e.blocker) && !keep.has(e.blocked)) { keep.add(e.blocked); grew = true }
      if (keep.has(e.blocked) && !keep.has(e.blocker)) { keep.add(e.blocker); grew = true }
    }
  }
  return keep
}

/** The sub-model FOCUS lays out for one component: its nodes with component-LOCAL
 * layers (blocker depth inside the component) and its edges.
 *
 * Layers are recomputed WITHIN the component: the full-graph layer of a
 * component's nodes can start anywhere, and `layoutGraph` groups by layer index,
 * so keeping global layers would render leading empty columns. Nodes are copied,
 * never mutated — the full-graph model stays intact. This is the local-depth
 * math the removed `sheetModel` carried, kept verbatim for the component case. */
export function componentModel(
  ids: Set<number>,
  edges: DepEdge[],
  nodes: Map<number, GraphNode>,
): { nodes: Map<number, GraphNode>; edges: DepEdge[] } {
  const layerOf = new Map<number, number>()
  const depth = (id: number, seen: Set<number>): number => {
    const memo = layerOf.get(id)
    if (memo != null) return memo
    if (seen.has(id)) return 0 // cycle guard, same discipline as buildNodes
    seen.add(id)
    const blockers = edges.filter((e) => e.blocked === id)
    const d = blockers.length
      ? 1 + Math.max(...blockers.map((e) => depth(e.blocker, seen)))
      : 0
    layerOf.set(id, d)
    return d
  }
  const sub = new Map<number, GraphNode>()
  for (const id of ids) {
    const n = nodes.get(id)
    if (!n) continue
    // Pins are rebuilt from the COMPONENT's edges: the full-graph ins/outs would
    // make the layout emit geometry for edges whose other endpoint is off the
    // component, and the renderer indexes endpoints with a non-null assertion —
    // a filtered edge must therefore never reach the layout.
    sub.set(id, {
      ...n,
      layer: depth(id, new Set()),
      ins: edges.filter((e) => e.blocked === id),
      outs: edges.filter((e) => e.blocker === id),
    })
  }
  return { nodes: sub, edges }
}

/** The satisfied (closed/merged) DIRECT blockers of a node — drawn as struck
 * ghost chips at the root of a FOCUS tree so the history that unblocked it stays
 * visible without cluttering the live tree. */
export function satisfiedBlockers(id: number, nodes: Map<number, GraphNode>): GraphNode[] {
  const self = nodes.get(id)
  if (!self) return []
  return self.ins
    .map((e) => nodes.get(e.blocker))
    .filter((b): b is GraphNode => !!b && b.state === 'done')
    .sort((a, b) => a.id - b.id)
}

/** A FRONTIER row: an open item and the state of every blocker on record. */
export interface FrontierItem {
  node: GraphNode
  /** All blockers on record (satisfied and unsatisfied), by id. */
  blockers: GraphNode[]
  /** Blockers still open (empty ⇒ newly unlocked). */
  waitingOn: GraphNode[]
}

function frontierItem(node: GraphNode, nodes: Map<number, GraphNode>): FrontierItem {
  const blockers = node.ins
    .map((e) => nodes.get(e.blocker))
    .filter((b): b is GraphNode => !!b)
    .sort((a, b) => a.id - b.id)
  return { node, blockers, waitingOn: blockers.filter((b) => b.state !== 'done') }
}

/**
 * NEWLY UNLOCKED: open items with at least one blocker ON RECORD whose blockers
 * are now ALL closed/merged. Distinct from `buildNodes` 'ready', which also
 * covers zero-blocker nodes — the frontier answers "what did a merge just free",
 * so a node that was never blocked does not belong here. Ordered by id.
 */
export function frontierReady(nodes: Map<number, GraphNode>): FrontierItem[] {
  return [...nodes.values()]
    .filter((n) => n.state === 'ready' && n.ins.length > 0)
    .sort((a, b) => a.id - b.id)
    .map((n) => frontierItem(n, nodes))
}

/** WAITING: open items with at least one STILL-OPEN blocker. Ordered by id. */
export function frontierWaiting(nodes: Map<number, GraphNode>): FrontierItem[] {
  return [...nodes.values()]
    .filter((n) => n.lifecycle === 'open' && n.ins.some((e) => nodes.get(e.blocker)?.state !== 'done'))
    .sort((a, b) => a.id - b.id)
    .map((n) => frontierItem(n, nodes))
}

/** The datalist of open items (id + title) the "jump to issue" input offers. */
export function openItems(nodes: Map<number, GraphNode>): GraphNode[] {
  return [...nodes.values()]
    .filter((n) => n.lifecycle === 'open')
    .sort((a, b) => a.id - b.id)
}

/**
 * The default FOCUS root on load: the first newly-unlocked frontier item if any
 * (the work a merge just freed), else the first open item, else any node. The
 * whole component renders regardless of which member is the root, so this only
 * picks a starting camera, not what is shown.
 */
export function defaultRoot(nodes: Map<number, GraphNode>): number | null {
  const ready = frontierReady(nodes)
  if (ready.length) return ready[0].node.id
  const open = openItems(nodes)
  if (open.length) return open[0].id
  const first = [...nodes.values()].sort((a, b) => a.id - b.id)[0]
  return first ? first.id : null
}
