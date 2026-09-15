import { useEffect, useRef, useState, useCallback } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Network, RotateCcw, Filter, Atom } from 'lucide-react'
import Graph from 'graphology'
import Sigma from 'sigma'
import { EmptyState } from '../../components/ui'
import SimpleSelect from '../../components/SimpleSelect'
import { knowledgeApi } from './api'
import type { GraphData, Source } from './types'
import { safeGetItem, safeSetItem } from '../../utils/safeStorage'
import { i18nT } from '../../i18n/t'

const TYPE_COLORS: Record<string, string> = {
  service: '#3b82f6',
  technology: '#22c55e',
  concept: '#a855f7',
  org: '#f97316',
}
const FALLBACK_COLOR = '#6b7280'
const PHYSICS_KEY = 'mc-kb-graph-physics'
// "Physics on" runs the ForceAtlas2 worker until the layout CONVERGES (node
// movement drops below a threshold — see startSettle), then freezes it so the
// graph reaches a stable end state instead of jittering forever at equilibrium.
// This is only the HARD CAP: if convergence is never detected, stop anyway so a
// pathological graph can't spin unbounded.
const PHYSICS_SETTLE_MS = 8000

/** Read current theme colors from CSS custom properties. */
function readColors() {
  const cs = getComputedStyle(document.documentElement)
  return {
    text: cs.getPropertyValue('--text').trim() || '#e5e7eb',
    muted: cs.getPropertyValue('--muted').trim() || '#9ca3af',
    accent: cs.getPropertyValue('--accent').trim() || '#fbbf24',
    bg: cs.getPropertyValue('--bg').trim() || '#0f1115',
    bgElevated: cs.getPropertyValue('--bg-elevated').trim() || '#1f2430',
    border: cs.getPropertyValue('--border').trim() || 'rgba(255,255,255,0.12)',
  }
}

/** Dim a node color by blending it toward the background. Sigma's node program
 *  renders 8-digit hex alpha as OPAQUE (same limitation as edges), so a faded
 *  node must be an actually-desaturated colour, not the base color + alpha. */
function dim(color: string, bg: string): string {
  const p = (h: string) => (h.startsWith('#') && h.length === 7
    ? [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)]
    : null)
  const c = p(color), b = p(bg)
  if (!c || !b) return color
  // 22% color, 78% background → clearly recedes but stays faintly visible.
  const mix = c.map((v, i) => Math.round(v * 0.22 + b[i] * 0.78))
  return `rgb(${mix[0]},${mix[1]},${mix[2]})`
}

/** Edge color that recedes on the canvas. Sigma's default edge program does
 *  NOT honor 8-digit hex alpha (it renders #RRGGBBAA as opaque), so a faint
 *  edge must be an actually-dim colour, not the bright --muted with alpha. We
 *  blend --muted toward --bg-elevated to sit just above the background. */
function edgeColor(muted: string, bg: string): string {
  const p = (h: string) => (h.startsWith('#') && h.length === 7
    ? [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)]
    : null)
  const m = p(muted), b = p(bg)
  if (!m || !b) return muted
  // 30% muted, 70% background → a dim edge that recedes but stays visible.
  const mix = m.map((c, i) => Math.round(c * 0.3 + b[i] * 0.7))
  return `rgb(${mix[0]},${mix[1]},${mix[2]})`
}

export default function KnowledgeGraph({
  onSelectEntity,
  highlightEntity,
}: {
  onSelectEntity?: (name: string) => void
  highlightEntity?: string | null
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const sigmaRef = useRef<Sigma | null>(null)
  const graphRef = useRef<Graph | null>(null)
  const layoutRef = useRef<InstanceType<typeof import('graphology-layout-forceatlas2/worker')['default']> | null>(null)
  const renderedKeyRef = useRef<string>('')
  const convergeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const convergePollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const scopeCleanupRef = useRef<(() => void) | null>(null)
  const onSelectRef = useRef(onSelectEntity)
  onSelectRef.current = onSelectEntity

  // Live focus state read by the reducers on every frame. `highlight` is the
  // external prop (search/select); `hovered` is the mouse-hover focus. Both are
  // refs so the reducer closures always see the current value without a rebuild.
  const highlightRef = useRef<string | null | undefined>(highlightEntity)
  const hoveredRef = useRef<string | null>(null)
  // Precomputed focus state: the single focused node and the set of nodes to
  // keep lit (focus + neighbors). Computed ONCE per hover/highlight change (in
  // recomputeFocus) and read cheaply by both reducers — never recomputed
  // per-node/per-edge per frame.
  const focusRef = useRef<{ focus: string | null; lit: Set<string> | null }>({ focus: null, lit: null })
  // Bridges so effect-scoped handlers and the highlight effect can call the
  // latest instances without recreating them or capturing stale closures.
  const startSettleRef = useRef<(() => void) | null>(null)
  const recomputeFocusRef = useRef<(() => void) | null>(null)
  // Read by the mount block + timer so async callbacks never act on a stale
  // enabled value captured when the effect ran.
  const physicsEnabledRef = useRef(false)

  const [selectedSourceId, setSelectedSourceId] = useState<string>('')
  const [physicsEnabled, setPhysicsEnabled] = useState(() => safeGetItem(PHYSICS_KEY) === '1')
  // Distinct from physicsEnabled (intent, persisted): physicsSettling is the
  // transient "worker is actively running the settle" state, used only for the
  // button's spinner-ish affordance. The button LABEL is driven by intent so it
  // never lies after an auto-stop.
  const [physicsSettling, setPhysicsSettling] = useState(false)
  physicsEnabledRef.current = physicsEnabled

  const { data: sources } = useQuery({
    queryKey: ['knowledge-sources'],
    queryFn: () => knowledgeApi<Source[]>('/sources'),
  })

  const graphQueryParams = selectedSourceId
    ? `/graph?limit=200&source_id=${selectedSourceId}`
    : '/graph?limit=200'
  const { data: graphData, isLoading } = useQuery({
    queryKey: ['knowledge-graph', selectedSourceId],
    queryFn: () => knowledgeApi<GraphData>(graphQueryParams),
  })

  // Start the settle: run the worker until the layout CONVERGES (node movement
  // drops below a threshold), not just until a timer fires. The timer is only a
  // hard CAP so a pathological graph can't spin forever. This way physics
  // reaches a stable end state within the window rather than freezing mid-drift.
  const startSettle = useCallback(() => {
    const layout = layoutRef.current
    const graph = graphRef.current
    if (!layout || !graph) return
    if (convergeTimerRef.current) { clearTimeout(convergeTimerRef.current); convergeTimerRef.current = null }
    if (convergePollRef.current) { clearInterval(convergePollRef.current); convergePollRef.current = null }
    try { layout.start() } catch { /* killed */ return }
    setPhysicsSettling(true)

    const finish = () => {
      if (convergeTimerRef.current) { clearTimeout(convergeTimerRef.current); convergeTimerRef.current = null }
      if (convergePollRef.current) { clearInterval(convergePollRef.current); convergePollRef.current = null }
      try { layoutRef.current?.stop() } catch { /* killed */ }
      try { sigmaRef.current?.refresh() } catch { /* noop */ }
      setPhysicsSettling(false)
    }

    // Poll node positions; when the total movement between samples falls below
    // a per-node epsilon, the layout has settled — stop early. Sampling a
    // bounded subset keeps the poll itself cheap. As movement decreases we also
    // ramp the worker's slowDown UP (ease-out): fast, energetic spread at the
    // start; smooth deceleration into the stable end state.
    let prev: Map<string, { x: number; y: number }> | null = null
    const sampleIds = graph.nodes().slice(0, 60)
    const EPS_PER_NODE = 0.4
    const SLOWDOWN_MIN = 2
    const SLOWDOWN_MAX = 14
    convergePollRef.current = setInterval(() => {
      const g = graphRef.current
      const lay = layoutRef.current
      if (!g) { finish(); return }
      const cur = new Map<string, { x: number; y: number }>()
      let movement = 0
      for (const id of sampleIds) {
        if (!g.hasNode(id)) continue
        const x = g.getNodeAttribute(id, 'x') as number
        const y = g.getNodeAttribute(id, 'y') as number
        cur.set(id, { x, y })
        const p = prev?.get(id)
        if (p) movement += Math.hypot(x - p.x, y - p.y)
      }
      const perNode = cur.size > 0 ? movement / cur.size : Infinity
      if (prev && cur.size > 0 && perNode < EPS_PER_NODE) {
        finish() // converged
        return
      }
      // Ease-out: as per-node movement falls from ~4px toward the stop epsilon,
      // interpolate slowDown from MIN (fast) up to MAX (heavily damped). Only
      // ever increased (monotonic) so a transient spike can't speed it back up.
      // The worker exposes a live `settings` object it reads each tick, but the
      // published type decls omit it — cast narrowly to reach slowDown.
      const laySettings = (lay as unknown as { settings?: { slowDown?: number } } | null)?.settings
      if (prev && laySettings) {
        const RAMP_START = 4 // px/node where damping begins to climb
        const t = Math.min(1, Math.max(0, (RAMP_START - perNode) / (RAMP_START - EPS_PER_NODE)))
        const target = SLOWDOWN_MIN + (SLOWDOWN_MAX - SLOWDOWN_MIN) * t
        const current = typeof laySettings.slowDown === 'number' ? laySettings.slowDown : SLOWDOWN_MIN
        if (target > current) laySettings.slowDown = target
      }
      prev = cur
    }, 250)

    // Hard cap: stop even if it never fully converges.
    convergeTimerRef.current = setTimeout(finish, PHYSICS_SETTLE_MS)
  }, [])
  startSettleRef.current = startSettle

  const stopSettle = useCallback(() => {
    if (convergeTimerRef.current) { clearTimeout(convergeTimerRef.current); convergeTimerRef.current = null }
    if (convergePollRef.current) { clearInterval(convergePollRef.current); convergePollRef.current = null }
    try { layoutRef.current?.stop() } catch { /* killed */ }
    try { sigmaRef.current?.refresh() } catch { /* noop */ }
    setPhysicsSettling(false)
  }, [])

  // Toggle handler — clean binary: OFF->ON runs a bounded settle; ON->OFF stops
  // the worker AND freezes sigma immediately (refresh paints the final frame so
  // no residual easing continues after the user turned physics off).
  const togglePhysics = useCallback(() => {
    const next = !physicsEnabled
    setPhysicsEnabled(next)
    safeSetItem(PHYSICS_KEY, next ? '1' : '0')
    if (next) {
      startSettle()
    } else {
      stopSettle()
      try { sigmaRef.current?.refresh() } catch { /* noop */ }
    }
  }, [physicsEnabled, startSettle, stopSettle])

  // Main effect: build graphology graph, run initial layout, mount sigma.
  useEffect(() => {
    if (!graphData || !graphData.nodes.length || !containerRef.current) return
    const key = graphData.nodes.map(n => n.id).join(',') + '|' + graphData.edges.length
    if (key === renderedKeyRef.current) return
    renderedKeyRef.current = key

    // Tear down any previous instance + its scoped observers before rebuilding.
    scopeCleanupRef.current?.()
    scopeCleanupRef.current = null
    if (convergeTimerRef.current) { clearTimeout(convergeTimerRef.current); convergeTimerRef.current = null }
    if (convergePollRef.current) { clearInterval(convergePollRef.current); convergePollRef.current = null }
    if (layoutRef.current) { try { layoutRef.current.kill() } catch { /* noop */ } layoutRef.current = null }
    if (sigmaRef.current) { try { sigmaRef.current.kill() } catch { /* noop */ } sigmaRef.current = null }
    graphRef.current = null

    const container = containerRef.current
    let aborted = false
    let colors = readColors()

    // Theme-aware label renderer for the hovered/highlighted node. Sigma's
    // DEFAULT highlight renderer paints a hardcoded white pill with dark text —
    // illegible in dark mode and on custom themes (the reported bug: highlighting
    // a node made its label unreadable). This draws the pill in the theme's
    // elevated-surface color with a themed border and theme text color, reading
    // `colors` LIVE on each paint so a light/dark switch is reflected without a
    // rebuild. Mirrors MemoryGraphTab's drawThemedHover.
    const drawThemedHover = (
      context: CanvasRenderingContext2D,
      data: { x: number; y: number; size: number; label?: string | null; color: string },
      settings: { labelSize: number; labelFont: string; labelWeight: string },
    ) => {
      const label = data.label
      if (!label) return
      const size = settings.labelSize
      context.font = `${settings.labelWeight} ${size}px ${settings.labelFont}`
      const PAD = 6
      const GAP = 4 // gap between node dot and text
      const textWidth = context.measureText(label).width
      // Text baseline: draw text starting just right of the node dot.
      const textX = data.x + data.size + GAP
      const boxH = size + PAD * 2
      // Box spans from just left of the node dot to just right of the text.
      const boxX = data.x - data.size - PAD
      const boxRight = textX + textWidth + PAD
      const boxW = boxRight - boxX
      const boxY = data.y - boxH / 2
      const r = boxH / 2
      context.beginPath()
      context.moveTo(boxX + r, boxY)
      context.arcTo(boxX + boxW, boxY, boxX + boxW, boxY + boxH, r)
      context.arcTo(boxX + boxW, boxY + boxH, boxX, boxY + boxH, r)
      context.arcTo(boxX, boxY + boxH, boxX, boxY, r)
      context.arcTo(boxX, boxY, boxX + boxW, boxY, r)
      context.closePath()
      // Fully opaque fill: if --bg-elevated resolves to an rgba() with alpha,
      // a translucent pill lets the underlying label bleed through and read as
      // doubled/overlaid text. Composite over the base bg first, then fill.
      context.save()
      context.fillStyle = colors.bg || colors.bgElevated
      context.fill()
      context.fillStyle = colors.bgElevated
      context.fill()
      context.restore()
      context.lineWidth = 1
      context.strokeStyle = colors.border
      context.stroke()
      // Node dot inside the pill.
      context.beginPath()
      context.arc(data.x, data.y, data.size, 0, Math.PI * 2)
      context.fillStyle = data.color
      context.fill()
      // Label text, vertically centered on the pill via textBaseline=middle so
      // it never clips top/bottom regardless of font metrics.
      context.fillStyle = colors.text
      context.textBaseline = 'middle'
      context.textAlign = 'left'
      context.fillText(label, textX, data.y)
    }

    // Build graphology graph.
    const graph = new Graph()

    // Degree map for node sizing (visual hierarchy: hubs render bigger).
    const degreeMap = new Map<string, number>()
    graphData.edges.forEach(e => {
      degreeMap.set(e.source, (degreeMap.get(e.source) || 0) + 1)
      degreeMap.set(e.target, (degreeMap.get(e.target) || 0) + 1)
    })

    // Golden-angle disc seeds initial positions (deterministic, even spread).
    const GOLDEN = Math.PI * (3 - Math.sqrt(5))
    const spread = 12
    // The parent addresses entities by NAME (highlightEntity, onSelectEntity),
    // but graphology keys nodes by id. This maps name -> id so an external
    // highlight resolves to the right node. Last-writer-wins on duplicate names.
    const nameToId = new Map<string, string>()
    graphData.nodes.forEach((n, i) => {
      const radius = spread * Math.sqrt(i + 0.5)
      const x = Math.cos(i * GOLDEN) * radius
      const y = Math.sin(i * GOLDEN) * radius
      const degree = degreeMap.get(n.id) || 0
      const size = Math.max(3, Math.min(12, 3 + degree * 1.5))
      const color = TYPE_COLORS[n.type] || FALLBACK_COLOR
      try {
        // `type` is NOT set: sigma reads node.type as a rendering-program name
        // and throws for unknown programs. Category lives in `entityType`.
        graph.addNode(n.id, { x, y, size, color, label: n.name, entityType: n.type })
        nameToId.set(n.name, n.id)
      } catch {
        if (import.meta.env.DEV) {
          // eslint-disable-next-line no-console
          console.warn('KnowledgeGraph: duplicate node id from API', n.id)
        }
      }
    })

    graphData.edges.forEach(e => {
      if (graph.hasNode(e.source) && graph.hasNode(e.target) && !graph.hasEdge(e.source, e.target)) {
        // `type` omitted for the same reason as nodes; relation lives in relationType.
        graph.addEdge(e.source, e.target, {
          color: edgeColor(colors.muted, colors.bgElevated),
          size: Math.max(0.4, (e.weight || 1) * 0.5),
          relationType: e.type,
        })
      }
    })

    graphRef.current = graph

    // Recompute the focus state ONCE (on hover/highlight change), storing it in
    // focusRef. External highlight takes precedence over hover so a programmatic
    // select and its camera animation always agree on the lit node; hover only
    // applies when there is no external highlight. The reducers then read
    // focusRef.current cheaply — no per-node graph.neighbors() rebuild per frame.
    // highlightRef holds an entity NAME (parent's addressing); hover holds a node
    // ID (sigma events). Both are resolved to an id here.
    const recomputeFocus = () => {
      const byName = highlightRef.current ? nameToId.get(highlightRef.current) : undefined
      const focus = byName || hoveredRef.current || null
      if (!focus || !graph.hasNode(focus)) {
        focusRef.current = { focus: null, lit: null }
        return
      }
      const lit = new Set<string>(graph.neighbors(focus))
      lit.add(focus)
      focusRef.current = { focus, lit }
    }
    recomputeFocusRef.current = recomputeFocus
    recomputeFocus() // honor an initial highlightEntity present at mount

    // One-shot d3-force for initial positions (time-bounded). Only d3-force is
    // imported, not the full d3 bundle — the worker owns ongoing layout.
    import('d3-force').then(async d3 => {
      if (aborted) return

      // Community detection + centroid seeding — the technique Gephi/Neo4j use
      // to make nodes visibly cluster with their neighbors instead of spreading
      // evenly. Louvain assigns each node a community; we pre-place each
      // community's members around a distinct centroid on a ring so the FA2
      // pass then tightens each group into a separated, orbiting cluster
      // (seeding gives the force layout the community structure to reinforce
      // rather than discover from a uniform disc). Best-effort: if the plugin is
      // absent or the graph is trivial, the golden-angle seeds already set stand.
      try {
        const louvain = (await import('graphology-communities-louvain')).default
        if (aborted) return
        const communities = louvain(graph) as Record<string, number>
        const groups = new Map<number, string[]>()
        for (const [node, c] of Object.entries(communities)) {
          if (!graph.hasNode(node)) continue
          graph.setNodeAttribute(node, 'community', c)
          const arr = groups.get(c) || []
          arr.push(node)
          groups.set(c, arr)
        }
        const ids = Array.from(groups.keys())
        if (ids.length > 1) {
          const R = 240 // ring radius separating community centroids
          const GOLD = Math.PI * (3 - Math.sqrt(5))
          ids.forEach((cid, gi) => {
            const angle = (gi / ids.length) * 2 * Math.PI
            const cx = Math.cos(angle) * R
            const cy = Math.sin(angle) * R
            ;(groups.get(cid) || []).forEach((node, mi) => {
              const rr = 8 * Math.sqrt(mi + 0.5)
              graph.setNodeAttribute(node, 'x', cx + Math.cos(mi * GOLD) * rr)
              graph.setNodeAttribute(node, 'y', cy + Math.sin(mi * GOLD) * rr)
            })
          })
        }
      } catch {
        /* louvain unavailable/failed — golden-angle seeds stand */
      }
      if (aborted) return

      type LNode = { id: string; x: number; y: number }
      type LEdge = { source: string; target: string }
      const simNodes: LNode[] = graphData.nodes
        .filter(n => graph.hasNode(n.id))
        .map(n => ({
          id: n.id,
          x: graph.getNodeAttribute(n.id, 'x') as number,
          y: graph.getNodeAttribute(n.id, 'y') as number,
        }))
      const simEdges: LEdge[] = graphData.edges
        .filter(e => graph.hasNode(e.source) && graph.hasNode(e.target))
        .map(e => ({ source: e.source, target: e.target }))

      const sim = d3.forceSimulation<LNode>(simNodes)
        .force('link', d3.forceLink<LNode, LEdge>(simEdges).id(d => d.id).distance(80).strength(0.6))
        .force('charge', d3.forceManyBody().strength(-200).distanceMax(400))
        .force('collide', d3.forceCollide(25))
        .force('x', d3.forceX(0).strength(0.03))
        .force('y', d3.forceY(0).strength(0.03))
        .alphaDecay(0.08)
        .stop()

      const MAX_TICKS = 300
      const BUDGET_MS = 250
      const deadline = performance.now() + BUDGET_MS
      for (let i = 0; i < MAX_TICKS && performance.now() < deadline; i++) sim.tick()

      simNodes.forEach(n => {
        if (graph.hasNode(n.id)) {
          graph.setNodeAttribute(n.id, 'x', n.x)
          graph.setNodeAttribute(n.id, 'y', n.y)
        }
      })

      if (aborted) return

      // Second one-shot pass: synchronous ForceAtlas2 in linLog mode to pull
      // communities into distinct clusters BEFORE first paint. Without this the
      // static default view (physics off) shows the even d3-force blob with no
      // visible grouping. Uses the same clustering settings as the live worker
      // so toggling physics on continues seamlessly from here.
      //
      // RESOURCE BUDGET: this runs synchronously on the main thread, so keep it
      // cheap. barnesHutOptimize is ON for every tier (O(n log n), not the
      // brute-force O(n²)), and the iteration count tapers with graph size:
      // 90 iters ≤300 nodes (the API cap), down to 0 past 3000 where the live
      // worker should own layout instead. In dev the pass is timed and warns if
      // it exceeds a 60ms frame-and-a-half budget so a future cap raise can't
      // silently reintroduce a stall.
      try {
        const order = graph.order
        // Iteration tiers: enough to reach NEAR-convergence so the static
        // physics-off layout matches the equilibrium the live worker settles to
        // (otherwise toggling physics on visibly jumps to a different end state).
        // Still bounded + barnesHut O(n log n) + dev-timed against the 60ms budget.
        const iterations = order > 3000 ? 0 : order > 800 ? 80 : order > 300 ? 140 : 220
        if (iterations > 0) {
          const fa2 = (await import('graphology-layout-forceatlas2')).default
          if (aborted) return
          const t0 = import.meta.env.DEV ? performance.now() : 0
          fa2.assign(graph, {
            iterations,
            settings: {
              linLogMode: true,
              // Readability spread: scalingRatio inflates inter-node distance
              // (Gephi's "Expansion") so clusters breathe instead of packing
              // into an unreadable knot; lower gravity lets them spread rather
              // than collapse to centre. linLog still gives the grouped shape.
              gravity: 1,
              scalingRatio: 20,
              barnesHutOptimize: true,
              adjustSizes: true,
              outboundAttractionDistribution: true,
              edgeWeightInfluence: 1.5,
            },
          })
          if (import.meta.env.DEV) {
            const ms = performance.now() - t0
            const BUDGET_MS = 60
            if (ms > BUDGET_MS) {
              // Values are milliseconds; the unit word is kept off the numbers so
              // this DEV-only diagnostic is not mistaken for a user-facing string.
              // eslint-disable-next-line no-console
              console.warn(
                `KnowledgeGraph: FA2 clustering (milliseconds) elapsed=${ms.toFixed(0)} ` +
                  `budget=${BUDGET_MS} order=${order}`,
              )
            }
          }
        }
      } catch { /* fa2 unavailable — fall back to the d3-force seed positions */ }

      if (aborted) return

      // Mount sigma with bounded, smooth camera behaviour.
      const sigma = new Sigma(graph, container, {
        renderLabels: true,
        labelRenderedSizeThreshold: 8,
        labelColor: { color: colors.text },
        labelSize: 11,
        // Theme-aware pill for the hovered/highlighted node (sigma's default is
        // a hardcoded white pill — illegible in dark mode / custom themes).
        defaultDrawNodeHover: drawThemedHover,
        // Drop edges + labels from the paint loop WHILE the camera is moving
        // (pan/zoom): they redraw the instant it settles, so this cuts per-frame
        // work during interaction with no lasting visual change — the main
        // render-cost lever on a dense graph.
        hideEdgesOnMove: true,
        hideLabelsOnMove: true,
        allowInvalidContainer: true,
        // Bound zoom so the user can't zoom into a void or lose the graph, and
        // soften the per-notch step so the wheel feels smooth, not jumpy.
        minCameraRatio: 0.1,
        maxCameraRatio: 10,
        zoomingRatio: 1.4,
        defaultNodeColor: FALLBACK_COLOR,
        defaultEdgeColor: colors.muted,
        nodeReducer: (nodeKey, data) => {
          const res = { ...data }
          const { focus, lit } = focusRef.current
          if (lit) {
            if (nodeKey === focus) {
              res.highlighted = true
              res.size = (data.size as number) * 1.4
              res.zIndex = 2
            } else if (!lit.has(nodeKey)) {
              res.color = dim(data.color as string, colors.bg)
              res.label = ''
              res.zIndex = 0
            } else {
              res.zIndex = 1
            }
          }
          return res
        },
        edgeReducer: (edgeKey, data) => {
          const res = { ...data }
          const { focus } = focusRef.current
          if (focus) {
            if (graph.hasExtremity(edgeKey, focus)) {
              // Relationship edge of the focused node: make it pop — brighter
              // (accent) and thicker so the connections read clearly.
              res.color = colors.accent
              res.size = Math.max(2, ((data.size as number) || 1) * 2.5)
              res.zIndex = 2
            } else {
              // Everything else recedes so only the relationships stand out.
              res.hidden = true
            }
          }
          return res
        },
      })
      sigmaRef.current = sigma

      // ── Node dragging (sigma v3 has no built-in drag) ──────────────────────
      let draggedNode: string | null = null
      let physicsWasRunningBeforeDrag = false
      const camera = sigma.getCamera()
      sigma.on('downNode', ({ node }) => {
        draggedNode = node
        graph.setNodeAttribute(node, 'highlighted', true)
        camera.disable() // stop panning while dragging
        // If the worker is settling, it owns positions and would overwrite the
        // dragged node's x/y on its next tick (node snaps back). FA2 has no
        // per-node pin, so pause the whole layout for the drag and resume after.
        if (convergeTimerRef.current || convergePollRef.current) {
          physicsWasRunningBeforeDrag = true
          if (convergeTimerRef.current) { clearTimeout(convergeTimerRef.current); convergeTimerRef.current = null }
          if (convergePollRef.current) { clearInterval(convergePollRef.current); convergePollRef.current = null }
          try { layoutRef.current?.stop() } catch { /* killed */ }
        }
      })
      const mouse = sigma.getMouseCaptor()
      mouse.on('mousemovebody', (e) => {
        if (!draggedNode) return
        const pos = sigma.viewportToGraph(e)
        graph.setNodeAttribute(draggedNode, 'x', pos.x)
        graph.setNodeAttribute(draggedNode, 'y', pos.y)
        // Suppress the camera pan the same gesture would otherwise trigger.
        e.preventSigmaDefault()
        e.original.preventDefault()
        e.original.stopPropagation()
      })
      const endDrag = () => {
        if (draggedNode) graph.removeNodeAttribute(draggedNode, 'highlighted')
        draggedNode = null
        camera.enable()
        // Resume the settle if the drag interrupted one, so the layout re-flows
        // around the moved node.
        if (physicsWasRunningBeforeDrag) {
          physicsWasRunningBeforeDrag = false
          startSettleRef.current?.()
        }
      }
      mouse.on('mouseup', endDrag)
      sigma.on('upNode', endDrag)
      sigma.on('upStage', endDrag)

      // ── Hover: focus node + neighbors, dim the rest ────────────────────────
      sigma.on('enterNode', ({ node }) => {
        hoveredRef.current = node
        recomputeFocus()
        container.style.cursor = 'pointer'
        sigma.refresh({ skipIndexation: true })
      })
      sigma.on('leaveNode', () => {
        hoveredRef.current = null
        recomputeFocus()
        container.style.cursor = 'default'
        sigma.refresh({ skipIndexation: true })
      })

      // ── Click: report to parent ────────────────────────────────────────────
      sigma.on('clickNode', ({ node }) => {
        onSelectRef.current?.(graph.getNodeAttribute(node, 'label') as string)
      })

      // Sigma reads container size at construction; if it was 0-sized at mount
      // (tab not yet laid out) the canvas comes up empty. Re-sync once real
      // dimensions resolve. rAF-coalesced so a CSS transition / flex reflow that
      // fires many callbacks collapses into one resize+refresh per frame.
      let resizeRaf = 0
      const resizeObserver = new ResizeObserver(() => {
        if (aborted || resizeRaf) return
        resizeRaf = requestAnimationFrame(() => {
          resizeRaf = 0
          if (!aborted) { try { sigma.resize(); sigma.refresh() } catch { /* noop */ } }
        })
      })
      resizeObserver.observe(container)

      // ForceAtlas2 worker (ongoing layout, off the main thread). Tuned for
      // READABLE COMMUNITY CLUSTERING, not just a calm settle:
      //  - linLogMode: the key lever — pulls tightly-connected groups into
      //    distinct, well-separated clusters (the classic Gephi look) instead
      //    of one even blob. linLog uses a log attraction, so scalingRatio is
      //    kept low (~1) and gravity raised to hold clusters on-screen.
      //  - outboundAttractionDistribution: hubs sit at cluster centres rather
      //    than collapsing everything toward them.
      //  - adjustSizes: node size is a collision radius (no overlap jitter).
      //  - slowDown: damps end-of-run vibration; barnesHut keeps it O(n log n).
      import('graphology-layout-forceatlas2/worker').then(({ default: FA2Layout }) => {
        if (aborted) return
        const layout = new FA2Layout(graph, {
          settings: {
            linLogMode: true,
            gravity: 1,
            scalingRatio: 20,
            // Start LOW so the layout spreads fast and energetically; the
            // convergence poll ramps slowDown UP as it settles (ease-out), so
            // motion decelerates smoothly into the end state instead of a
            // constant speed that stops abruptly.
            slowDown: 2,
            // Match the sync mount pass (always on) so the live worker converges
            // to the SAME equilibrium the static physics-off layout shows —
            // otherwise barnesHut's approximation lands a visibly different end
            // state when you first toggle physics on.
            barnesHutOptimize: true,
            adjustSizes: true,
            outboundAttractionDistribution: true,
            edgeWeightInfluence: 1.5,
          },
        })
        layoutRef.current = layout
        // Drive start/stop from the current intent, not a captured value.
        if (physicsEnabledRef.current) startSettle()
      }).catch(() => { /* FA2 unavailable — physics button no-ops */ })

      // Theme reactivity.
      const themeObserver = new MutationObserver(() => {
        colors = readColors()
        graph.forEachEdge(edge => graph.setEdgeAttribute(edge, 'color', edgeColor(colors.muted, colors.bgElevated)))
        sigma.setSetting('labelColor', { color: colors.text })
        sigma.refresh()
      })
      themeObserver.observe(document.documentElement, {
        attributes: true, attributeFilter: ['data-theme'],
      })

      // Single scoped-cleanup, held in a ref (never stashed on the DOM element).
      scopeCleanupRef.current = () => {
        themeObserver.disconnect()
        resizeObserver.disconnect()
        if (resizeRaf) cancelAnimationFrame(resizeRaf)
      }
    }).catch(() => { /* d3-force import failed */ })

    return () => {
      aborted = true
      if (convergeTimerRef.current) { clearTimeout(convergeTimerRef.current); convergeTimerRef.current = null }
      if (convergePollRef.current) { clearInterval(convergePollRef.current); convergePollRef.current = null }
      scopeCleanupRef.current?.()
      scopeCleanupRef.current = null
      if (layoutRef.current) { try { layoutRef.current.kill() } catch { /* */ } layoutRef.current = null }
      if (sigmaRef.current) { try { sigmaRef.current.kill() } catch { /* */ } sigmaRef.current = null }
      graphRef.current = null
      renderedKeyRef.current = ''
    }
  }, [graphData, startSettle])

  // Keep the highlight ref current and re-run the reducers when it changes.
  useEffect(() => {
    highlightRef.current = highlightEntity
    recomputeFocusRef.current?.()
    const sigma = sigmaRef.current
    if (!sigma) return
    sigma.refresh({ skipIndexation: true })
    // focusRef now holds the RESOLVED node id (highlightEntity is a name).
    const focusId = focusRef.current.focus
    if (focusId && graphRef.current?.hasNode(focusId)) {
      const pos = sigma.getNodeDisplayData(focusId)
      if (pos) {
        sigma.getCamera().animate(
          { x: pos.x, y: pos.y, ratio: 0.5 },
          { duration: 500, easing: 'quadraticInOut' }
        )
      }
    }
  }, [highlightEntity])

  // Recenter: reset the camera to frame the whole graph.
  const recenter = useCallback(() => {
    const sigma = sigmaRef.current
    if (!sigma) return
    sigma.getCamera().animate(
      { x: 0.5, y: 0.5, ratio: 1, angle: 0 },
      { duration: 400, easing: 'quadraticInOut' }
    )
  }, [])

  if (isLoading) {
    return (
      <div className="text-muted text-sm p-4">
        {i18nT('pages.knowledge.knowledgeGraph.loading_graph')}
      </div>
    )
  }
  if (!graphData || !graphData.nodes.length) {
    return (
      <EmptyState
        icon={<Network size={40} />}
        title={i18nT('pages.knowledge.knowledgeGraph.no_graph_data_yet')}
        subtitle={i18nT('pages.knowledge.knowledgeGraph.ingest_documents_to_build_the_entity_graph')}
      />
    )
  }

  const physicsLabel = physicsEnabled
    ? i18nT('pages.knowledge.knowledgeGraph.pause_physics')
    : i18nT('pages.knowledge.knowledgeGraph.start_physics')

  return (
    <div className="border border-border rounded-lg overflow-hidden flex flex-col">
      <div className="px-4 py-2 border-b border-border flex items-center gap-3 text-[12px] text-muted shrink-0">
        <span>
          {graphData.nodes.length} {i18nT('pages.knowledge.knowledgeGraph.nodes')}{' '}
          {graphData.edges.length} {i18nT('pages.knowledge.knowledgeGraph.edges')}
        </span>
        <button
          onClick={recenter}
          aria-label={i18nT('pages.knowledge.knowledgeGraph.recenter')}
          className="px-2 py-0.5 text-[11px] border border-border rounded hover:bg-bg-elevated bg-transparent cursor-pointer text-muted flex items-center gap-1"
        >
          <RotateCcw size={10} /> {i18nT('pages.knowledge.knowledgeGraph.recenter')}
        </button>
        <button
          onClick={togglePhysics}
          aria-label={physicsLabel}
          aria-pressed={physicsEnabled}
          className={`px-2 py-0.5 text-[11px] border rounded cursor-pointer flex items-center gap-1 ${
            physicsEnabled
              ? 'border-accent text-accent bg-accent/10'
              : 'border-border text-muted hover:bg-bg-elevated bg-transparent'
          }`}
          title={physicsLabel}
        >
          <Atom size={10} className={physicsSettling ? 'animate-spin' : ''} />
          {physicsLabel}
        </button>
        {sources && sources.length > 1 && (
          <span className="flex items-center gap-1">
            <Filter size={10} />
            <SimpleSelect
              options={sources.map((s) => s.id)}
              optionLabels={sources.map((s) => s.name)}
              value={selectedSourceId}
              onChange={setSelectedSourceId}
              clearLabel={i18nT('pages.knowledge.knowledgeGraph.all_sources')}
              aria-label={i18nT('pages.knowledge.knowledgeGraph.filter_by_source')}
              className="h-6 text-[11px] px-1.5"
            />
          </span>
        )}
        <span className="ml-auto flex gap-2">
          {Object.entries(TYPE_COLORS).map(([t, c]) => (
            <span key={t} className="flex items-center gap-1">
              <span className="w-2 h-2 rounded-full inline-block" style={{ background: c }} />
              {t}
            </span>
          ))}
        </span>
      </div>
      <div
        ref={containerRef}
        role="application"
        aria-label={i18nT('pages.knowledge.knowledgeGraph.graph_region_label')}
        className="w-full relative"
        style={{
          height: '500px',
          backgroundColor: 'var(--bg)',
          // The mature graph-viz tools (Obsidian, Neo4j Bloom, Cosmograph) keep
          // the backdrop QUIET — flat dark so the graph is the figure — and get
          // "depth" from the NODES (additive glow over a void reads as deep
          // space), not from background texture. So the backdrop is just a
          // whisper-soft radial vignette that darkens the edges toward a "well"
          // you look into. Pure CSS, zero per-frame cost. Node glow (below, in
          // the sigma node program) is where the depth actually comes from.
          backgroundImage:
            'radial-gradient(ellipse 90% 90% at 50% 46%, transparent 55%, color-mix(in srgb, var(--bg) 55%, #000) 100%)',
        }}
      />
    </div>
  )
}
