/**
 * A FigJam-style whiteboard canvas (PoC): stickies, shapes, text and connectors
 * on an infinite dot grid, with the conductor's answers and status overlay pinned
 * to shapes.
 *
 * Layers, back to front:
 * - the world: shapes (DOM) and connectors (SVG), moved and scaled by the view;
 * - screen chrome: selection, handles, connect dots, the selection toolbar, the
 *   ask box, answers and overlay items. Placed from each shape's box on every
 *   render, so they follow drags, pans and zooms and stay crisp at any zoom;
 * - the tool dock and zoom control.
 *
 * Controlled: the parent owns the board; every change, live ones included, comes
 * back through `onChange`. Undo history is kept here, one step per gesture.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type MutableRefObject, type ReactNode } from 'react'
import {
  ArrowUpRight, Circle, Diamond, Eye, EyeOff, Frame, Hand, Link2, Maximize2, MessageCircle, Minus, MousePointer2, Plus, Shield, ShieldCheck, Square, StickyNote, Target, Type,
} from 'lucide-react'
import type { ThreadView } from '../docConductor'
import type { OverlayItem } from '../docOverlay'
import { placeAround } from '../overlayPlace'
import {
  CONN_INK, DEFAULT_SIZE, SECTION_TITLE_H, SWATCH_STYLE, askSurvives, askTarget, boardAccess, boardGoalEvery, boundsOf, conductorAccessOf, labelScale, newId, sectionMembers, shapeAt, wheelPanStep, wheelZoomFactor, zOrdered,
  type Board, type Connector, type Pt, type Shape, type ShapeKind, type Swatch,
} from './boardModel'
import BoardContextMenu, { type MenuAction } from './BoardContextMenu'
import { copyShapes, pasteClip, restack, setLocked, unlockAll, type BoardClip } from './boardEditing'
import AskDraft from './AskDraft'
import { ShapeLinkText, useMentionPicker, type BoardLinks } from './BoardMentions'
import { SessionCardView, SessionPicker } from './SessionCard'
import type { SessionRef } from '../SessionMention'
import ConductorThread, { CARD_MAX_H, CARD_W } from './ConductorThread'
import ConnectorToolbar from './ConnectorToolbar'
import DashedOutline from './DashedOutline'
import { shapeLook, swatchStyle } from './shapeLook'
import ShapeToolbar from './ShapeToolbar'
import WatchRing from './WatchRing'
import {
  SIDES, connectorRoute, movedSegment, pointBox, segmentDrag, sideAnchor, sideNear, type SegmentDrag, type Side,
} from './connectorGeometry'

/** `ask` points at a place and starts a conversation with the conductor about it. */
type Tool = 'select' | 'hand' | 'connector' | 'ask' | ShapeKind
interface View { x: number; y: number; zoom: number }
/** A resize handle: a corner moves two sides, an edge one. */
type Corner = 'nw' | 'ne' | 'sw' | 'se' | 'n' | 'e' | 's' | 'w'
const EDGE_CURSOR: Record<'n' | 'e' | 's' | 'w', string> = { n: 'ns-resize', s: 'ns-resize', e: 'ew-resize', w: 'ew-resize' }

type Drag =
  | { kind: 'pan'; sx: number; sy: number; view0: View }
  | { kind: 'move'; start: Pt; origin: Map<string, Pt>; snapshot: Board; moved: boolean }
  | { kind: 'resize'; id: string; corner: Corner; start: Pt; box0: Shape; snapshot: Board }
  | { kind: 'marquee'; sx: number; sy: number; cx: number; cy: number; base: Set<string> }
  // A new connector from a shape; `fromSide` is the connect dot it was dragged from.
  | { kind: 'connect'; from: string; fromSide?: Side; at: Pt }
  // One end of the selected connector, dragged to another side or shape.
  | { kind: 'reattach'; id: string; end: 'from' | 'to'; at: Pt }
  // One segment of the selected elbow, slid sideways to reshape its route.
  | { kind: 'segment'; id: string; seg: SegmentDrag; start: Pt; snapshot: Board }
  // A press with a shape tool: a click drops a default-sized shape, a drag sizes
  // it. `id` is set once the drag has started and the shape exists.
  | { kind: 'create'; shape: ShapeKind; start: Pt; snapshot: Board; id: string | null }

const MIN_ZOOM = 0.2
const MAX_ZOOM = 3
const GRID = 24
/** Drags snap at half the dot spacing, so a shape can sit on a dot or midway. */
const SNAP = GRID / 2
const HISTORY_CAP = 100
/** Screen px around a hovered shape that still count as hovering it, so its connect dots (14px out) stay reachable. */
const HOVER_MARGIN = 24
/** Screen px within which a dragged connector end snaps onto a side's midpoint. */
const SIDE_REACH = 28
const DOT_OFFSET = 14

/**
 * Where a dragged connector end lands: on a side's midpoint when it is within
 * reach (so a dot is a precise target, even just outside its shape), else on the
 * body of the shape under it, which leaves the side to face the other end.
 */
function dropOn(shapes: Shape[], p: Pt, zoom: number, not?: string): { shape: Shape; side?: Side } | null {
  const reach = SIDE_REACH / zoom
  let best: { shape: Shape; side: Side; d: number } | null = null
  for (const s of shapes) {
    // A section is a container, not something a line joins to.
    if (s.id === not || s.kind === 'section') continue
    const side = sideNear(s, p, reach)
    if (!side) continue
    const a = sideAnchor(s, side)
    const d = Math.hypot(a.x - p.x, a.y - p.y)
    if (!best || d < best.d) best = { shape: s, side, d }
  }
  if (best) return { shape: best.shape, side: best.side }
  const hit = shapeAt(shapes, p, { zoom })
  return hit && hit.id !== not && hit.kind !== 'section' ? { shape: hit } : null
}

const connColor = (c: Pick<Connector, 'color'>) => (c.color ? SWATCH_STYLE[c.color].stroke : CONN_INK)
/** Connector width in board units, so it scales with zoom like FigJam's. */
const CONNECTOR_STROKE = 3

/**
 * What Copy last took. Kept for the tab rather than per board, so a copy pastes
 * into another board too; it never leaves the page.
 */
let boardClipboard: BoardClip | null = null

const OVERLAYS_KEY = 'kirocrew.board.overlays'

/** Below this zoom the connect dots are hidden: you are surveying the board, not wiring shapes. */
const MIN_CONNECT_ZOOM = 0.5

/** The closest the background dots may sit on screen, in px, before every other one drops out. */
const MIN_DOT_GAP = 16

/**
 * The dots' spacing on screen at *zoom*. Zooming out would pack them tighter and
 * tighter, so as in FigJam each time the gap falls under MIN_DOT_GAP the grid
 * doubles (every other row and column goes) and the gap springs back.
 */
function dotSpacing(zoom: number): number {
  const gap = GRID * zoom
  const halvings = gap >= MIN_DOT_GAP ? 0 : Math.ceil(Math.log2(MIN_DOT_GAP / gap))
  return gap * 2 ** halvings
}

/** Snap a world coordinate to the snap grid; Alt held during a drag turns it off. */
const snap = (v: number, on: boolean) => (on ? Math.round(v / SNAP) * SNAP : Math.round(v))

const TOOLS: { tool: Tool; icon: ReactNode; label: string; key: string }[] = [
  { tool: 'select', icon: <MousePointer2 size={18} />, label: 'Select', key: 'V' },
  { tool: 'hand', icon: <Hand size={18} />, label: 'Hand', key: 'H' },
  { tool: 'sticky', icon: <StickyNote size={18} />, label: 'Sticky note', key: 'S' },
  { tool: 'rect', icon: <Square size={18} />, label: 'Rectangle', key: 'R' },
  { tool: 'ellipse', icon: <Circle size={18} />, label: 'Ellipse', key: 'O' },
  { tool: 'diamond', icon: <Diamond size={18} />, label: 'Diamond', key: 'D' },
  { tool: 'text', icon: <Type size={18} />, label: 'Text', key: 'T' },
  { tool: 'section', icon: <Frame size={18} />, label: 'Section', key: 'F' },
  { tool: 'connector', icon: <ArrowUpRight size={18} />, label: 'Connector', key: 'L' },
  { tool: 'session', icon: <Link2 size={18} />, label: 'Link a session', key: 'K' },
  { tool: 'ask', icon: <MessageCircle size={18} />, label: 'Ask the conductor', key: 'C' },
]
const TOOL_BY_KEY = new Map(TOOLS.map(t => [t.key.toLowerCase(), t.tool]))

export interface BoardCanvasProps {
  board: Board
  onChange: (next: Board) => void
  overlay: OverlayItem[]
  threads: Record<string, ThreadView>
  /** A new ask about these shapes; the board already carries it. */
  /** A new conversation: its id, the first message, and the board with the ask recorded. */
  onAsk: (askId: string, text: string, board: Board) => void
  onFollowUp: (askId: string, text: string) => void
  onOpenSession: (key: string) => void
  onReadAsk: (askId: string) => void
  /** Filled with a `commit` that applies a change as one undo step: how the
   *  conductor's edits land, so one Cmd+Z takes a whole reply back. */
  apiRef?: MutableRefObject<{ commit: (next: Board) => void } | null>
  /** The sessions a shape's text can link to, newest first. */
  sessions?: SessionRef[]
  /** The board's own session, which the "@" picker leaves out. */
  selfKey?: string
  /** What the latest goal check said about one section's goal, for its popover. */
  goalReport?: (sectionId: string) => string | null
}


export default function BoardCanvas({ board, onChange, overlay, threads, onAsk, onFollowUp, onOpenSession, onReadAsk, apiRef, sessions, selfKey, goalReport }: BoardCanvasProps) {
  const root = useRef<HTMLDivElement>(null)
  const [view, setView] = useState<View>({ x: 120, y: 100, zoom: 1 })
  const [tool, setTool] = useState<Tool>('select')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [selectedConn, setSelectedConn] = useState<string | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [hovered, setHovered] = useState<string | null>(null)
  const [drag, setDrag] = useState<Drag | null>(null)
  /** A conversation being started: what it is about and where its pin sits (board units). */
  const [draft, setDraft] = useState<{ shapeIds: string[]; at: Pt } | null>(null)
  /** The one conversation whose card is open. */
  const [openAsk, setOpenAsk] = useState<string | null>(null)
  // The conductor's overlay can be hidden to see and grab what it covers. Kept for the
  // browser, so it stays as you left it.
  const closed = board.conductorDefault === 'closed'
  const [overlaysOn, setOverlaysOn] = useState(() => {
    try { return localStorage.getItem(OVERLAYS_KEY) !== 'off' } catch { return true }
  })
  const toggleOverlays = () => setOverlaysOn(on => {
    try { localStorage.setItem(OVERLAYS_KEY, on ? 'off' : 'on') } catch { /* private mode: it just will not be remembered */ }
    return !on
  })
  /** The open right-click menu: where (canvas px and board units) and which shapes it acts on. */
  const [menu, setMenu] = useState<{ x: number; y: number; at: Pt; ids: string[] } | null>(null)
  const closeMenu = useCallback(() => setMenu(null), [])
  const [spaceDown, setSpaceDown] = useState(false)

  // The latest values for handlers that outlive a render (window listeners).
  const boardRef = useRef(board)
  boardRef.current = board
  const viewRef = useRef(view)
  viewRef.current = view
  const undo = useRef<Board[]>([])
  const redo = useRef<Board[]>([])

  const commit = useCallback((next: Board, before: Board = boardRef.current) => {
    undo.current = [...undo.current.slice(-HISTORY_CAP + 1), before]
    redo.current = []
    onChange(next)
  }, [onChange])

  useEffect(() => {
    if (!apiRef) return
    apiRef.current = { commit: next => commit(next) }
    return () => { apiRef.current = null }
  }, [apiRef, commit])

  const screenBox = (s: { x: number; y: number; w: number; h: number }) => ({
    x: s.x * view.zoom + view.x, y: s.y * view.zoom + view.y, w: s.w * view.zoom, h: s.h * view.zoom,
  })
  const toScreen = (p: Pt): Pt => ({ x: p.x * view.zoom + view.x, y: p.y * view.zoom + view.y })
  const toWorld = useCallback((clientX: number, clientY: number): Pt => {
    const r = root.current?.getBoundingClientRect()
    const v = viewRef.current
    return { x: (clientX - (r?.left ?? 0) - v.x) / v.zoom, y: (clientY - (r?.top ?? 0) - v.y) / v.zoom }
  }, [])

  const byId = useMemo(() => new Map(board.shapes.map(s => [s.id, s])), [board.shapes])
  const selShapes = board.shapes.filter(s => selected.has(s.id))
  const selBox = boundsOf(selShapes)
  const hoveredShape = hovered ? byId.get(hovered) : undefined
  // Connect dots: on the hovered shape or the one selected, never on a section, and not
  // when zoomed out so far that you are looking at the overall picture, not joining things.
  const dotCandidate = hoveredShape ?? (selShapes.length === 1 && !selShapes[0].locked ? selShapes[0] : undefined)
  const dotShape = dotCandidate && dotCandidate.kind !== 'section' && view.zoom >= MIN_CONNECT_ZOOM ? dotCandidate : undefined

  // ---- zoom ------------------------------------------------------------------

  const zoomAt = useCallback((sx: number, sy: number, factor: number) => {
    setView(v => {
      const zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, v.zoom * factor))
      const k = zoom / v.zoom
      return { zoom, x: sx - (sx - v.x) * k, y: sy - (sy - v.y) * k }
    })
  }, [])

  useEffect(() => {
    const el = root.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      if ((e.target as HTMLElement).closest('[data-board-ui]')) return
      e.preventDefault()
      if (e.ctrlKey || e.metaKey) {
        const r = el.getBoundingClientRect()
        zoomAt(e.clientX - r.left, e.clientY - r.top, wheelZoomFactor(e.deltaY))
      } else {
        setView(v => ({ ...v, x: v.x - wheelPanStep(e.deltaX, e.deltaMode), y: v.y - wheelPanStep(e.deltaY, e.deltaMode) }))
      }
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [zoomAt])

  const fit = () => {
    const b = boundsOf(board.shapes)
    const r = root.current?.getBoundingClientRect()
    if (!b || !r) { setView({ x: 120, y: 100, zoom: 1 }); return }
    const zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Math.min((r.width - 160) / b.w, (r.height - 200) / b.h, 1.4)))
    setView({ zoom, x: (r.width - b.w * zoom) / 2 - b.x * zoom, y: (r.height - b.h * zoom) / 2 - b.y * zoom - 30 })
  }

  // ---- creating and editing ------------------------------------------------------

  const defaultColor = (kind: ShapeKind): Swatch => (kind === 'sticky' ? 'yellow' : kind === 'text' || kind === 'section' ? 'smoke' : 'sky')

  // A new shape is selected and ready to type into, as in FigJam.
  const finishCreate = (id: string) => {
    setSelected(new Set([id]))
    setSelectedConn(null)
    setTool('select')
    setEditing(id)
  }

  const create = (kind: ShapeKind, at: Pt) => {
    const size = DEFAULT_SIZE[kind]
    const shape: Shape = {
      id: newId(), kind, x: snap(at.x - size.w / 2, true), y: snap(at.y - size.h / 2, true), w: size.w, h: size.h,
      text: '', color: defaultColor(kind),
    }
    commit({ ...boardRef.current, shapes: [...boardRef.current.shapes, shape] })
    finishCreate(shape.id)
  }
  // The drag listeners live across renders; this keeps them on the current `create`.
  const createRef = useRef(create)
  createRef.current = create

  // The Link session tool: where its picker is open, and the card it makes.
  const [linking, setLinking] = useState<{ sx: number; sy: number; at: Pt } | null>(null)
  const linkSession = (s: SessionRef) => {
    const at = linking?.at
    setLinking(null)
    setTool('select')
    if (!at) return
    const size = DEFAULT_SIZE.session
    const card: Shape = {
      id: newId(), kind: 'session', session: s.key, text: s.title, color: 'white',
      x: snap(at.x - size.w / 2, true), y: snap(at.y - size.h / 2, true), w: size.w, h: size.h,
    }
    commit({ ...boardRef.current, shapes: [...boardRef.current.shapes, card] })
    setSelected(new Set([card.id]))
    setSelectedConn(null)
  }
  /** A session card's own action: open what it stands for. */
  const openCard = (s: Shape | undefined) => { if (s?.kind === 'session' && s.session) openRef.current(s.session) }

  /** The box a drag from `a` to `b` draws, on the grid unless `free`; stickies stay square. */
  const dragBox = (kind: ShapeKind, a0: Pt, b0: Pt, free = false) => {
    const a = { x: snap(a0.x, !free), y: snap(a0.y, !free) }
    const b = { x: snap(b0.x, !free), y: snap(b0.y, !free) }
    let w = Math.max(GRID, Math.abs(b.x - a.x))
    let h = Math.max(GRID, Math.abs(b.y - a.y))
    if (kind === 'sticky') { w = h = Math.max(w, h) }
    const x = b.x < a.x ? a.x - w : a.x
    const y = b.y < a.y ? a.y - h : a.y
    return { x, y, w, h }
  }

  const setText = (id: string, text: string) => {
    onChange({ ...boardRef.current, shapes: boardRef.current.shapes.map(s => (s.id === id ? { ...s, text } : s)) })
  }

  const editStart = useRef<Board | null>(null)
  useEffect(() => { if (editing) editStart.current = boardRef.current }, [editing])
  const endEdit = () => {
    const id = editing
    setEditing(null)
    if (!id) return
    const b = boardRef.current
    const shape = b.shapes.find(s => s.id === id)
    // An empty text box is nothing: it goes, the way it does in FigJam.
    const next = shape && shape.kind === 'text' && !shape.text.trim() ? removeShapes(b, new Set([id])) : b
    if (editStart.current && JSON.stringify(editStart.current) !== JSON.stringify(next)) commit(next, editStart.current)
    else if (next !== b) onChange(next)
    editStart.current = null
  }

  const removeShapes = (b: Board, ids: Set<string>): Board => ({
    ...b,
    shapes: b.shapes.filter(s => !ids.has(s.id)),
    connectors: b.connectors.filter(c => !ids.has(c.from) && !ids.has(c.to)),
    asks: b.asks.filter(a => askSurvives(a, ids)),
  })

  const deleteSelection = () => {
    const b = boardRef.current
    if (selectedConn) { commit({ ...b, connectors: b.connectors.filter(c => c.id !== selectedConn) }); setSelectedConn(null); return }
    if (!selected.size) return
    commit(removeShapes(b, selected))
    setSelected(new Set())
  }

  /** A connector's route; the end being dragged follows the pointer and snaps to sides. */
  const routeOf = (c: Connector) => {
    const from = byId.get(c.from)
    const to = byId.get(c.to)
    if (!from || !to) return null
    if (drag?.kind === 'reattach' && drag.id === c.id) {
      const drop = dropOn(board.shapes, drag.at, view.zoom, drag.end === 'from' ? c.to : c.from)
      const box = drop?.shape ?? pointBox(drag.at)
      return drag.end === 'from'
        ? connectorRoute(box, to, { ...c, fromSide: drop?.side, route: undefined })
        : connectorRoute(from, box, { ...c, toSide: drop?.side, route: undefined })
    }
    return connectorRoute(from, to, c)
  }

  const updateConn = (id: string, patch: Partial<Connector>) => {
    const b = boardRef.current
    commit({ ...b, connectors: b.connectors.map(c => (c.id === id ? { ...c, ...patch } : c)) })
  }

  const duplicateSelection = () => {
    const b = boardRef.current
    const copies = b.shapes.filter(s => selected.has(s.id)).map(s => ({ ...s, id: newId(), x: s.x + 24, y: s.y + 24 }))
    if (!copies.length) return
    commit({ ...b, shapes: [...b.shapes, ...copies] })
    setSelected(new Set(copies.map(c => c.id)))
  }

  // Picking a swatch swaps a custom colour out.
  const recolor = (color: Swatch) => restyle({ color, hex: undefined })

  /** A change to every selected shape's look, as one undo step. */
  const restyle = (patch: Partial<Pick<Shape, 'color' | 'fill' | 'weight' | 'border' | 'borderColor' | 'hex' | 'borderHex'>>) => {
    const b = boardRef.current
    commit({ ...b, shapes: b.shapes.map(s => (selected.has(s.id) ? { ...s, ...patch } : s)) })
  }

  /**
   * The same, for a colour dragged or typed: each step shows at once but does not
   * enter the undo history; the last one records the whole gesture as one step.
   */
  const liveBase = useRef<Board | null>(null)
  const restyleLive = (patch: Partial<Pick<Shape, 'hex' | 'borderHex'>>, final: boolean) => {
    const b = boardRef.current
    liveBase.current ??= b
    const next = { ...b, shapes: b.shapes.map(s => (selected.has(s.id) ? { ...s, ...patch } : s)) }
    if (!final) { onChange(next); return }
    const before = liveBase.current
    liveBase.current = null
    if (JSON.stringify(before) !== JSON.stringify(next)) commit(next, before)
    else onChange(before)
  }

  /** Turn every selected shape into another kind, keeping its place, text and colour; a sticky is square. */
  const changeKind = (kind: ShapeKind) => {
    const b = boardRef.current
    commit({
      ...b,
      shapes: b.shapes.map(s => {
        if (!selected.has(s.id) || s.kind === kind) return s
        return kind === 'sticky' ? { ...s, kind, w: Math.max(s.w, s.h), h: Math.max(s.w, s.h) } : { ...s, kind }
      }),
    })
  }

  const doUndo = () => {
    const prev = undo.current.pop()
    if (!prev) return
    redo.current.push(boardRef.current)
    onChange(prev)
  }
  const doRedo = () => {
    const next = redo.current.pop()
    if (!next) return
    undo.current.push(boardRef.current)
    onChange(next)
  }

  /** Open the input for a new conversation about *shapeIds*, its pin at *at* (board units). */
  const startAsk = (shapeIds: string[], at: Pt) => {
    setSelectedConn(null)
    setOpenAsk(null)
    setDraft({ shapeIds, at })
  }

  /** The pin's spot for an ask about the current selection: its top-right corner. */
  const selectionPin = (): Pt | null => {
    const box = boundsOf(board.shapes.filter(s => selected.has(s.id)))
    return box ? { x: box.x + box.w, y: box.y } : null
  }

  const sendDraft = (text: string) => {
    if (!draft) return
    const id = newId('a')
    // A pin on a shape sits at the shape; on a section, at an offset from its corner so
    // it moves with it; on the board, where it was put.
    const host = draft.shapeIds.length === 1 ? board.shapes.find(s => s.id === draft.shapeIds[0]) : undefined
    const at = !draft.shapeIds.length ? draft.at
      : host?.kind === 'section' ? { x: draft.at.x - host.x, y: draft.at.y - host.y }
        : host ? { x: draft.at.x - host.x, y: draft.at.y - host.y } : undefined
    const ask = { id, shapeIds: draft.shapeIds, ...(at ? { at: { x: Math.round(at.x), y: Math.round(at.y) } } : {}) }
    const next = { ...boardRef.current, asks: [...boardRef.current.asks, ask] }
    commit(next)
    setDraft(null)
    setOpenAsk(id)
    onAsk(id, text, next)
  }

  const deleteAsk = (id: string) => {
    commit({ ...boardRef.current, asks: boardRef.current.asks.filter(a => a.id !== id) })
    if (openAsk === id) setOpenAsk(null)
  }

  /** Where an ask's pin is, in board units: its offset from its shape or section, or its own spot. */
  const pinAt = (a: { shapeIds: string[]; at?: Pt }): Pt | null => {
    const shapes = a.shapeIds.map(id => byId.get(id)).filter((s): s is Shape => !!s)
    if (a.shapeIds.length && !shapes.length) return null
    if (shapes.length === 1 && a.at) return { x: shapes[0].x + a.at.x, y: shapes[0].y + a.at.y }
    if (shapes.length) { const b = boundsOf(shapes)!; return { x: b.x + b.w, y: b.y } }
    return a.at ?? null
  }

  /** What an ask is about, for its card and its input's hint. */
  const aboutOf = (shapeIds: string[]): string => {
    const shapes = shapeIds.map(id => byId.get(id)).filter((s): s is Shape => !!s)
    if (!shapes.length) return 'the whole board'
    const clip = (t: string) => {
      const line = t.trim().split('\n')[0]
      if (line.length <= 40) return line
      // Cut at a word, not inside one.
      const cut = line.slice(0, 40)
      return `${cut.slice(0, cut.lastIndexOf(' ') > 20 ? cut.lastIndexOf(' ') : 40).trimEnd()}…`
    }
    const name = (s: Shape) => (s.text.trim() ? `"${clip(s.text)}"` : s.kind === 'section' ? 'this section' : `this ${s.kind === 'rect' ? 'rectangle' : s.kind}`)
    return shapes.length === 1 ? name(shapes[0]) : `${shapes.length} shapes`
  }

  // ---- pointer gestures ------------------------------------------------------------

  const onPointerDown = (e: React.PointerEvent) => {
    const target = e.target as HTMLElement
    if (target.closest('[data-board-ui]') || (editing && target.closest('textarea'))) return
    // The right button opens the menu (onContextMenu); it never starts a drag.
    if (e.button === 2) return
    if (editing) endEdit()
    root.current?.focus({ preventScroll: true })
    // A press elsewhere puts away an unsent question and closes an open conversation.
    setDraft(null)
    setOpenAsk(null)
    const world = toWorld(e.clientX, e.clientY)
    const panning = tool === 'hand' || spaceDown || e.button === 1
    if (panning) { setDrag({ kind: 'pan', sx: e.clientX, sy: e.clientY, view0: viewRef.current }); return }
    if (tool === 'ask') {
      // Pointed: the conversation is about what is here, walking up from the point.
      e.preventDefault()
      startAsk(askTarget(board.shapes, world, view.zoom).shapeIds, world)
      setTool('select')
      return
    }
    if (tool === 'session') {
      // Pointed: the session picker opens there, and the card lands where it was.
      e.preventDefault()
      const r = root.current!.getBoundingClientRect()
      setLinking({ sx: e.clientX - r.left, sy: e.clientY - r.top, at: world })
      return
    }

    const handle = target.closest('[data-handle]') as HTMLElement | null
    if (handle) {
      const id = handle.dataset.shape!
      const box0 = byId.get(id)
      if (box0) setDrag({ kind: 'resize', id, corner: handle.dataset.handle as Corner, start: world, box0, snapshot: board })
      return
    }
    const dot = target.closest('[data-connect]') as HTMLElement | null
    if (dot) { setDrag({ kind: 'connect', from: dot.dataset.connect!, fromSide: dot.dataset.side as Side, at: world }); return }
    const connEnd = target.closest('[data-conn-end]') as HTMLElement | null
    if (connEnd && selectedConn) { setDrag({ kind: 'reattach', id: selectedConn, end: connEnd.dataset.connEnd as 'from' | 'to', at: world }); return }
    const segEl = target.closest('[data-conn-seg]') as HTMLElement | null
    const segConn = segEl && board.connectors.find(c => c.id === selectedConn)
    const segRoute = segConn && routeOf(segConn)
    if (segEl && segConn && segRoute) {
      const seg = segmentDrag(segRoute.points, Number(segEl.dataset.connSeg), ...segRoute.sides)
      setDrag({ kind: 'segment', id: segConn.id, seg, start: world, snapshot: board })
      return
    }
    const conn = target.closest('[data-conn-id]') as HTMLElement | null
    if (conn && tool === 'select') { setSelected(new Set()); setSelectedConn(conn.dataset.connId!); return }

    if (tool !== 'select' && tool !== 'connector') {
      // The press creates a shape and focuses its text editor. Left to its
      // default, the same press then moves focus to the canvas, which ends the
      // edit at once (and an empty text box is deleted when its edit ends).
      e.preventDefault()
      setDrag({ kind: 'create', shape: tool, start: world, snapshot: board, id: null })
      return
    }
    const hit = shapeAt(board.shapes, world, { zoom: view.zoom })
    if (tool === 'connector') {
      if (hit && hit.kind !== 'section') setDrag({ kind: 'connect', from: hit.id, at: world })
      return
    }
    setSelectedConn(null)
    if (hit) {
      let next = selected
      if (e.shiftKey) {
        next = new Set(selected)
        if (next.has(hit.id)) next.delete(hit.id)
        else next.add(hit.id)
      } else if (!selected.has(hit.id)) next = new Set([hit.id])
      setSelected(next)
      // A section carries what it holds, as in FigJam.
      const carried = new Set(next)
      for (const s of board.shapes) if (s.kind === 'section' && next.has(s.id)) sectionMembers(board.shapes, s).forEach(m => carried.add(m.id))
      const origin = new Map(board.shapes.filter(s => carried.has(s.id)).map(s => [s.id, { x: s.x, y: s.y }]))
      setDrag({ kind: 'move', start: world, origin, snapshot: board, moved: false })
      return
    }
    const r = root.current!.getBoundingClientRect()
    if (!e.shiftKey) setSelected(new Set())
    setDrag({ kind: 'marquee', sx: e.clientX - r.left, sy: e.clientY - r.top, cx: e.clientX - r.left, cy: e.clientY - r.top, base: e.shiftKey ? new Set(selected) : new Set() })
  }

  useEffect(() => {
    if (!drag) return
    const move = (e: PointerEvent) => {
      const world = toWorld(e.clientX, e.clientY)
      const b = boardRef.current
      if (drag.kind === 'pan') {
        setView({ ...drag.view0, x: drag.view0.x + e.clientX - drag.sx, y: drag.view0.y + e.clientY - drag.sy })
      } else if (drag.kind === 'move') {
        const dx = world.x - drag.start.x
        const dy = world.y - drag.start.y
        if (!drag.moved && Math.hypot(dx, dy) * viewRef.current.zoom < 3) return
        drag.moved = true
        // The grabbed group moves as one: snapping its first shape decides the
        // step, so shapes keep their spacing relative to each other.
        const lead = drag.origin.values().next().value
        const sdx = lead ? snap(lead.x + dx, !e.altKey) - lead.x : Math.round(dx)
        const sdy = lead ? snap(lead.y + dy, !e.altKey) - lead.y : Math.round(dy)
        onChange({ ...b, shapes: b.shapes.map(s => { const o = drag.origin.get(s.id); return o ? { ...s, x: o.x + sdx, y: o.y + sdy } : s }) })
      } else if (drag.kind === 'resize') {
        const { box0, corner } = drag
        const on = !e.altKey
        const left = corner.includes('w')
        const top = corner.includes('n')
        // An edge moves one side only; a corner moves both.
        const movesX = corner !== 'n' && corner !== 's'
        const movesY = corner !== 'e' && corner !== 'w'
        // The dragged side lands on the grid; the opposite side stays put.
        const cx = snap((left ? box0.x : box0.x + box0.w) + world.x - drag.start.x, on)
        const cy = snap((top ? box0.y : box0.y + box0.h) + world.y - drag.start.y, on)
        const min = on ? GRID : 32
        let w = movesX ? Math.max(min, left ? box0.x + box0.w - cx : cx - box0.x) : box0.w
        let h = movesY ? Math.max(min, top ? box0.y + box0.h - cy : cy - box0.y) : box0.h
        // Stickies stay square, like paper notes: an edge drag grows both ways.
        if (box0.kind === 'sticky') { w = h = movesX && movesY ? Math.max(w, h) : movesX ? w : h }
        const x = left ? box0.x + box0.w - w : box0.x
        const y = top ? box0.y + box0.h - h : box0.y
        onChange({ ...b, shapes: b.shapes.map(s => (s.id === drag.id ? { ...s, x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h) } : s)) })
      } else if (drag.kind === 'marquee') {
        const r = root.current!.getBoundingClientRect()
        const cx = e.clientX - r.left
        const cy = e.clientY - r.top
        setDrag({ ...drag, cx, cy })
        const v = viewRef.current
        const x0 = (Math.min(drag.sx, cx) - v.x) / v.zoom
        const y0 = (Math.min(drag.sy, cy) - v.y) / v.zoom
        const x1 = (Math.max(drag.sx, cx) - v.x) / v.zoom
        const y1 = (Math.max(drag.sy, cy) - v.y) / v.zoom
        // A section is selected only when the marquee takes all of it, so a marquee
        // drawn inside one picks its cards, not the section around them.
        const hit = b.shapes.filter(s => !s.locked && (s.kind === 'section'
          ? s.x >= x0 && s.x + s.w <= x1 && s.y - SECTION_TITLE_H >= y0 && s.y + s.h <= y1
          : s.x < x1 && s.x + s.w > x0 && s.y < y1 && s.y + s.h > y0)).map(s => s.id)
        setSelected(new Set([...drag.base, ...hit]))
      } else if (drag.kind === 'connect' || drag.kind === 'reattach') {
        setDrag({ ...drag, at: world })
      } else if (drag.kind === 'segment') {
        const { seg } = drag
        const value = snap(seg.points[seg.seg][seg.axis] + world[seg.axis] - drag.start[seg.axis], !e.altKey)
        const route = movedSegment(seg, value)
        onChange({ ...b, connectors: b.connectors.map(c => (c.id === drag.id ? { ...c, route } : c)) })
      } else if (drag.kind === 'create') {
        // Below a few screen pixels it is still a click, not a drag.
        if (!drag.id && Math.hypot(world.x - drag.start.x, world.y - drag.start.y) * viewRef.current.zoom < 4) return
        const box = dragBox(drag.shape, drag.start, world, e.altKey)
        if (!drag.id) {
          drag.id = newId()
          const shape: Shape = { id: drag.id, kind: drag.shape, ...box, text: '', color: defaultColor(drag.shape) }
          onChange({ ...b, shapes: [...b.shapes, shape] })
          setSelected(new Set([drag.id]))
        } else {
          onChange({ ...b, shapes: b.shapes.map(s => (s.id === drag.id ? { ...s, ...box } : s)) })
        }
      }
    }
    const up = (e: PointerEvent) => {
      const b = boardRef.current
      if (drag.kind === 'move' && drag.moved) commit(b, drag.snapshot)
      else if (drag.kind === 'resize') commit(b, drag.snapshot)
      else if (drag.kind === 'segment' && JSON.stringify(b) !== JSON.stringify(drag.snapshot)) commit(b, drag.snapshot)
      else if (drag.kind === 'create') {
        if (drag.id) { commit(b, drag.snapshot); finishCreate(drag.id) } else createRef.current(drag.shape, drag.start)
      }
      else if (drag.kind === 'connect') {
        const drop = dropOn(b.shapes, toWorld(e.clientX, e.clientY), viewRef.current.zoom, drag.from)
        if (drop && !b.connectors.some(c => c.from === drag.from && c.to === drop.shape.id)) {
          const conn: Connector = { id: newId('c'), from: drag.from, to: drop.shape.id }
          if (drag.fromSide) conn.fromSide = drag.fromSide
          if (drop.side) conn.toSide = drop.side
          commit({ ...b, connectors: [...b.connectors, conn] })
        }
        if (tool === 'connector') setTool('select')
      }
      else if (drag.kind === 'reattach') {
        const c = b.connectors.find(k => k.id === drag.id)
        const drop = c && dropOn(b.shapes, toWorld(e.clientX, e.clientY), viewRef.current.zoom, drag.end === 'from' ? c.to : c.from)
        if (c && drop) {
          // Dropped on a side's dot: pinned there. On a shape's body: that side faces the other end.
          // A hand-shaped route was for the old ends, so it goes and the elbow routes itself.
          const patch: Partial<Connector> = drag.end === 'from'
            ? { from: drop.shape.id, fromSide: drop.side, route: undefined }
            : { to: drop.shape.id, toSide: drop.side, route: undefined }
          const next = { ...c, ...patch }
          if (JSON.stringify(next) !== JSON.stringify(c)) commit({ ...b, connectors: b.connectors.map(k => (k.id === c.id ? next : k)) })
        }
      }
      setDrag(null)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    return () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up) }
  }, [drag, toWorld, onChange, commit, tool])

  const onHover = (e: React.PointerEvent) => {
    if (drag || tool !== 'select') { if (hovered) setHovered(null); return }
    const target = e.target as HTMLElement
    if (target.closest('[data-board-ui]')) return
    // The connect dots sit outside the shape: reaching for one must not unhover it.
    if (target.closest('[data-connect]')) return
    const world = toWorld(e.clientX, e.clientY)
    const hit = shapeAt(board.shapes, world, { zoom: view.zoom })
    if (!hit && hovered) {
      const s = byId.get(hovered)
      const pad = HOVER_MARGIN / view.zoom
      if (s && world.x >= s.x - pad && world.x <= s.x + s.w + pad && world.y >= s.y - pad && world.y <= s.y + s.h + pad) return
    }
    if ((hit?.id ?? null) !== hovered) setHovered(hit?.id ?? null)
  }

  const onDoubleClick = (e: React.MouseEvent) => {
    if ((e.target as HTMLElement).closest('[data-board-ui]')) return
    const world = toWorld(e.clientX, e.clientY)
    const hit = shapeAt(board.shapes, world, { zoom: view.zoom })
    // A session card has no text to edit: a double-click opens its session.
    if (hit?.kind === 'session') { setSelected(new Set([hit.id])); openCard(hit) } else if (hit) { setSelected(new Set([hit.id])); setEditing(hit.id) } else create('sticky', world)
  }

  // ---- keys --------------------------------------------------------------------------

  const onKeyDown = (e: React.KeyboardEvent) => {
    const t = e.target as HTMLElement
    if (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT') return
    const mod = e.metaKey || e.ctrlKey
    if (e.key === ' ') { setSpaceDown(true); e.preventDefault(); return }
    if (mod && e.key.toLowerCase() === 'z') { e.preventDefault(); if (e.shiftKey) doRedo(); else doUndo(); return }
    if (mod && e.key.toLowerCase() === 'y') { e.preventDefault(); doRedo(); return }
    if (mod && e.key.toLowerCase() === 'd') { e.preventDefault(); duplicateSelection(); return }
    if (mod && e.key.toLowerCase() === 'a') { e.preventDefault(); setSelected(new Set(board.shapes.filter(s => !s.locked).map(s => s.id))); return }
    if (mod && !e.shiftKey && e.code === 'KeyC') { e.preventDefault(); runAction('copy', [...selected]); return }
    if (mod && !e.shiftKey && e.code === 'KeyX') { e.preventDefault(); runAction('copy', [...selected]); runAction('delete', [...selected]); return }
    if (mod && !e.shiftKey && e.code === 'KeyV') { e.preventDefault(); runAction('paste', []); return }
    if (mod && e.shiftKey && e.code === 'KeyL') { e.preventDefault(); runAction(e.altKey ? 'unlockAll' : 'lock', [...selected]); return }
    if (e.code === 'BracketRight' || e.code === 'BracketLeft') {
      e.preventDefault()
      const up = e.code === 'BracketRight'
      runAction(mod ? (up ? 'forward' : 'backward') : (up ? 'front' : 'back'), [...selected])
      return
    }
    if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); deleteSelection(); return }
    if (e.key === 'Escape') { setSelected(new Set()); setSelectedConn(null); setTool('select'); setDraft(null); setOpenAsk(null); return }
    if (e.key === 'Enter' && selected.size === 1) {
      e.preventDefault()
      const one = byId.get([...selected][0])
      if (one?.kind === 'session') openCard(one)
      else setEditing([...selected][0])
      return
    }
    // C talks to the conductor (FigJam's comment key): about the selection at once;
    // with nothing selected it picks the Ask tool, so the next click says where.
    // preventDefault keeps the "c" out of the input that opens focused.
    if (!mod && !e.altKey && e.key.toLowerCase() === 'c') {
      e.preventDefault()
      const at = selected.size ? selectionPin() : null
      if (at) startAsk([...selected], at)
      else setTool('ask')
      return
    }
    if (!mod && !e.altKey) {
      const next = TOOL_BY_KEY.get(e.key.toLowerCase())
      if (next) setTool(next)
    }
  }
  // ---- the right-click menu ---------------------------------------------------------

  /** One menu action on *ids*; Paste lands at *at*, or beside what was copied. */
  const runAction = (a: MenuAction, ids: string[], at?: Pt) => {
    const b = boardRef.current
    const set = new Set(ids)
    if (a === 'copy') { boardClipboard = copyShapes(b, set) ?? boardClipboard; return }
    if (a === 'paste') {
      if (!boardClipboard) return
      const pasted = pasteClip(b, boardClipboard, at)
      commit(pasted.board)
      setSelectedConn(null)
      setSelected(new Set(pasted.ids))
      return
    }
    if (a === 'unlockAll') { if (b.shapes.some(s => s.locked)) commit(unlockAll(b)); return }
    if (!set.size) return
    if (a === 'delete') { commit(removeShapes(b, set)); setSelected(new Set()); return }
    if (a === 'lock') {
      // Mixed: lock them all, as FigJam does; all locked already: unlock them.
      const lock = !b.shapes.filter(s => set.has(s.id)).every(s => s.locked)
      commit(setLocked(b, set, lock))
      setSelected(lock ? new Set() : set)
      if (editing && set.has(editing)) setEditing(null)
      return
    }
    commit(restack(b, set, a))
  }

  const onContextMenu = (e: React.MouseEvent) => {
    if ((e.target as HTMLElement).closest('[data-board-ui]')) return
    e.preventDefault()
    if (editing) endEdit()
    const r = root.current!.getBoundingClientRect()
    const at = toWorld(e.clientX, e.clientY)
    // A locked shape still takes a right-click, so it can be unlocked.
    const hit = shapeAt(board.shapes, at, { locked: true, zoom: view.zoom })
    let ids: string[] = []
    if (hit && selected.has(hit.id)) ids = [...selected]
    else if (hit) { ids = [hit.id]; setSelected(hit.locked ? new Set() : new Set([hit.id])) }
    else setSelected(new Set())
    setSelectedConn(null)
    setDraft(null)
    setMenu({ x: e.clientX - r.left, y: e.clientY - r.top, at, ids })
  }

  const onKeyUp = (e: React.KeyboardEvent) => { if (e.key === ' ') setSpaceDown(false) }

  // ---- overlay and answers, pinned to shapes in screen space ------------------------------

  const anchorShape = (anchor: string | null): Shape | null => {
    if (!anchor) return null
    if (anchor.startsWith('shape:')) return byId.get(anchor.slice(6).trim()) ?? null
    const a = anchor.trim().toLowerCase()
    return board.shapes.find(s => s.text.trim().toLowerCase() === a) ?? board.shapes.find(s => a && s.text.toLowerCase().includes(a)) ?? null
  }
  const placedOverlay = overlay.map(item => ({ item, shape: anchorShape(item.anchor) }))
  /**
   * The box an overlay item is placed around. A section's label sits just above its
   * body, so items placed above it (`top`) or on its corner start above the label, not on it.
   * The label is SECTION_TITLE_H tall on screen at 100% and above, and stays that size below.
   */
  const overlayBox = (shape: Shape, place: string) => {
    const r = screenBox(shape)
    if (shape.kind !== 'section' || (place !== 'top' && place !== 'corner')) return r
    const label = SECTION_TITLE_H * Math.max(view.zoom, 1)
    return { x: r.x, y: r.y - label, w: r.w, h: r.h + label }
  }
  const tray = placedOverlay.filter(p => !p.shape)

  // Overlay markup is the conductor's, so its session links are delegated from
  // the layer rather than bound per element.
  const overlayLayer = useRef<HTMLDivElement>(null)
  const openRef = useRef(onOpenSession)
  openRef.current = onOpenSession
  // Session links in shapes' text: one object while the list is the same, so no
  // shape redraws for an unrelated change.
  const links = useMemo<BoardLinks>(() => ({ sessions: sessions ?? [], self: selfKey ?? null, open: k => openRef.current(k) }), [sessions, selfKey])
  useEffect(() => {
    const el = overlayLayer.current
    if (!el) return
    const onClick = (e: MouseEvent) => {
      const hit = (e.target as HTMLElement).closest('[data-open-session]')
      const key = hit?.getAttribute('data-open-session')
      if (key) { e.preventDefault(); openRef.current(key) }
    }
    el.addEventListener('click', onClick)
    return () => el.removeEventListener('click', onClick)
  }, [])

  // Keep the ask box in view when it opens.

  // Overlay items never sit on each other: each one that overlaps an item already
  // placed steps out of its way (up for items above a shape, down for the rest).
  useLayoutEffect(() => {
    const layer = overlayLayer.current
    if (!layer) return
    const els = [...layer.querySelectorAll<HTMLElement>('[data-testid="board-overlay-item"][data-place]')]
    els.forEach(el => { el.style.marginTop = '' })
    const placed: DOMRect[] = []
    for (const el of els.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top)) {
      let r = el.getBoundingClientRect()
      const up = el.dataset.place === 'top'
      let shift = 0
      for (let pass = 0; pass < 12; pass++) {
        const hit = placed.find(p => r.left < p.right && r.right > p.left && r.top < p.bottom + 4 && r.bottom + 4 > p.top)
        if (!hit) break
        const step = up ? -(r.bottom - hit.top + 6) : hit.bottom - r.top + 6
        shift += step
        r = new DOMRect(r.left, r.top + step, r.width, r.height)
      }
      if (shift) el.style.marginTop = `${shift}px`
      placed.push(r)
    }
  })

  const cursor = drag?.kind === 'pan' ? 'grabbing' : tool === 'hand' || spaceDown ? 'grab' : tool === 'select' ? 'default' : 'crosshair'
  const showChrome = !drag || drag.kind === 'marquee'

  return (
    <div
      ref={root}
      role="application"
      aria-label="Whiteboard"
      className="relative h-full w-full overflow-hidden outline-none select-none"
      data-testid="board-canvas"
      tabIndex={0}
      onPointerDown={onPointerDown}
      onPointerMove={onHover}
      onDoubleClick={onDoubleClick}
      onContextMenu={onContextMenu}
      onKeyDown={onKeyDown}
      onKeyUp={onKeyUp}
      style={{
        cursor,
        // FigJam's canvas: a slightly grey ground with round, clearly visible dots.
        background: 'color-mix(in srgb, var(--bg) 95%, var(--text))',
        backgroundImage: 'radial-gradient(circle, color-mix(in srgb, var(--text) 26%, transparent) 1.3px, transparent 1.6px)',
        backgroundSize: `${dotSpacing(view.zoom)}px ${dotSpacing(view.zoom)}px`,
        backgroundPosition: `${view.x}px ${view.y}px`,
      }}
    >
      {/* The world: shapes, then connectors over them (a line across a shape shows),
          both moved and scaled by the view. */}
      <div style={{ position: 'absolute', left: 0, top: 0, transform: `translate(${view.x}px, ${view.y}px) scale(${view.zoom})`, transformOrigin: '0 0' }}>
        {zOrdered(board.shapes).map(s => (
          <ShapeView key={s.id} shape={s} editing={editing === s.id} zoom={view.zoom} links={links} onText={t => setText(s.id, t)} onEndEdit={endEdit}
            mark={s.kind !== 'section' ? undefined : conductorAccessOf(board, s) === boardAccess(board) ? undefined : conductorAccessOf(board, s) === 'deny' ? 'protected' : 'open'} />
        ))}
        <svg style={{ position: 'absolute', left: 0, top: 0, width: 1, height: 1, overflow: 'visible' }} aria-hidden>
          <defs>
            <marker id="board-arrow" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse" overflow="visible">
              {/* An open chevron, as on FigJam connectors; sized in stroke widths. */}
              <path d="M4,0.5 L8.5,5 L4,9.5" fill="none" stroke="context-stroke" strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round" />
            </marker>
          </defs>
          {board.connectors.map(c => {
            const route = routeOf(c)
            if (!route) return null
            const on = selectedConn === c.id
            const arrows = c.arrows ?? 'end'
            return (
              <g key={c.id} data-conn-id={c.id} data-testid="board-connector" data-line={c.line ?? 'elbow'}>
                <path d={route.d} fill="none" stroke="transparent" strokeWidth={14 / view.zoom} style={{ pointerEvents: 'stroke', cursor: 'pointer' }} />
                {on && <path d={route.d} fill="none" stroke="var(--accent)" strokeOpacity={0.25} strokeWidth={CONNECTOR_STROKE + 6} strokeLinecap="round" strokeLinejoin="round" style={{ pointerEvents: 'none' }} />}
                <path d={route.d} fill="none" stroke={connColor(c)} strokeWidth={CONNECTOR_STROKE} strokeLinecap="round" strokeLinejoin="round"
                  strokeDasharray={c.dashed ? `${CONNECTOR_STROKE * 3} ${CONNECTOR_STROKE * 2.5}` : undefined}
                  markerEnd={arrows !== 'none' ? 'url(#board-arrow)' : undefined} markerStart={arrows === 'both' ? 'url(#board-arrow)' : undefined}
                  style={{ pointerEvents: 'none' }} />
              </g>
            )
          })}
          {drag?.kind === 'connect' && byId.get(drag.from) && (() => {
            const drop = dropOn(board.shapes, drag.at, view.zoom, drag.from)
            const route = connectorRoute(byId.get(drag.from)!, drop?.shape ?? pointBox(drag.at), { fromSide: drag.fromSide, toSide: drop?.side })
            return <path d={route.d} fill="none" stroke="var(--accent)" strokeWidth={CONNECTOR_STROKE} strokeDasharray="8 6" strokeLinecap="round" strokeLinejoin="round" markerEnd="url(#board-arrow)" />
          })()}
        </svg>
      </div>

      {/* While a conversation is open, what it is about is tinted: a translucent wash over exactly
          the shapes it concerns (no outline, no box around anything that was not asked about). */}
      {(board.asks.find(a => a.id === openAsk)?.shapeIds ?? []).map(id => byId.get(id)).filter((x): x is Shape => !!x).map(s => {
        const r = screenBox(s)
        return (
          <div key={`about-${s.id}`} data-testid="ask-target" className="pointer-events-none absolute"
            style={{
              left: r.x, top: r.y, width: r.w, height: r.h, zIndex: 5,
              borderRadius: s.kind === 'ellipse' ? '50%' : s.kind === 'section' ? 12 : 6,
              background: 'color-mix(in srgb, var(--accent) 22%, transparent)',
            }} />
        )
      })}

      {/* Selection outline and resize handles. */}
      {showChrome && selShapes.map(s => {
        const r = screenBox(s)
        return (
          <div key={`sel-${s.id}`} className="pointer-events-none absolute" style={{ left: r.x - 2, top: r.y - 2, width: r.w + 4, height: r.h + 4, border: '1.5px solid var(--accent)', borderRadius: s.kind === 'ellipse' ? '50%' : s.kind === 'section' ? 14 : 8 }} />
        )
      })}
      {/* Edges of the selected shape: hover one and drag to move just that side.
          Drawn before the corner handles, so a corner still wins where they meet. */}
      {showChrome && selShapes.length === 1 && !editing && !selShapes[0].locked && (['n', 'e', 's', 'w'] as const).map(c => {
        const r = screenBox(selShapes[0])
        const t = 10
        const pos = c === 'n' ? { left: r.x, top: r.y - t / 2, width: r.w, height: t }
          : c === 's' ? { left: r.x, top: r.y + r.h - t / 2, width: r.w, height: t }
            : c === 'w' ? { left: r.x - t / 2, top: r.y, width: t, height: r.h }
              : { left: r.x + r.w - t / 2, top: r.y, width: t, height: r.h }
        return <div key={c} data-handle={c} data-shape={selShapes[0].id} data-testid={`handle-${c}`} className="absolute" style={{ ...pos, cursor: EDGE_CURSOR[c] }} />
      })}
      {showChrome && selShapes.length === 1 && !editing && (['nw', 'ne', 'sw', 'se'] as Corner[]).map(c => {
        const r = screenBox(selShapes[0])
        const left = c.endsWith('w') ? r.x - 6 : r.x + r.w - 4
        const top = c.startsWith('n') ? r.y - 6 : r.y + r.h - 4
        return (
          <div key={c} data-handle={c} data-shape={selShapes[0].id} data-testid={`handle-${c}`} className="absolute"
            style={{ left, top, width: 10, height: 10, borderRadius: 3, background: 'var(--bg)', border: '1.5px solid var(--accent)', cursor: c === 'nw' || c === 'se' ? 'nwse-resize' : 'nesw-resize' }} />
        )
      })}

      {/* Connect dots on the hovered shape, or on the one selected shape: drag one onto another shape. */}
      {!drag && !editing && dotShape && (() => {
        const r = screenBox(dotShape)
        const o = DOT_OFFSET
        const dots = [{ x: r.x + r.w / 2, y: r.y - o }, { x: r.x + r.w + o, y: r.y + r.h / 2 }, { x: r.x + r.w / 2, y: r.y + r.h + o }, { x: r.x - o, y: r.y + r.h / 2 }]
        return dots.map((d, i) => (
          <div key={i} data-connect={dotShape.id} data-side={SIDES[i]} data-testid="connect-dot" className="absolute"
            style={{ left: d.x - 6, top: d.y - 6, width: 12, height: 12, borderRadius: '50%', background: 'var(--accent)', border: '2px solid var(--bg)', boxShadow: '0 1px 4px rgba(0,0,0,.25)', cursor: 'crosshair' }} />
        ))
      })()}

      {/* While an end is dragged: the side midpoints of the shape it would land on. */}
      {(drag?.kind === 'connect' || drag?.kind === 'reattach') && (() => {
        const c = drag.kind === 'reattach' ? board.connectors.find(k => k.id === drag.id) : null
        const not = drag.kind === 'connect' ? drag.from : c ? (drag.end === 'from' ? c.to : c.from) : undefined
        const drop = dropOn(board.shapes, drag.at, view.zoom, not)
        if (!drop) return null
        return SIDES.map(side => {
          const p = toScreen(sideAnchor(drop.shape, side))
          const snapped = drop.side === side
          return (
            <div key={side} data-testid="connect-target" data-snapped={snapped || undefined} className="pointer-events-none absolute"
              style={{ left: p.x - 6, top: p.y - 6, width: 12, height: 12, borderRadius: '50%', background: snapped ? 'var(--accent)' : 'var(--bg)', border: '2px solid var(--accent)' }} />
          )
        })
      })()}

      {/* The selected connector: a handle on each end to re-attach it, and its toolbar. */}
      {selectedConn && !drag && (() => {
        const c = board.connectors.find(k => k.id === selectedConn)
        const route = c && routeOf(c)
        if (!c || !route) return null
        const ends: ['from' | 'to', Pt][] = [['from', toScreen(route.a)], ['to', toScreen(route.b)]]
        const pts = route.points.map(toScreen)
        const top = Math.min(...pts.map(p => p.y))
        const cx = (Math.min(...pts.map(p => p.x)) + Math.max(...pts.map(p => p.x))) / 2
        return (
          <>
            {/* A pill on each elbow segment long enough to grab: drag it sideways to reshape the route. */}
            {(c.line ?? 'elbow') === 'elbow' && pts.slice(0, -1).map((p, j) => {
              const q = pts[j + 1]
              const flat = Math.abs(p.y - q.y) < 0.5
              if (Math.hypot(q.x - p.x, q.y - p.y) < 36) return null
              return (
                <div key={`seg-${j}`} data-conn-seg={j} data-testid="conn-seg" className="absolute"
                  style={{
                    left: (p.x + q.x) / 2 - (flat ? 12 : 3), top: (p.y + q.y) / 2 - (flat ? 3 : 12), width: flat ? 24 : 6, height: flat ? 6 : 24,
                    borderRadius: 3, background: 'var(--accent)', border: '1px solid var(--bg)', cursor: flat ? 'ns-resize' : 'ew-resize', zIndex: 7,
                  }} />
              )
            })}
            {ends.map(([end, p]) => (
              <div key={end} data-conn-end={end} data-testid={`conn-end-${end}`} className="absolute"
                style={{ left: p.x - 8, top: p.y - 8, width: 16, height: 16, borderRadius: '50%', background: 'var(--bg)', border: '2.5px solid var(--accent)', boxShadow: '0 1px 4px rgba(0,0,0,.2)', cursor: 'grab', zIndex: 7 }} />
            ))}
            <div data-board-ui className="absolute" style={{ left: cx, top: top - 16, transform: 'translate(-50%, -100%)', zIndex: 8 }} onPointerDown={e => e.stopPropagation()}>
              <ConnectorToolbar conn={c} onChange={patch => updateConn(c.id, patch)}
                onDelete={() => { commit({ ...boardRef.current, connectors: boardRef.current.connectors.filter(k => k.id !== c.id) }); setSelectedConn(null) }} />
            </div>
          </>
        )
      })()}

      {/* Marquee. */}
      {drag?.kind === 'marquee' && (
        <div className="pointer-events-none absolute" style={{
          left: Math.min(drag.sx, drag.cx), top: Math.min(drag.sy, drag.cy), width: Math.abs(drag.cx - drag.sx), height: Math.abs(drag.cy - drag.sy),
          background: 'color-mix(in srgb, var(--accent) 10%, transparent)', border: '1px solid color-mix(in srgb, var(--accent) 60%, transparent)', borderRadius: 4,
        }} />
      )}

      {/* Conversations with the conductor: a pin where each was started; hover to peek,
          click for the card. One is open at a time. */}
      {board.asks.map(a => {
        const at = pinAt(a)
        if (!at) return null
        const p = toScreen(at)
        // The card opens on whichever side of the pin has room, and lifts to stay above the dock.
        const room = { w: root.current?.clientWidth ?? 1e4, h: root.current?.clientHeight ?? 1e4 }
        const side = p.x + 36 + CARD_W + 16 > room.w && p.x - 36 - CARD_W > 16 ? 'left' : 'right'
        const lift = Math.max(0, Math.min(p.y - 28 - 16, p.y - 28 + CARD_MAX_H - (room.h - 96)))
        return (
          <div key={a.id} data-board-ui data-testid="board-thread" data-ask={a.id} className="absolute"
            style={{ left: p.x, top: p.y - 28, zIndex: openAsk === a.id ? 12 : 7 }} onPointerDown={e => e.stopPropagation()}>
            <ConductorThread thread={threads[a.id]} about={aboutOf(a.shapeIds)} open={openAsk === a.id} side={side} lift={lift}
              onOpenChange={o => { setOpenAsk(o ? a.id : null); if (o) { setDraft(null); onReadAsk(a.id) } }}
              onSend={text => onFollowUp(a.id, text)} onDelete={() => deleteAsk(a.id)} onOpenSession={onOpenSession} />
          </div>
        )
      })}
      {draft && (() => {
        const p = toScreen(draft.at)
        return (
          <div data-board-ui className="absolute" style={{ left: p.x, top: p.y - 28, zIndex: 13 }} onPointerDown={e => e.stopPropagation()}>
            <AskDraft about={aboutOf(draft.shapeIds)} onSend={sendDraft} onCancel={() => { setDraft(null); root.current?.focus({ preventScroll: true }) }} />
          </div>
        )
      })()}
      {linking && (
        <SessionPicker x={linking.sx} y={linking.sy} sessions={links.sessions} self={links.self} onPick={linkSession}
          onClose={() => { setLinking(null); setTool('select'); root.current?.focus({ preventScroll: true }) }} />
      )}

      {/* The conductor's overlay, pinned to shapes. */}
      <div data-testid="board-overlay" ref={overlayLayer}>
        {overlaysOn && placedOverlay.filter(p => p.shape).map(({ item, shape }) => (
          // Pointer events pass through to the board, so an overlay never blocks a drag;
          // only its own links and buttons catch the pointer.
          <div key={item.key} data-board-ui data-testid="board-overlay-item" data-place={item.place} className="absolute pointer-events-none [&_a]:pointer-events-auto [&_button]:pointer-events-auto" onPointerDown={e => e.stopPropagation()}
            style={{ ...placeAround(item.place, overlayBox(shape!, item.place)), zIndex: 6, maxWidth: 300, fontSize: 12.5, lineHeight: 1.45, color: 'var(--text)' }}
            // Sanitized by parseOverlay: scripts, handlers, forms and loads are gone.
            dangerouslySetInnerHTML={{ __html: item.html }} />
        ))}
        {overlaysOn && tray.length > 0 && (
          <div data-board-ui data-testid="board-overlay-tray" className="pointer-events-none absolute right-4 top-4 flex w-[240px] flex-col gap-1.5 [&_a]:pointer-events-auto [&_button]:pointer-events-auto" style={{ zIndex: 6 }} onPointerDown={e => e.stopPropagation()}>
            {tray.map(({ item }) => <div key={item.key} data-testid="board-overlay-item" dangerouslySetInnerHTML={{ __html: item.html }} />)}
          </div>
        )}
      </div>

      {/* The selection toolbar: colours, ask the conductor, delete. */}
      {showChrome && selBox && !editing && (() => {
        const r = screenBox(selBox)
        return (
          <div data-board-ui className="absolute" style={{ left: r.x + r.w / 2, top: r.y - 14, transform: 'translate(-50%, -100%)', zIndex: 8 }} onPointerDown={e => e.stopPropagation()}>
            {!draft && (
              <ShapeToolbar shapes={selShapes} onColor={recolor} onFill={fill => restyle({ fill: fill === 'solid' ? undefined : fill })}
                onKind={changeKind} onWeight={weight => restyle({ weight: weight === 'medium' ? undefined : weight })}
                onBorder={border => restyle({ border: border === 'solid' ? undefined : border })} onBorderColor={borderColor => restyle({ borderColor, borderHex: undefined })}
                onHex={(hex, final) => restyleLive({ hex }, final)} onBorderHex={(borderHex, final) => restyleLive({ borderHex }, final)}
                onOpen={selShapes.length === 1 && selShapes[0].kind === 'session' ? () => openCard(selShapes[0]) : undefined}
                onGoal={goal => { const b = boardRef.current; commit({ ...b, shapes: b.shapes.map(s => (selected.has(s.id) && s.kind === 'section' ? { ...s, goal } : s)) }) }}
                goalEvery={boardGoalEvery(board)} goalLatest={selShapes.length === 1 ? goalReport?.(selShapes[0].id) ?? null : null}
                onGoalEvery={m => commit({ ...boardRef.current, goalEvery: m })}
                access={selShapes.length === 1 ? conductorAccessOf(board, selShapes[0]) : 'allow'}
                onAccess={() => {
                  const b = boardRef.current
                  const sec = b.shapes.find(s => s.id === selShapes[0]?.id)
                  if (!sec || sec.kind !== 'section') return
                  const next = conductorAccessOf(b, sec) === 'allow' ? 'deny' : 'allow'
                  // Stored only when it differs from what the section would inherit anyway.
                  const inherited = conductorAccessOf(b, { ...sec, conductor: undefined })
                  commit({ ...b, shapes: b.shapes.map(s => (s.id === sec.id ? { ...s, conductor: next === inherited ? undefined : next } : s)) })
                }}
                onAsk={() => { const at = selectionPin(); if (at) startAsk([...selected], at) }} onDelete={deleteSelection} />
            )}
          </div>
        )
      })()}

      {/* Empty board. */}
      {board.shapes.length === 0 && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
          <div className="text-center text-muted">
            <div className="text-base font-medium text-text">A blank board</div>
            <div className="mt-1 text-sm">Pick a sticky from the dock, or double-click anywhere to add one.</div>
          </div>
        </div>
      )}

      {menu && (
        <BoardContextMenu x={menu.x} y={menu.y} bounds={{ w: root.current?.clientWidth ?? 9999, h: root.current?.clientHeight ?? 9999 }}
          hasSelection={menu.ids.length > 0} canPaste={!!boardClipboard} anyLocked={board.shapes.some(s => s.locked)}
          onAction={a => runAction(a, menu.ids, menu.at)} onClose={() => { closeMenu(); root.current?.focus({ preventScroll: true }) }} />
      )}

      {/* The tool dock. */}
      <div data-board-ui className="absolute bottom-5 left-1/2 flex -translate-x-1/2 items-center gap-0.5 rounded-2xl border border-border bg-bg-elevated p-1.5 shadow-xl" style={{ zIndex: 9 }} onPointerDown={e => e.stopPropagation()} data-testid="board-dock">
        {TOOLS.map(t => (
          <button key={t.tool} type="button" aria-label={t.label} title={`${t.label} (${t.key})`} aria-pressed={tool === t.tool} data-testid={`tool-${t.tool}`}
            onClick={() => { setTool(t.tool); root.current?.focus() }}
            className={`flex h-9 w-9 items-center justify-center rounded-xl transition-colors ${tool === t.tool ? 'bg-accent text-accent-fg' : 'text-text hover:bg-bg-hover'}`}>
            {t.icon}
          </button>
        ))}
      </div>

      {/* Zoom. */}
      <div data-board-ui className="absolute bottom-5 right-5 flex items-center gap-0.5 rounded-xl border border-border bg-bg-elevated p-1 shadow-lg" style={{ zIndex: 9 }} onPointerDown={e => e.stopPropagation()}>
        <button type="button" aria-label="Zoom out" onClick={() => { const r = root.current!.getBoundingClientRect(); zoomAt(r.width / 2, r.height / 2, 1 / 1.2) }} className="rounded-lg p-1.5 text-text hover:bg-bg-hover"><Minus size={14} /></button>
        <button type="button" aria-label="Reset zoom" onClick={() => setView(v => ({ ...v, zoom: 1 }))} className="min-w-[48px] rounded-lg px-1 py-1 text-xs tabular-nums text-text hover:bg-bg-hover">{Math.round(view.zoom * 100)}%</button>
        <button type="button" aria-label="Zoom in" onClick={() => { const r = root.current!.getBoundingClientRect(); zoomAt(r.width / 2, r.height / 2, 1.2) }} className="rounded-lg p-1.5 text-text hover:bg-bg-hover"><Plus size={14} /></button>
        <button type="button" aria-label="Fit to board" title="Fit to board" onClick={fit} className="rounded-lg p-1.5 text-text hover:bg-bg-hover"><Maximize2 size={14} /></button>
        <span className="mx-0.5 h-5 w-px bg-border" />
        <button type="button" data-testid="board-conductor-mode" aria-pressed={closed}
          aria-label={closed ? 'Conductor: only sections opened to it' : 'Conductor: everywhere except protected sections'}
          title={closed ? 'The conductor can only change sections you opened to it. Click to let it work everywhere except protected sections.' : 'The conductor can change anything except protected sections. Click to let it change only sections you open.'}
          onClick={() => { const b = boardRef.current; commit({ ...b, conductorDefault: closed ? undefined : 'closed' }) }}
          className={`flex items-center gap-1 rounded-lg p-1.5 hover:bg-bg-hover ${closed ? 'text-accent' : 'text-text'}`}>
          {closed ? <ShieldCheck size={14} /> : <Shield size={14} />}
        </button>
        <button type="button" aria-label={overlaysOn ? 'Hide overlays' : 'Show overlays'} title={overlaysOn ? 'Hide the conductor\'s overlays' : 'Show the conductor\'s overlays'}
          aria-pressed={!overlaysOn} data-testid="board-overlay-toggle" onClick={toggleOverlays}
          className={`flex items-center gap-1 rounded-lg p-1.5 hover:bg-bg-hover ${overlaysOn ? 'text-text' : 'text-accent'}`}>
          {overlaysOn ? <Eye size={14} /> : <EyeOff size={14} />}
          {!overlaysOn && placedOverlay.length > 0 && <span className="text-[11px] tabular-nums">{placedOverlay.length}</span>}
        </button>
      </div>
    </div>
  )
}

/** One shape in world space, with its text editor while it is being edited. */
type AccessMark = 'protected' | 'open' | undefined

function SectionView({ shape: s, editing, zoom, mark, onText, onEndEdit }: { shape: Shape; editing: boolean; zoom: number; mark?: AccessMark; onText: (t: string) => void; onEndEdit: () => void }) {
  const sw = swatchStyle(s.color, s.hex)
  const look = shapeLook(s)
  // The label is drawn larger as the board shrinks, from its bottom-left corner, so it stays the same size on screen.
  const k = labelScale(zoom)
  const labelW = Math.min(s.w / k, 260)
  const title: CSSProperties = {
    position: 'absolute', left: 2, bottom: '100%', marginBottom: 6 * k, height: SECTION_TITLE_H - 8, maxWidth: labelW,
    transform: k > 1 ? `scale(${+k.toFixed(3)})` : undefined, transformOrigin: 'left bottom',
    fontSize: 14, fontWeight: 600, lineHeight: `${SECTION_TITLE_H - 8}px`, color: 'var(--text)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
  }
  // A section keeps its soft 2px edge until it is given an outline of its own.
  const custom = s.fill === 'none' || !!s.border || !!s.borderColor || !!s.borderHex
  return (
    <div
      data-shape-id={s.id} data-testid="board-shape" data-kind="section"
      style={{
        position: 'absolute', left: s.x, top: s.y, width: s.w, height: s.h, borderRadius: 14,
        background: s.fill === 'none' ? 'transparent' : `color-mix(in srgb, ${sw.fill} 85%, transparent)`,
        border: custom ? `${look.width}px solid ${look.dashed ? 'transparent' : look.border}` : `2px solid color-mix(in srgb, ${sw.stroke} 40%, transparent)`,
      }}
    >
      {custom && look.dashed && <DashedOutline w={s.w} h={s.h} width={look.width} color={look.border} radius={14} />}
      {s.goal && <WatchRing w={s.w} h={s.h} radius={14} />}
      {editing ? (
        <input
          autoFocus
          aria-label="Section title"
          data-testid="board-shape-editor"
          value={s.text}
          placeholder="Section"
          onChange={e => onText(e.target.value)}
          onBlur={onEndEdit}
          onFocus={e => e.currentTarget.select()}
          onKeyDown={e => { e.stopPropagation(); if (e.key === 'Escape' || e.key === 'Enter') e.currentTarget.blur() }}
          onPointerDown={e => e.stopPropagation()}
          style={{ ...title, width: labelW, border: 'none', outline: 'none', background: 'transparent', font: 'inherit', fontSize: 14, fontWeight: 600, padding: 0 }}
        />
      ) : (
        <div style={{ ...title, display: 'flex', alignItems: 'center', gap: 8, textOverflow: 'clip' }}>
          <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>{s.text || <span style={{ opacity: 0.45 }}>Section</span>}</span>
          {mark && (
            <span data-testid="section-access-chip" data-mark={mark}
              title={mark === 'protected' ? 'Protected: the conductor cannot change this section or what is in it' : 'Open: the conductor can change this section and what is in it'}
              style={{ display: 'inline-flex', flexShrink: 0, alignItems: 'center', gap: 4, padding: '0 8px', height: 20, borderRadius: 999, fontSize: 11, fontWeight: 600, lineHeight: 1,
                color: 'var(--muted)', background: 'color-mix(in srgb, var(--text) 8%, transparent)' }}>
              {mark === 'protected' ? <ShieldCheck size={11} /> : <Shield size={11} />} {mark === 'protected' ? 'Protected' : 'Open'}
            </span>
          )}
          {s.goal && (
            <span data-testid="section-goal-chip" title={`Watching: ${s.goal.text}`}
              style={{ display: 'inline-flex', flexShrink: 0, alignItems: 'center', gap: 4, padding: '0 8px', height: 20, borderRadius: 999, fontSize: 11, fontWeight: 600, lineHeight: 1,
                color: 'var(--accent)', background: 'color-mix(in srgb, var(--accent) 16%, transparent)' }}>
              <Target size={11} /> Watching
            </span>
          )}
        </div>
      )}
    </div>
  )
}

function ShapeView({ shape: s, editing, zoom, links, mark, onText, onEndEdit }: { shape: Shape; editing: boolean; zoom: number; links: BoardLinks; mark?: AccessMark; onText: (t: string) => void; onEndEdit: () => void }) {
  const field = useRef<HTMLTextAreaElement>(null)
  const picker = useMentionPicker({ value: s.text, onText, links, field, zoom })
  if (s.kind === 'session') return <SessionCardView shape={s} session={links.sessions.find(x => x.key === s.session) ?? null} />
  const sw = swatchStyle(s.color, s.hex)
  const look = shapeLook(s)
  // A dashed outline is an SVG stroke over a transparent border of the same width.
  const outline = `${look.width}px solid ${look.dashed ? 'transparent' : look.border}`
  if (s.kind === 'section') return <SectionView shape={s} editing={editing} zoom={zoom} mark={mark} onText={onText} onEndEdit={onEndEdit} />
  const text: CSSProperties = s.kind === 'sticky'
    ? { fontSize: 16, lineHeight: 1.35, color: look.ink, textAlign: 'left', padding: 16 }
    : s.kind === 'text'
      ? { fontSize: 20, lineHeight: 1.3, color: 'var(--text)', textAlign: 'left', padding: '4px 2px', fontWeight: 500 }
      : { fontSize: 15, lineHeight: 1.35, color: look.ink, textAlign: 'center', padding: s.kind === 'diamond' ? '18% 22%' : 14 }
  const box: CSSProperties = { position: 'absolute', left: s.x, top: s.y, width: s.w, height: s.h, display: 'flex' }
  const shell: CSSProperties =
    s.kind === 'sticky' ? (look.solid && !s.border && !s.borderColor && !s.borderHex
      ? { background: sw.paper, borderRadius: 4, boxShadow: '0 1px 2px rgba(0,0,0,.12), 0 8px 20px -6px rgba(0,0,0,.22)' }
      : { background: look.fill, border: outline, borderRadius: 4 })
      : s.kind === 'rect' ? { background: look.fill, border: outline, borderRadius: 8 }
        : s.kind === 'ellipse' ? { background: look.fill, border: outline, borderRadius: '50%' }
          : {}
  const align: CSSProperties = s.kind === 'sticky' || s.kind === 'text'
    ? { alignItems: 'flex-start', justifyContent: 'flex-start' }
    : { alignItems: 'center', justifyContent: 'center' }
  return (
    <div data-shape-id={s.id} data-testid="board-shape" data-kind={s.kind} style={{ ...box, ...shell, ...align }}>
      {look.dashed && s.kind !== 'text' && (
        // A diamond has no CSS border, so its outline starts at the box edge.
        <DashedOutline w={s.w} h={s.h} width={look.width} color={look.border} ellipse={s.kind === 'ellipse'} diamond={s.kind === 'diamond'}
          offset={s.kind === 'diamond' ? 0 : look.width} radius={s.kind === 'sticky' ? 4 : 8} />
      )}
      {s.kind === 'diamond' && (
        // Drawn in the shape's own units so the stroke is as thick as the other
        // shapes' borders and scales with zoom, without stretching on wide diamonds.
        <svg width="100%" height="100%" viewBox={`0 0 ${s.w} ${s.h}`} style={{ position: 'absolute', inset: 0, overflow: 'visible' }} aria-hidden>
          <polygon
            points={`${s.w / 2},${look.width} ${s.w - look.width},${s.h / 2} ${s.w / 2},${s.h - look.width} ${look.width},${s.h / 2}`}
            fill={look.fill} stroke={look.dashed ? 'none' : look.border} strokeWidth={look.width} strokeLinejoin="round"
          />
        </svg>
      )}
      {editing ? (
        <>
          <textarea
            ref={field}
            autoFocus
            aria-label="Shape text"
            data-testid="board-shape-editor"
            value={s.text}
            onChange={e => picker.onChange(e.target.value, e.target.selectionStart, (e.nativeEvent as InputEvent).data)}
            onBlur={() => { picker.close(); onEndEdit() }}
            onFocus={e => e.currentTarget.select()}
            // The "@" picker takes its keys first; Escape with it open only closes it.
            onKeyDown={e => { e.stopPropagation(); if (picker.onKeyDown(e)) return; if (e.key === 'Escape') e.currentTarget.blur() }}
            onPointerDown={e => e.stopPropagation()}
            style={{ ...text, position: 'relative', width: '100%', height: '100%', resize: 'none', border: 'none', outline: 'none', background: 'transparent', font: 'inherit', fontSize: text.fontSize, fontWeight: text.fontWeight, overflow: 'hidden' }}
          />
          {picker.menu}
        </>
      ) : (
        <div style={{ ...text, position: 'relative', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', overflow: 'hidden', maxHeight: '100%' }}>
          {s.text ? <ShapeLinkText text={s.text} links={links} /> : (s.kind === 'text' ? <span style={{ opacity: 0.4 }}>Text</span> : null)}
        </div>
      )}
    </div>
  )
}
