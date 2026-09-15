import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { motion, useReducedMotion } from 'framer-motion'
import { Minus, Plus, Search, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { useDialogFocusTrap } from '../hooks/useDialogFocusTrap'
import { DOUBLE_TAP_MS, DOUBLE_TAP_SLOP, DOUBLE_TAP_ZOOM, usePinchZoom } from '../hooks/usePinchZoom'
import { Btn, IconButton } from './ui'

/** Diagram zoom bounds. `1` is fit-to-viewport. The ceiling is higher than the
 *  image viewer's because the content is vector: a mermaid label at 8px in a
 *  fit-scaled diagram needs more magnification to read than a photo detail does,
 *  and scaling an SVG costs no resolution. */
const DIAGRAM_ZOOM_MIN = 1
const DIAGRAM_ZOOM_MAX = 8
const DIAGRAM_ZOOM_STEP = 0.5
/** Travel a one-finger drag must cover before it counts as a pan rather than a
 *  tap — below it the double-tap and click-out paths are left alone. */
const DRAG_SLOP = 6

function isEditableTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null
  if (!el || typeof el.tagName !== 'string') return false
  return el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable === true
}

/**
 * Full-viewport viewer for an inline-rendered SVG diagram (mermaid).
 *
 * Why not the existing image `Lightbox` (MarkdownRenderer): that viewer is
 * `<img src>`-based, and mermaid emits SVG whose text labels live in
 * `<foreignObject>` HTML — browsers refuse to paint foreignObject content in an
 * image context, so serializing the diagram to a data URI would blank every
 * label. The SVG has to stay live DOM. Why not `Modal`: it is card chrome
 * (max-width, padded body, header bar) that fights the fit-to-viewport goal and
 * carries no focus trap. This component composes the shared dialog primitives
 * instead: `useDialogFocusTrap` (focus in on open, restore on close, Escape,
 * Tab cycling) + portal + `role="dialog"`.
 *
 * Scaling: mermaid pins its SVG to the source column via an inline
 * `max-width` and gives it a `viewBox`, so clearing the pin and setting
 * width/height to 100% lets `preserveAspectRatio` (default `xMidYMid meet`)
 * scale the vector output to fit the viewport without distortion or raster
 * blur. An SVG without a viewBox cannot fit-scale, so it keeps its natural
 * size and the host scrolls — the content is never cropped either way.
 *
 * The markup is inserted with `createContextualFragment`, the same path (and
 * therefore the same sanitization posture) as the inline `MermaidBlock`
 * rendering the identical string: mermaid output under
 * `securityLevel: 'strict'`, never raw user HTML.
 *
 * Magnification: this viewer owns its own zoom (`usePinchZoom`) because page
 * zoom is off on touch shell-wide, and fit-to-viewport is exactly the state in
 * which a diagram's labels are smallest. Without it a phone user has no gesture
 * left that magnifies a diagram at all — which is what shipped briefly, and is
 * the reason the gesture lives in a shared hook now rather than in the image
 * viewer alone.
 */
export default function DiagramLightbox({ svg, onClose }: { svg: string; onClose: () => void }) {
  const { t } = useTranslation()
  const dialogRef = useRef<HTMLDivElement>(null)
  const hostRef = useRef<HTMLDivElement>(null)
  const reduceMotion = useReducedMotion()
  useDialogFocusTrap(dialogRef, onClose)

  // A finished pinch or double-tap is not a click-out. Without this the click
  // synthesised after the last finger lifts reaches the dismiss handler below and
  // closes the viewer the user just spent the gesture zooming into.
  const suppressClickRef = useRef(false)
  /** True once the SVG has been fit-scaled to the viewport, i.e. it carried a
   *  `viewBox`. Only then is zoom/pan the right mechanism: a no-viewBox SVG keeps
   *  its natural size and the surrounding scroller reaches its edges instead. */
  const [fitted, setFitted] = useState(false)
  const [dragging, setDragging] = useState(false)
  const dragRef = useRef({ id: -1, startX: 0, startY: 0, baseX: 0, baseY: 0, active: false })
  const { zoom, setZoom, pan, setPan, pinching, clampPan, trackPointerDown, trackPointerMove, trackPointerUp, reset } =
    usePinchZoom({
      targetRef: hostRef,
      // Claim a trackpad gesture anywhere in the overlay, not just over the SVG:
      // the padding around a fit-scaled diagram is visually part of the viewer.
      containRef: dialogRef,
      // Only while the SVG is actually fit-scaled. A natural-size (no-viewBox) SVG
      // gets no transform, so claiming its pinch would do nothing AND suppress the
      // browser page zoom — which, unlike on fit-scaled content, genuinely does
      // magnify it. Not binding leaves that fallback intact.
      enabled: fitted,
      min: DIAGRAM_ZOOM_MIN,
      max: DIAGRAM_ZOOM_MAX,
      onPinchEnd: () => { suppressClickRef.current = true },
    })

  // Reset to fit whenever the diagram changes, so a zoom from the previous one is
  // never inherited by the next.
  useEffect(() => { reset() }, [svg, reset])

  // Re-clamp the pan after a zoom change: the pannable box is a function of the
  // zoom, so shrinking back toward fit must pull an out-of-range pan back in.
  useEffect(() => { setPan(p => (zoom <= DIAGRAM_ZOOM_MIN ? { x: 0, y: 0 } : clampPan(p.x, p.y))) }, [zoom, clampPan, setPan])

  /** Double-tap toggles fit <-> DOUBLE_TAP, anchored where the user tapped so the
   *  label they aimed at is what they get. The shell withholds the browser's own
   *  double-tap zoom, and this is one of the two surfaces where users still reach
   *  for it. */
  const lastTapRef = useRef({ t: 0, x: 0, y: 0 })
  const onTap = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (e.pointerType === 'mouse') return
    const now = Date.now()
    const last = lastTapRef.current
    const isDouble = now - last.t < DOUBLE_TAP_MS && Math.hypot(e.clientX - last.x, e.clientY - last.y) < DOUBLE_TAP_SLOP
    lastTapRef.current = { t: now, x: e.clientX, y: e.clientY }
    if (!isDouble) return
    lastTapRef.current = { t: 0, x: 0, y: 0 }
    suppressClickRef.current = true
    if (zoom > DIAGRAM_ZOOM_MIN) { setZoom(DIAGRAM_ZOOM_MIN); setPan({ x: 0, y: 0 }); return }
    // Anchor the zoom-in at the tap: the point tapped is at content-local offset
    // `(tap - centre)`, and holding it there under the new scale is what puts the
    // label the user aimed at under their finger rather than at the centre.
    const cx = window.innerWidth / 2
    const cy = window.innerHeight / 2
    const z = DOUBLE_TAP_ZOOM
    setZoom(z)
    setPan(clampPan((e.clientX - cx) * (1 - z), (e.clientY - cy) * (1 - z), z))
  }, [zoom, setZoom, setPan, clampPan])

  const onPointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    suppressClickRef.current = false
    // A pinch owns the gesture when it seats; neither the tap nor the pan path
    // must also run. The first contact already ran through `onTap` and left a
    // tap candidate, so clear it — otherwise a single tap shortly after the
    // pinch lifts completes a double-tap the user never made.
    if (trackPointerDown(e)) {
      dragRef.current.active = false
      setDragging(false)
      lastTapRef.current = { t: 0, x: 0, y: 0 }
      return
    }
    onTap(e)
    // One-finger drag pans a zoomed diagram. Without it the zoom is a trap: the
    // gesture magnifies the centre and nothing reaches the edges, which is the
    // same shape of dead end this whole fix is about.
    if (zoom <= DIAGRAM_ZOOM_MIN) return
    if ((e.target as HTMLElement | null)?.closest('button')) return
    dragRef.current = { id: e.pointerId, startX: e.clientX, startY: e.clientY, baseX: pan.x, baseY: pan.y, active: true }
  }, [trackPointerDown, onTap, zoom, pan])

  const onPointerMove = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (trackPointerMove(e)) return
    const d = dragRef.current
    if (!d.active || e.pointerId !== d.id) return
    const dx = e.clientX - d.startX
    const dy = e.clientY - d.startY
    // Below the slop the gesture is still a candidate tap; committing to a drag
    // early would eat the double-tap.
    if (!dragging && Math.hypot(dx, dy) < DRAG_SLOP) return
    if (!dragging) {
      setDragging(true)
      // A committed drag is not a tap, so it must not leave a tap candidate
      // behind: two quick flick-pans starting near the same point would then
      // read as a double-tap and reset the very zoom and pan being navigated.
      // Clearing at the COMMIT point is what keeps a real double-tap working —
      // its two touches stay under the slop by definition and never reach here.
      lastTapRef.current = { t: 0, x: 0, y: 0 }
    }
    suppressClickRef.current = true
    setPan(clampPan(d.baseX + dx, d.baseY + dy))
  }, [trackPointerMove, dragging, clampPan, setPan])

  const onPointerUp = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    trackPointerUp(e)
    const d = dragRef.current
    if (d.active && e.pointerId === d.id) { d.active = false; if (dragging) setDragging(false) }
  }, [trackPointerUp, dragging])

  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    const range = document.createRange()
    range.selectNodeContents(host)
    range.deleteContents()
    host.appendChild(range.createContextualFragment(svg))
    const el = host.querySelector('svg')
    if (el && el.getAttribute('viewBox')) {
      // Clear mermaid's column-width pin and fill the host; viewBox +
      // preserveAspectRatio do the aspect-correct fitting.
      el.style.maxWidth = 'none'
      el.style.maxHeight = 'none'
      el.style.width = '100%'
      el.style.height = '100%'
      setFitted(true)
    } else {
      setFitted(false)
    }
  }, [svg])

  const zoomIn = useCallback(
    () => setZoom(z => Math.min(DIAGRAM_ZOOM_MAX, +(z + DIAGRAM_ZOOM_STEP).toFixed(2))),
    [setZoom],
  )
  const zoomOut = useCallback(
    () => setZoom(z => Math.max(DIAGRAM_ZOOM_MIN, +(z - DIAGRAM_ZOOM_STEP).toFixed(2))),
    [setZoom],
  )
  const resetZoom = useCallback(() => {
    setZoom(DIAGRAM_ZOOM_MIN)
    setPan({ x: 0, y: 0 })
  }, [setZoom, setPan])

  // While the viewer is open, claim Escape so an enclosing <Modal> does not also
  // dismiss itself on the same keypress. Also handle zoom keyboard shortcuts (+, -, 0).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
      } else if (!fitted || isEditableTarget(e.target) || e.metaKey || e.ctrlKey || e.altKey) {
        return
      } else if (e.key === '+' || e.key === '=') {
        e.preventDefault()
        zoomIn()
      } else if (e.key === '-' || e.key === '_') {
        e.preventDefault()
        zoomOut()
      } else if (e.key === '0') {
        e.preventDefault()
        resetZoom()
      }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [fitted, zoomIn, zoomOut, resetZoom])

  return createPortal(
    <motion.div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-label={t('components.diagramLightbox.diagram_viewer')}
      className="fixed inset-0 z-[9999] bg-bg/95 backdrop-blur-sm flex flex-col"
      initial={reduceMotion ? false : { opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.15 }}
      // Click-out dismissal: any click that is not on the diagram itself (or on
      // the controls, which handle themselves) closes the viewer. Escape is
      // handled by useDialogFocusTrap plus the preventDefault claim above.
      onClick={e => {
        // A pinch, drag or double-tap just finished — that click is gesture residue.
        if (suppressClickRef.current) { suppressClickRef.current = false; return }
        const el = e.target as HTMLElement
        if (!el.closest('svg') && !el.closest('button')) onClose()
      }}
    >
      <div className="flex items-center justify-end gap-2 px-4 h-12 shrink-0">
        {fitted && zoom > DIAGRAM_ZOOM_MIN && (
          <Btn
            aria-label={t('components.markdownRenderer.reset_zoom')}
            title={t('components.markdownRenderer.reset_zoom')}
            className="px-2 py-1.5 border-none text-muted hover:text-text"
            onClick={(e) => { e.stopPropagation(); resetZoom() }}
          >
            <Search className="lucide-inline" aria-hidden="true" />
            <span>{t('components.markdownRenderer.reset_zoom')}</span>
          </Btn>
        )}
        <button
          aria-label={t('components.diagramLightbox.close')}
          title={t('components.diagramLightbox.close')}
          className="p-1.5 rounded-md text-muted hover:text-text hover:bg-bg-hover transition-colors cursor-pointer"
          onClick={onClose}
        >
          <X className="lucide-inline" aria-hidden="true" />
        </button>
      </div>
      {fitted && (
        <div className="fixed bottom-safe-offset-6 left-1/2 -translate-x-1/2 z-10 flex items-center gap-1 rounded-full bg-bg-elevated/90 backdrop-blur-md ring-1 ring-border shadow-lg px-2 py-1">
          <IconButton
            aria-label={t('components.markdownRenderer.zoom_out')}
            title={t('components.markdownRenderer.zoom_out')}
            disabled={zoom <= DIAGRAM_ZOOM_MIN}
            className="p-1.5 rounded-full disabled:opacity-40 disabled:hover:bg-transparent disabled:cursor-default"
            onClick={(e) => { e.stopPropagation(); zoomOut() }}
          >
            <Minus className="lucide-inline" aria-hidden="true" />
          </IconButton>
          <IconButton
            aria-label={t('components.markdownRenderer.zoom_in')}
            title={t('components.markdownRenderer.zoom_in')}
            disabled={zoom >= DIAGRAM_ZOOM_MAX}
            className="p-1.5 rounded-full disabled:opacity-40 disabled:hover:bg-transparent disabled:cursor-default"
            onClick={(e) => { e.stopPropagation(); zoomIn() }}
          >
            <Plus className="lucide-inline" aria-hidden="true" />
          </IconButton>
        </div>
      )}
      {/* min-h-0 lets the flex child shrink to the viewport. overflow-auto is
          retained as the escape hatch for a no-viewBox SVG kept at natural size —
          that case is NOT fit-scaled, so scrolling (not zoom) is what reaches its
          edges, and `touch-none` is therefore applied to the inner wrapper only
          when `fitted`.
          `touch-action` resolves over the whole ANCESTOR CHAIN up to the element
          that would scroll, so keeping it off this div is not enough: on the
          dialog root it disables touch panning here just as effectively, and a
          wrapper-only assertion cannot detect that. `DiagramLightbox.zoom.test.tsx`
          asserts across the chain for that reason — unlike `Lightbox`, which owns
          no scroller and correctly suppresses gestures at its own root. */}
      <div className="flex-1 min-h-0 overflow-auto px-6 pb-6">
        <div
          ref={hostRef}
          className={`w-full h-full flex items-center justify-center ${fitted ? 'touch-none' : ''}`}
          style={fitted ? {
            transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
            // No transition during a gesture: a pinch already produces a frame per
            // move, and easing between them lags the fingers. Nor for a user who
            // opted out of motion — a double-tap animates a 2.5x scale, which is
            // exactly the kind of movement that setting exists to suppress. The
            // zoom itself still happens; only the easing to it is dropped.
            transition: pinching || dragging || reduceMotion ? 'none' : 'transform 150ms ease-out',
            cursor: zoom > DIAGRAM_ZOOM_MIN ? (dragging ? 'grabbing' : 'grab') : undefined,
          } : undefined}
          onPointerDown={fitted ? onPointerDown : undefined}
          onPointerMove={fitted ? onPointerMove : undefined}
          onPointerUp={fitted ? onPointerUp : undefined}
          onPointerCancel={fitted ? onPointerUp : undefined}
        />
      </div>
    </motion.div>,
    document.body
  )
}
