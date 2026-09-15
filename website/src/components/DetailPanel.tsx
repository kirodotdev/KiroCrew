import { safeSetItem } from '../utils/safeStorage'
import { useIsMobile } from '../hooks/useIsMobile'
import React, { useState, useEffect, useLayoutEffect, useRef } from 'react'
import { motion } from 'framer-motion'
import { X } from 'lucide-react'
import { Btn } from './ui'
import { usePointerDrag } from '../hooks/usePointerDrag'

import { i18nT } from '../i18n/t'
interface DetailPanelProps {
  title: React.ReactNode
  /** Optional glyph rendered immediately left of the title, identifying what
   * kind of thing the panel holds (e.g. the artifact `Component` icon). Kept
   * optional so callers that bake identity into `title` are unaffected. */
  icon?: React.ReactNode
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
   * the cap stays the viewport-only bound — no row measurement — so callers in
   * other layouts keep their behavior unchanged. */
  reserveWidth?: number
  storageKey?: string
  children: React.ReactNode
  /** Drop the default px-5 py-4 children padding. Used by panels that fill the viewport themselves (e.g. the diff). */
  noPadding?: boolean
  /** Override the header's default border-color/bg (e.g. to match an embedded editor). When provided, replaces the default `border-border bg-bg` styling. */
  headerClassName?: string
  /** Embedded mode: the panel fills its parent (width 100%, no resize handle,
   *  no left border, no width animation) instead of being a standalone
   *  right-docked panel. Used when the panel is a tab body inside SidePanel —
   *  the tab strip is the shell, so the panel only contributes its header
   *  (title + actions) and content. */
  embedded?: boolean
  /** Replace the default header rows (title + close + headerActions and the
   *  secondary row) with a single caller-provided bar. Used by tab bodies in
   *  SidePanel where the tab chip already owns identity + close, so the panel
   *  renders one minimal single-bar toolbar instead.
   *  When set, `title`, `headerActions`, and `secondaryHeaderActions` are
   *  ignored. */
  customHeader?: React.ReactNode
  /** Override the standalone panel's frame chrome. Default is the docked
   *  search panel's bare `border-l border-border bg-bg` slab; a page that
   *  wants the chat SidePanel's card frame (`mb-2 border-l border-t border-b
   *  rounded-l-xl`) passes that recipe here. Layout classes (flex column,
   *  h-full, overflow) stay fixed. */
  frameClassName?: string
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
  // reserve. Without one, keep the viewport-only bound (no row term).
  const rowCap = reserveWidth === undefined ? Infinity : rowWidth - reserveWidth
  return Math.min(rowCap, viewportCap)
}
const clampPanelWidth = (w: number, minWidth: number, rowWidth: number, reserveWidth?: number) =>
  Math.max(minWidth, Math.min(w, maxPanelWidth(rowWidth, reserveWidth)))

export default function DetailPanel({ title, icon, onClose, footer, headerActions, secondaryHeaderActions, initialWidth = 380, minWidth = 300, reserveWidth, storageKey, children, noPadding = false, headerClassName, embedded = false, customHeader, frameClassName }: DetailPanelProps) {
  const isMobile = useIsMobile()
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
  // Panel width captured at drag start; the resize delta is applied against it.
  const startWRef = useRef(0)
  // True while a resize-handle drag is in progress. The window `resize` listener
  // must not fight an active drag: a viewport change mid-drag would otherwise
  // clamp `width` down and, via onEnd below, persist that clamped value over the
  // width the user actually dragged to.
  const draggingRef = useRef(false)

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

  const drag = usePointerDrag({
    threshold: 6,
    onStart: () => {
      draggingRef.current = true
      startWRef.current = widthRef.current
    },
    onMove: ({ dx }) => {
      // The handle grows width as the pointer moves LEFT. The hook reports
      // dx = clientX - startX, so the raw target is `startW - dx`: drag left
      // (dx < 0) widens, drag right (dx > 0) narrows.
      //
      // Hard-clamp (no rubber-band): this panel is `shrink-0` inside an
      // `overflow-hidden` row, so the row/viewport cap is a LAYOUT INVARIANT
      // — letting width exceed it, even transiently, pushes content
      // off-screen. Rubber-band is for soft scroll edges, not overflow guards.
      setWidth(clampPanelWidth(startWRef.current - dx, minWidth, rowWidth(), reserveWidth))
    },
    onEnd: ({ committed }) => {
      // ALWAYS clear the suppression flag — the hook fires onEnd on every
      // pointer-up (even a sub-threshold tap on the thin handle), so the resize /
      // reserveWidth re-clamp guards can never get wedged on `true` and re-expose
      // the overflow. A tap did nothing else, so stop here.
      draggingRef.current = false
      if (!committed) return
      // Persist the dragged PREFERRED width (widthRef holds it: a resize
      // suppressed mid-drag never touched it) so returning to a larger screen
      // restores it — then clamp only the LIVE render down to what currently
      // fits. Clamping before persisting would lose the preferred width.
      const preferred = widthRef.current
      if (storageKey) safeSetItem(storageKey, String(preferred))
      setWidth(clampPanelWidth(preferred, minWidth, rowWidth(), reserveWidth))
    },
  })


  const body = (
    <>
      {!embedded && (
        /* Drag-to-resize splitter: pointer-only affordance (no meaningful
            keyboard gesture for a 6px handle); role="separator" is the correct
            ARIA role. */
        <div role="separator" aria-orientation="vertical" aria-label={i18nT('components.detailPanel.resize_panel')} className="absolute left-0 top-0 bottom-0 w-[6px] cursor-col-resize z-20 group/drag" style={{ touchAction: 'none' }} {...drag}>
          <div className="absolute left-0 top-0 bottom-0 w-[2px] transition-colors duration-200 bg-transparent group-hover/drag:bg-accent resize-accent" />
        </div>
      )}
      {customHeader ?? (<>
      <div className={`flex items-center justify-between px-3 h-12 shrink-0 border-b ${headerClassName ?? 'border-border'}`}>
        <div className="flex items-center gap-2 min-w-0">
          <Btn className="p-1.5 shrink-0" onClick={onClose} aria-label={i18nT('components.detailPanel.close_panel')} title={i18nT('components.detailPanel.close_panel')}><X size={16} /></Btn>
          {icon}
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
      </>)}
      <div className={noPadding ? "flex-1 overflow-hidden flex flex-col" : "flex-1 overflow-y-auto px-5 py-4 flex flex-col gap-4"}>
        {children}
      </div>
      {footer && (
        <div className="shrink-0 border-t border-border px-5 py-3 flex items-center justify-between">
          {footer}
        </div>
      )}
    </>
  )

  // Embedded: fill the parent (SidePanel tab body) — no resize handle, no left
  // border, no width animation. Only the header + content contribute.
  // While narrow, take the same full-width path `embedded` callers already use.
  // The alternative -- keeping the pixel width and lowering the floor -- cannot
  // work: `minWidth` is applied AFTER every cap in `clampPanelWidth`, so no
  // caller can configure its way below it.
  //
  // This alone is NOT sufficient, and that is the point of the caller-side
  // change that ships with it: dropping the pixel width means a caller that
  // wraps this panel in its OWN content-sized box (an animated `width: 'auto'`
  // with `shrink-0`) gets a box that hugs its content, and the panel comes out
  // NARROWER than the floor it replaced. Measured at a 390px row: 42px.
  if (embedded || isMobile) {
    return (
      <div className="h-full w-full min-w-0 bg-bg flex flex-col overflow-hidden relative">
        {body}
      </div>
    )
  }

  return (
    <motion.div
      ref={wrapperRef}
      initial={{ width: 0, opacity: 0 }}
      animate={{ width: 'auto', opacity: 1 }}
      exit={{ width: 0, opacity: 0 }}
      transition={{ width: { type: 'spring', bounce: 0, duration: 0.3 }, opacity: { duration: 0.12 } }}
      className="shrink-0 overflow-hidden h-full"
    >
      <div className={`shrink-0 ${frameClassName ?? 'border-l border-border bg-bg'} flex flex-col h-full overflow-hidden relative`} style={{ width, minWidth }}>
        {body}
      </div>
    </motion.div>
  )
}
