import { safeSetItem } from '../utils/safeStorage'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Monitor, Maximize2, Minimize2, Minus, X } from 'lucide-react'

import { useBrowserFrame } from '../hooks/useBrowserFrame'

/**
 * BrowserLiveView — floating window that mirrors the headless [BROWSE] Chromium.
 *
 * On a cloud desktop the browse session runs headless on the dev host; this
 * panel mirrors it to the laptop over the dashboard's existing WS + reverse-SSH
 * tunnel. Frames arrive as `kirocrew-browser-frame` window events (routed from
 * the WS `browser_frame` message in useWebSocket) — each is a screenshot the
 * agent captured (or the proxy's idle active-pump), forwarded by the Playwright
 * MCP proxy.
 *
 * Self-contained, lifecycle-driven — there is no top-bar button. Three states:
 *   hidden → (first frame) → open  ⇄  chip (corner)
 * - It stays hidden until the first frame, then auto-opens as a small,
 *   non-disruptive thumbnail in the bottom-right corner.
 * - Closing (the header ✕) fully dismisses the window for the *current* browse
 *   session: unlike minimize, it leaves no chip and suppresses the idle active-
 *   pump's frames from re-opening it. A genuinely new browse session (a
 *   different session_key) still surfaces automatically, so close means "hide
 *   this session's mirror," not "disable the feature."
 * - The window is a free-floating, **fully resizable** rect: drag the header to
 *   move it, and drag any of the eight edge/corner handles to resize (like a
 *   normal desktop window). Position and size are always clamped into the
 *   viewport, so no handle can escape off-screen. The chosen size persists across
 *   sessions in localStorage (position re-anchors to the corner on each open).
 *   The frame is `object-contain`, so a bigger window shows more of the page at
 *   higher fidelity rather than cropping. A header button also one-click toggles
 *   between the compact default and a large preset.
 * - Minimizing collapses it to a tiny corner chip rather than destroying it; the
 *   chip is the re-open affordance and only exists while there's browse activity.
 *   New frames update the image but never force a collapsed panel back open.
 * Read-only by design (no debug port; interactive control is out of scope —).
 */

type Mode = 'hidden' | 'chip' | 'open'

interface Dims {
  w: number
  h: number
}

// Top-left-anchored window rect (px). x/y are the panel's left/top; w/h its size.
interface Rect {
  x: number
  y: number
  w: number
  h: number
}

// Which sides a resize handle drives. A corner handle sets two.
interface Edge {
  l?: boolean
  r?: boolean
  t?: boolean
  b?: boolean
}

const MIN_W = 180
const MIN_H = 120
const MARGIN = 16 // keep this gap between the panel and every viewport edge
const DEFAULT_DIMS: Dims = { w: 260, h: 180 }
// One-click "expand" preset (clamped to the viewport on apply); the handles still
// allow any size in between.
const LARGE_DIMS: Dims = { w: 860, h: 580 }
const DIMS_KEY = 'mc-browse-mirror-dims'

const clamp = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v))

function loadDims(): Dims {
  try {
    const raw = localStorage.getItem(DIMS_KEY)
    if (raw) {
      const d = JSON.parse(raw)
      if (typeof d?.w === 'number' && typeof d?.h === 'number') {
        return { w: Math.max(MIN_W, d.w), h: Math.max(MIN_H, d.h) }
      }
    }
  } catch {
    /* ignore malformed persisted size */
  }
  return { ...DEFAULT_DIMS }
}

// Keep a rect fully inside the viewport: bound the size, then bound the position
// so left/top can't push any edge off-screen. Used after every move/resize/toggle
// and on window resize, so the panel (and its handles) stay reachable.
export function clampRect(r: Rect): Rect {
  const vw = window.innerWidth
  const vh = window.innerHeight
  // Reserve MARGIN on BOTH sides (2 * MARGIN), matching cornerRect: capping size
  // to a single margin would, at max width/height, force x/y to MARGIN and push
  // the right/bottom edge flush to the viewport — losing the gap and making those
  // resize handles unreachable.
  const w = clamp(r.w, MIN_W, Math.max(MIN_W, vw - 2 * MARGIN))
  const h = clamp(r.h, MIN_H, Math.max(MIN_H, vh - 2 * MARGIN))
  const x = clamp(r.x, MARGIN, Math.max(MARGIN, vw - w - MARGIN))
  const y = clamp(r.y, MARGIN, Math.max(MARGIN, vh - h - MARGIN))
  return { x, y, w, h }
}

// Place a panel of size `d` in the bottom-right corner (the auto-open home).
export function cornerRect(d: Dims): Rect {
  const vw = window.innerWidth
  const vh = window.innerHeight
  const w = clamp(d.w, MIN_W, Math.max(MIN_W, vw - 2 * MARGIN))
  const h = clamp(d.h, MIN_H, Math.max(MIN_H, vh - 2 * MARGIN))
  return { x: vw - w - MARGIN, y: vh - h - MARGIN, w, h }
}

// Resize `start` by a pointer delta, driving only the sides named in `edges`.
// The opposite (undriven) edge stays pinned, MIN_W/MIN_H are enforced, and every
// edge is clamped to the viewport — so a drag can never invert the rect or leave
// the screen.
export function resizeRect(start: Rect, edges: Edge, dx: number, dy: number): Rect {
  const vw = window.innerWidth
  const vh = window.innerHeight
  let { x, y, w, h } = start
  const right = start.x + start.w
  const bottom = start.y + start.h
  if (edges.r) w = clamp(start.w + dx, MIN_W, vw - MARGIN - start.x)
  if (edges.b) h = clamp(start.h + dy, MIN_H, vh - MARGIN - start.y)
  if (edges.l) {
    x = clamp(start.x + dx, MARGIN, right - MIN_W)
    w = right - x
  }
  if (edges.t) {
    y = clamp(start.y + dy, MARGIN, bottom - MIN_H)
    h = bottom - y
  }
  return { x, y, w, h }
}

// Eight resize handles: four thin edge strips + four corner squares. Corners sit
// above edges (z-20 vs z-10) so a corner drag wins where they overlap.
const HANDLES: { name: string; edges: Edge; cls: string; cursor: string; corner?: boolean }[] = [
  { name: 'top', edges: { t: true }, cls: 'top-0 left-0 right-0 h-1.5', cursor: 'cursor-ns-resize' },
  { name: 'bottom', edges: { b: true }, cls: 'bottom-0 left-0 right-0 h-1.5', cursor: 'cursor-ns-resize' },
  { name: 'left', edges: { l: true }, cls: 'left-0 top-0 bottom-0 w-1.5', cursor: 'cursor-ew-resize' },
  { name: 'right', edges: { r: true }, cls: 'right-0 top-0 bottom-0 w-1.5', cursor: 'cursor-ew-resize' },
  { name: 'top-left', edges: { t: true, l: true }, cls: 'top-0 left-0 h-3 w-3', cursor: 'cursor-nwse-resize', corner: true },
  { name: 'top-right', edges: { t: true, r: true }, cls: 'top-0 right-0 h-3 w-3', cursor: 'cursor-nesw-resize', corner: true },
  { name: 'bottom-left', edges: { b: true, l: true }, cls: 'bottom-0 left-0 h-3 w-3', cursor: 'cursor-nesw-resize', corner: true },
  { name: 'bottom-right', edges: { b: true, r: true }, cls: 'bottom-0 right-0 h-3 w-3', cursor: 'cursor-nwse-resize', corner: true },
]

export default function BrowserLiveView() {
  const [mode, setMode] = useState<Mode>('hidden')
  const { frame, lastTs, sessionKey, sessionName } = useBrowserFrame()
  const [rect, setRect] = useState<Rect>(() => cornerRect(loadDims()))
  const dragRef = useRef<{ dx: number; dy: number } | null>(null)
  const resizeRef = useRef<{ px: number; py: number; rect: Rect; edges: Edge } | null>(null)
  // Set by the header ✕: records which session's mirror the user explicitly
  // closed so the frame handler won't auto-reopen it under the idle active-pump.
  // `null` = nothing dismissed; `{ key }` = keep hidden while frames carry `key`.
  const dismissedRef = useRef<{ key: string | null } | null>(null)

  // Persist the chosen window size (not position) so it survives reloads; the
  // panel always re-opens in the corner at this size.
  useEffect(() => {
    try {
      safeSetItem(DIMS_KEY, JSON.stringify({ w: rect.w, h: rect.h }))
    } catch {
      /* ignore quota / unavailable storage */
    }
  }, [rect.w, rect.h])

  // Keep the panel on-screen if the viewport shrinks (e.g. window resize, dev
  // tools open) — clamp position/size back into view.
  useEffect(() => {
    const onResize = () => setRect(r => clampRect(r))
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  // Frames auto-open the panel the first time, so the user sees activity even if
  // they never opened it. Once it's open or collapsed to the chip, a new frame
  // only updates the image — it never forces a collapsed panel back open. This
  // listener drives ONLY the mode transition; the frame/lastTs/sessionKey state
  // is owned by the useBrowserFrame hook. (The docked right-panel "Web Preview"
  // tab is a separate URL-iframe feature and does NOT consume this frame stream;
  // this floating window is currently the hook's only consumer.)
  useEffect(() => {
    const onFrame = (e: Event) => {
      const d = (e as CustomEvent<{ data?: string; session_key?: string }>).detail
      if (!d?.data) return
      const incoming = d.session_key || null
      setMode(m => {
        if (m !== 'hidden') return m
        // Honor an explicit close: stay hidden while frames keep arriving for the
        // dismissed session. A different session_key clears the dismissal so new
        // browse activity still auto-opens the mirror.
        if (dismissedRef.current) {
          if (dismissedRef.current.key === incoming) return 'hidden'
          dismissedRef.current = null
        }
        // First reveal: drop the panel in the bottom-right corner at its saved size.
        setRect(r => cornerRect({ w: r.w, h: r.h }))
        return 'open'
      })
    }
    window.addEventListener('kirocrew-browser-frame', onFrame)
    return () => window.removeEventListener('kirocrew-browser-frame', onFrame)
  }, [])

  // Programmatic open⇄chip toggle. No UI button dispatches this today (the panel
  // is lifecycle-driven); kept as an internal hook for a future shortcut/command.
  useEffect(() => {
    const onToggle = () => setMode(m => (m === 'open' ? 'chip' : 'open'))
    window.addEventListener('kirocrew-toggle-browser-live', onToggle)
    return () => window.removeEventListener('kirocrew-toggle-browser-live', onToggle)
  }, [])

  // Move from the header. Tracks the cursor→top-left offset so the panel doesn't
  // jump on grab; the new position is clamped into the viewport.
  const onHeaderPointerDown = useCallback((e: React.PointerEvent) => {
    dragRef.current = { dx: e.clientX - rect.x, dy: e.clientY - rect.y }
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  }, [rect.x, rect.y])
  const onHeaderPointerMove = useCallback((e: React.PointerEvent) => {
    const d = dragRef.current
    if (!d) return
    setRect(r => clampRect({ ...r, x: e.clientX - d.dx, y: e.clientY - d.dy }))
  }, [])
  const onHeaderPointerUp = useCallback(() => {
    dragRef.current = null
  }, [])

  // Resize from any of the eight handles. The handler is shared; each handle
  // binds it with its own set of driven edges. stopPropagation keeps a handle
  // drag from also starting a header move.
  const makeResizePointerDown = useCallback(
    (edges: Edge) => (e: React.PointerEvent) => {
      e.stopPropagation()
      resizeRef.current = { px: e.clientX, py: e.clientY, rect, edges }
      ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
    },
    [rect],
  )
  const onResizePointerMove = useCallback((e: React.PointerEvent) => {
    const r = resizeRef.current
    if (!r) return
    setRect(resizeRect(r.rect, r.edges, e.clientX - r.px, e.clientY - r.py))
  }, [])
  const onResizePointerUp = useCallback(() => {
    resizeRef.current = null
  }, [])

  // Quick one-click size toggle, complementing the free-resize handles: snap to
  // the large preset when small, back to the compact default when already large.
  // "Expanded" is derived from the current width (closer to the large preset than
  // the default), so the toggle stays correct even after an arbitrary handle resize.
  const expanded = rect.w >= (DEFAULT_DIMS.w + LARGE_DIMS.w) / 2
  const onToggleSize = useCallback(() => {
    setRect(r => {
      const target = r.w >= (DEFAULT_DIMS.w + LARGE_DIMS.w) / 2 ? DEFAULT_DIMS : LARGE_DIMS
      return clampRect({ ...r, w: target.w, h: target.h })
    })
  }, [])

  // Close fully dismisses the mirror (no chip). We remember the closed session so
  // the idle active-pump's frames don't bounce it back open; a new session still
  // auto-opens (see onFrame). Distinct from minimize, which keeps a re-open chip.
  const onClose = useCallback(() => {
    dismissedRef.current = { key: sessionKey }
    setMode('hidden')
  }, [sessionKey])

  if (mode === 'hidden') return null

  if (mode === 'chip') {
    return (
      <button
        className="fixed z-[60] bottom-4 right-4 flex items-center gap-2 px-3 py-2 rounded-full border border-border bg-card shadow-lg hover:bg-bg-hover transition-colors"
        onClick={() => setMode('open')}
        aria-label="Show live browser view"
        title="Show live browser view"
      >
        <Monitor size={14} className="text-muted" />
        <span className="text-[12px] font-medium text-text">Browser</span>
        <span
          className={`inline-block w-1.5 h-1.5 rounded-full ${frame ? 'animate-pulse' : ''}`}
          style={{ backgroundColor: frame ? 'var(--ok)' : 'var(--muted)' }}
          aria-hidden
        />
      </button>
    )
  }

  return (
    <div
      className="fixed z-[60] flex flex-col rounded-xl border border-border bg-card shadow-xl overflow-hidden"
      style={{ left: rect.x, top: rect.y, width: rect.w, height: rect.h }}
      role="dialog"
      aria-label="Live browser view"
    >
      {/* Eight resize handles (four edges + four corners); the panel is a free,
          viewport-clamped rect, so any handle can grow or shrink it. */}
      {HANDLES.map(hd => (
        <div
          key={hd.name}
          onPointerDown={makeResizePointerDown(hd.edges)}
          onPointerMove={onResizePointerMove}
          onPointerUp={onResizePointerUp}
          onPointerCancel={onResizePointerUp}
          role="separator"
          aria-label={`Resize live browser view (${hd.name})`}
          title="Drag to resize"
          className={`absolute ${hd.cls} ${hd.cursor} ${hd.corner ? 'z-20' : 'z-10'}`}
        />
      ))}
      <div
        className="flex items-center gap-2 px-3 py-2 border-b border-border cursor-move select-none"
        style={{ backgroundColor: 'var(--bg-elevated)' }}
        onPointerDown={onHeaderPointerDown}
        onPointerMove={onHeaderPointerMove}
        onPointerUp={onHeaderPointerUp}
        onPointerCancel={onHeaderPointerUp}
      >
        <Monitor size={14} className="shrink-0 text-muted" />
        <span className="shrink-0 text-[13px] font-medium text-text">Browser — live</span>
        <span
          className={`inline-block w-1.5 h-1.5 rounded-full ${frame ? 'animate-pulse' : ''}`}
          style={{ backgroundColor: frame ? 'var(--ok)' : 'var(--muted)' }}
          aria-hidden
        />
        {sessionName ? (
          <span
            className="flex-1 min-w-0 truncate text-[12px] text-muted"
            title={sessionName}
          >
            · {sessionName}
          </span>
        ) : (
          <div className="flex-1" />
        )}
        <button
          onPointerDown={e => e.stopPropagation()}
          onClick={onToggleSize}
          aria-label={expanded ? 'Shrink live browser view' : 'Expand live browser view'}
          title={expanded ? 'Shrink' : 'Expand'}
          className="relative z-30 p-1 rounded hover:bg-bg-hover text-muted hover:text-text transition-colors"
        >
          {expanded ? <Minimize2 size={13} /> : <Maximize2 size={13} />}
        </button>
        <button
          onPointerDown={e => e.stopPropagation()}
          onClick={() => setMode('chip')}
          aria-label="Minimize live browser view to corner"
          title="Minimize to corner"
          className="relative z-30 p-1 rounded hover:bg-bg-hover text-muted hover:text-text transition-colors"
        >
          <Minus size={14} />
        </button>
        <button
          onPointerDown={e => e.stopPropagation()}
          onClick={onClose}
          aria-label="Close live browser view"
          title="Close"
          className="relative z-30 p-1 rounded hover:bg-bg-hover text-muted hover:text-text transition-colors"
        >
          <X size={14} />
        </button>
      </div>

      <div className="relative bg-black flex-1 min-h-0 flex items-center justify-center">
        {frame ? (
          <img
            src={frame}
            alt="Live browser session"
            className="max-w-full max-h-full object-contain"
          />
        ) : (
          <div className="flex flex-col items-center gap-2 py-8 text-muted">
            <Monitor size={18} />
            <span className="text-[11px]">Waiting for the browser to take a screenshot…</span>
          </div>
        )}
      </div>

      <div className="px-3 py-1.5 border-t border-border text-[11px] text-muted flex items-center justify-between gap-2">
        <span className="truncate">
          Read-only mirror{rect.w > 380 ? ' of the headless browse session' : ''}
        </span>
        {lastTs && <span className="shrink-0">updated {new Date(lastTs).toLocaleTimeString()}</span>}
      </div>
    </div>
  )
}
