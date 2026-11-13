/**
 * A FigJam-style canvas editor: stickies, shapes, text, sections and connectors
 * on an infinite dot grid.
 *
 * Layers, back to front:
 * - the world: shapes (DOM) and connectors (SVG), moved and scaled by the view;
 * - screen chrome: selection, handles, connect dots and the selection toolbar.
 *   Placed from each shape's box on every render, so they follow drags, pans and
 *   zooms and stay crisp at any zoom;
 * - the tool dock and zoom control.
 *
 * Controlled: the parent owns the canvas; every change, live ones included, comes
 * back through `onChange`. Undo history is kept here, one step per gesture.
 * `readOnly` keeps pan and zoom and drops everything that edits.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { useImeGuard } from '../../hooks/useImeGuard'
import {
  ArrowUpRight, ChevronDown, Circle, Diamond, Frame, Hand, Maximize2, Minus, MousePointer2, Plus, Square, StickyNote, Type,
} from 'lucide-react'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../ui/dropdown-menu'
import { useIsMobile } from '../../hooks/useIsMobile'
import { InstantTip, useInstantTip } from '../InstantTip'
import {
  CONN_INK, DEFAULT_SIZE, alignOf, defaultAlign, defaultFontSize, fontSizeOf, DEFAULT_TEXT_SIZE, MAX_TEXT_SIZE, MIN_TEXT_SIZE, SECTION_TITLE_H, SWATCH_STYLE, boundsOf, labelScale, newId, pointBounds, sectionMembers, shapeAt, wheelPanStep, wheelZoomFactor, zOrdered,
  type Canvas, type Connector, type Pt, type Shape, type ShapeKind, type Swatch, type TextAlign,
} from './canvasModel'
import CanvasContextMenu, { type MenuAction } from './CanvasContextMenu'
import {
  ACTIVE_STICKER, FLOAT_SHADOW, NOTE_DROP, TAPE, TAPE_SHADOW, noteClip, noteFold, LABEL_ORIGIN, SECTION_RIM, STICKER_DROP, STICKER_EDGE, TAG_SHADOW, solidBorder, stickerShadow,
} from './canvasPaint'
import { copyShapes, pasteClip, restack, setLocked, unlockAll, type CanvasClip } from './canvasEditing'
import ConnectorToolbar from './ConnectorToolbar'
import DashedOutline from './DashedOutline'
import { shapeLook, swatchStyle, textInk } from './shapeLook'
import TextToolbar from './TextToolbar'
import ShapeToolbar from './ShapeToolbar'
import {
  SIDES, connectorRoute, movedSegment, pointBox, segmentDrag, sideAnchor, sideNear, type SegmentDrag, type Side,
} from './connectorGeometry'

type Tool = 'select' | 'hand' | 'connector' | ShapeKind
interface View { x: number; y: number; zoom: number }
/** A resize handle: a corner moves two sides, an edge one. */
type Corner = 'nw' | 'ne' | 'sw' | 'se' | 'n' | 'e' | 's' | 'w'
const EDGE_CURSOR: Record<'n' | 'e' | 's' | 'w', string> = { n: 'ns-resize', s: 'ns-resize', e: 'ew-resize', w: 'ew-resize' }

type Drag =
  | { kind: 'pan'; sx: number; sy: number; view0: View }
  | { kind: 'move'; start: Pt; origin: Map<string, Pt>; snapshot: Canvas; moved: boolean }
  | { kind: 'resize'; id: string; corner: Corner; start: Pt; box0: Shape; snapshot: Canvas }
  | { kind: 'marquee'; sx: number; sy: number; cx: number; cy: number; base: Set<string> }
  // A new connector from a shape; `fromSide` is the connect dot it was dragged from.
  | { kind: 'connect'; from: string; fromSide?: Side; at: Pt }
  // One end of the selected connector, dragged to another side or shape.
  | { kind: 'reattach'; id: string; end: 'from' | 'to'; at: Pt }
  // One segment of the selected elbow, slid sideways to reshape its route.
  | { kind: 'segment'; id: string; seg: SegmentDrag; start: Pt; snapshot: Canvas }
  // A press with a shape tool: a click drops a default-sized shape, a drag sizes
  // it. `id` is set once the drag has started and the shape exists.
  | { kind: 'create'; shape: ShapeKind; start: Pt; snapshot: Canvas; id: string | null }

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
const DOT_OFFSET = 18
/** How long the pointer rests on a dock tool before its tooltip shows. */
const TIP_DELAY_MS = 500
/** Screen px between a selected shape and its ring: clear of the sticker's white edge. */
const SEL_GAP = 9

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
/** Connector width in canvas units, so it scales with zoom like FigJam's. */
const CONNECTOR_STROKE = 3
/** Corner radii of a sticky and a rectangle: round enough to read as cut stickers. */
const STICKY_RADIUS = 4
/** A sticky's folded corner and its tape, in canvas units. */
const NOTE_FOLD = 22
const NOTE_TAPE = { w: 56, h: 18 }

/**
 * How far a sticky leans, in degrees: the same for a note every time it is drawn
 * (read from its id), between -3 and 3, so a board of notes looks pinned up by
 * hand. Only the drawing leans; its box, selection and connectors stay square.
 */
export function noteTilt(id: string): number {
  let h = 0
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) | 0
  return Math.round(((((h % 61) + 61) % 61) / 10 - 3) * 10) / 10
}
const RECT_RADIUS = 16

/**
 * What Copy last took. Kept for the tab rather than per canvas, so a copy pastes
 * into another canvas too; it never leaves the page.
 */
let canvasClipboard: CanvasClip | null = null

/** Below this zoom the connect dots are hidden: you are surveying the canvas, not wiring shapes. */
const MIN_CONNECT_ZOOM = 0.5

/** The closest the background dots may sit on screen, in px, before every other one drops out. */
const MIN_DOT_GAP = 16

/**
 * The dots' spacing on screen at *zoom*. Zooming out would pack them tighter and
 * tighter, so as in FigJam each time the gap falls under MIN_DOT_GAP the grid
 * doubles (every other row and column goes) and the gap springs back.
 */
export function dotSpacing(zoom: number): number {
  const gap = GRID * zoom
  const halvings = gap >= MIN_DOT_GAP ? 0 : Math.ceil(Math.log2(MIN_DOT_GAP / gap))
  return gap * 2 ** halvings
}

/** Snap a world coordinate to the snap grid; Alt held during a drag turns it off. */
const snap = (v: number, on: boolean) => (on ? Math.round(v / SNAP) * SNAP : Math.round(v))

const TOOL_KEYS: { tool: Tool; key: string }[] = [
  { tool: 'select', key: 'V' },
  { tool: 'hand', key: 'H' },
  { tool: 'sticky', key: 'S' },
  { tool: 'rect', key: 'R' },
  { tool: 'ellipse', key: 'O' },
  { tool: 'diamond', key: 'D' },
  { tool: 'text', key: 'T' },
  { tool: 'section', key: 'F' },
  { tool: 'connector', key: 'L' },
]
const TOOL_BY_KEY = new Map(TOOL_KEYS.map(t => [t.key.toLowerCase(), t.tool]))

export interface CanvasEditorProps {
  canvas: Canvas
  onChange: (next: Canvas) => void
  /** Pan and zoom only: no tools, no selection, no edits. */
  readOnly?: boolean
  /**
   * Changes when the canvas is replaced from outside (another tab, an agent, a
   * reload) rather than by an edit here. The undo history goes with it: undoing
   * would write back a document that no longer exists, over the one that does.
   */
  epoch?: number
}

export default function CanvasEditor({ canvas, onChange, readOnly = false, epoch = 0 }: CanvasEditorProps) {
  const { t } = useTranslation()
  const root = useRef<HTMLDivElement>(null)
  const [view, setView] = useState<View>({ x: 120, y: 100, zoom: 1 })
  const [tool, setTool] = useState<Tool>('select')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [selectedConn, setSelectedConn] = useState<string | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [hovered, setHovered] = useState<string | null>(null)
  const [drag, setDrag] = useState<Drag | null>(null)
  /** The open right-click menu: where (canvas px and canvas units) and which shapes it acts on. */
  const [menu, setMenu] = useState<{ x: number; y: number; at: Pt; ids: string[] } | null>(null)
  const closeMenu = useCallback(() => setMenu(null), [])
  const [spaceDown, setSpaceDown] = useState(false)

  const tools: { tool: Tool; icon: ReactNode; label: string; key: string }[] = [
    { tool: 'select', icon: <MousePointer2 size={18} />, label: t('components.canvas.tool_select'), key: 'V' },
    { tool: 'hand', icon: <Hand size={18} />, label: t('components.canvas.tool_hand'), key: 'H' },
    { tool: 'sticky', icon: <StickyNote size={18} />, label: t('components.canvas.tool_sticky'), key: 'S' },
    { tool: 'rect', icon: <Square size={18} />, label: t('components.canvas.tool_rect'), key: 'R' },
    { tool: 'ellipse', icon: <Circle size={18} />, label: t('components.canvas.tool_ellipse'), key: 'O' },
    { tool: 'diamond', icon: <Diamond size={18} />, label: t('components.canvas.tool_diamond'), key: 'D' },
    { tool: 'text', icon: <Type size={18} />, label: t('components.canvas.tool_text'), key: 'T' },
    { tool: 'section', icon: <Frame size={18} />, label: t('components.canvas.tool_section'), key: 'F' },
    { tool: 'connector', icon: <ArrowUpRight size={18} />, label: t('components.canvas.tool_connector'), key: 'L' },
  ]

  // The latest values for handlers that outlive a render (window listeners).
  const canvasRef = useRef(canvas)
  canvasRef.current = canvas
  const viewRef = useRef(view)
  viewRef.current = view
  const undo = useRef<{ before: Canvas; after: Canvas }[]>([])
  const redo = useRef<{ before: Canvas; after: Canvas }[]>([])
  /** Further values replace the top colour entry unless a fresh gesture starts. */
  const liveColour = useRef(false)

  const commit = useCallback((next: Canvas, before: Canvas = canvasRef.current) => {
    liveColour.current = false
    undo.current = [...undo.current, { before, after: next }].slice(-HISTORY_CAP)
    redo.current = []
    onChange(next)
  }, [onChange])

  // Going read-only (a past version opened, say) drops whatever was mid-edit.
  useEffect(() => {
    if (!readOnly) return
    setSelected(new Set())
    setSelectedConn(null)
    setEditing(null)
    setMenu(null)
    setTool('select')
  }, [readOnly])

  // A locked shape is never picked, however it became locked: an undo can restore
  // a lock on a shape that is selected, and an outside change can lock or remove
  // one. Every action on the selection (resize, delete, edit, restyle) reads this
  // set, so pruning it here keeps all of them off locked shapes.
  useEffect(() => {
    const usable = new Set(canvas.shapes.filter(s => !s.locked).map(s => s.id))
    setSelected(cur => ([...cur].every(id => usable.has(id)) ? cur : new Set([...cur].filter(id => usable.has(id)))))
  }, [canvas.shapes])

  const screenBox = (s: { x: number; y: number; w: number; h: number }) => ({
    x: s.x * view.zoom + view.x, y: s.y * view.zoom + view.y, w: s.w * view.zoom, h: s.h * view.zoom,
  })
  const toScreen = (p: Pt): Pt => ({ x: p.x * view.zoom + view.x, y: p.y * view.zoom + view.y })
  const toWorld = useCallback((clientX: number, clientY: number): Pt => {
    const r = root.current?.getBoundingClientRect()
    const v = viewRef.current
    return { x: (clientX - (r?.left ?? 0) - v.x) / v.zoom, y: (clientY - (r?.top ?? 0) - v.y) / v.zoom }
  }, [])

  const byId = useMemo(() => new Map(canvas.shapes.map(s => [s.id, s])), [canvas.shapes])
  const selShapes = canvas.shapes.filter(s => selected.has(s.id))
  const selBox = boundsOf(selShapes)
  const hoveredShape = hovered ? byId.get(hovered) : undefined
  // Connect dots: on the hovered shape or the one selected, never on a section, and not
  // when zoomed out so far that you are looking at the overall picture, not joining things.
  const dotCandidate = hoveredShape ?? (selShapes.length === 1 && !selShapes[0].locked ? selShapes[0] : undefined)
  const dotShape = !readOnly && dotCandidate && dotCandidate.kind !== 'section' && view.zoom >= MIN_CONNECT_ZOOM ? dotCandidate : undefined

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
      if ((e.target as HTMLElement).closest('[data-canvas-ui]')) return
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
    const b = boundsOf(canvas.shapes)
    const r = root.current?.getBoundingClientRect()
    if (!b || !r) { setView({ x: 120, y: 100, zoom: 1 }); return }
    const zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Math.min((r.width - 160) / b.w, (r.height - 200) / b.h, 1.4)))
    setView({ zoom, x: (r.width - b.w * zoom) / 2 - b.x * zoom, y: (r.height - b.h * zoom) / 2 - b.y * zoom - 30 })
  }

  /** Zoom by *factor* about the middle of the canvas, as the zoom menu's steps do. */
  const zoomBy = (factor: number) => {
    const r = root.current?.getBoundingClientRect()
    if (r) zoomAt(r.width / 2, r.height / 2, factor)
  }

  // A phone's canvas is narrower than a selection toolbar and its panels, so there
  // the toolbar sits across the top of the canvas (under zoom) instead of over the
  // selection, and its panels open beneath it, as wide as the canvas.
  const narrow = useIsMobile()
  const toolbarAt = (x: number, y: number): CSSProperties => (narrow
    ? { left: 12, right: 12, top: 56, display: 'flex', justifyContent: 'center', zIndex: 8 }
    : { left: x, top: y, transform: 'translate(-50%, -100%)', zIndex: 8 })

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
    commit({ ...canvasRef.current, shapes: [...canvasRef.current.shapes, shape] })
    finishCreate(shape.id)
  }
  // The drag listeners live across renders; this keeps them on the current `create`.
  const createRef = useRef(create)
  createRef.current = create

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
    onChange({ ...canvasRef.current, shapes: canvasRef.current.shapes.map(s => (s.id === id ? { ...s, text } : s)) })
  }

  const editStart = useRef<Canvas | null>(null)
  useEffect(() => { if (editing) editStart.current = canvasRef.current }, [editing])
  /**
   * Ends the text edit and returns the document it leaves. A handler that goes on
   * to read the canvas reads this, since the render that carries it comes later.
   */
  const endEdit = (): Canvas => {
    const id = editing
    setEditing(null)
    const b = canvasRef.current
    if (!id) return b
    const shape = b.shapes.find(s => s.id === id)
    // An empty text box is nothing: it goes, the way it does in FigJam.
    const next = shape && shape.kind === 'text' && !shape.text.trim() ? removeShapes(b, new Set([id])) : b
    const top = undo.current[undo.current.length - 1]
    if (next !== b && top && JSON.stringify(top.before) === JSON.stringify(next)) {
      // A new text box left empty: the canvas is back where it was before the box
      // was made, so the step that made it goes rather than a second step being
      // added, and one undo never brings back an empty placeholder box.
      undo.current.pop()
      onChange(next)
    } else if (editStart.current && JSON.stringify(editStart.current) !== JSON.stringify(next)) commit(next, editStart.current)
    else if (next !== b) onChange(next)
    editStart.current = null
    return next
  }

  const removeShapes = (b: Canvas, ids: Set<string>): Canvas => ({
    ...b,
    shapes: b.shapes.filter(s => !ids.has(s.id)),
    connectors: b.connectors.filter(c => !ids.has(c.from) && !ids.has(c.to)),
  })

  const deleteSelection = () => {
    const b = canvasRef.current
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
      const drop = dropOn(canvas.shapes, drag.at, view.zoom, drag.end === 'from' ? c.to : c.from)
      const box = drop?.shape ?? pointBox(drag.at)
      return drag.end === 'from'
        ? connectorRoute(box, to, { ...c, fromSide: drop?.side, route: undefined })
        : connectorRoute(from, box, { ...c, toSide: drop?.side, route: undefined })
    }
    return connectorRoute(from, to, c)
  }

  const updateConn = (id: string, patch: Partial<Connector>) => {
    const b = canvasRef.current
    commit({ ...b, connectors: b.connectors.map(c => (c.id === id ? { ...c, ...patch } : c)) })
  }

  const duplicateSelection = () => {
    const b = canvasRef.current
    const copies = b.shapes.filter(s => selected.has(s.id)).map(s => ({ ...s, id: newId(), x: s.x + 24, y: s.y + 24 }))
    if (!copies.length) return
    commit({ ...b, shapes: [...b.shapes, ...copies] })
    setSelected(new Set(copies.map(c => c.id)))
  }

  // Picking a swatch swaps a custom colour out.
  const recolor = (color: Swatch) => restyle({ color, hex: undefined })

  /** A change to every selected shape's look, as one undo step. */
  const restyle = (patch: Partial<Pick<Shape, 'color' | 'fill' | 'weight' | 'border' | 'borderColor' | 'hex' | 'borderHex' | 'ink' | 'inkHex' | 'bold' | 'strike' | 'align'>>) => {
    const b = canvasRef.current
    commit({ ...b, shapes: b.shapes.map(s => (selected.has(s.id) ? { ...s, ...patch } : s)) })
  }

  /** Live colours replace one history entry, retaining the gesture's first state. */
  const restyleLive = (patch: Partial<Pick<Shape, 'hex' | 'borderHex' | 'inkHex'>>, final: boolean, fresh: boolean) => {
    const b = canvasRef.current
    const next = { ...b, shapes: b.shapes.map(s => (selected.has(s.id) ? { ...s, ...patch } : s)) }
    const top = liveColour.current && !fresh ? undo.current.pop() : undefined
    const before = top?.before ?? b
    undo.current = [...undo.current, { before, after: next }].slice(-HISTORY_CAP)
    redo.current = []
    liveColour.current = !final
    if (final && JSON.stringify(before) === JSON.stringify(next)) undo.current.pop()
    onChange(next)
  }

  // A canvas replaced from outside takes the undo history with it, and every
  // gesture still open: each holds a copy of the old document as its starting
  // point (a text edit, a colour being dragged, a move or resize), and finishing
  // it would record that copy as the step to undo back to.
  useEffect(() => {
    undo.current = []
    redo.current = []
    editStart.current = null
    liveColour.current = false
    setEditing(null)
    setDrag(null)
  }, [epoch])

  /**
   * A new text size for every selected shape that holds text. A text box grows to
   * fit its lines at the new size and never shrinks below what was drawn, so
   * nothing is cut off; a shape keeps its size and its text reflows inside it.
   */
  const resizeText = (size: number) => {
    const b = canvasRef.current
    const px = Math.round(Math.min(MAX_TEXT_SIZE, Math.max(MIN_TEXT_SIZE, size)))
    commit({
      ...b,
      shapes: b.shapes.map(s => {
        if (!selected.has(s.id) || s.kind === 'section') return s
        const { fontSize: _was, ...plain } = s
        void _was
        if (s.kind !== 'text') return { ...plain, ...fontSizeOf(s.kind, px) }
        const lines = Math.max(1, s.text.split('\n').length)
        const h = Math.max(s.h * px / (s.fontSize ?? defaultFontSize(s.kind)), Math.ceil(lines * px * 1.3 + 8))
        const w = Math.max(s.w, Math.ceil(px * 4))
        const { fontSize: _old, ...rest } = s
        void _old
        return { ...rest, ...(px !== DEFAULT_TEXT_SIZE ? { fontSize: px } : {}), w: Math.round(w), h: Math.round(h) }
      }),
    })
  }

  /** One alignment for every selected shape, stored only where it differs from that kind's default. */
  const setAlign = (a: TextAlign) => {
    const b = canvasRef.current
    commit({
      ...b,
      shapes: b.shapes.map(s => {
        if (!selected.has(s.id) || s.kind === 'section') return s
        const { align: _old, ...rest } = s
        void _old
        return { ...rest, ...alignOf(s.kind, a) }
      }),
    })
  }

  /** Turn every selected shape into another kind, keeping its place, text and colour; a sticky is square. */
  const changeKind = (kind: ShapeKind) => {
    const b = canvasRef.current
    commit({
      ...b,
      shapes: b.shapes.map(s => {
        if (!selected.has(s.id) || s.kind === kind) return s
        return kind === 'sticky' ? { ...s, kind, w: Math.max(s.w, s.h), h: Math.max(s.w, s.h) } : { ...s, kind }
      }),
    })
  }

  const doUndo = () => {
    liveColour.current = false
    const prev = undo.current.pop()
    if (!prev) return
    redo.current.push({ before: prev.before, after: canvasRef.current })
    onChange(prev.before)
  }
  const doRedo = () => {
    liveColour.current = false
    const next = redo.current.pop()
    if (!next) return
    undo.current.push({ before: canvasRef.current, after: next.after })
    onChange(next.after)
  }

  // ---- pointer gestures ------------------------------------------------------------

  const onPointerDown = (e: React.PointerEvent) => {
    const target = e.target as HTMLElement
    if (target.closest('[data-canvas-ui]') || (editing && target.closest('textarea'))) return
    // The right button opens the menu (onContextMenu); it never starts a drag.
    if (e.button === 2) return
    // Ending an edit can remove an empty text box, so the press reads the
    // document that leaves rather than the one this render was given.
    const doc = editing ? endEdit() : canvas
    root.current?.focus({ preventScroll: true })
    const world = toWorld(e.clientX, e.clientY)
    const panning = readOnly || tool === 'hand' || spaceDown || e.button === 1
    if (panning) { setDrag({ kind: 'pan', sx: e.clientX, sy: e.clientY, view0: viewRef.current }); return }

    const handle = target.closest('[data-handle]') as HTMLElement | null
    if (handle) {
      const id = handle.dataset.shape!
      const box0 = doc.shapes.find(s => s.id === id)
      if (box0) setDrag({ kind: 'resize', id, corner: handle.dataset.handle as Corner, start: world, box0, snapshot: doc })
      return
    }
    const dot = target.closest('[data-connect]') as HTMLElement | null
    if (dot) { setDrag({ kind: 'connect', from: dot.dataset.connect!, fromSide: dot.dataset.side as Side, at: world }); return }
    const connEnd = target.closest('[data-conn-end]') as HTMLElement | null
    if (connEnd && selectedConn) { setDrag({ kind: 'reattach', id: selectedConn, end: connEnd.dataset.connEnd as 'from' | 'to', at: world }); return }
    const segEl = target.closest('[data-conn-seg]') as HTMLElement | null
    const segConn = segEl && doc.connectors.find(c => c.id === selectedConn)
    const segRoute = segConn && routeOf(segConn)
    if (segEl && segConn && segRoute) {
      const seg = segmentDrag(segRoute.points, Number(segEl.dataset.connSeg), ...segRoute.sides)
      setDrag({ kind: 'segment', id: segConn.id, seg, start: world, snapshot: doc })
      return
    }
    const conn = target.closest('[data-conn-id]') as HTMLElement | null
    if (conn && tool === 'select') { setSelected(new Set()); setSelectedConn(conn.dataset.connId!); return }

    if (tool !== 'select' && tool !== 'connector') {
      // The press creates a shape and focuses its text editor. Left to its
      // default, the same press then moves focus to the canvas, which ends the
      // edit at once (and an empty text box is deleted when its edit ends).
      e.preventDefault()
      setDrag({ kind: 'create', shape: tool, start: world, snapshot: doc, id: null })
      return
    }
    const hit = shapeAt(doc.shapes, world, { zoom: view.zoom })
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
      for (const s of doc.shapes) if (s.kind === 'section' && next.has(s.id)) sectionMembers(doc.shapes, s).forEach(m => carried.add(m.id))
      const origin = new Map(doc.shapes.filter(s => carried.has(s.id)).map(s => [s.id, { x: s.x, y: s.y }]))
      setDrag({ kind: 'move', start: world, origin, snapshot: doc, moved: false })
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
      const b = canvasRef.current
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
      const b = canvasRef.current
      if (drag.kind === 'move' && drag.moved) commit(b, drag.snapshot)
      else if (drag.kind === 'resize' && JSON.stringify(b) !== JSON.stringify(drag.snapshot)) commit(b, drag.snapshot)
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
    if (readOnly || drag || tool !== 'select') { if (hovered) setHovered(null); return }
    const target = e.target as HTMLElement
    if (target.closest('[data-canvas-ui]')) return
    // The connect dots sit outside the shape: reaching for one must not unhover it.
    if (target.closest('[data-connect]')) return
    const world = toWorld(e.clientX, e.clientY)
    const hit = shapeAt(canvas.shapes, world, { zoom: view.zoom })
    if (!hit && hovered) {
      const s = byId.get(hovered)
      const pad = HOVER_MARGIN / view.zoom
      if (s && world.x >= s.x - pad && world.x <= s.x + s.w + pad && world.y >= s.y - pad && world.y <= s.y + s.h + pad) return
    }
    if ((hit?.id ?? null) !== hovered) setHovered(hit?.id ?? null)
  }

  const onDoubleClick = (e: React.MouseEvent) => {
    if (readOnly || (e.target as HTMLElement).closest('[data-canvas-ui]')) return
    const world = toWorld(e.clientX, e.clientY)
    const hit = shapeAt(canvas.shapes, world, { zoom: view.zoom })
    if (hit) { setSelected(new Set([hit.id])); setEditing(hit.id) } else create('sticky', world)
  }

  // ---- keys --------------------------------------------------------------------------

  const onKeyDown = (e: React.KeyboardEvent) => {
    const tg = e.target as HTMLElement
    if (tg.tagName === 'TEXTAREA' || tg.tagName === 'INPUT') return
    // A key on one of the canvas's own controls (the dock, zoom, a toolbar or
    // menu) belongs to that control: Space and Enter press it, and no single-key
    // shortcut runs. Chords (undo, copy) still reach the canvas, since a click on
    // a toolbar button leaves focus there.
    if (tg !== e.currentTarget && tg.closest('[data-canvas-ui]') && !(e.metaKey || e.ctrlKey)) return
    if (e.key === ' ') { setSpaceDown(true); e.preventDefault(); return }
    if (readOnly) return
    const mod = e.metaKey || e.ctrlKey
    if (mod && e.key.toLowerCase() === 'z') { e.preventDefault(); if (e.shiftKey) doRedo(); else doUndo(); return }
    if (mod && e.key.toLowerCase() === 'y') { e.preventDefault(); doRedo(); return }
    if (mod && e.key.toLowerCase() === 'd') { e.preventDefault(); duplicateSelection(); return }
    if (mod && e.key.toLowerCase() === 'a') { e.preventDefault(); setSelected(new Set(canvas.shapes.filter(s => !s.locked).map(s => s.id))); return }
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
    if (e.key === 'Escape') { setSelected(new Set()); setSelectedConn(null); setTool('select'); return }
    if (e.key === 'Enter' && selected.size === 1) { e.preventDefault(); setEditing([...selected][0]); return }
    if (!mod && !e.altKey) {
      const next = TOOL_BY_KEY.get(e.key.toLowerCase())
      if (next) setTool(next)
    }
  }

  // ---- the right-click menu ---------------------------------------------------------

  /** One menu action on *ids*; Paste lands at *at*, or beside what was copied. */
  const runAction = (a: MenuAction, ids: string[], at?: Pt) => {
    const b = canvasRef.current
    // Only Lock reaches a locked shape; every other action skips it.
    const locked = new Set(b.shapes.filter(s => s.locked).map(s => s.id))
    const set = new Set(a === 'lock' ? ids : ids.filter(id => !locked.has(id)))
    if (a === 'copy') { canvasClipboard = copyShapes(b, set) ?? canvasClipboard; return }
    if (a === 'paste') {
      if (!canvasClipboard) return
      const pasted = pasteClip(b, canvasClipboard, at)
      commit(pasted.canvas)
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
    if ((e.target as HTMLElement).closest('[data-canvas-ui]')) return
    e.preventDefault()
    if (readOnly) return
    const doc = editing ? endEdit() : canvas
    const r = root.current!.getBoundingClientRect()
    const at = toWorld(e.clientX, e.clientY)
    // A locked shape still takes a right-click, so it can be unlocked.
    const hit = shapeAt(doc.shapes, at, { locked: true, zoom: view.zoom })
    let ids: string[] = []
    if (hit && selected.has(hit.id)) ids = [...selected]
    else if (hit) { ids = [hit.id]; setSelected(hit.locked ? new Set() : new Set([hit.id])) }
    else setSelected(new Set())
    setSelectedConn(null)
    setMenu({ x: e.clientX - r.left, y: e.clientY - r.top, at, ids })
  }

  const onKeyUp = (e: React.KeyboardEvent) => { if (e.key === ' ') setSpaceDown(false) }

  const cursor = drag?.kind === 'pan' ? 'grabbing' : readOnly || tool === 'hand' || spaceDown ? 'grab' : tool === 'select' ? 'default' : 'crosshair'
  const showChrome = !readOnly && (!drag || drag.kind === 'marquee')
  // The selection ring and handles stay up while a shape is resized: a text box
  // has no fill or edge of its own, so without them there is nothing to see.
  const showFrame = showChrome || (!readOnly && drag?.kind === 'resize')

  return (
    // The canvas is one keyboard-driven surface (tools, shortcuts, arrow-free
    // pointer gestures) announced as an application, so it takes focus and keys
    // itself; every chrome control inside it is a real button.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/no-noninteractive-tabindex -- role="application" is the canvas's own keyboard surface; its controls are real buttons
    <div ref={root} role="application" tabIndex={0}
      aria-label={readOnly ? t('components.canvas.label_readonly') : t('components.canvas.label')}
      className="relative h-full w-full overflow-hidden select-none"
      data-testid="canvas-editor"
      onPointerDown={onPointerDown}
      onPointerMove={onHover}
      onDoubleClick={onDoubleClick}
      onContextMenu={onContextMenu}
      onKeyDown={onKeyDown}
      onKeyUp={onKeyUp}
      onBlur={e => { if (!root.current?.contains(e.relatedTarget as Node)) setSpaceDown(false) }}
      style={{
        cursor,
        // Every finger gesture on the board is the canvas's own (move, draw, pan),
        // so the browser must not take a drag as a page scroll.
        touchAction: 'none',
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
        {zOrdered(canvas.shapes).map(s => (
          <ShapeView key={s.id} shape={s} editing={editing === s.id} zoom={view.zoom} onText={text => setText(s.id, text)} onEndEdit={endEdit} />
        ))}
        <svg style={{ position: 'absolute', left: 0, top: 0, width: 1, height: 1, overflow: 'visible' }} aria-hidden>
          <defs>
            <marker id="canvas-arrow" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse" overflow="visible">
              {/* An open chevron, as on FigJam connectors; sized in stroke widths. */}
              <path d="M4,0.5 L8.5,5 L4,9.5" fill="none" stroke="context-stroke" strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round" />
            </marker>
          </defs>
          {canvas.connectors.map(c => {
            const route = routeOf(c)
            if (!route) return null
            const on = selectedConn === c.id
            const arrows = c.arrows ?? 'end'
            return (
              <g key={c.id} data-conn-id={c.id} data-testid="canvas-connector" data-line={c.line ?? 'elbow'}>
                <path d={route.d} fill="none" stroke="transparent" strokeWidth={14 / view.zoom} style={{ pointerEvents: readOnly ? 'none' : 'stroke', cursor: 'pointer' }} />
                {on && <path d={route.d} fill="none" stroke="var(--accent)" strokeOpacity={0.25} strokeWidth={CONNECTOR_STROKE + 6} strokeLinecap="round" strokeLinejoin="round" style={{ pointerEvents: 'none' }} />}
                <path d={route.d} fill="none" stroke={connColor(c)} strokeWidth={CONNECTOR_STROKE} strokeLinecap="round" strokeLinejoin="round"
                  strokeDasharray={c.dashed ? `${CONNECTOR_STROKE * 3} ${CONNECTOR_STROKE * 2.5}` : undefined}
                  markerEnd={arrows !== 'none' ? 'url(#canvas-arrow)' : undefined} markerStart={arrows === 'both' ? 'url(#canvas-arrow)' : undefined}
                  style={{ pointerEvents: 'none' }} />
              </g>
            )
          })}
          {drag?.kind === 'connect' && byId.get(drag.from) && (() => {
            const drop = dropOn(canvas.shapes, drag.at, view.zoom, drag.from)
            const route = connectorRoute(byId.get(drag.from)!, drop?.shape ?? pointBox(drag.at), { fromSide: drag.fromSide, toSide: drop?.side })
            return <path d={route.d} fill="none" stroke="var(--accent)" strokeWidth={CONNECTOR_STROKE} strokeDasharray="8 6" strokeLinecap="round" strokeLinejoin="round" markerEnd="url(#canvas-arrow)" />
          })()}
        </svg>
      </div>

      {/* Selection outline and resize handles. */}
      {showFrame && selShapes.map(s => {
        const r = screenBox(s)
        return (
          // A ring just outside the sticker's white edge, so the edge stays visible.
          <div key={`sel-${s.id}`} className="pointer-events-none absolute" style={{ left: r.x - SEL_GAP, top: r.y - SEL_GAP, width: r.w + SEL_GAP * 2, height: r.h + SEL_GAP * 2, border: '3px solid var(--accent)', borderRadius: s.kind === 'ellipse' ? '50%' : s.kind === 'section' ? 18 : s.kind === 'rect' ? 22 : 15 }} />
        )
      })}
      {/* Edges of the selected shape: hover one and drag to move just that side.
          Drawn before the corner handles, so a corner still wins where they meet. */}
      {showFrame && selShapes.length === 1 && !editing && !selShapes[0].locked && (['n', 'e', 's', 'w'] as const).map(c => {
        const r = screenBox(selShapes[0])
        const th = 10
        const pos = c === 'n' ? { left: r.x, top: r.y - th / 2, width: r.w, height: th }
          : c === 's' ? { left: r.x, top: r.y + r.h - th / 2, width: r.w, height: th }
            : c === 'w' ? { left: r.x - th / 2, top: r.y, width: th, height: r.h }
              : { left: r.x + r.w - th / 2, top: r.y, width: th, height: r.h }
        return <div key={c} data-handle={c} data-shape={selShapes[0].id} data-testid={`handle-${c}`} className="absolute" style={{ ...pos, cursor: EDGE_CURSOR[c] }} />
      })}
      {showFrame && selShapes.length === 1 && !editing && (['nw', 'ne', 'sw', 'se'] as Corner[]).map(c => {
        const r = screenBox(selShapes[0])
        // Round purple handles with a white edge, centred on the selection ring's corners.
        const left = (c.endsWith('w') ? r.x - SEL_GAP : r.x + r.w + SEL_GAP) - 8
        const top = (c.startsWith('n') ? r.y - SEL_GAP : r.y + r.h + SEL_GAP) - 8
        return (
          <div key={c} data-handle={c} data-shape={selShapes[0].id} data-testid={`handle-${c}`} className="absolute"
            style={{ left, top, width: 16, height: 16, borderRadius: '50%', background: 'var(--accent)', border: `3px solid ${STICKER_EDGE}`, boxShadow: '0 1px 3px rgba(0,0,0,.3)', cursor: c === 'nw' || c === 'se' ? 'nwse-resize' : 'nesw-resize' }} />
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
        const c = drag.kind === 'reattach' ? canvas.connectors.find(k => k.id === drag.id) : null
        const not = drag.kind === 'connect' ? drag.from : c ? (drag.end === 'from' ? c.to : c.from) : undefined
        const drop = dropOn(canvas.shapes, drag.at, view.zoom, not)
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
      {selectedConn && !drag && !readOnly && (() => {
        const c = canvas.connectors.find(k => k.id === selectedConn)
        const route = c && routeOf(c)
        if (!c || !route) return null
        const ends: ['from' | 'to', Pt][] = [['from', toScreen(route.a)], ['to', toScreen(route.b)]]
        const pts = route.points.map(toScreen)
        const span = pointBounds(pts) ?? { x: 0, y: 0, w: 0, h: 0 }
        const top = span.y
        const cx = span.x + span.w / 2
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
            <div data-canvas-ui data-testid="canvas-toolbar-place" className="absolute" style={toolbarAt(cx, top - 16)} onPointerDown={e => e.stopPropagation()}>
              <ConnectorToolbar conn={c} onChange={patch => updateConn(c.id, patch)}
                onDelete={() => { commit({ ...canvasRef.current, connectors: canvasRef.current.connectors.filter(k => k.id !== c.id) }); setSelectedConn(null) }} />
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

      {/* The selection toolbar: kind, colours, outline, delete. */}
      {showChrome && selBox && !editing && (() => {
        const r = screenBox(selBox)
        return (
          <div data-canvas-ui data-testid="canvas-toolbar-place" className="absolute" style={toolbarAt(r.x + r.w / 2, r.y - 14)} onPointerDown={e => e.stopPropagation()}>
            {selShapes.every(s => s.kind === 'text') ? (
              <TextToolbar shapes={selShapes}
                onInk={ink => restyle({ ink, inkHex: undefined })} onInkHex={(inkHex, final, fresh) => restyleLive({ inkHex }, final, fresh)}
                onSize={resizeText}
                onBold={bold => restyle({ bold: bold || undefined })} onStrike={strike => restyle({ strike: strike || undefined })}
                onAlign={setAlign} onDelete={deleteSelection} />
            ) : <ShapeToolbar shapes={selShapes} onColor={recolor} onFill={fill => restyle({ fill: fill === 'solid' ? undefined : fill })}
              onKind={changeKind} onWeight={weight => restyle({ weight: weight === 'medium' ? undefined : weight })}
              onBorder={border => restyle({ border: border === 'solid' ? undefined : border })} onBorderColor={borderColor => restyle({ borderColor, borderHex: undefined })}
              onHex={(hex, final, fresh) => restyleLive({ hex }, final, fresh)} onBorderHex={(borderHex, final, fresh) => restyleLive({ borderHex }, final, fresh)}
              onAlign={setAlign} onSize={resizeText} onDelete={deleteSelection} />}
          </div>
        )
      })()}

      {/* Empty canvas. */}
      {canvas.shapes.length === 0 && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
          <div className="text-center text-muted">
            <div className="text-base font-medium text-text">{t('components.canvas.empty_title')}</div>
            {!readOnly && <div className="mt-1 text-sm">{t('components.canvas.empty_hint')}</div>}
          </div>
        </div>
      )}

      {menu && (
        <CanvasContextMenu x={menu.x} y={menu.y} bounds={{ w: root.current?.clientWidth ?? 9999, h: root.current?.clientHeight ?? 9999 }}
          hasSelection={menu.ids.length > 0} canEdit={menu.ids.some(id => !canvas.shapes.find(s => s.id === id)?.locked)} canPaste={!!canvasClipboard} anyLocked={canvas.shapes.some(s => s.locked)}
          onAction={a => runAction(a, menu.ids, menu.at)} onClose={() => { closeMenu(); root.current?.focus({ preventScroll: true }) }} />
      )}

      {/* The dock scrolls within the canvas, including a narrow desktop split pane. */}
      {!readOnly && (
        <div data-canvas-ui role="toolbar" aria-label={t('components.canvas.tools')} className="absolute bottom-3 left-1/2 flex max-w-[calc(100%-1.5rem)] -translate-x-1/2 items-center gap-1 overflow-x-auto rounded-2xl bg-bg-elevated p-2 text-text md:bottom-5" style={{ zIndex: 9, boxShadow: FLOAT_SHADOW }} onPointerDown={e => e.stopPropagation()} data-testid="canvas-dock">
          {tools.map(tl => (
            <DockTool key={tl.tool} tool={tl.tool} icon={tl.icon} label={tl.label} shortcut={tl.key} active={tool === tl.tool}
              onPick={() => { setTool(tl.tool); root.current?.focus() }} />
          ))}
        </div>
      )}

      {/* Zoom stays top right, off the dock's row at every canvas width. */}
      <div data-canvas-ui data-testid="canvas-zoom" className="absolute right-3 top-3 flex items-center rounded-2xl bg-bg-elevated p-1.5 text-text" style={{ zIndex: 9, boxShadow: FLOAT_SHADOW }} onPointerDown={e => e.stopPropagation()}>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button type="button" aria-label={t('components.canvas.zoom_menu')} title={t('components.canvas.zoom_menu')} data-testid="canvas-zoom-menu"
              className="flex min-w-[56px] items-center justify-center gap-0.5 rounded-lg px-1.5 py-1 text-[12px] tabular-nums text-text hover:bg-bg-hover">
              {t('components.canvas.zoom_percent', { percent: Math.round(view.zoom * 100) })}
              <ChevronDown size={12} className="text-muted" aria-hidden />
            </button>
          </DropdownMenuTrigger>
          {/* Marked as canvas chrome: its keys belong to the menu, never to the canvas's shortcuts. */}
          <DropdownMenuContent align="end" data-canvas-ui>
            <DropdownMenuItem onSelect={() => zoomBy(1.2)}><Plus size={13} aria-hidden />{t('components.canvas.zoom_in')}</DropdownMenuItem>
            <DropdownMenuItem onSelect={() => zoomBy(1 / 1.2)}><Minus size={13} aria-hidden />{t('components.canvas.zoom_out')}</DropdownMenuItem>
            <DropdownMenuItem onSelect={() => setView(v => ({ ...v, zoom: 1 }))}>{t('components.canvas.reset_zoom')}</DropdownMenuItem>
            <DropdownMenuItem onSelect={fit}><Maximize2 size={13} aria-hidden />{t('components.canvas.fit')}</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  )
}

/** A dock choice with its hint portalled outside the scrolling strip. */
function DockTool({ tool, icon, label, shortcut, active, onPick }: {
  tool: Tool; icon: ReactNode; label: string; shortcut: string; active: boolean; onPick: () => void
}) {
  const { tip, tipHandlers, tipId } = useInstantTip({ openDelayMs: TIP_DELAY_MS })
  return (
    <>
      <button type="button" aria-label={label} aria-keyshortcuts={shortcut} aria-pressed={active} data-testid={`tool-${tool}`}
        {...tipHandlers} onClick={onPick}
        style={active ? { boxShadow: ACTIVE_STICKER } : undefined}
        className={`relative flex h-9 w-9 shrink-0 items-center justify-center rounded-xl transition-colors ${active ? 'bg-accent text-accent-fg' : 'text-text hover:bg-bg-hover'}`}>
        {icon}
      </button>
      <InstantTip tip={tip} tipId={tipId} arrow className="whitespace-nowrap">
        <span data-testid="canvas-tool-tip" className="flex items-center gap-2">{label}<span className="opacity-60">{shortcut}</span></span>
      </InstantTip>
    </>
  )
}

function SectionView({ shape: s, editing, zoom, onText, onEndEdit }: { shape: Shape; editing: boolean; zoom: number; onText: (text: string) => void; onEndEdit: () => void }) {
  const { t } = useTranslation()
  const ime = useImeGuard()
  const sw = swatchStyle(s.color, s.hex)
  const look = shapeLook(s)
  // The label is drawn larger as the canvas shrinks, from its bottom-left corner, so it stays the same size on screen.
  const k = labelScale(zoom)
  const labelW = Math.min(s.w / k, 260)
  const title: CSSProperties = {
    position: 'absolute', left: 2, bottom: '100%', marginBottom: 6 * k, height: SECTION_TITLE_H - 8, maxWidth: labelW,
    transform: k > 1 ? `scale(${+k.toFixed(3)})` : undefined, transformOrigin: LABEL_ORIGIN,
    fontSize: 14, fontWeight: 600, lineHeight: `${SECTION_TITLE_H - 8}px`, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
    // The title is a white tag stuck above the section, as the shapes are stickers.
    background: STICKER_EDGE, color: SWATCH_STYLE.white.ink, padding: '0 10px', borderRadius: 8, boxShadow: TAG_SHADOW,
  }
  // A section keeps its white rim until it is given an outline of its own.
  const custom = s.fill === 'none' || !!s.border || !!s.borderColor || !!s.borderHex
  return (
    <div
      data-shape-id={s.id} data-testid="canvas-shape" data-kind="section"
      style={{
        position: 'absolute', left: s.x, top: s.y, width: s.w, height: s.h, borderRadius: 14,
        background: s.fill === 'none' ? 'transparent' : `color-mix(in srgb, ${sw.fill} 85%, transparent)`,
        border: custom ? solidBorder(look.width, look.dashed ? 'transparent' : look.border) : undefined,
        boxShadow: custom ? undefined : SECTION_RIM,
      }}
    >
      {custom && look.dashed && <DashedOutline w={s.w} h={s.h} width={look.width} color={look.border} radius={14} />}
      {editing ? (
        <input
          autoFocus
          aria-label={t('components.canvas.section_title')}
          data-testid="canvas-shape-editor"
          value={s.text}
          placeholder={t('components.canvas.section_placeholder')}
          onChange={e => onText(e.target.value)}
          // The Enter that picks an IME candidate must not end the edit, so Enter commits only once claimed.
          {...ime.bindComposition<HTMLInputElement>({ onBlur: onEndEdit, onFocus: e => e.currentTarget.select() })}
          onKeyDown={e => {
            e.stopPropagation()
            if (e.key === 'Escape' || (e.key === 'Enter' && ime.claimEnter(e))) e.currentTarget.blur()
          }}
          onPointerDown={e => e.stopPropagation()}
          style={{ ...title, width: labelW, border: 'none', outline: 'none', font: 'inherit', fontSize: 14, fontWeight: 600 }}
        />
      ) : (
        <div style={{ ...title, textOverflow: 'ellipsis' }}>
          {s.text || <span style={{ opacity: 0.45 }}>{t('components.canvas.section_placeholder')}</span>}
        </div>
      )}
    </div>
  )
}

/** One shape in world space, with its text editor while it is being edited. */
function ShapeView({ shape: s, editing, zoom, onText, onEndEdit }: { shape: Shape; editing: boolean; zoom: number; onText: (text: string) => void; onEndEdit: () => void }) {
  const { t } = useTranslation()
  // A shape's text is centred both ways while it is typed, as in FigJam: the field
  // is as tall as its lines and the box centres it, rather than filling the box
  // from the top.
  const centred = s.kind === 'rect' || s.kind === 'ellipse' || s.kind === 'diamond'
  const field = useRef<HTMLTextAreaElement>(null)
  useLayoutEffect(() => {
    const el = field.current
    if (!el || !centred || !editing) return
    el.style.height = '0px'
    el.style.height = `${el.scrollHeight}px`
  }, [centred, editing, s.text, s.w, s.h, s.fontSize])
  if (s.kind === 'section') return <SectionView shape={s} editing={editing} zoom={zoom} onText={onText} onEndEdit={onEndEdit} />
  const sw = swatchStyle(s.color, s.hex)
  const look = shapeLook(s)
  // A dashed outline is an SVG stroke over a transparent border of the same width.
  const outline = solidBorder(look.width, look.dashed ? 'transparent' : look.border)
  const text: CSSProperties = s.kind === 'sticky'
    ? { fontSize: s.fontSize ?? defaultFontSize(s.kind), lineHeight: 1.35, color: look.ink, textAlign: s.align ?? defaultAlign(s.kind), padding: 16 }
    : s.kind === 'text'
      ? {
        fontSize: s.fontSize ?? defaultFontSize(s.kind), lineHeight: 1.3, color: textInk(s), textAlign: s.align ?? defaultAlign(s.kind), padding: '4px 2px',
        fontWeight: s.bold ? 700 : 500, textDecoration: s.strike ? 'line-through' : undefined,
      }
      : { fontSize: s.fontSize ?? defaultFontSize(s.kind), lineHeight: 1.35, color: look.ink, textAlign: s.align ?? defaultAlign(s.kind), padding: s.kind === 'diamond' ? '18% 22%' : 14 }
  const box: CSSProperties = { position: 'absolute', left: s.x, top: s.y, width: s.w, height: s.h, display: 'flex' }
  // A sticker carries its white edge and lift as box-shadow; an outlined shape its border.
  const paint = (radius: number | string): CSSProperties => (look.sticker
    ? { background: s.kind === 'sticky' ? sw.paper : look.fill, borderRadius: radius, boxShadow: stickerShadow(look.edge, look.edgeWidth) }
    : { background: look.fill, border: outline, borderRadius: radius })
  // A sticky's paper is drawn by its own layers below; its box only carries the
  // lift, which follows the folded outline, and the tilt, dropped while typing.
  const shell: CSSProperties =
    s.kind === 'sticky' ? { filter: NOTE_DROP, transform: editing ? undefined : `rotate(${noteTilt(s.id)}deg)` }
      : s.kind === 'rect' ? paint(RECT_RADIUS)
        : s.kind === 'ellipse' ? paint('50%')
          : {}
  const align: CSSProperties = s.kind === 'sticky' || s.kind === 'text'
    ? { alignItems: 'flex-start', justifyContent: 'flex-start' }
    : { alignItems: 'center', justifyContent: 'center' }
  return (
    <div data-shape-id={s.id} data-testid="canvas-shape" data-kind={s.kind} style={{ ...box, ...shell, ...align }}>
      {s.kind === 'sticky' && (
        <>
          <span aria-hidden style={{ position: 'absolute', inset: 0, borderRadius: STICKY_RADIUS, background: look.solid ? sw.paper : look.fill, clipPath: noteClip(NOTE_FOLD) }} />
          <span aria-hidden style={{ position: 'absolute', right: 0, top: 0, width: NOTE_FOLD, height: NOTE_FOLD, borderBottomLeftRadius: STICKY_RADIUS, background: noteFold(sw.stroke) }} />
          <span aria-hidden data-testid="sticky-tape" style={{ position: 'absolute', top: -NOTE_TAPE.h / 2, left: '50%', width: NOTE_TAPE.w, height: NOTE_TAPE.h, transform: 'translateX(-50%) rotate(-3deg)', background: TAPE, boxShadow: TAPE_SHADOW, borderRadius: 2 }} />
        </>
      )}
      {look.dashed && s.kind !== 'text' && s.kind !== 'sticky' && (
        // A diamond has no CSS border, so its outline starts at the box edge.
        <DashedOutline w={s.w} h={s.h} width={look.width} color={look.border} ellipse={s.kind === 'ellipse'} diamond={s.kind === 'diamond'}
          offset={s.kind === 'diamond' ? 0 : look.width} radius={RECT_RADIUS} />
      )}
      {s.kind === 'diamond' && (
        // Drawn in the shape's own units so the stroke is as thick as the other
        // shapes' borders and scales with zoom, without stretching on wide diamonds.
        // A sticker diamond's white edge is a stroke twice the edge wide, painted
        // under the fill, so only its outer half shows.
        // Sized to the shape's own box, so its units are canvas units with no viewBox.
        <svg width={s.w} height={s.h} style={{ position: 'absolute', inset: 0, overflow: 'visible', filter: look.sticker ? STICKER_DROP : undefined }} aria-hidden>
          <polygon
            points={`${s.w / 2},${look.width} ${s.w - look.width},${s.h / 2} ${s.w / 2},${s.h - look.width} ${look.width},${s.h / 2}`}
            fill={look.fill} strokeLinejoin="round"
            {...(look.sticker
              ? { stroke: look.edge, strokeWidth: look.edgeWidth * 2, paintOrder: 'stroke' }
              : { stroke: look.dashed ? 'none' : look.border, strokeWidth: look.width })}
          />
        </svg>
      )}
      {editing ? (
        <textarea
          ref={field}
          autoFocus
          rows={1}
          placeholder={centred ? t('components.canvas.add_text') : undefined}
          aria-label={t('components.canvas.shape_text')}
          data-testid="canvas-shape-editor"
          value={s.text}
          onChange={e => onText(e.target.value)}
          onBlur={onEndEdit}
          onFocus={e => e.currentTarget.select()}
          onKeyDown={e => { e.stopPropagation(); if (e.key === 'Escape') e.currentTarget.blur() }}
          onPointerDown={e => e.stopPropagation()}
          style={{ ...text, position: 'relative', zIndex: 1, width: '100%', height: centred ? undefined : '100%', maxHeight: '100%', resize: 'none', border: 'none', outline: 'none', background: 'transparent', font: 'inherit', fontSize: text.fontSize, fontWeight: text.fontWeight, textDecoration: text.textDecoration, overflow: 'hidden' }}
        />
      ) : (
        // A text box's text spans the box, so its alignment has room to act; in the
        // flex box it would otherwise shrink to the width of its own words.
        <div style={{ ...text, position: 'relative', zIndex: 1, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', overflow: 'hidden', maxHeight: '100%', width: s.kind === 'text' || centred ? '100%' : undefined }}>
          {s.text || (s.kind === 'text' ? <span style={{ opacity: 0.4 }}>{t('components.canvas.text_placeholder')}</span> : null)}
        </div>
      )}
    </div>
  )
}
