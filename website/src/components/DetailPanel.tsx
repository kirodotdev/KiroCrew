import { safeSetItem } from '../utils/safeStorage'
import React, { useState, useEffect, useLayoutEffect, useCallback, useRef } from 'react'
import { motion } from 'framer-motion'
import { X } from 'lucide-react'
import { Btn } from './ui'

interface DetailPanelProps {
  title: React.ReactNode
  onClose: () => void
  footer?: React.ReactNode
  headerActions?: React.ReactNode
  /** Optional second toolbar rendered below the main header. Used to
   * separate identity/view actions (close, refresh, fullscreen, etc.)
   * from contextual editor controls (mode toggle, save, formatting).
   * Only renders when provided. */
  secondaryHeaderActions?: React.ReactNode
  initialWidth?: number
  minWidth?: number
  /** Opt-in: horizontal space (px) to keep clear for the panel's left-side
   * siblings (e.g. the session sidebar + a usable chat-pane minimum) so the
   * panel never grows past its flex row and overflows the `overflow-hidden`
   * container. Callers in a shared, shrinkable row (the chat surface) pass a
   * live, sidebar-aware value (see `panelReserve` in ChatPage). When omitted,
   * the cap stays the historical viewport-only bound — no row measurement — so
   * callers in other layouts keep their prior behavior unchanged. */
  reserveWidth?: number
  storageKey?: string
  children: React.ReactNode
  /** Drop the default px-5 py-4 children padding. Used by panels that fill the viewport themselves (e.g. Monaco diff). */
  noPadding?: boolean
  /** Override the header's default border-color/bg (e.g. to match an embedded editor). When provided, replaces the default `border-border bg-bg` styling. */
  headerClassName?: string
}

/**
 * Upper bound for the panel width. The panel is `shrink-0` inside an
 * `overflow-hidden` flex row it shares with its left-side siblings (the session
 * sidebar and the chat pane; the app nav rail is one level up, outside this
 * row). The cap must be the panel's room in THAT ROW minus the space those
 * siblings need (`reserveWidth`), not a fraction of the whole window: a
 * window-based cap lets the panel exceed the row, collapse the chat pane to
 * zero, overflow off-screen, and reflow its content past the viewport edge.
 * `rowWidth` is measured from the panel's parent element; when it isn't
 * measurable yet (initial mount / jsdom) it is Infinity so the row term drops
 * out and only the viewport bound applies. The row term is also skipped
 * entirely when a caller supplies no `reserveWidth` (opt-in). A `60% of the
 * viewport` bound is kept as a secondary ceiling so a huge reserve can't force
 * an unusably narrow-vs-screen panel. Matches the drag cap in onDragStart.
 *
 * Residual: when `rowWidth - reserveWidth < minWidth`, the `minWidth` floor in
 * `clampPanelWidth` wins, so the panel can be wider than its row and overflow
 * again. Only reachable on a viewport narrower than `minWidth + reserveWidth`
 * (e.g. a very wide sidebar on a small window) where the layout is already
 * unusable; the floor is preferred over an unreadably narrow panel.
 */
const maxPanelWidth = (rowWidth: number, reserveWidth?: number) => {
  const viewportCap = typeof window !== 'undefined' ? Math.round(window.innerWidth * 0.6) : Infinity
  // Opt-in: only apply the row-minus-reserve cap when a caller supplies a
  // reserve. Without one, keep the historical viewport-only bound (no row term).
  const rowCap = reserveWidth === undefined ? Infinity : rowWidth - reserveWidth
  return Math.min(rowCap, viewportCap)
}
const clampPanelWidth = (w: number, minWidth: number, rowWidth: number, reserveWidth?: number) =>
  Math.max(minWidth, Math.min(w, maxPanelWidth(rowWidth, reserveWidth)))

export default function DetailPanel({ title, onClose, footer, headerActions, secondaryHeaderActions, initialWidth = 380, minWidth = 300, reserveWidth, storageKey, children, noPadding = false, headerClassName }: DetailPanelProps) {
  // Outer wrapper ref, used to measure the panel's flex row (its parent) so the
  // width cap tracks the actual available room rather than the whole viewport.
  const wrapperRef = useRef<HTMLDivElement>(null)
  // Measured width of the panel's flex row (its parent). A non-positive measure
  // means the row isn't laid out yet (initial mount, or jsdom) — return Infinity
  // so the row term drops out and the cap degrades to the viewport-only bound
  // (the old behavior), rather than subtracting the reserve from a bogus width.
  const rowWidth = () => {
    const w = wrapperRef.current?.parentElement?.getBoundingClientRect().width
    return w && w > 0 ? w : Infinity
  }
  const [width, setWidth] = useState(() => {
    // The row isn't mounted at first render, so seed against the viewport-only
    // cap (Infinity row); the layout effect re-clamps against the real row
    // width once measurable, before paint.
    if (storageKey) {
      const v = parseInt(localStorage.getItem(storageKey) || '', 10)
      if (!isNaN(v) && v >= minWidth) return clampPanelWidth(v, minWidth, Infinity, reserveWidth)
    }
    return clampPanelWidth(initialWidth, minWidth, Infinity, reserveWidth)
  })
  const widthRef = useRef(width)
  widthRef.current = width
  const moveRef = useRef<((ev: MouseEvent) => void) | null>(null)
  const upRef = useRef<(() => void) | null>(null)
  // True while a resize-handle drag is in progress. The window `resize` listener
  // must not fight an active drag: a viewport change mid-drag would otherwise
  // clamp `width` down and, via onUp below, persist that clamped value over the
  // width the user actually dragged to.
  const draggingRef = useRef(false)

  useEffect(() => {
    return () => {
      if (moveRef.current) document.removeEventListener('mousemove', moveRef.current)
      if (upRef.current) document.removeEventListener('mouseup', upRef.current)
    }
  }, [])

  // Re-clamp on viewport shrink so a persisted width that's wider than the
  // current row can never leave the right-edge header actions off-screen or
  // push content past the viewport edge. Only clamps down (never auto-grows),
  // and is suppressed while a drag is in progress (see draggingRef) so it can't
  // clobber the in-flight drag value; the preferred width stays in localStorage
  // and is restored (re-clamped) on a larger screen.
  useEffect(() => {
    const onResize = () => {
      if (draggingRef.current) return
      setWidth((w) => clampPanelWidth(w, minWidth, rowWidth(), reserveWidth))
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [minWidth, reserveWidth])

  // Re-clamp against the real row width once the row is mounted, and whenever
  // `reserveWidth` changes (e.g. the sidebar is drag-resized). A sidebar drag
  // shrinks the panel's available room but fires no window `resize` event, so
  // the window listener above never sees it. Suppressed mid-drag for the same
  // reason. Layout effect so the correction lands before paint (no flash of an
  // over-wide panel on first mount).
  useLayoutEffect(() => {
    if (draggingRef.current) return
    setWidth((w) => clampPanelWidth(w, minWidth, rowWidth(), reserveWidth))
  }, [minWidth, reserveWidth])

  const onDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    draggingRef.current = true
    const startX = e.clientX; const startW = widthRef.current
    const onMove = (ev: MouseEvent) => {
      setWidth(clampPanelWidth(startW + (startX - ev.clientX), minWidth, rowWidth(), reserveWidth))
    }
    const onUp = () => {
      draggingRef.current = false
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      moveRef.current = null; upRef.current = null
      // Persist the width the user dragged to (their preferred width) BEFORE
      // re-clamping the render. A resize that arrived mid-drag was suppressed,
      // so widthRef.current still holds the dragged value; this keeps the
      // preferred width in localStorage for restore (re-clamped) on a larger
      // screen rather than saving a resize-clamped value.
      if (storageKey) safeSetItem(storageKey, String(widthRef.current))
      // Re-clamp the live render once to the current row, in case a resize
      // arrived mid-drag, so the panel can't stay wider than its row.
      setWidth((w) => clampPanelWidth(w, minWidth, rowWidth(), reserveWidth))
    }
    moveRef.current = onMove; upRef.current = onUp
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }, [minWidth, reserveWidth, storageKey])

  return (
    <motion.div
      ref={wrapperRef}
      initial={{ width: 0, opacity: 0 }}
      animate={{ width: 'auto', opacity: 1 }}
      exit={{ width: 0, opacity: 0 }}
      transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
      className="shrink-0 overflow-hidden h-full"
    >
      <div className="shrink-0 border-l border-border bg-bg flex flex-col h-full overflow-hidden relative" style={{ width, minWidth }}>
        {/* Drag-to-resize splitter: pointer-only affordance (no meaningful
            keyboard gesture for a 6px handle); role="separator" is the correct
            ARIA role. */}
        {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
        <div role="separator" aria-orientation="vertical" aria-label="Resize panel" className="absolute left-0 top-0 bottom-0 w-[6px] cursor-col-resize z-20 group/drag" onMouseDown={onDragStart}>
          <div className="absolute left-0 top-0 bottom-0 w-[2px] transition-colors duration-200 bg-transparent group-hover/drag:bg-accent" />
        </div>
        <div className={`flex items-center justify-between px-3 h-12 shrink-0 border-b ${headerClassName ?? 'border-border'}`}>
          <div className="flex items-center gap-2 min-w-0">
            <Btn className="p-1.5 shrink-0" onClick={onClose} aria-label="Close panel" title="Close panel"><X size={16} /></Btn>
            <span className="text-base font-semibold text-text-strong truncate">{title}</span>
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            {headerActions}
          </div>
        </div>
        {secondaryHeaderActions && (
          <div className={`flex items-center justify-between px-3 h-10 shrink-0 border-b ${headerClassName ?? 'border-border'} bg-bg-elevated/30`}>
            {secondaryHeaderActions}
          </div>
        )}
        <div className={noPadding ? "flex-1 overflow-hidden flex flex-col" : "flex-1 overflow-y-auto px-5 py-4 flex flex-col gap-4"}>
          {children}
        </div>
        {footer && (
          <div className="shrink-0 border-t border-border px-5 py-3 flex items-center justify-between">
            {footer}
          </div>
        )}
      </div>
    </motion.div>
  )
}
