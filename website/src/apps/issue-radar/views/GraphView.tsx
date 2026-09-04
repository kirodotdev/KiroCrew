// Graph — the dependency dashboard tab (#5187 M2), rebuilt as FRONTIER + FOCUS.
//
// The global dependency "fabric" with numbered sheets is gone. The tab answers
// two questions the approved mockup (demo_real_template.html) makes concrete:
//
//   FRONTIER  — "what is actionable now": a list of NEWLY UNLOCKED items (every
//               blocker on record now closed/merged) then WAITING items (>=1
//               still-open blocker). A row expands a WHY line and can open the
//               item's tree in FOCUS.
//   FOCUS     — "why is THIS blocked / what does it gate": the entire connected
//               component of a selected root over LIVE constraint edges, laid out
//               left-to-right by component-local blocker depth. Any node re-roots;
//               Enter opens it in-app. The root's satisfied direct blockers show
//               as struck ghost chips.
//
// Two hard rules from the spec:
//  - NEVER hardcode colours. Every hue is a theme token (var(--accent) for the
//    "go/ready" state, var(--warn) for waiting, var(--danger) for CI fail only,
//    surfaces/lines/text from the app palette). The tab must survive theming.
//  - The graph math is pure and unit-tested in `lib/deps.ts`; this file paints it.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useReducedMotion } from 'framer-motion'
import { RefreshCw, Waypoints } from 'lucide-react'
import { useIssueRadar } from '../context'
import { issueRadarApi, type DepsResponse } from '../api'
import { repoScopeKey } from '../lib/links'
import {
  buildNodes, layoutGraph, readySet, reduceInferred,
  componentOf, componentModel, satisfiedBlockers,
  frontierReady, frontierWaiting, openItems, defaultRoot,
  type GraphNode, type NodeState, type FrontierItem,
} from '../lib/deps'
import { i18nT } from '../../../i18n/t'
import { splitOnPlaceholder } from '../../crew-companion/splitOnPlaceholder'

/** The theme token a node state paints with. `open`/`done` are drawn muted. */
function stateColor(state: NodeState): string {
  switch (state) {
    case 'ready':
    case 'running':
      return 'var(--accent)'
    case 'hold':
      return 'var(--warn)'
    case 'done':
      return 'var(--muted-strong)'
    default:
      return 'var(--muted)'
  }
}

/** The LED label a node state shows (WAIT/RDY/RUN/DONE/OPEN). */
function stateLabel(state: NodeState): string {
  switch (state) {
    case 'hold': return i18nT('apps.issueRadar.views.graphView.led_wait')
    case 'ready': return i18nT('apps.issueRadar.views.graphView.led_ready')
    case 'running': return i18nT('apps.issueRadar.views.graphView.led_run')
    case 'done': return i18nT('apps.issueRadar.views.graphView.led_done')
    default: return i18nT('apps.issueRadar.views.graphView.led_open')
  }
}

const SIGIL = (n: GraphNode) => (n.kind === 'pull' ? 'PR' : 'IS')
type SubView = 'frontier' | 'focus'
const WAITING_CAP = 30

// How long a jumped-to row keeps its identifying ring. Long enough to survive the
// smooth scroll that precedes it and still be there when the eye arrives; short
// enough that it reads as "here" rather than as a persistent selection.
const REVEAL_RING_MS = 1600

export default function GraphView() {
  const { active, issues, pulls, openRef } = useIssueRadar()
  const scopeKey = repoScopeKey(active)
  const reduceMotion = useReducedMotion()

  // One-shot unlock detection, unchanged semantics: null = first observation
  // (seed silently), full-graph snapshot after each paint, reset on scope change.
  const prevReadyRef = useRef<Set<number> | null>(null)
  const prevScopeRef = useRef(scopeKey)
  if (prevScopeRef.current !== scopeKey) {
    prevScopeRef.current = scopeKey
    prevReadyRef.current = null
  }

  const depsQuery = useQuery({
    queryKey: ['issue-radar', 'deps', scopeKey],
    queryFn: () => issueRadarApi.deps(active),
    retry: false,
    staleTime: 60_000,
    refetchInterval: false,
  })

  const deps: DepsResponse | null = depsQuery.data ?? null

  const [view, setView] = useState<SubView>('frontier')
  const [root, setRoot] = useState<number | null>(null)
  const [jump, setJump] = useState('')
  // FRONTIER: which waiting row a jump asked to reveal, and whether the full
  // waiting list is expanded. A jump to a row beyond WAITING_CAP force-expands
  // so the target is actually in the DOM to scroll to. `seq` bumps on every
  // jump so re-jumping to the same id re-triggers the scroll effect.
  const [showAllWaiting, setShowAllWaiting] = useState(false)
  const [reveal, setReveal] = useState<{ id: number; seq: number } | null>(null)

  // Build the full-graph nodes once per payload; every view derives from this.
  const built = useMemo(
    () => (deps ? buildNodes(deps, issues, pulls, prevReadyRef.current) : null),
    [deps, issues, pulls],
  )

  // Snapshot the ready set after paint so the NEXT fetch compares against it.
  useEffect(() => {
    if (built) prevReadyRef.current = readySet(built)
  }, [built])

  // Default root once the graph is known / on scope change.
  useEffect(() => {
    if (built && (root == null || !built.has(root))) setRoot(defaultRoot(built))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [built])

  // Narrow branch: below 720px usable the tree cannot render without forced
  // panning, so a narrow pane defaults to FRONTIER on the transition INTO narrow
  // (the tree stays reachable via any row's "open tree", and FOCUS itself
  // scrolls in its container). We never force the user back off FOCUS once they
  // chose it, so this only nudges the view on the edge into narrow — no stored
  // narrow flag is read during render, so a ref (not state) holds the bucket.
  const wasNarrowRef = useRef(false)
  const roRef = useRef<ResizeObserver | null>(null)
  const shellRef = useCallback((el: HTMLDivElement | null) => {
    roRef.current?.disconnect()
    roRef.current = null
    if (!el) return
    const apply = (w: number) => {
      const isNarrow = w < 720
      if (isNarrow && !wasNarrowRef.current) setView('frontier')
      wasNarrowRef.current = isNarrow
    }
    apply(el.getBoundingClientRect().width)
    const ro = new ResizeObserver((es) => apply(es[0]?.contentRect.width ?? 720))
    ro.observe(el)
    roRef.current = ro
  }, [])

  const openNode = (n: GraphNode) => openRef({ kind: n.kind === 'pull' ? 'pull' : 'issue', number: n.id })

  const focusOn = (id: number) => { setRoot(id); setView('focus') }

  // ── loading ──
  if (depsQuery.isLoading) {
    return (
      <div className="h-full flex items-center justify-center text-muted text-[13px] font-mono">
        {i18nT('apps.issueRadar.views.graphView.loading')}
      </div>
    )
  }

  // ── empty / unavailable (404, error, or no edges) ──
  const hasGraph = !!built && !!deps && deps.edges.length > 0
  if (!hasGraph) {
    return <EmptyBoard error={depsQuery.isError} onRetry={() => depsQuery.refetch()} refreshing={depsQuery.isFetching} />
  }

  const nodes = built!
  const readyItems = frontierReady(nodes)
  const waitingItems = frontierWaiting(nodes)
  const opens = openItems(nodes)

  const onJump = (raw: string) => {
    const n = parseInt(raw, 10)
    if (!Number.isFinite(n) || !nodes.has(n)) return
    if (view === 'focus') {
      // FOCUS: re-root the tree, exactly as before.
      focusOn(n)
      setJump('')
      return
    }
    // FRONTIER: reveal the matching row in place.
    //
    // The picker's datalist is the whole open-item list, which is a SUPERSET of
    // the rows FRONTIER draws: an item that is neither unblocked nor blocked by
    // a still-open blocker has no row here. So a suggestion the input ITSELF
    // offered can name something this view cannot reveal, and setting `reveal`
    // for it would resolve no ref and silently do nothing -- the same
    // looks-like-an-affordance-does-nothing failure this change exists to remove.
    // Falling back to the focus tree keeps every offered suggestion answerable.
    const idx = waitingItems.findIndex((f) => f.node.id === n)
    const inReady = readyItems.some((f) => f.node.id === n)
    if (idx < 0 && !inReady) {
      focusOn(n)
      setJump('')
      return
    }
    // A waiting row past the collapsed cap needs the list expanded first, so the
    // row exists in the DOM to scroll to.
    if (idx >= WAITING_CAP) setShowAllWaiting(true)
    setReveal((r) => ({ id: n, seq: (r?.seq ?? 0) + 1 }))
    setJump('')
  }

  return (
    <div ref={shellRef} className="h-full flex flex-col bg-bg text-text font-mono">
      {/* Header: repo, sub-view tabs, and the FOCUS "jump to issue" picker. */}
      <div className="flex-none flex items-center gap-3 px-2 md:px-4 h-11 border-b border-border text-[11px] tracking-[.08em] text-muted overflow-x-auto">
        <span className="whitespace-nowrap">
          {i18nT('apps.issueRadar.views.graphView.header')} · <span className="text-text-strong font-semibold">{active.owner}/{active.repo}</span>
        </span>
        <div role="tablist" aria-label={i18nT('apps.issueRadar.views.graphView.views_label')} className="flex items-center gap-1">
          {(['frontier', 'focus'] as const).map((v) => (
            <button
              key={v}
              role="tab"
              aria-selected={view === v}
              onClick={() => setView(v)}
              className={`px-3 h-6 rounded-sm border text-[11px] tracking-[.08em] cursor-pointer whitespace-nowrap ${
                view === v
                  ? 'border-border-strong text-accent bg-card'
                  : 'border-transparent text-muted hover:text-text'
              }`}
            >
              {v === 'frontier'
                ? i18nT('apps.issueRadar.views.graphView.tab_frontier')
                : i18nT('apps.issueRadar.views.graphView.tab_focus')}
            </button>
          ))}
        </div>
        <div className="ml-auto flex items-center gap-2">
          {/* Jump-to-issue: in FOCUS it re-roots the tree; in FRONTIER it
              reveals/scrolls to the matching row (expanding the waiting
              overflow when the row sits past the collapsed cap). */}
          <label htmlFor="ir-graph-jump" className="contents">
            <span className="sr-only">{i18nT('apps.issueRadar.views.graphView.jump_label')}</span>
            <input
              id="ir-graph-jump"
              list="ir-graph-open-items"
              value={jump}
              onChange={(e) => setJump(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') onJump((e.target as HTMLInputElement).value) }}
              placeholder={i18nT('apps.issueRadar.views.graphView.jump_placeholder')}
              aria-label={i18nT('apps.issueRadar.views.graphView.jump_label')}
              className="w-40 md:w-52 bg-card border border-border-strong text-text px-2.5 py-1 rounded-sm text-[11px] focus:outline-none focus:border-accent"
            />
          </label>
          <datalist id="ir-graph-open-items">
            {opens.map((n) => (
              <option key={n.id} value={n.id}>{(n.title || '').slice(0, 50)}</option>
            ))}
          </datalist>
          <button
            onClick={() => depsQuery.refetch()}
            disabled={depsQuery.isFetching}
            aria-label={i18nT('apps.issueRadar.views.graphView.refresh')}
            title={i18nT('apps.issueRadar.views.graphView.refresh')}
            className="inline-flex items-center justify-center h-7 w-7 rounded-md border border-border text-muted hover:text-text hover:border-border-strong disabled:opacity-40 cursor-pointer"
          >
            <RefreshCw size={13} className={depsQuery.isFetching ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      {view === 'frontier' ? (
        <Frontier
          ready={readyItems}
          waiting={waitingItems}
          reduceMotion={!!reduceMotion}
          onOpenTree={focusOn}
          showAllWaiting={showAllWaiting}
          onToggleWaiting={() => setShowAllWaiting((s) => !s)}
          reveal={reveal}
        />
      ) : (
        <FocusTree
          root={root}
          nodes={nodes}
          liveEdges={deps!.edges}
          reduceMotion={!!reduceMotion}
          onReRoot={setRoot}
          onOpen={openNode}
        />
      )}

      {/* Caption footer. */}
      <div className="flex-none flex items-center gap-3 h-11 px-2 md:px-4 border-t border-border text-[11px] text-muted">
        <span className="truncate">
          {view === 'frontier'
            ? i18nT('apps.issueRadar.views.graphView.frontier_caption')
            : i18nT('apps.issueRadar.views.graphView.focus_caption')}
        </span>
        <span className="ml-auto flex-none tabular-nums">
          {i18nT('apps.issueRadar.views.graphView.ready_count', { n: readyItems.length })}
        </span>
      </div>
    </div>
  )
}

/* ── FRONTIER ─────────────────────────────────────────────────────────── */

function Frontier({ ready, waiting, reduceMotion, onOpenTree, showAllWaiting, onToggleWaiting, reveal }: {
  ready: FrontierItem[]
  waiting: FrontierItem[]
  reduceMotion: boolean
  onOpenTree: (id: number) => void
  showAllWaiting: boolean
  onToggleWaiting: () => void
  reveal: { id: number; seq: number } | null
}) {
  const shownWaiting = showAllWaiting ? waiting : waiting.slice(0, WAITING_CAP)
  const overflow = waiting.length - shownWaiting.length
  // Row DOM refs, keyed by node id, so a jump can scroll the matching row into
  // view and flash a brief accent ring on it.
  const rowRefs = useRef(new Map<number, HTMLDivElement | null>())

  useEffect(() => {
    if (!reveal) return
    const el = rowRefs.current.get(reveal.id)
    if (el) {
      el.scrollIntoView({ block: 'center', behavior: reduceMotion ? 'auto' : 'smooth' })
      // Centring alone does not say WHICH row matched: the waiting list is up to
      // 268 near-identical rows, so after a jump the target has to identify
      // itself. The ring is that identification, and it self-clears rather than
      // persisting as a selection the user would then have to dismiss.
      el.setAttribute('data-ir-reveal', '1')
      const t = setTimeout(() => el.removeAttribute('data-ir-reveal'), REVEAL_RING_MS)
      // Clear the attribute in the cleanup too, not just the timer. A second jump
      // inside REVEAL_RING_MS re-runs this effect, and cancelling the pending
      // removal without also removing the attribute would leave the PREVIOUS row
      // ringed indefinitely -- two highlighted rows, one of them lying.
      return () => {
        clearTimeout(t)
        el.removeAttribute('data-ir-reveal')
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reveal?.id, reveal?.seq])

  const setRowRef = useCallback((id: number, el: HTMLDivElement | null) => {
    if (el) rowRefs.current.set(id, el)
    else rowRefs.current.delete(id)
  }, [])

  return (
    <div className="flex-1 min-h-0 overflow-y-auto px-2 md:px-6 py-5">
      <h2 className="text-[11px] tracking-[.26em] text-text-strong font-semibold mb-0.5">
        {i18nT('apps.issueRadar.views.graphView.frontier_heading')}
      </h2>
      <div className="text-[10px] text-muted-strong mb-3.5">
        {i18nT('apps.issueRadar.views.graphView.frontier_sub', { ready: ready.length, waiting: waiting.length })}
      </div>

      {ready.length === 0 && waiting.length === 0 && (
        <div className="text-[11px] text-muted">{i18nT('apps.issueRadar.views.graphView.no_frontier')}</div>
      )}

      {ready.map((f) => (
        <FrontierRow key={f.node.id} item={f} unlocked reduceMotion={reduceMotion} onOpenTree={onOpenTree} rowRef={setRowRef} />
      ))}

      {waiting.length > 0 && (
        <div className="max-w-[820px] mt-5 mb-2.5 pt-2.5 border-t border-border text-[9px] tracking-[.24em] text-muted-strong">
          {i18nT('apps.issueRadar.views.graphView.waiting_heading', { n: waiting.length })}
        </div>
      )}
      <div id="ir-frontier-waiting">
        {shownWaiting.map((f) => (
          <FrontierRow key={f.node.id} item={f} unlocked={false} reduceMotion={reduceMotion} onOpenTree={onOpenTree} rowRef={setRowRef} />
        ))}
      </div>
      {waiting.length > WAITING_CAP && (
        <div className="max-w-[820px] mt-1.5">
          <button
            type="button"
            onClick={onToggleWaiting}
            aria-expanded={showAllWaiting}
            aria-controls="ir-frontier-waiting"
            className="text-[10px] text-muted-strong hover:text-text underline underline-offset-2 cursor-pointer bg-transparent border-none p-0 font-mono"
          >
            {showAllWaiting
              ? i18nT('apps.issueRadar.views.graphView.fewer_waiting')
              : i18nT('apps.issueRadar.views.graphView.more_waiting', { n: overflow })}
          </button>
        </div>
      )}
    </div>
  )
}

function FrontierRow({ item, unlocked, reduceMotion, onOpenTree, rowRef }: {
  item: FrontierItem
  unlocked: boolean
  reduceMotion: boolean
  onOpenTree: (id: number) => void
  rowRef: (id: number, el: HTMLDivElement | null) => void
}) {
  const [open, setOpen] = useState(false)
  const { node, blockers, waitingOn } = item
  const satisfied = blockers.filter((b) => b.state === 'done')
  const idColor = unlocked ? 'var(--accent)' : 'var(--warn)'
  return (
    <div
      ref={(el) => rowRef(node.id, el)}
      className="max-w-[820px] mb-2 border border-border rounded-[3px] bg-card data-[ir-reveal]:border-accent"
    >
      <button
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="w-full flex items-baseline gap-2.5 px-3.5 py-2.5 text-left cursor-pointer hover:bg-bg-hover"
      >
        <span className="text-[12px] font-bold flex-none" style={{ color: idColor }}>
          {SIGIL(node)}-{node.id}
        </span>
        <span className="text-[12px] text-text flex-1 min-w-0 truncate">{node.title}</span>
        <Led node={node} reduceMotion={reduceMotion} />
      </button>
      {open && (
        <div className="px-3.5 pb-3 pl-[30px] text-[10.5px] text-muted leading-relaxed">
          {unlocked ? (
            <WhyLine text={i18nT('apps.issueRadar.views.graphView.why_all_satisfied')} refs={satisfied} struck />
          ) : (
            <>
              <WhyLine text={i18nT('apps.issueRadar.views.graphView.why_waiting')} refs={waitingOn} />
              {satisfied.length > 0 && (
                <> · <WhyLine text={i18nT('apps.issueRadar.views.graphView.why_satisfied')} refs={satisfied} struck /></>
              )}
            </>
          )}
          {' · '}
          <button
            onClick={(e) => { e.stopPropagation(); onOpenTree(node.id) }}
            className="text-accent underline underline-offset-2 cursor-pointer bg-transparent border-none p-0 font-mono text-[10.5px]"
          >
            {i18nT('apps.issueRadar.views.graphView.open_tree')}
          </button>
        </div>
      )}
    </div>
  )
}

/** One WHY line: a whole-sentence catalog key with a `{{refs}}` placeholder the
 * styled reference list slots into — so the sentence stays in ONE key (a
 * translator can reorder around the refs) rather than being concatenated from
 * fragments across JSX. */
function WhyLine({ text, refs, struck = false }: {
  text: string
  refs: GraphNode[]
  struck?: boolean
}) {
  const parts = splitOnPlaceholder(text, 'refs')
  return (
    <span>
      {parts.map((p, i) =>
        p === null
          ? <Refs key="refs" nodes={refs} struck={struck} />
          : <span key={i}>{p}</span>,
      )}
    </span>
  )
}

/** A run of `#n` references, satisfied ones struck through. */
function Refs({ nodes, struck = false }: { nodes: GraphNode[]; struck?: boolean }) {
  if (nodes.length === 0) return <span className="text-muted-strong">—</span>
  return (
    <>
      {nodes.map((n, i) => (
        <span key={n.id}>
          {i > 0 && ' · '}
          <span className={struck ? 'text-muted-strong line-through' : 'text-text'}>#{n.id}</span>
        </span>
      ))}
    </>
  )
}

/** A small status dot + label used by frontier rows. */
function Led({ node, reduceMotion }: { node: GraphNode; reduceMotion: boolean }) {
  const c = stateColor(node.state)
  const go = node.state === 'ready' || node.unlocked
  return (
    <span className="flex-none inline-flex items-center gap-1.5">
      <span
        className={`inline-block w-[7px] h-[7px] rounded-full ${go && !reduceMotion ? 'animate-pulse' : ''}`}
        style={go
          ? { backgroundColor: 'var(--accent)' }
          : { backgroundColor: 'transparent', border: `1.5px solid ${c}` }}
        aria-hidden="true"
      />
      <span className="text-[9px] tracking-[.12em]" style={{ color: c }}>{stateLabel(node.state)}</span>
    </span>
  )
}

/* ── FOCUS TREE ───────────────────────────────────────────────────────── */

const FOCUS = {
  CW: 228, CH: 52, COLW: 310, X0: 48, Y0: 64, GAP: 20, GHOST_W: 160,
} as const

function FocusTree({ root, nodes, liveEdges, reduceMotion, onReRoot, onOpen }: {
  root: number | null
  nodes: Map<number, GraphNode>
  liveEdges: DepsResponse['edges']
  reduceMotion: boolean
  onReRoot: (id: number) => void
  onOpen: (n: GraphNode) => void
}) {
  const model = useMemo(() => {
    if (root == null || !nodes.has(root)) return null
    // LIVE constraint edges only: a satisfied blocker is history, shown as a
    // ghost, not a tree edge.
    const live = liveEdges.filter((e) => nodes.get(e.blocker)?.state !== 'done')
    const ids = componentOf(root, live)
    const compEdges = reduceInferred(live.filter((e) => ids.has(e.blocker) && ids.has(e.blocked)))
    const sub = componentModel(ids, compEdges, nodes)
    return { ids, layout: layoutGraph(sub.nodes), sub, edges: compEdges }
  }, [root, nodes, liveEdges])

  if (root == null || !model) {
    return (
      <div className="flex-1 min-h-0 flex items-center justify-center text-muted text-[12px] font-mono px-2 md:px-6 text-center">
        {i18nT('apps.issueRadar.views.graphView.focus_empty')}
      </div>
    )
  }

  const rootNode = nodes.get(root)!
  const ghosts = satisfiedBlockers(root, nodes).slice(0, 3)
  const L = model.layout
  const N = model.sub.nodes

  // Widen the canvas on the left to make room for ghost chips + dashed leads.
  const leftPad = ghosts.length ? FOCUS.GHOST_W + 60 : 0
  const totalW = L.width + leftPad + 24
  const totalH = Math.max(L.height + 40, 360)
  const shift = leftPad // translate the tree right so ghosts fit at x<leftPad

  const rootBox = L.nodes.get(root)!

  return (
    <div className="flex-1 min-h-0 overflow-auto">
      <svg
        role="img"
        aria-label={i18nT('apps.issueRadar.views.graphView.aria_tree', {
          kind: SIGIL(rootNode), number: root, nodes: model.ids.size, edges: model.edges.length,
        })}
        viewBox={`0 0 ${totalW} ${totalH}`}
        preserveAspectRatio="xMidYMid meet"
        className="block w-full h-full min-w-[320px]"
      >
        <rect width={totalW} height={totalH} fill="var(--bg)" />

        {/* Header line: tree of IS-<n>: X nodes · Y live edges. */}
        <text x={16} y={26} fill="var(--muted-strong)" fontSize={11} letterSpacing=".1em">
          {i18nT('apps.issueRadar.views.graphView.tree_stat', {
            kind: SIGIL(rootNode), number: root, nodes: model.ids.size, edges: model.edges.length,
          })}
        </text>

        {/* Satisfied ghost blockers of the root — struck, dashed leads. */}
        {ghosts.map((g, i) => {
          const gy = (rootBox.y + shift) - 70 - i * 46
          const gx = Math.max(8, (rootBox.x + shift) - leftPad + 8)
          const lead = [
            'M', gx + FOCUS.GHOST_W, gy + 18,
            'C', gx + FOCUS.GHOST_W + 40, gy + 18,
            (rootBox.x + shift) - 40, rootBox.y + FOCUS.CH / 2,
            rootBox.x + shift, rootBox.y + FOCUS.CH / 2,
          ].join(' ')
          return (
            <g key={g.id} opacity={0.5}>
              <path d={lead} fill="none" stroke="var(--muted-strong)" strokeWidth={1} strokeDasharray="2 5" />
              <rect x={gx} y={gy} width={FOCUS.GHOST_W} height={34} rx={3} fill="var(--card)" stroke="var(--muted-strong)" />
              <text x={gx + 10} y={gy + 15} fill="var(--muted-strong)" fontSize={10} fontWeight={700} textDecoration="line-through">
                {SIGIL(g)}-{g.id}
              </text>
              <text x={gx + 10} y={gy + 27} fill="var(--muted-strong)" fontSize={10}>
                {i18nT('apps.issueRadar.views.graphView.ghost_satisfied')}
              </text>
            </g>
          )
        })}

        {/* Live edges. */}
        {L.edges.map((g) => {
          const a = L.nodes.get(g.edge.blocker)!
          const b = L.nodes.get(g.edge.blocked)!
          const ax = a.x + shift + FOCUS.CW
          const ay = a.y + FOCUS.CH / 2
          const bx = b.x + shift
          const by = b.y + FOCUS.CH / 2
          const mx = (ax + bx) / 2
          const d = ['M', ax, ay, 'C', mx + 34, ay, mx - 34, by, bx, by].join(' ')
          const col = stateColor(N.get(g.edge.blocked)?.state ?? 'hold')
          return (
            <path
              key={g.key}
              d={d}
              fill="none"
              stroke={col}
              strokeWidth={1.6}
              opacity={0.8}
              strokeDasharray={g.edge.source === 'inferred' ? '3 6' : undefined}
              strokeOpacity={g.edge.source === 'inferred' ? 0.55 : 0.8}
            />
          )
        })}

        {/* Nodes — click to re-root, Enter to open in-app. */}
        {[...N.values()].map((n) => {
          const box = L.nodes.get(n.id)!
          const x = box.x + shift
          const y = box.y
          const sel = n.id === root
          const c = stateColor(n.state)
          const title = n.title || ''
          const shown = title.length > 26 ? `${title.slice(0, 26)}…` : title
          const ledLabel = n.state === 'ready'
            ? i18nT('apps.issueRadar.views.graphView.led_ready')
            : model.edges.some((e) => e.blocked === n.id)
              ? i18nT('apps.issueRadar.views.graphView.led_wait')
              : i18nT('apps.issueRadar.views.graphView.led_open')
          return (
            <g
              key={n.id}
              role="button"
              tabIndex={0}
              aria-label={i18nT('apps.issueRadar.views.graphView.aria_node', {
                kind: SIGIL(n), number: n.id, state: stateLabel(n.state), title: title || String(n.id),
              })}
              style={{ cursor: 'pointer' }}
              onClick={() => onReRoot(n.id)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') { e.preventDefault(); onOpen(n) }
                else if (e.key === ' ') { e.preventDefault(); onReRoot(n.id) }
              }}
            >
              {sel && (
                <rect x={x - 4} y={y - 4} width={FOCUS.CW + 8} height={FOCUS.CH + 8} rx={6} fill="none" stroke="var(--accent)" opacity={0.3} />
              )}
              <rect
                x={x} y={y} width={FOCUS.CW} height={FOCUS.CH} rx={3}
                fill="var(--card)"
                stroke={sel ? 'var(--accent)' : 'var(--border-strong)'}
                strokeWidth={sel ? 2 : 1.2}
              />
              <text x={x + 11} y={y + 20} fill={c} fontSize={12} fontWeight={700}>
                {SIGIL(n)}-{n.id}
              </text>
              <text x={x + FOCUS.CW - 11} y={y + 20} fill={c} fontSize={10} letterSpacing=".1em" textAnchor="end">
                {ledLabel}
              </text>
              {n.ciFailed && (
                <text x={x + FOCUS.CW - 11} y={y + 34} fill="var(--danger)" fontSize={10} letterSpacing=".1em" textAnchor="end">
                  {i18nT('apps.issueRadar.views.graphView.ci_fail')}
                </text>
              )}
              <text x={x + 11} y={y + 38} fill="var(--muted)" fontSize={10}>
                {shown}
              </text>
              {(n.state === 'ready' || n.unlocked) && !reduceMotion && (
                <circle cx={x + FOCUS.CW - 6} cy={y + FOCUS.CH - 6} r={3.2} fill="var(--accent)">
                  <animate attributeName="opacity" values="0.35;1;0.35" dur="1.8s" repeatCount="indefinite" />
                </circle>
              )}
            </g>
          )
        })}
      </svg>
    </div>
  )
}

/* ── EMPTY / UNAVAILABLE ──────────────────────────────────────────────── */

/** Designed empty state — the deps route is not enabled yet (404/500), errored,
 * or the repo has no dependency edges. Not a bare "no data": a small unpopulated
 * board so the tab still stands alone. */
function EmptyBoard({ error, onRetry, refreshing }: { error: boolean; onRetry: () => void; refreshing: boolean }) {
  return (
    <div className="h-full flex flex-col items-center justify-center gap-4 bg-bg text-text font-mono px-2 md:px-6">
      {/* Decorative mini-board drawn with borders, not an inline SVG (the icon
          rule blocks single-line SVGs in added lines; the data plane is the
          FOCUS tree, drawn as a multi-line SVG). */}
      <div className="relative w-56 h-28 border border-border" aria-hidden="true">
        {(['-top-px -left-px', '-top-px -right-px', '-bottom-px -left-px', '-bottom-px -right-px'] as const).map((pos) => (
          <span key={pos} className={`absolute ${pos} w-3 h-3 border border-muted-strong rounded-full`} />
        ))}
        <span className="absolute top-2 inset-x-0 text-center text-[10px] tracking-[.2em] text-muted-strong">
          {i18nT('apps.issueRadar.views.graphView.empty_board')}
        </span>
        <span className="absolute left-6 top-1/2 -translate-y-1/2 w-12 h-8 bg-card border border-border-strong" />
        <span className="absolute right-6 top-1/2 -translate-y-1/2 w-12 h-8 bg-card border border-border-strong" />
        <span className="absolute left-[4.5rem] right-[4.5rem] top-1/2 border-t border-dashed border-border-strong" />
      </div>
      <div className="flex items-center gap-1.5 text-[14px] font-medium text-text">
        <Waypoints size={16} className="text-accent opacity-70" />
        {error
          ? i18nT('apps.issueRadar.views.graphView.empty_unavailable_title')
          : i18nT('apps.issueRadar.views.graphView.empty_title')}
      </div>
      <p className="text-[12px] text-muted max-w-sm text-center leading-relaxed">
        {error
          ? i18nT('apps.issueRadar.views.graphView.empty_unavailable_blurb')
          : i18nT('apps.issueRadar.views.graphView.empty_blurb')}
      </p>
      <button
        onClick={onRetry}
        disabled={refreshing}
        className="inline-flex items-center gap-1.5 text-[11px] tracking-[.06em] px-4 py-2 border border-border-strong rounded-md text-text hover:border-accent hover:text-accent disabled:opacity-40 cursor-pointer"
      >
        <RefreshCw size={12} className={refreshing ? 'animate-spin' : ''} />
        {i18nT('apps.issueRadar.views.graphView.retry')}
      </button>
    </div>
  )
}
