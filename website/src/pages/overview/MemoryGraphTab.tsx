import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AnimatePresence, motion } from 'framer-motion'
import { Circle, Maximize2, Minus, Network as NetworkIcon, Plus, RefreshCw, Square, Triangle } from 'lucide-react'
import type { Graph as CosmosGraph } from '@cosmos.gl/graph'
import { api } from '../../api/client'
import type { GraphBbox, GraphCluster, GraphKind, GraphNodeRow } from '../../api/client/memory'
import { Card, CardTitle, Btn, IconButton, IconButtonGroup } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { errMessage } from '../../utils/thunkError'
import { queryReadFailed } from '../../utils/queryReadFailed'
import { i18nT } from '../../i18n/t'
import { fmtNumber } from '../../i18n/format'
import { useReducedMotion } from '../../hooks/useReducedMotion'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useIsTouchDevice } from '../../hooks/useIsTouchDevice'
import { useEscapeLayer } from '../../hooks/useEscapeLayer'
import { useJumpToMemories, type GraphBoxPick } from './memoryPage/memoryJump'
import { MemoryGraphDetail } from './memoryPage/MemoryGraphDetail'
import { KindPills } from './memoryPage/KindPills'
import type { KindTab } from './memoryPage/memoryListView'
import { attachGraphGestures, type Box } from './memoryGraphGestures'
import {
  FETCH_MARGIN, LABEL_PX, MAX_LABELS, LINK_BUDGET, TOPIC_LINE_HEIGHT, labelGrid, topicLinePx, topicLines, topicOrder, NODE_BUDGET, canvasLabel, memoryLines, contains, dotPxAt, drawnLinks, fitBox, linkAlpha, linkLevel, linkRule, linksInView, inkAlpha, topicLinkAlpha, topicLinks, labelBudget, zoomFromFit, nextInDirection, panBy, placeLabels, screenRectToBox, showsNodes,
  toScreen, visibleBox, zoomAt, type Dir, type DrawnLink, type LabelCandidate, type PlacedLabel, type ScreenMark, type Size, type View,
} from './memoryGraph/graphView'
import { NodeCache, PAGE_ROWS, gridStep, tilesFor } from './memoryGraph/nodeCache'
import { KIND_SHAPE, KIND_SIZE, readGraphColors, topicColors, type GraphColors } from './memoryGraph/graphColors'

// Bundle shape, not runtime: cosmos.gl's d3 modules sit in the `vendor-d3`
// chunk, which stays a lazy chunk of its own only while a page reaches it
// through `import('d3')`, as this tab did before cosmos.gl. Without this edge
// the bundler folds that chunk into the shared ones and re-cuts unrelated
// chunks: the first load grew 52.6 KB and Excalidraw's font chunk merged into
// `prod` (check-bundle-size.mjs on an analyze build, 2026-11-26).
export const loadD3 = () => import('d3')

/** Smallest canvas height; otherwise it fills the window below its toolbar. */
const MIN_CANVAS = 420
/** The dashboard's standard motion: a cluster opening into its nodes, a camera move. */
const MOTION_MS = 250
const easeOut = (t: number) => 1 - (1 - t) ** 3
/** Past this many nodes on screen, labels name clusters rather than memories. */
const DENSE_LABELS = 300
/** A faded experience never drops below this opacity, so it stays findable. */
const MIN_ALPHA = 0.45
const DIMMED_ALPHA = 0.1
/** A cluster's soft disc is never smaller than this, in CSS pixels. */
const CLUSTER_MIN_PX = 7
const MATCH_EXTRA_PX = 3
/** Place replacement labels this long after the view stops moving. */
const SETTLE_MS = 120
/** A zoom resizes the marks at most this often; the settle sizes them exactly. */
const PAINT_MS = 100
/** Tiles that arrive within this long of each other join the drawing together. */
const SWAP_MS = 250
/** Tile reads in flight at once. */
const FETCH_LANES = 4
/** The zoom controls' corner (bottom-3 right-3, Fit above a vertical +/− group),
 *  kept clear of labels so none is drawn under a button. */
const CONTROLS_BOX = (size: Size) => ({ x: size.width - 60, y: size.height - 160, width: 60, height: 160 })
/** The "Hold Space to pan" line's corner (bottom-3 left-3), kept clear of labels too. */
const PAN_HINT_BOX = (size: Size) => ({ x: 0, y: size.height - 44, width: 200, height: 44 })
/** Zoom step of the + and − buttons and keys. */
const ZOOM_STEP = 1.6
/** On-screen link length (CSS px) where a link starts to fade, and where it is a trace. */
const LINK_NEAR_PX = 60
const LINK_FAR_PX = 220
/** cosmos.gl's default `spaceSize`; its transform is centred on half of it. */
const COSMOS_SPACE = 4096

const TAB_KIND: Record<Exclude<KindTab, 'all'>, GraphKind> = { fact: 'fact', directive: 'lesson', episode: 'episode' }
/** Each kind pill's mark: the shape its memories are drawn as, so the pills are the key. */
// Filled, with a round stroke join, so the square and triangle corners are rounded
// like the canvas marks (scripts/cosmos-rounded-marks.mjs).
const MARK = { size: 10, strokeWidth: 3, fill: 'currentColor', strokeLinejoin: 'round', className: 'shrink-0 text-muted', 'aria-hidden': true } as const
const KIND_MARKS: Partial<Record<KindTab, React.ReactNode>> = {
  fact: <Circle {...MARK} />,
  directive: <Square {...MARK} />,
  episode: <Triangle {...MARK} />,
}
/** A topic's name size, the same for every topic: 13 px on a phone-wide canvas, 15 wider. */
const topicPx = (width: number) => (width < 600 ? 13 : 15)

type Shown =
  | { mode: 'clusters'; items: GraphCluster[] }
  | { mode: 'nodes'; items: GraphNodeRow[] }

type Tile = { key: string; box: GraphBbox }

/** A memory's dot size for the view: smallest with the whole store in view. */
const dotPx = (view: View, size: Size, bounds: GraphBbox | undefined) => dotPxAt(zoomFromFit(view, size, bounds))

/**
 * The tiles to read, keyed by filter, on one fixed grid per layout. A store of
 * one page is one read; a store that fits the node budget is read whole, in
 * parallel tiles, so a pan or zoom never reaches an unread place; a larger one
 * reads the tiles around the view. `inView` keeps only the tiles the view
 * touches: the ones that must be in before nodes replace clusters.
 */
function neededTiles(view: View, size: Size, kinds: string[] | undefined, bounds: GraphBbox | undefined, total: number, inView: boolean) {
  const kindKey = kinds?.join(',') || 'all'
  if (!bounds) return { kindKey, tiles: [] as Tile[] }
  if (total <= PAGE_ROWS) return { kindKey, tiles: [{ key: 'whole', box: bounds }] }
  const seen = visibleBox(view, size, inView ? 0 : FETCH_MARGIN)
  // Tiles outside the layout hold nothing and are never read.
  const clip: GraphBbox = [Math.max(seen[0], bounds[0]), Math.max(seen[1], bounds[1]), Math.min(seen[2], bounds[2]), Math.min(seen[3], bounds[3])]
  if (clip[0] > clip[2] || clip[1] > clip[3]) return { kindKey, tiles: [] as Tile[] }
  const want = total <= NODE_BUDGET && !inView ? bounds : clip
  return { kindKey, tiles: tilesFor(want, gridStep(bounds)) }
}

/** The graph of one store: `store` names a bot's memory; empty is Global memory.
 *  Memoized: the page around it re-renders on its own polls, and re-rendering
 *  this tab then cost a pan or zoom a 250 ms frame on a phone. */
export default memo(MemoryGraphTab)

function MemoryGraphTab({ store = '' }: { store?: string }) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLDivElement>(null)
  const cosmosRef = useRef<CosmosGraph | null>(null)
  const originRef = useRef<{ ox: number; oy: number } | null>(null)
  const viewRef = useRef<View>({ cx: 0, cy: 0, k: 1 })
  const sizeRef = useRef<Size>({ width: 800, height: MIN_CANVAS })
  const shownRef = useRef<Shown>({ mode: 'clusters', items: [] })
  /** The overview's links between neighbouring topics (topicLinks). */
  const topicLinksRef = useRef<DrawnLink[]>([])
  const cacheRef = useRef(new NodeCache())
  const colorsRef = useRef<GraphColors | null>(null)
  const hoveredRef = useRef<number | null>(null)
  const reduced = useReducedMotion()
  const phone = useIsMobile()
  // A touch screen has no Space key: one finger pans there.
  const touch = useIsTouchDevice()
  const touchRef = useRef(touch)
  touchRef.current = touch
  const reducedRef = useRef(reduced)
  reducedRef.current = reduced
  const jump = useJumpToMemories()
  /** The memory a tap or Enter opened, shown over the graph (MemoryGraphDetail). */
  const [opened, setOpened] = useState<{ ref: string; record: GraphNodeRow['record'] } | null>(null)

  const [height, setHeight] = useState(MIN_CANVAS)
  const [kind, setKind] = useState<KindTab>('all')
  const kinds = useMemo(() => (kind === 'all' ? undefined : [TAB_KIND[kind]]), [kind])
  const kindsRef = useRef(kinds)
  kindsRef.current = kinds
  const [searchInput, setSearchInput] = useState('')
  const [search, setSearch] = useState('')
  const [matches, setMatches] = useState<Set<string> | null>(null)
  const matchesRef = useRef(matches)
  matchesRef.current = matches
  const [box, setBox] = useState<Box | null>(null)
  const [picked, setPicked] = useState<GraphBoxPick | null>(null)
  const [selectPending, setSelectPending] = useState(false)
  const cancelGesture = useRef<(() => void) | null>(null)
  /** Bumped when a box count in flight no longer matches what is on screen. */
  const selectGen = useRef(0)
  const clearSelection = useCallback(() => {
    cancelGesture.current?.()
    selectGen.current++
    setSelectPending(false); setPicked(null); setBox(null)
  }, [])
  // Escape cancels a drag, a pending count or a selection through the shared stack.
  useEscapeLayer(picked !== null || box !== null || selectPending, clearSelection)
  // Escape closes an opened memory first; the graph behind it keeps its view.
  useEscapeLayer(opened !== null, () => setOpened(null))
  /** The names on the canvas live in their own layer (GraphLabels): placing them
   *  re-renders only that layer, not the whole tab, which cost a settle 100+ ms. */
  const labelsRef = useRef<PlacedLabel[]>([])
  const labelSink = useRef<(labels: PlacedLabel[]) => void>(() => {})
  const setLabels = useCallback((next: PlacedLabel[]) => {
    if (next.length === 0 && labelsRef.current.length === 0) return
    labelsRef.current = next; labelSink.current(next)
  }, [])
  const [hover, setHover] = useState<PlacedLabel | null>(null)
  const [focusId, setFocusId] = useState<string | null>(null)
  const focusRef = useRef(focusId)
  focusRef.current = focusId
  const [tick, setTick] = useState(0)
  /** A node, search or box read that failed, with the step that tries it again. */
  const [liveError, setLiveError] = useState<{ error: unknown; retry: () => void } | null>(null)
  /** The canvas could not load or start (no WebGL, a failed chunk). */
  const [canvasError, setCanvasError] = useState<unknown>(null)
  /** Bumped by Refresh, so the search runs again too. */
  const [reloads, setReloads] = useState(0)

  const overviewRead = useQuery({
    queryKey: ['memory-graph', 'overview', store],
    queryFn: () => api.memoryGraphOverview(store),
    // The first open of a store starts its layout build: check again until it is in.
    refetchInterval: q => (q.state.data?.status === 'building' ? 1000 : false),
  })
  const { data: overview, isError, error, isFetching, refetch } = overviewRead
  const clusters = useMemo(() => overview?.clusters ?? [], [overview])
  const clustersRef = useRef(clusters)
  clustersRef.current = clusters
  const totals = overview?.totals ?? {}
  const total = (totals.episode || 0) + (totals.fact || 0) + (totals.lesson || 0)
  const selectedTotal = kinds ? kinds.reduce((n, k) => n + (totals[k] || 0), 0) : total
  const ready = overview?.status === 'ready' && total > 0
  const boundsRef = useRef(overview?.bounds)
  boundsRef.current = overview?.bounds
  /** The selected kinds fit the node budget: read them whole, and never draw clusters. */
  const wholeRef = useRef(false)
  wholeRef.current = selectedTotal <= NODE_BUDGET
  const totalRef = useRef(selectedTotal)
  totalRef.current = selectedTotal
  /** The full build the drawn positions came from; a reply from another one means it moved. */
  const layoutRef = useRef(overview?.layout)

  // ---- drawing -------------------------------------------------------------

  /** Push the view to cosmos.gl: its transform puts layout point (cx, cy) mid-screen. */
  const applyView = useCallback(() => {
    const g = cosmosRef.current, o = originRef.current
    if (!g || !o) return
    const { cx, cy, k } = viewRef.current, { width: w, height: h } = sizeRef.current
    g.setViewTransform({ k, x: w / 2 - k * cx + k * o.ox - (k * w) / 2, y: h / 2 + k * cy - k * o.oy - (k * h) / 2 })
  }, [])

  const measureRef = useRef<{ ctx: CanvasRenderingContext2D | null; family: string; widths: Map<string, number> } | null>(null)
  const widthOf = useCallback((t: string, px: number, bold = px > LABEL_PX) => {
    if (!measureRef.current) {
      const ctx = document.createElement('canvas').getContext('2d')
      // Measured in the labels' own font, read from the page like scrollPeek does.
      const cs = canvasRef.current ? getComputedStyle(canvasRef.current) : null
      measureRef.current = { ctx, family: cs?.fontFamily ?? 'sans-serif', widths: new Map() }
    }
    const { ctx, family, widths } = measureRef.current
    const key = `${px}|${bold ? 1 : 0}|${t}`
    let w = widths.get(key)
    if (w === undefined) {
      if (ctx) ctx.font = `${bold ? 600 : 400} ${px}px ${family}`
      w = Math.ceil(ctx ? ctx.measureText(t).width : t.length * px * 0.6) + 12; widths.set(key, w)
    }
    return w
  }, [])
  const placeOverlay = useCallback((keepPrevious = false) => {
    const shown = shownRef.current, view = viewRef.current, size = sizeRef.current
    // A topic is named over its cluster in a deeper shade of the cluster's colour, as data maps do.
    const tint = topicColors(clusters, boundsRef.current, colorsRef.current?.dark ?? false).text
    // Topic names are placed in spiral order (topicOrder), one size for the view,
    // each on its own disc, skipped when its grid cell already has a name or it
    // would touch one already placed.
    const zoom = zoomFromFit(view, size, boundsRef.current)
    const topics = () => {
      const named = clusters.filter(c => c.label && c.count > 0)
      const order = topicOrder(named, view, size, boundsRef.current, zoom)
      const px = topicPx(size.width)
      return named.map((c, i) => {
        const lines = topicLines(c.label!, line => widthOf(line, px, true), topicLinePx(size.width))
        return { id: `c${c.id}`, at: toScreen(view, size, c), text: lines.join(' '), lines, rank: -order[i], px, color: tint.get(c.id), centred: true }
      })
    }
    let cands: LabelCandidate[]
    let marks: ScreenMark[] = []
    if (shown.mode === 'clusters') cands = topics()
    else {
      const markPx = dotPx(view, size, boundsRef.current)
      // Only what is on screen is counted and named: cutting a label for each of
      // tens of thousands of memories cost a settle hundreds of ms on a phone.
      const seen: { n: GraphNodeRow; at: { x: number; y: number } }[] = []
      for (const n of shown.items) {
        const at = toScreen(view, size, n)
        if (at.x >= 0 && at.y >= 0 && at.x <= size.width && at.y <= size.height) seen.push({ n, at })
        if (seen.length > DENSE_LABELS) break
      }
      // A dense view names its topics; single memories get names once few are on screen.
      if (seen.length > DENSE_LABELS) cands = topics()
      else {
        // Each memory is named by its short title over at most two lines, never
        // cut: one that does not fit, or would cover others, is left unnamed.
        cands = seen.flatMap(({ n, at }) => {
          const lines = memoryLines(canvasLabel(n), line => widthOf(line, LABEL_PX))
          return lines ? [{ id: n.id, at, text: lines.join(' '), lines, rank: n.weight + (matchesRef.current?.has(n.id) ? 10 : 0), mark: markPx }] : []
        })
        marks = cands.map(n => ({ id: n.id, at: n.at, px: markPx }))
      }
    }
    // Topic names spread over the whole view, as many as fit without touching.
    const budget = shown.mode === 'clusters' || marks.length === 0 ? MAX_LABELS : labelBudget(zoom)
    // Topic names: one per cell of a grid about 2 names wide by 3 names tall.
    const nameH = Math.round(topicPx(size.width) * TOPIC_LINE_HEIGHT * 2 + topicPx(size.width) * 0.5)
    const avoid = touchRef.current ? [CONTROLS_BOX(size)] : [CONTROLS_BOX(size), PAN_HINT_BOX(size)]
    const grid = labelGrid(size, topicLinePx(size.width), nameH)
    const placed = placeLabels(cands, size, budget, widthOf, marks, marks.length ? undefined : grid, avoid)
    // A sparse view can have no title that fits; keep naming its topics then.
    const next = placed.length ? placed : placeLabels(topics(), size, MAX_LABELS, widthOf, [], grid, avoid)
    // A camera move may leave every topic anchor off screen before any title
    // fits. Hold the last names until there is a replacement, not an empty set.
    if (next.length || !keepPrevious) setLabels(next)
  }, [clusters, setLabels, widthOf])

  const linksRef = useRef<DrawnLink[]>([])
  const allLinksRef = useRef<DrawnLink[]>([])
  /** The drawn links' colours: their cluster's colour, faint with the whole store
   *  in view and stronger zoomed in; a hovered or focused memory's own links lit
   *  and the rest dimmed. */
  const paintLinks = useCallback(() => {
    const g = cosmosRef.current, colors = colorsRef.current
    if (!g || !colors) return
    const links = linksRef.current, shown = shownRef.current
    const zoom = zoomFromFit(viewRef.current, sizeRef.current, boundsRef.current)
    const topic = topicColors(clustersRef.current, boundsRef.current, colors.dark).dot
    const focusIndex = shown.mode === 'nodes' && focusRef.current ? shown.items.findIndex(x => (x as GraphNodeRow).id === focusRef.current) : -1
    const at = hoveredRef.current ?? (focusIndex >= 0 ? focusIndex : null)
    const rgba = new Float32Array(links.length * 4)
    links.forEach((l, i) => {
      const rgb = topic.get(l.cluster) ?? colors.unclustered
      const focus = at === null ? 'none' : l.a === at || l.b === at ? 'lit' : 'dim'
      // Written in place: a temporary array per link fed the garbage collector.
      rgba[i * 4] = rgb[0]; rgba[i * 4 + 1] = rgb[1]; rgba[i * 4 + 2] = rgb[2]; rgba[i * 4 + 3] = l.hub ? topicLinkAlpha(l.sim) : linkAlpha(zoom, l.sim, focus)
    })
    g.setLinkColors(rgba)
  }, [])

  /** The box, zoom and level the drawn links were picked for, and from which links. */
  const pickedRef = useRef<{ box: GraphBbox; k: number; level: string; all: DrawnLink[] } | null>(null)
  /** Hand cosmos.gl the links worth drawing for this view (linksInView). Handing
   *  links over rebuilds cosmos.gl's link tables (about 90 ms on a throttled
   *  phone), so a settle keeps the drawn set while the view stays inside the box
   *  it was picked for (the view plus FETCH_MARGIN) at about the same zoom. */
  const pickLinks = useCallback((onSettle = false) => {
    const g = cosmosRef.current
    if (!g) return
    const shown = shownRef.current, view = viewRef.current, all = allLinksRef.current
    const level = linkLevel(zoomFromFit(view, sizeRef.current, boundsRef.current))
    const last = pickedRef.current
    if (onSettle && last && last.all === all && last.level === level && Math.abs(Math.log(view.k / last.k)) < Math.log(1.5)
      && contains(last.box, visibleBox(view, sizeRef.current))) { paintLinks(); return }
    const box = visibleBox(view, sizeRef.current, FETCH_MARGIN)
    pickedRef.current = { box, k: view.k, level, all }
    // The overview joins topics, not memories; closer in, each memory's closest
    // few, inside its topic and then to its neighbours (linkLevel).
    const links = shown.mode !== 'nodes' ? []
      : level === 'topics' ? topicLinksRef.current
        : linksInView(all, shown.items as GraphNodeRow[], box, LINK_BUDGET, linkRule(level, view.k, LINK_FAR_PX))
    linksRef.current = links
    const pairs = new Float32Array(links.length * 2)
    links.forEach((l, i) => { pairs[i * 2] = l.a; pairs[i * 2 + 1] = l.b })
    g.setLinks(pairs)
    g.setLinkWidths(new Float32Array(links.length).fill(1))
    paintLinks()
  }, [paintLinks])

  /** Each shown mark's size in CSS px at this zoom: a memory by its kind (and a
   *  search match larger), a cluster about as wide on screen as it is. */
  const sizesRef = useRef(new Float32Array())
  const markSizes = useCallback((nodePx: number) => {
    const shown = shownRef.current, m = matchesRef.current, k = viewRef.current.k
    // One buffer per shown set: a zoom asks ten times a second, and a new array
    // each time fed the garbage collector pauses of 100 ms and more.
    if (sizesRef.current.length !== shown.items.length) sizesRef.current = new Float32Array(shown.items.length)
    const sizes = sizesRef.current
    shown.items.forEach((item, i) => {
      if (shown.mode === 'clusters') {
        // A soft disc about as wide on screen as the cluster, never a blob.
        sizes[i] = Math.max(CLUSTER_MIN_PX, Math.min(48, 2 * (item as GraphCluster).r * k))
      } else {
        const node = item as GraphNodeRow
        sizes[i] = (m?.has(node.id) ? nodePx + MATCH_EXTRA_PX : nodePx) * KIND_SIZE[node.kind]
      }
    })
    return sizes
  }, [])

  /** Sizes alone, for the frames of a zoom: a full paint at every step costs the frame. */
  const resize = useCallback(() => {
    cosmosRef.current?.setPointSizes(markSizes(dotPx(viewRef.current, sizeRef.current, boundsRef.current)))
  }, [markSizes])

  /** Colours and sizes of what is shown, for the current search and focus. */
  const paint = useCallback(() => {
    const g = cosmosRef.current, colors = colorsRef.current
    if (!g || !colors) return
    const shown = shownRef.current, m = matchesRef.current, n = shown.items.length
    const nodePx = dotPx(viewRef.current, sizeRef.current, boundsRef.current), k = viewRef.current.k
    const topic = topicColors(clustersRef.current, boundsRef.current, colors.dark).dot
    const byId = new Map(clustersRef.current.map(c => [c.id, c]))
    // Each cluster's dots ink a fixed share of its disc on screen: light where they pile up, solid once zoomed in.
    const ink = new Map(clustersRef.current.map(c => [c.id, inkAlpha(c.count, nodePx, c.r * k)]))
    const rgba = new Float32Array(n * 4), shapes = new Float32Array(n)
    shown.items.forEach((item, i) => {
      let rgb: [number, number, number], alpha: number
      if (shown.mode === 'clusters') {
        const c = item as GraphCluster
        rgb = topic.get(c.id) ?? colors.unclustered; alpha = 0.35
      } else {
        const node = item as GraphNodeRow
        // Colour is the topic; kind is the shape.
        rgb = topic.get(node.cluster) ?? colors.unclustered
        shapes[i] = KIND_SHAPE[node.kind]
        const share = byId.has(node.cluster) ? ink.get(node.cluster)! : 1
        // An experience fades with recall's weight for it; facts and lessons do not.
        alpha = share * (node.kind === 'episode' ? Math.max(MIN_ALPHA, Math.min(1, node.weight)) : 1)
        if (m) alpha = m.has(node.id) ? 1 : DIMMED_ALPHA
      }
      rgba[i * 4] = rgb[0]; rgba[i * 4 + 1] = rgb[1]; rgba[i * 4 + 2] = rgb[2]; rgba[i * 4 + 3] = alpha
    })
    g.setPointColors(rgba)
    g.setPointSizes(markSizes(nodePx))
    g.setPointShapes(shapes)
    const focusIndex = shown.mode === 'nodes' && focusRef.current ? shown.items.findIndex(x => (x as GraphNodeRow).id === focusRef.current) : -1
    g.setConfigPartial({ focusedPointIndex: focusIndex >= 0 ? focusIndex : undefined })
    paintLinks()
  }, [markSizes, paintLinks])

  /**
   * Show clusters or nodes for the current view. Entering the node view opens
   * each cluster: its nodes start at the cluster's centre and move to their own
   * places (instant with reduced motion).
   */
  const refreshShown = useCallback(() => {
    const g = cosmosRef.current
    if (!g) return
    const before = shownRef.current
    // Clusters stay until the view's tiles are in, so a cold open never blanks the
    // canvas and the nodes open out of their cluster when they arrive. Zoomed in past
    // the clusters, the first tile in view is enough: the rest join as they come, so
    // a slow read never leaves the view holding one growing, empty disc.
    const tiles = neededTiles(viewRef.current, sizeRef.current, kindsRef.current, boundsRef.current, totalRef.current, true)
    const cached = (t: Tile) => cacheRef.current.has(`${tiles.kindKey}|${t.key}`)
    const nodeLevel = wholeRef.current || showsNodes(viewRef.current, sizeRef.current, clusters)
    const zoomedIn = !!boundsRef.current && !contains(visibleBox(viewRef.current, sizeRef.current), boundsRef.current)
    const wantNodes = nodeLevel
      && (before.mode === 'nodes' || tiles.tiles.every(cached) || (zoomedIn && tiles.tiles.some(cached)))
    let next: Shown
    if (wantNodes) {
      const want = kindsRef.current
      next = { mode: 'nodes', items: cacheRef.current.all().filter(n => !want || want.includes(n.kind)) }
    } else {
      // shortcut: overview clusters include every kind; filter them when replies carry per-kind counts.
      next = { mode: 'clusters', items: clusters.filter(c => c.count > 0) }
    }
    shownRef.current = next
    // Links join the memories drawn now; clusters have none.
    allLinksRef.current = next.mode === 'nodes' ? drawnLinks(next.items as GraphNodeRow[]) : []
    topicLinksRef.current = next.mode === 'nodes' ? topicLinks(allLinksRef.current, next.items as GraphNodeRow[], clusters) : []
    pickLinks()
    const pos = new Float32Array(next.items.length * 2)
    next.items.forEach((it, i) => { pos[i * 2] = it.x; pos[i * 2 + 1] = it.y })
    const opening = before.mode === 'clusters' && next.mode === 'nodes' && next.items.length > 0
    if (opening && !reducedRef.current) {
      const at = new Map(clusters.map(c => [c.id, c]))
      const start = new Float32Array(pos.length)
      ;(next.items as GraphNodeRow[]).forEach((nd, i) => {
        const c = at.get(nd.cluster)
        start[i * 2] = c ? c.x : nd.x; start[i * 2 + 1] = c ? c.y : nd.y
      })
      g.setPointPositions(start, true)
      paint()
      g.render(0, 0)
      g.setPointPositions(pos, true)
      g.render(0, MOTION_MS)
    } else {
      // Nodes that arrived since the last draw join at their places, with no motion.
      g.setPointPositions(pos, true)
      paint()
      g.render(0, 0)
    }
    placeOverlay()
  }, [clusters, paint, pickLinks, placeOverlay])

  // ---- fetching nodes for the view -------------------------------------------

  /**
   * Read the tiles the view needs, in the background, while the canvas keeps
   * drawing what it has. A tile is read once on the fixed grid, so moving the
   * view never reloads one; each tile that arrives joins the drawing at once.
   * A pass already running only marks that the view moved, and reads again.
   */
  const fetchGen = useRef(0)
  const inflight = useRef(new Set<string>())
  const passRef = useRef<{ running: boolean; again: boolean }>({ running: false, again: false })
  const swapFrame = useRef<number | null>(null)
  const swappedAt = useRef(0)
  /** When the view last moved (syncView). */
  const movedAt = useRef(0)
  const swapIn = useCallback(() => {
    if (swapFrame.current !== null) return
    // Each swap re-uploads every drawn mark: tiles arriving together join in one.
    const wait = Math.max(0, swappedAt.current + SWAP_MS - performance.now())
    const swap = () => {
      // Not while the view moves: the swap would cost a pan or zoom its frames.
      // The view's own tiles still come in at once when nothing is drawn for it.
      const still = movedAt.current + SETTLE_MS - performance.now()
      if (still > 0 && shownRef.current.mode === 'nodes') { swapFrame.current = window.setTimeout(swap, still); return }
      swapFrame.current = null
      swappedAt.current = performance.now()
      liveFns.current.refreshShown()
    }
    swapFrame.current = window.setTimeout(swap, wait)
  }, [])
  const fetchVisible = useCallback(() => {
    const pass = passRef.current
    if (pass.running) { pass.again = true; return }
    if (!wholeRef.current && !showsNodes(viewRef.current, sizeRef.current, clusters)) return
    const gen = fetchGen.current
    // One filter per tile read: a reply is stored under the filter it was asked for.
    const askKinds = kindsRef.current
    const { kindKey, tiles } = neededTiles(viewRef.current, sizeRef.current, askKinds, boundsRef.current, totalRef.current, false)
    // The tiles nearest the middle of the view first, so what is on screen arrives
    // before the rest of a store that is read whole.
    const { cx, cy } = viewRef.current
    const away = (t: Tile) => Math.hypot((t.box[0] + t.box[2]) / 2 - cx, (t.box[1] + t.box[3]) / 2 - cy)
    const queue = tiles.filter(t => !cacheRef.current.has(`${kindKey}|${t.key}`) && !inflight.current.has(`${kindKey}|${t.key}`))
      .sort((a, b) => away(a) - away(b))
    if (!queue.length) return
    pass.running = true
    const readTile = async (tile: Tile) => {
      const key = `${kindKey}|${tile.key}`
      inflight.current.add(key)
      const rows: GraphNodeRow[] = []
      let cursor: string | null = null
      try {
        // shortcut: a very large store can make one tile large; subdivide tiles
        // whose first page returns a cursor when that cost needs bounding.
        do {
          const page = await api.memoryGraphNodes(store, tile.box, { kinds: askKinds, cursor })
          if (gen !== fetchGen.current) return // the cache was cleared for a new layout
          if (page.layout !== layoutRef.current) { liveFns.current.relayout(); return }
          rows.push(...page.nodes)
          cursor = page.cursor
        } while (cursor)
        cacheRef.current.add(key, rows)
        swapIn()
      } finally {
        inflight.current.delete(key)
      }
    }
    const lanes = Array.from({ length: Math.min(FETCH_LANES, queue.length) }, async () => {
      for (let tile = queue.shift(); tile; tile = queue.shift()) await readTile(tile)
    })
    void Promise.all(lanes).catch(error => {
      if (gen === fetchGen.current) setLiveError({ error, retry: fetchVisible })
    }).finally(() => {
      pass.running = false
      if (pass.again) { pass.again = false; liveFns.current.fetchVisible() }
    })
  }, [clusters, store, swapIn])

  /**
   * Every view change, at most once a frame: move the camera, swap clusters and
   * nodes when the level changes, fetch what came into view. Keep the placed
   * names until the replacement is ready, so a move never blanks the overlay.
   */
  const frameRef = useRef<number | null>(null)
  const settleRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const hoverShownRef = useRef(false)
  const lastK = useRef(0)
  const paintedAt = useRef(0)
  /** A zoom moved past the last repaint: the settle repaints. */
  const zoomDirty = useRef(false)
  /** Draw the current view now: camera, dot sizes, clusters or memories, and labels after it settles. */
  const syncView = useCallback(() => {
    movedAt.current = performance.now()
    applyView()
    const nodesNow = wholeRef.current || showsNodes(viewRef.current, sizeRef.current, clusters)
    if ((shownRef.current.mode === 'nodes') !== nodesNow) refreshShown()
    else if (lastK.current !== viewRef.current.k) {
      // Dots and cluster discs are sized by zoom, so a zoom resizes them; a pan does
      // not. Repainting tens of thousands of marks every frame of a zoom costs the
      // frame on a phone, so a zoom repaints at most every PAINT_MS and once more
      // when it settles; between repaints the camera still moves every frame.
      // Mid-zoom only the sizes follow; colours and links catch up at the settle.
      const now = performance.now()
      if (now - paintedAt.current >= PAINT_MS) { paintedAt.current = now; resize(); cosmosRef.current?.render(0, 0) }
      zoomDirty.current = true
    }
    lastK.current = viewRef.current.k
    if (hoverShownRef.current) { hoverShownRef.current = false; setHover(null) }
    if (settleRef.current) clearTimeout(settleRef.current)
    // Replace the names and update links once the view stops moving.
    // Each step is its own task, so a settle never holds one frame for all of them;
    // a new move cancels the steps not yet run.
    const steps = [
      () => { if (zoomDirty.current) { zoomDirty.current = false; paint(); cosmosRef.current?.render(0, 0) } },
      () => placeOverlay(true),
      () => { pickLinks(true); cosmosRef.current?.render(0, 0) },
    ]
    const next = () => { const step = steps.shift(); if (!step) return; step(); settleRef.current = setTimeout(next, 0) }
    settleRef.current = setTimeout(next, SETTLE_MS)
    fetchVisible()
  }, [applyView, clusters, fetchVisible, paint, pickLinks, placeOverlay, refreshShown, resize])
  const viewChanged = useCallback(() => {
    if (frameRef.current !== null) return
    frameRef.current = requestAnimationFrame(() => { frameRef.current = null; syncView() })
  }, [syncView])

  /** Move the camera to `to` over MOTION_MS, easing out, or at once with reduced
   *  motion. Every frame redraws the whole view, so dots grow and clusters open
   *  as the camera moves; a new move, pan or zoom stops it where it is. */
  const animRef = useRef<number | null>(null)
  /** Where the move in flight is going. */
  const flightTo = useRef<View | null>(null)
  const stopFlight = useCallback(() => {
    if (animRef.current !== null) { cancelAnimationFrame(animRef.current); animRef.current = null }
  }, [])
  const flyTo = useCallback((to: View) => {
    stopFlight()
    flightTo.current = to
    if (reducedRef.current) { viewRef.current = to; viewChanged(); return }
    const from = { ...viewRef.current }, t0 = performance.now()
    const step = (now: number) => {
      const t = Math.min(1, (now - t0) / MOTION_MS), e = easeOut(t)
      viewRef.current = { cx: from.cx + (to.cx - from.cx) * e, cy: from.cy + (to.cy - from.cy) * e, k: from.k * (to.k / from.k) ** e }
      animRef.current = t < 1 ? requestAnimationFrame(step) : null
      syncView()
    }
    animRef.current = requestAnimationFrame(step)
  }, [stopFlight, syncView, viewChanged])

  /** This render's callbacks, for the handlers the canvas binds once: a newer
   *  overview (Refresh, a store write) gives them new clusters. */
  /** Positions moved on the server (a rebuilt layout was adopted): drop every
   *  cached node and count, read the overview again, then the nodes in view. */
  const relayout = useCallback(() => {
    fetchGen.current++; clearSelection()
    cacheRef.current.clear()
    void refetch().then(r => {
      // Read again against the build the overview now names, before any effect runs.
      layoutRef.current = r.data?.layout
      if (cosmosRef.current) { liveFns.current.refreshShown(); liveFns.current.fetchVisible() }
    })
  }, [clearSelection, refetch])
  const liveFns = useRef({ viewChanged, flyTo, stopFlight, paint, paintLinks, relayout, refreshShown, fetchVisible, placeOverlay })
  liveFns.current = { viewChanged, flyTo, stopFlight, paint, paintLinks, relayout, refreshShown, fetchVisible, placeOverlay }
  // A new overview from another build replaces what is cached, so nothing mixes the two.
  useEffect(() => {
    const was = layoutRef.current
    layoutRef.current = overview?.layout
    if (was !== undefined && was !== overview?.layout) {
      fetchGen.current++; clearSelection()
      cacheRef.current.clear()
      if (cosmosRef.current) { liveFns.current.refreshShown(); liveFns.current.fetchVisible() }
    }
  }, [clearSelection, overview?.layout])

  // ---- the canvas ---------------------------------------------------------

  useEffect(() => {
    const el = canvasRef.current
    if (!ready || !el) return
    let disposed = false
    let teardown: (() => void) | undefined
    // The counter itself, not its value: cleanup bumps it so a fetch in flight stops.
    const generation = fetchGen
    // cosmos.gl (WebGL) loads with the canvas, as its own chunk.
    void import('@cosmos.gl/graph').then(({ Graph }) => {
    if (disposed) return
    // The real size before the first view: the transform is computed from it.
    // The content box (inside the 1px border), the size cosmos.gl draws in and
    // centres its transform on; the border box would put every mark k px off.
    if (el.clientWidth > 0) sizeRef.current = { width: el.clientWidth, height: el.clientHeight }
    colorsRef.current = readGraphColors()
    let g: CosmosGraph
    try {
    g = new Graph(el, {
      enableSimulation: false,
      enableZoom: false,
      enableDrag: false,
      rescalePositions: false,
      fitViewOnInit: false,
      scalePointsOnZoom: false,
      // Thin links between related memories, coloured and faded by paintLinks,
      // 1 CSS px wide at every zoom. A link longer on screen than the range fades
      // to a trace, so neighbours read as joined and long crossings stay quiet.
      renderLinks: true,
      linkDefaultWidth: 1,
      scaleLinksOnZoom: false,
      linkVisibilityDistanceRange: [LINK_NEAR_PX, LINK_FAR_PX],
      linkVisibilityMinTransparency: 0,
      // A slight bow, so two links that meet at a memory read apart.
      curvedLinks: true,
      curvedLinkSegments: 9,
      curvedLinkControlPointDistance: 0.15,
      backgroundColor: colorsRef.current.background,
      focusedPointRingColor: colorsRef.current.ring,
      hoveredPointRingColor: colorsRef.current.ring,
      renderHoveredPointRing: true,
      pixelRatio: window.devicePixelRatio || 1,
      transitionDuration: MOTION_MS,
      attribution: '',
      onPointMouseOver: (index) => {
        const item = shownRef.current.items[index]
        hoveredRef.current = index
        if (!item) return
        const at = toScreen(viewRef.current, sizeRef.current, item)
        const text = shownRef.current.mode === 'nodes' ? (item as GraphNodeRow).label : (item as GraphCluster).label ?? fmtNumber((item as GraphCluster).count)
        const size = sizeRef.current
        const lines = memoryLines(text, line => widthOf(line, LABEL_PX, true), Math.min(320, size.width))
        const avoid = touchRef.current ? [CONTROLS_BOX(size)] : [CONTROLS_BOX(size), PAN_HINT_BOX(size)]
        const placed = lines ? placeLabels([{ id: 'hover', at, text, lines, rank: 1, centred: true }], size, 1, widthOf, [], undefined, avoid)[0] : undefined
        hoverShownRef.current = !!placed
        setHover(placed ?? null)
        // Its own links light up and the rest dim.
        liveFns.current.paintLinks(); g.render(0, 0)
      },
      onPointMouseOut: () => { hoveredRef.current = null; hoverShownRef.current = false; setHover(null); liveFns.current.paintLinks(); g.render(0, 0) },
      onPointClick: (index) => {
        const shown = shownRef.current
        const item = shown.items[index]
        if (!item) return
        if (shown.mode === 'clusters') {
          const c = item as GraphCluster
          liveFns.current.flyTo(fitBox([c.x - c.r, c.y - c.r, c.x + c.r, c.y + c.r], sizeRef.current))
          return
        }
        const node = item as GraphNodeRow
        setOpened({ ref: node.ref, record: node.record })
      },
    })
    } catch (err) {
      // No WebGL here (an old browser, a locked-down VM): say so; search and filters stay usable.
      // eslint-disable-next-line no-console -- intentional init-failure diagnostic
      console.warn('MemoryGraph: the canvas could not start', err)
      setCanvasError(err)
      return
    }
    cosmosRef.current = g
    // The WebGL device starts after the constructor returns, so a browser without
    // WebGL fails here rather than in the try above.
    g.ready.catch(err => { if (!disposed) setCanvasError(err) })
    // cosmos.gl's world origin: the centre of its `spaceSize` square (default 4096).
    originRef.current = { ox: COSMOS_SPACE / 2, oy: COSMOS_SPACE / 2 }
    if (overview?.bounds) viewRef.current = fitBox(overview.bounds, sizeRef.current)
    refreshShown()
    applyView()
    fetchVisible()

    const { detach, cancel } = attachGraphGestures(el, {
      // A pan or zoom by hand stops a camera move in flight, where it is.
      panBy: d => { liveFns.current.stopFlight(); viewRef.current = panBy(viewRef.current, d, boundsRef.current); liveFns.current.viewChanged() },
      zoomAt: (at, f) => { liveFns.current.stopFlight(); viewRef.current = zoomAt(viewRef.current, sizeRef.current, at, f); liveFns.current.viewChanged() },
    }, {
      overNode: () => hoveredRef.current !== null,
      onBox: setBox,
      onClear: clearSelection,
      onSelect: area => {
        const b = screenRectToBox(viewRef.current, sizeRef.current, area)
        const bbox = b.join(','), drawnLayout = layoutRef.current
        const select = () => {
          if (layoutRef.current !== drawnLayout) { clearSelection(); return }
          const gen = ++selectGen.current, askKinds = kindsRef.current
          setSelectPending(true)
          void api.memoryGraphSelect(store, b, askKinds).then(r => {
            if (gen !== selectGen.current) return
            setSelectPending(false)
            // Counted on another build: the box means other memories now.
            if (r.layout !== drawnLayout) { clearSelection(); liveFns.current.relayout(); return }
            setPicked(r.count && drawnLayout ? { bbox, layout: drawnLayout, count: r.count, kinds: askKinds } : null)
          })
            .catch(error => { if (gen !== selectGen.current) return; setSelectPending(false); setPicked(null); setLiveError({ error, retry: select }) })
        }
        select()
      },
    })
    cancelGesture.current = cancel
    // Recolour live when the user switches light/dark (`<html data-theme>`).
    const themeObserver = new MutationObserver(() => {
      colorsRef.current = readGraphColors()
      g.setConfigPartial({ backgroundColor: colorsRef.current.background, focusedPointRingColor: colorsRef.current.ring, hoveredPointRingColor: colorsRef.current.ring })
      liveFns.current.paint(); g.render(0, 0); liveFns.current.placeOverlay()
    })
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    teardown = () => {
      detach(); cancelGesture.current = null; themeObserver.disconnect()
      g.destroy(); cosmosRef.current = null
    }
    }).catch(err => { if (!disposed) setCanvasError(err) })
    return () => {
      disposed = true
      teardown?.()
      if (animRef.current !== null) cancelAnimationFrame(animRef.current)
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current)
      frameRef.current = null
      if (settleRef.current) clearTimeout(settleRef.current)
      if (swapFrame.current !== null) clearTimeout(swapFrame.current)
      swapFrame.current = null
      generation.current++ // a fetch still in flight stops at its next page
    }
    // The canvas is built once per store and layout; every other change goes through refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, store, overview?.bounds?.join(',')])

  // A new kind filter fetches its own nodes; the cache is kept per kind set.
  useEffect(() => {
    // Answers still in flight were asked for the old filter.
    fetchGen.current++; clearSelection()
    if (cosmosRef.current) { refreshShown(); fetchVisible() }
  }, [clearSelection, kinds, refreshShown, fetchVisible])

  // The canvas fills the window below it, follows its box, and keeps the view centred.
  useLayoutEffect(() => {
    const el = wrapRef.current
    if (!el) return
    let page: HTMLElement | null = el.parentElement
    while (page && !/(auto|scroll)/.test(getComputedStyle(page).overflowY)) page = page.parentElement
    const fit = () => {
      const bottom = page ? page.getBoundingClientRect().bottom : window.innerHeight
      setHeight(Math.max(MIN_CANVAS, Math.floor(bottom - el.getBoundingClientRect().top - 32)))
    }
    fit()
    window.addEventListener('resize', fit)
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => {
      const c = canvasRef.current
      if (c && c.clientWidth > 0) sizeRef.current = { width: c.clientWidth, height: c.clientHeight }
      setTick(t => t + 1)
    })
    if (canvasRef.current) observer?.observe(canvasRef.current)
    return () => { window.removeEventListener('resize', fit); observer?.disconnect() }
  }, [ready])
  useEffect(() => { if (cosmosRef.current) viewChanged() }, [tick, viewChanged])

  // ---- search over every node, on the server -----------------------------------

  useEffect(() => { const t = setTimeout(() => setSearch(searchInput.trim()), 300); return () => clearTimeout(t) }, [searchInput])
  useEffect(() => {
    if (!ready) return
    if (!search) { setMatches(null); return }
    let live = true
    void api.memoryGraphSearch(store, search, { kinds }).then(r => {
      if (!live) return
      if (r.layout !== layoutRef.current) { liveFns.current.relayout(); return }
      setMatches(new Set(r.matches.map(m => m.id)))
      if (r.matches.length) {
        const xs = r.matches.map(m => m.x), ys = r.matches.map(m => m.y)
        flyTo(fitBox([Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)], sizeRef.current, 64))
      }
    }).catch(error => { if (live) { setMatches(null); setLiveError({ error, retry: () => setReloads(n => n + 1) }) } })
    return () => { live = false }
  }, [search, kinds, ready, store, flyTo, reloads, overview?.layout])
  useEffect(() => { matchesRef.current = matches; if (cosmosRef.current) { paint(); cosmosRef.current.render(0, 0); placeOverlay() } }, [matches, paint, placeOverlay])

  // ---- zoom controls: + and − about the centre, and fit the whole store ---------

  const zoomBy = (factor: number) => {
    const size = sizeRef.current
    // A second press during a move zooms on from where the first one was going.
    const from = animRef.current !== null && flightTo.current ? flightTo.current : viewRef.current
    flyTo(zoomAt(from, size, { x: size.width / 2, y: size.height / 2 }, factor))
  }
  const fitAll = () => { if (boundsRef.current) flyTo(fitBox(boundsRef.current, sizeRef.current)) }

  // ---- keyboard: arrows move between the nodes on screen, Enter opens one ----------

  const onKey = (e: React.KeyboardEvent) => {
    const dirs: Record<string, Dir> = { ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down' }
    const shown = shownRef.current, view = viewRef.current, size = sizeRef.current
    const items = shown.mode === 'nodes' ? (shown.items as GraphNodeRow[]).map(n => ({ id: n.id, at: toScreen(view, size, n) }))
      : (shown.items as GraphCluster[]).map(c => ({ id: `c${c.id}`, at: toScreen(view, size, c) }))
    const onScreen = items.filter(p => p.at.x >= 0 && p.at.y >= 0 && p.at.x <= size.width && p.at.y <= size.height)
    if (e.key in dirs) {
      e.preventDefault()
      const from = onScreen.find(p => p.id === focusId)?.at ?? { x: size.width / 2, y: size.height / 2 }
      const next = focusId && onScreen.some(p => p.id === focusId) ? nextInDirection(onScreen, from, dirs[e.key]) : onScreen.sort((a, b) => Math.hypot(a.at.x - from.x, a.at.y - from.y) - Math.hypot(b.at.x - from.x, b.at.y - from.y))[0]?.id ?? null
      if (next) setFocusId(next)
    } else if (e.key === 'Enter' && focusId) {
      e.preventDefault()
      if (shown.mode === 'clusters') {
        const c = (shown.items as GraphCluster[]).find(x => `c${x.id}` === focusId)
        if (c) { setFocusId(null); flyTo(fitBox([c.x - c.r, c.y - c.r, c.x + c.r, c.y + c.r], size)) }
      } else {
        const node = (shown.items as GraphNodeRow[]).find(x => x.id === focusId)
        if (node) setOpened({ ref: node.ref, record: node.record })
      }
    } else if (e.key === 'Escape') { setFocusId(null); clearSelection() }
    else if (e.key === '+' || e.key === '=') { e.preventDefault(); zoomBy(1 / ZOOM_STEP) }
    else if (e.key === '-' || e.key === '_') { e.preventDefault(); zoomBy(ZOOM_STEP) }
    else if (e.key === '0') { e.preventDefault(); fitAll() }
  }
  useEffect(() => { focusRef.current = focusId; if (cosmosRef.current) { paint(); cosmosRef.current.render(0, 0) } }, [focusId, paint])
  const focusedText = useMemo(() => {
    if (!focusId) return ''
    const shown = shownRef.current
    if (shown.mode === 'nodes') return (shown.items as GraphNodeRow[]).find(n => n.id === focusId)?.label ?? ''
    const c = (shown.items as GraphCluster[]).find(x => `c${x.id}` === focusId)
    return c ? `${c.label ?? ''} ${fmtNumber(c.count)}`.trim() : ''
    // `tick` re-reads after the shown set changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusId, tick])

  // ---- render ------------------------------------------------------------------

  /** Read everything again: the overview, the nodes in view and the search. */
  const reload = () => {
    setLiveError(null)
    fetchGen.current++; clearSelection()
    cacheRef.current.clear()
    if (cosmosRef.current) { refreshShown(); fetchVisible() }
    setReloads(n => n + 1)
    void refetch()
  }
  const failed = queryReadFailed(overviewRead)
  const title = <CardTitle><NetworkIcon className="lucide-inline" /> {i18nT('pages.overview.memoryGraphTab.memory_graph')}
    <Btn onClick={reload} className="ml-2"><RefreshCw className="lucide-inline" /> {i18nT('pages.overview.memoryGraphTab.refresh')}</Btn>
  </CardTitle>
  // A failed Refresh over a graph already drawn keeps the canvas (and its renderer) and says so above it.
  if (failed && !ready) return <Card>{title}<div className="mb-3 flex flex-col items-start gap-2">
    {isError ? <ErrorNotice askAgent testId="memory-graph-load-error" title={i18nT('pages.overview.memoryGraphTab.load_failed')} message={errMessage(error) || i18nT('pages.overview.memoryGraphTab.load_failed_unknown')} messagePlacement="below" className="w-full" />
      : <p role="status" className="text-muted text-sm">{i18nT('pages.overview.memoryGraphTab.loading_graph_data')}</p>}
    <Btn className="aria-disabled:opacity-30 aria-disabled:cursor-default" aria-disabled={isFetching || undefined} onClick={() => { if (!isFetching) void refetch() }}><RefreshCw className="lucide-inline" /> {i18nT('pages.overview.memoryGraphTab.retry')}</Btn>
  </div></Card>
  if (!overview || overview.status === 'building') return <Card>{title}<p role="status" className="text-muted text-sm">{i18nT('pages.overview.memoryGraphTab.loading_graph_data')}</p></Card>
  if (!ready) return <Card>{title}<p className="text-muted text-sm">{i18nT('pages.overview.memoryGraphTab.no_memory_data_to_visualize_add_preferences_proj')}</p></Card>

  const counts = { fact: totals.fact || 0, directive: totals.lesson || 0, episode: totals.episode || 0 }
  return <Card className="mb-0 max-w-none">
    {title}
    <div className="mb-3 flex flex-wrap items-center gap-2">
      <input aria-label={i18nT('pages.overview.memoryGraphTab.search_memory_nodes')}
        className="bg-bg-elevated border border-border rounded-md px-3 py-1.5 text-text text-sm font-body outline-hidden transition-colors focus-ring min-w-[200px] flex-1"
        placeholder={i18nT('pages.overview.memoryGraphTab.search_nodes')} value={searchInput} onChange={e => setSearchInput(e.target.value)} />
      <KindPills wrap={!phone} kind={kind} counts={counts} marks={KIND_MARKS} onChange={k => { setKind(k); setPicked(null) }} />
    </div>
    {failed && isError && <ErrorNotice askAgent testId="memory-graph-load-error" className="mb-3 w-full" messagePlacement="below"
      title={i18nT('pages.overview.memoryGraphTab.load_failed')} message={errMessage(error) || i18nT('pages.overview.memoryGraphTab.load_failed_unknown')}
      actions={<button type="button" disabled={isFetching} onClick={() => { void refetch() }}><RefreshCw className="lucide-inline" /> {i18nT('pages.overview.memoryGraphTab.retry')}</button>} />}
    {liveError && <ErrorNotice askAgent testId="memory-graph-read-error" className="mb-3 w-full" messagePlacement="below"
      title={i18nT('pages.overview.memoryGraphTab.load_failed')} message={errMessage(liveError.error) || i18nT('pages.overview.memoryGraphTab.load_failed_unknown')}
      actions={<button type="button" onClick={() => { const { retry } = liveError; setLiveError(null); retry() }}><RefreshCw className="lucide-inline" /> {i18nT('pages.overview.memoryGraphTab.retry')}</button>} />}
    <div ref={wrapRef} className="relative">
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/no-noninteractive-tabindex -- role="application" is the graph's own keyboard surface (arrows move between memories, Enter opens one), like CanvasEditor's */}
      <div ref={canvasRef} tabIndex={0} role="application" aria-roledescription={i18nT('pages.overview.memoryGraphTab.memory_graph')}
        aria-label={i18nT('pages.overview.memoryGraphTab.memory_graph')} aria-describedby="memory-graph-keys"
        onKeyDown={onKey} onBlur={() => setFocusId(null)} data-testid="memory-graph-canvas"
        className="w-full overflow-hidden rounded-md border border-border bg-bg-elevated focus-ring" style={{ height }} />
      {canvasError != null && <div className="absolute inset-x-3 top-3">
        <ErrorNotice askAgent testId="memory-graph-canvas-error" messagePlacement="below"
          title={i18nT('pages.overview.memoryGraphTab.canvas_unavailable')} message={errMessage(canvasError) || i18nT('pages.overview.memoryGraphTab.load_failed_unknown')} />
      </div>}
      <p id="memory-graph-keys" className="sr-only">{i18nT('pages.overview.memoryGraphTab.keyboard_hint')}</p>
      <p className="sr-only" aria-live="polite">{focusedText}</p>
      {/* inset-px: the canvas's border, so the overlay's origin is the drawing's. */}
      <div aria-hidden="true" className="pointer-events-none absolute inset-px overflow-hidden">
        {canvasError == null && <GraphLabels sink={labelSink} initial={labelsRef} />}
        {hover && <GraphLabel label={hover} reduced testId="memory-graph-hover" />}
        {box && <div data-testid="memory-graph-box" className="absolute rounded-sm border border-accent bg-[color-mix(in_srgb,var(--accent)_12%,transparent)]" style={{ left: box.x, top: box.y, width: box.width, height: box.height }} />}
      </div>
      {canvasError == null && !touch && <p data-testid="memory-graph-pan-hint" className="pointer-events-none absolute bottom-3 left-3 m-0 rounded-md border border-border bg-card px-2 py-1 text-[12px] text-text shadow-sm">{i18nT('pages.overview.memoryGraphTab.hold_space_to_pan')}</p>}
      {canvasError == null && <div className="absolute bottom-3 right-3 flex flex-col items-center gap-2">
        <IconButtonGroup>
          <IconButton aria-label={i18nT('pages.overview.memoryGraphTab.fit_view')} aria-keyshortcuts="0" onClick={fitAll}><Maximize2 size={16} aria-hidden="true" /></IconButton>
        </IconButtonGroup>
        <IconButtonGroup className="flex-col">
          <IconButton aria-label={i18nT('components.canvas.zoom_in')} aria-keyshortcuts="+" onClick={() => zoomBy(1 / ZOOM_STEP)}><Plus size={16} aria-hidden="true" /></IconButton>
          <IconButton aria-label={i18nT('components.canvas.zoom_out')} aria-keyshortcuts="-" onClick={() => zoomBy(ZOOM_STEP)}><Minus size={16} aria-hidden="true" /></IconButton>
        </IconButtonGroup>
      </div>}
      {opened && <MemoryGraphDetail store={store} record={opened.record} nodeRef={opened.ref} phone={phone} onClose={() => setOpened(null)} onRefresh={reload} />}
      {picked && <div role="status" className="absolute left-3 top-3 flex items-center gap-2 rounded-lg border border-border bg-card px-3 py-1.5 text-[13px] text-text shadow-sm">
        <span className="tabular-nums">{i18nT('memoryPage.graph_selected', { count: picked.count, formatted: fmtNumber(picked.count) })}</span>
        {jump && <Btn primary className="min-h-8 whitespace-nowrap" onClick={() => jump([], undefined, picked)}>{i18nT('memoryPage.show_in_memories')}</Btn>}
      </div>}
    </div>
  </Card>
}

/** The names drawn over the canvas, re-rendered on their own when they move. */
function GraphLabels({ sink, initial }: { sink: React.MutableRefObject<(labels: PlacedLabel[]) => void>; initial: React.MutableRefObject<PlacedLabel[]> }) {
  const [labels, setLabels] = useState(initial.current)
  const reduced = useReducedMotion()
  useLayoutEffect(() => { sink.current = setLabels; return () => { sink.current = () => {} } }, [sink])
  return <div data-testid="memory-graph-labels"><AnimatePresence initial={false}>
    {labels.map(l => <GraphLabel key={`${l.id}:${l.text}`} label={l} reduced={reduced} />)}
  </AnimatePresence></div>
}

/** Placement and rendering use the same padded box, including hover names. */
function GraphLabel({ label: l, reduced, testId }: { label: PlacedLabel; reduced: boolean; testId?: string }) {
  return <motion.span data-testid={testId} initial={{ opacity: reduced ? 1 : 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
    transition={{ duration: reduced ? 0 : MOTION_MS / 1000, ease: easeOut }}
    className={`absolute box-border px-1.5 text-text [text-shadow:0_0_3px_var(--bg-elevated),0_0_3px_var(--bg-elevated),0_0_6px_var(--bg-elevated)] flex flex-col justify-center whitespace-nowrap ${l.centred ? 'font-semibold items-center text-center' : 'items-start text-left'}`}
    style={{ left: l.box.x, top: l.box.y, width: l.box.width, height: l.box.height, fontSize: l.px ?? LABEL_PX, lineHeight: TOPIC_LINE_HEIGHT, color: l.color }}>
    {(l.lines ?? [l.text]).map((line, i) => <span key={i}>{line}</span>)}
  </motion.span>
}
