/**
 * The layout editor — the Neo-Frame "Bench" build mode, ported (core pass).
 *
 * This PR ships the CORE editing experience: draggable palette tiles that spawn
 * a cursor-following ghost, drop-onto-cell with a live valid/invalid preview,
 * drag-a-pane's-title-bar to move it (and off the grid to remove it), and
 * grid-dimension steppers. The interaction model is ported from spark-neoframe's
 * `App.tsx`; the LOOK is ported from its `App.css` onto Kiro Crew's theme tokens
 * (`layoutEditor.css`), with a per-element tint like Neo-Frame's chipStyle.
 *
 * Three further interaction layers land as their own small follow-up PRs and are
 * deliberately NOT here: corner-grip resize, draggable fr track dividers, and the
 * tabs container. Each is additive and independently revertible (RFC §7's
 * one-thing-at-a-time rollout).
 *
 * It edits an EDIT MODEL (`GridSpec`, from the merged PR 1 model) and never
 * renders a live pane — no subject is selected here, so a cell only shows its
 * element label. That is deliberate (RFC §7): the editor produces and edits a
 * spec, the renderer (a later PR) draws live panes. This file therefore imports
 * only the pure model (`grid.ts` geometry + `editModel.ts` mint) and the shared
 * element vocabulary, nothing from the render path.
 */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  MessageSquare,
  PanelRight,
  LayoutGrid,
  Rows,
  X,
  FolderTree,
  GitBranch,
  GitPullRequest,
  Users,
  TerminalSquare,
  NotebookPen,
  ListChecks,
  type LucideIcon,
} from 'lucide-react'
import {
  canPlace,
  clamp,
  findItem,
  firstFreeCell,
  removeItemById,
  type GridItem,
  type GridSpec,
} from './grid'
import { newItem } from './editModel'
import type { ElementKind } from './layoutTree'
import { i18nT } from '../../../i18n/t'
import './layoutEditor.css'

/** Per-element glyph + tint + label catalog key, the Kiro Crew analogue of
 *  Neo-Frame's def.icon/def.tint. The label is a catalog key resolved with
 *  `i18nT` at render (a module constant cannot hold a translated literal). */
const ELEMENT_META: Record<GridItem['element'], { icon: LucideIcon; labelKey: string; tint: string }> = {
  chat: { icon: MessageSquare, labelKey: 'components.crewLayout.element.chat', tint: '#5b8cf5' },
  sidePanel: { icon: PanelRight, labelKey: 'components.crewLayout.element.sidePanel', tint: '#c48ee0' },
  files: { icon: FolderTree, labelKey: 'components.crewLayout.element.files', tint: '#3ddc84' },
  git: { icon: GitBranch, labelKey: 'components.crewLayout.element.git', tint: '#e0846e' },
  changes: { icon: GitPullRequest, labelKey: 'components.crewLayout.element.changes', tint: '#f0b054' },
  subagents: { icon: Users, labelKey: 'components.crewLayout.element.subagents', tint: '#56c6d6' },
  terminal: { icon: TerminalSquare, labelKey: 'components.crewLayout.element.terminal', tint: '#9b8ec4' },
  notes: { icon: NotebookPen, labelKey: 'components.crewLayout.element.notes', tint: '#d6a656' },
  workLog: { icon: ListChecks, labelKey: 'components.crewLayout.element.workLog', tint: '#7ac77a' },
  group: { icon: LayoutGrid, labelKey: 'components.crewLayout.element.group', tint: '#f0b054' },
  tabs: { icon: Rows, labelKey: 'components.crewLayout.element.tabs', tint: '#e0846e' },
}

// The tabs container is added in a follow-up PR; the core palette places content
// elements only.
const CONTENT_PALETTE: ElementKind[] = [
  'chat',
  'sidePanel',
  'files',
  'git',
  'changes',
  'subagents',
  'terminal',
  'notes',
  'workLog',
]
const MAX_DIM = 6

type Drag =
  | { kind: 'palette'; element: GridItem['element']; pointerId: number; x: number; y: number; startX: number; startY: number }
  | {
      kind: 'move'
      itemId: string
      element: GridItem['element']
      pointerId: number
      w: number
      h: number
      grabX: number
      grabY: number
      x: number
      y: number
    }

interface Preview {
  rect: { x: number; y: number; w: number; h: number }
  valid: boolean
  replaceId?: string
}

/** fr track weights sized to a dimension, defaulting to equal tracks. A spec's
 *  optional `colSizes`/`rowSizes` carry a non-equal split through save→reopen;
 *  a length mismatch (dimension changed) falls back to equal so the editor never
 *  renders against a stale-length track array. */
function trackSizes(sizes: number[] | undefined, dim: number): number[] {
  return sizes && sizes.length === dim ? sizes : Array(dim).fill(1)
}

/** Map a fraction (0..1) along an axis to its track index, honouring unequal fr
 *  weights. Uniform division (`floor(frac * dim)`) is WRONG when tracks differ
 *  (e.g. `[3, 2]`): it splits the axis into equal halves the rendered grid does
 *  not have, so a drop lands in the wrong cell. Walk the cumulative weight
 *  fractions instead, so the hit-test matches what `grid-template-*: Nfr` drew. */
export function trackIndexAtFraction(sizes: number[], frac: number): number {
  const total = sizes.reduce((s, n) => s + n, 0)
  if (total <= 0) return 0
  let acc = 0
  for (let i = 0; i < sizes.length; i++) {
    acc += sizes[i]
    if (frac < acc / total) return i
  }
  return sizes.length - 1
}

export default function LayoutEditor({ spec, onChange }: { spec: GridSpec; onChange: (next: GridSpec) => void }) {
  const canvasRef = useRef<HTMLDivElement | null>(null)
  const [drag, setDrag] = useState<Drag | null>(null)

  // Track weights live ON the spec (colSizes/rowSizes) so a non-equal split
  // (e.g. the floor seed's [3, 2]) renders; the divider-drag that EDITS them
  // arrives in a follow-up PR.
  const cols = trackSizes(spec.colSizes, spec.cols)
  const rows = trackSizes(spec.rowSizes, spec.rows)

  /* ----------------------- pointer → cell targeting ---------------------- */

  const cellAt = (px: number, py: number): { cx: number; cy: number } | null => {
    const el = canvasRef.current
    if (!el) return null
    const r = el.getBoundingClientRect()
    if (px < r.left || px > r.right || py < r.top || py > r.bottom) return null
    // Resolve through the fr weights the grid actually rendered, not a uniform
    // split — a drop over a 3fr column must not read as the midpoint of 2 cols.
    const cx = trackIndexAtFraction(cols, (px - r.left) / r.width)
    const cy = trackIndexAtFraction(rows, (py - r.top) / r.height)
    return { cx, cy }
  }

  const itemAt = (cx: number, cy: number, ignoreId?: string) =>
    spec.items.find((i) => i.id !== ignoreId && cx >= i.x && cx < i.x + i.w && cy >= i.y && cy < i.y + i.h)

  /* --------------------------- live preview ------------------------------ */

  // Resolve the drop preview from a pointer position. Called at render with the
  // drag's last-known coords (for the ghost) AND at drop time with the pointerup's
  // LIVE coords — a pointerup that crosses a cell boundary after the final
  // pointermove must commit where the pointer actually released, not the stale
  // last-move cell.
  const computePreview = (px: number, py: number): Preview | null => {
    if (!drag) return null
    const loc = cellAt(px, py)
    if (!loc) return null
    if (drag.kind === 'palette') {
      const rect = { x: loc.cx, y: loc.cy, w: 1, h: 1 }
      if (canPlace(spec.items, rect, spec.cols, spec.rows)) return { rect, valid: true }
      // Dropping onto an occupied cell replaces the item under it.
      const target = itemAt(loc.cx, loc.cy)
      if (target) return { rect: { x: target.x, y: target.y, w: target.w, h: target.h }, valid: true, replaceId: target.id }
      return { rect, valid: false }
    }
    const w = Math.min(drag.w, spec.cols)
    const h = Math.min(drag.h, spec.rows)
    const rect = {
      x: clamp(loc.cx - drag.grabX, 0, spec.cols - w),
      y: clamp(loc.cy - drag.grabY, 0, spec.rows - h),
      w,
      h,
    }
    if (canPlace(spec.items, rect, spec.cols, spec.rows, drag.itemId)) return { rect, valid: true }
    const target = itemAt(loc.cx, loc.cy, drag.itemId)
    if (target) return { rect: { x: target.x, y: target.y, w: target.w, h: target.h }, valid: true, replaceId: target.id }
    return { rect, valid: false }
  }

  // Render-time preview (the ghost) uses the drag's last-known coords.
  const preview: Preview | null = drag ? computePreview(drag.x, drag.y) : null

  /* ----------------------------- drops ----------------------------------- */

  useEffect(() => {
    if (!drag) return
    const onMove = (e: PointerEvent) => {
      if (e.pointerId !== drag.pointerId) return
      setDrag({ ...drag, x: e.clientX, y: e.clientY })
    }
    const onUp = (e: PointerEvent) => {
      // Only the pointer that started this drag commits it.
      if (e.pointerId !== drag.pointerId) return
      // Recompute from the LIVE release coords — a pointerup that crossed a cell
      // boundary after the last pointermove must land where it actually released.
      const p = computePreview(e.clientX, e.clientY)
      if (drag.kind === 'palette') {
        const moved = Math.hypot(e.clientX - drag.startX, e.clientY - drag.startY) > 5
        if (!moved) {
          // A click (no drag) drops into the first free cell.
          const free = firstFreeCell(spec.items, spec.cols, spec.rows)
          if (free) onChange({ ...spec, items: [...spec.items, newItem(drag.element, { ...free, w: 1, h: 1 })] })
        } else if (p?.valid) {
          const target = p.replaceId ? spec.items.filter((i) => i.id !== p.replaceId) : spec.items
          onChange({ ...spec, items: [...target, newItem(drag.element, p.rect)] })
        }
      } else if (drag.kind === 'move') {
        const dragged = findItem(spec.items, drag.itemId)
        const loc = cellAt(e.clientX, e.clientY)
        if (dragged && !loc) {
          // Dragged off the grid → remove.
          onChange({ ...spec, items: removeItemById(spec.items, drag.itemId) })
        } else if (dragged && p?.valid) {
          let items = spec.items
          if (p.replaceId) items = items.filter((i) => i.id !== p.replaceId)
          onChange({ ...spec, items: items.map((i) => (i.id === drag.itemId ? { ...i, ...p.rect } : i)) })
        }
      }
      setDrag(null)
    }
    // A cancelled drag (browser gesture takeover, device loss, touch capture)
    // must clear WITHOUT committing — otherwise it stays armed and a later
    // stray pointerup could take the off-grid removal branch on the wrong pane.
    const onCancel = (e: PointerEvent) => {
      if (e.pointerId !== drag.pointerId) return
      setDrag(null)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onCancel)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onCancel)
    }
  })

  /* ------------------------------ grid dims ------------------------------ */

  const minCols = Math.max(1, ...spec.items.map((i) => i.x + i.w))
  const minRows = Math.max(1, ...spec.items.map((i) => i.y + i.h))

  const setDims = (nextCols: number, nextRows: number) => {
    // Never shrink through a placed item — clamp to the max occupied edge.
    const c = clamp(nextCols, minCols, MAX_DIM)
    const r = clamp(nextRows, minRows, MAX_DIM)
    // Reset track weights to equal ONLY on the axis whose count changed — a
    // resized axis has a different number of tracks, so its old fr array no
    // longer applies; the untouched axis keeps its weights (changing cols must
    // not wipe a custom row split, and vice-versa).
    onChange({
      ...spec,
      cols: c,
      rows: r,
      ...(c !== spec.cols ? { colSizes: Array(c).fill(1) } : {}),
      ...(r !== spec.rows ? { rowSizes: Array(r).fill(1) } : {}),
    })
  }

  /* ------------------------------ item move ------------------------------ */

  const startMove = (e: React.PointerEvent, item: GridItem) => {
    if (e.button !== 0) return
    e.preventDefault()
    const loc = cellAt(e.clientX, e.clientY)
    setDrag({
      kind: 'move',
      itemId: item.id,
      element: item.element,
      pointerId: e.pointerId,
      w: item.w,
      h: item.h,
      grabX: loc ? clamp(loc.cx - item.x, 0, item.w - 1) : 0,
      grabY: loc ? clamp(loc.cy - item.y, 0, item.h - 1) : 0,
      x: e.clientX,
      y: e.clientY,
    })
  }

  // Keyboard counterpart to the pointer drag on a pane's title bar (WCAG
  // 2.1.1): arrow keys nudge a placed pane one cell, and it lands only when the
  // destination is free (no silent overlap). Gives a keyboard-only user the
  // arrange path the pointer bar otherwise monopolised.
  const moveItemByKey = (item: GridItem, dx: number, dy: number) => {
    const nx = clamp(item.x + dx, 0, spec.cols - item.w)
    const ny = clamp(item.y + dy, 0, spec.rows - item.h)
    if (nx === item.x && ny === item.y) return
    const rect = { x: nx, y: ny, w: item.w, h: item.h }
    if (!canPlace(spec.items, rect, spec.cols, spec.rows, item.id)) return
    onChange({ ...spec, items: spec.items.map((i) => (i.id === item.id ? { ...i, ...rect } : i)) })
  }

  const onBarKeyDown = (e: React.KeyboardEvent, item: GridItem) => {
    const delta: Record<string, [number, number]> = {
      ArrowLeft: [-1, 0],
      ArrowRight: [1, 0],
      ArrowUp: [0, -1],
      ArrowDown: [0, 1],
    }
    const d = delta[e.key]
    if (d) {
      e.preventDefault()
      moveItemByKey(item, d[0], d[1])
    }
  }

  /* ------------------------------- render -------------------------------- */

  const renderBody = (item: GridItem): ReactNode => {
    // The builder never renders a live pane — no subject is selected here, so a
    // registry cell would only return its "Select a member" empty state anyway.
    // Draw the element label directly from the shared meta so the editor depends
    // on the model + element meta alone, not on the renderer / any real
    // component. A cell's live rendering is entirely the renderer's job. Every
    // GridItem['element'] has a meta entry (the record is total over the union),
    // so no "unknown element" fallback is reachable here.
    return <div className="le-cell-label">{i18nT(ELEMENT_META[item.element].labelKey)}</div>
  }

  const ghostMeta = drag ? ELEMENT_META[drag.element] : null
  const ghostOffGrid = drag?.kind === 'move' && cellAt(drag.x, drag.y) === null
  const GhostIcon = ghostMeta?.icon

  // Keyboard / no-drag placement: Tab+Enter (or a plain click) on a palette tile
  // drops the element into the first free cell, the same branch the no-move
  // pointerup takes. Guarded on `drag === null` so it never fires mid-drag.
  const placeInFirstFree = (element: GridItem['element']) => {
    if (drag) return
    const free = firstFreeCell(spec.items, spec.cols, spec.rows)
    if (free) onChange({ ...spec, items: [...spec.items, newItem(element, { ...free, w: 1, h: 1 })] })
  }

  return (
    // narrow-viewport-required: DESKTOP-ONLY by design. This is the layout
    // BUILDER — a drag-and-drop editing surface (palette shelf + fixed cols×rows
    // canvas) with no phone use case; it ships as a desktop Crew Members modal in
    // RFC §7 PR 4, never at a phone width. Pointer-drag onto a grid has no
    // sensible single-column fallback, so there is deliberately no narrow branch.
    <div className="le-root" data-testid="layout-editor">
      {/* Palette shelf */}
      <div className="le-palette">
        <div className="le-palette-group">
          <span className="le-palette-label">{i18nT('components.crewLayout.paletteContent')}</span>
          <div className="le-tiles">
            {CONTENT_PALETTE.map((el) => (
              <Tile key={el} element={el} onStartDrag={setDrag} onPlace={placeInFirstFree} />
            ))}
          </div>
        </div>
        <span className="le-hint">{i18nT('components.crewLayout.hint')}</span>
        <div className="le-dims">
          <Stepper axis="cols" value={spec.cols} min={minCols} max={MAX_DIM} onChange={(c) => setDims(c, spec.rows)} />
          <span className="le-dims-x">×</span>
          <Stepper axis="rows" value={spec.rows} min={minRows} max={MAX_DIM} onChange={(rw) => setDims(spec.cols, rw)} />
        </div>
      </div>

      {/* Canvas */}
      <div className="le-canvas-wrap">
        <div
          ref={canvasRef}
          className="le-canvas"
          style={{
            gridTemplateColumns: cols.map((n) => `${n}fr`).join(' '),
            gridTemplateRows: rows.map((n) => `${n}fr`).join(' '),
          }}
        >
          {/* drop-target cells */}
          {Array.from({ length: spec.cols * spec.rows }, (_, i) => {
            const x = i % spec.cols
            const y = Math.floor(i / spec.cols)
            const inPrev =
              preview &&
              x >= preview.rect.x &&
              x < preview.rect.x + preview.rect.w &&
              y >= preview.rect.y &&
              y < preview.rect.y + preview.rect.h
            return (
              <div
                key={`c${x}-${y}`}
                className={`le-cell ${inPrev ? (preview!.valid ? 'le-on-target' : 'le-invalid') : ''}`}
                style={{ gridColumn: `${x + 1}`, gridRow: `${y + 1}` }}
              />
            )
          })}

          {/* placed items */}
          <AnimatePresence>
            {spec.items.map((item) => {
              const isMoving = drag?.kind === 'move' && drag.itemId === item.id
              const isTarget = preview?.replaceId === item.id
              const meta = ELEMENT_META[item.element]
              const BarIcon = meta.icon
              return (
                <motion.div
                  key={item.id}
                  className={`le-item ${isMoving ? 'le-moving' : ''} ${isTarget ? 'le-on-target' : ''}`}
                  initial={{ opacity: 0, scale: 0.96 }}
                  animate={{ opacity: isMoving ? 0.35 : 1, scale: 1 }}
                  exit={{ opacity: 0, scale: 0.96 }}
                  transition={{ duration: 0.18, ease: [0.32, 1.4, 0.55, 1] }}
                  style={{
                    ['--le-tint' as string]: meta.tint,
                    gridColumn: `${item.x + 1} / span ${item.w}`,
                    gridRow: `${item.y + 1} / span ${item.h}`,
                    zIndex: isMoving ? 2 : 1,
                  }}
                >
                  <div
                    className="le-bar"
                    role="button"
                    tabIndex={0}
                    aria-label={i18nT('components.crewLayout.moveElement', { label: i18nT(meta.labelKey) })}
                    onPointerDown={(e) => startMove(e, item)}
                    onKeyDown={(e) => onBarKeyDown(e, item)}
                  >
                    <span className="le-bar-icon">
                      <BarIcon size={11} strokeWidth={2.25} />
                    </span>
                    <span className="le-bar-title">{i18nT(meta.labelKey)}</span>
                    <button
                      type="button"
                      className="le-close"
                      title={i18nT('components.crewLayout.remove')}
                      aria-label={i18nT('components.crewLayout.removeElement', { label: i18nT(meta.labelKey) })}
                      onPointerDown={(e) => e.stopPropagation()}
                      onClick={() => onChange({ ...spec, items: removeItemById(spec.items, item.id) })}
                    >
                      <X size={13} />
                    </button>
                  </div>
                  <div className="le-body">{renderBody(item)}</div>
                </motion.div>
              )
            })}
          </AnimatePresence>
        </div>
      </div>

      {/* drag ghost */}
      {ghostMeta && GhostIcon && drag && (
        <div
          className={`le-ghost ${ghostOffGrid ? 'le-removing' : ''}`}
          style={{ ['--le-tint' as string]: ghostMeta.tint, left: drag.x, top: drag.y }}
        >
          <span className="le-ghost-icon">
            <GhostIcon size={18} strokeWidth={2} />
          </span>
          <span>{ghostOffGrid ? i18nT('components.crewLayout.remove') : i18nT(ghostMeta.labelKey)}</span>
        </div>
      )}
    </div>
  )
}

/** A palette tile: a drag source for one element type. Hoisted to module scope
 *  (not declared in the editor's render body) so a `setDrag` re-render during a
 *  drag does not give it a new type identity and remount all nine buttons at
 *  pointer-event frequency — which dropped the pressed tile's grabbing cursor
 *  the moment a drag began. Takes its callbacks as props instead. */
function Tile({
  element,
  onStartDrag,
  onPlace,
}: {
  element: GridItem['element']
  onStartDrag: (d: Drag) => void
  onPlace: (element: GridItem['element']) => void
}) {
  const meta = ELEMENT_META[element]
  const Icon = meta.icon
  return (
    <button
      type="button"
      className="le-tile"
      title={i18nT('components.crewLayout.dragTile', { label: i18nT(meta.labelKey) })}
      onPointerDown={(e) => {
        if (e.button !== 0) return
        e.preventDefault()
        onStartDrag({ kind: 'palette', element, pointerId: e.pointerId, x: e.clientX, y: e.clientY, startX: e.clientX, startY: e.clientY })
      }}
      // Keyboard activation dispatches a click with detail===0, never
      // pointerdown, so without this Tab+Enter/Space on a tile does nothing and
      // the editor has no non-pointer way to add an element (WCAG 2.1.1). Gating
      // on detail===0 means ONLY a keyboard click places — the compatibility
      // click that trails a pointer gesture carries detail>=1 and is ignored, so
      // a pointer drag/tap (handled on pointerup) never double-adds.
      onClick={(e) => {
        if (e.detail === 0) onPlace(element)
      }}
    >
      <span className="le-tile-icon" style={{ ['--le-tint' as string]: meta.tint }}>
        <Icon size={15} strokeWidth={2} />
      </span>
      <span className="le-tile-label">{i18nT(meta.labelKey)}</span>
    </button>
  )
}

function Stepper({
  axis,
  value,
  min,
  max,
  onChange,
}: {
  axis: 'cols' | 'rows'
  value: number
  min: number
  max: number
  onChange: (v: number) => void
}) {
  const noun = i18nT(axis === 'cols' ? 'components.crewLayout.cols' : 'components.crewLayout.rows')
  const fewer = i18nT(axis === 'cols' ? 'components.crewLayout.fewerCols' : 'components.crewLayout.fewerRows')
  const more = i18nT(axis === 'cols' ? 'components.crewLayout.moreCols' : 'components.crewLayout.moreRows')
  return (
    <span className="le-stepper" title={`${noun} (${min}–${max})`}>
      <button type="button" disabled={value <= min} onClick={() => onChange(value - 1)} aria-label={fewer}>
        −
      </button>
      <span className="le-stepper-val">{value}</span>
      <button type="button" disabled={value >= max} onClick={() => onChange(value + 1)} aria-label={more}>
        +
      </button>
    </span>
  )
}
