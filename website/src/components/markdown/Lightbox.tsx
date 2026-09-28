import React, { useCallback, useEffect, useRef, useState } from 'react'
import { Download, Minus, Plus, Search, X } from 'lucide-react'
import Clickable from '../Clickable'
import { DOUBLE_TAP_MS, DOUBLE_TAP_SLOP, DOUBLE_TAP_ZOOM, usePinchZoom } from '../../hooks/usePinchZoom'
import { isEditableTarget } from '../../utils/editableTarget'
import { downloadBlob } from '../../utils/download'
import { i18nT } from '../../i18n/t'
import { fmtNumber } from '../../i18n/format'

type LightboxImage = { src: string; alt: string }
type LightboxDetail = { images: LightboxImage[]; index: number }

/** Lightbox zoom (enlarge) bounds. `1` is fit-to-screen; each step scales the
 *  fit box up so the image can overflow the viewport and be panned via the
 *  scrollable overlay. */
const LIGHTBOX_ZOOM_MIN = 1
const LIGHTBOX_ZOOM_MAX = 5
const LIGHTBOX_ZOOM_STEP = 0.5

/** Swipe-to-dismiss (touch only, fit zoom only) tuning.
 *
 *  `SLOP` is the travel a touch must cover before the drag counts as a gesture
 *  rather than a tap — below it the tap-to-close/tap-a-button paths are left
 *  alone. `DISTANCE` is the release threshold that dismisses. `TRAVEL` is the
 *  distance mapped to the full dim/shrink feedback, so the backdrop fades and
 *  the image shrinks proportionally to how far the finger has pulled.
 *
 *  Distance is deliberately the ONLY dismiss criterion: a velocity path would
 *  buy a sub-`DISTANCE` flick and cost per-move rate tracking plus its own
 *  threshold, and the flick a user actually makes travels past `DISTANCE`
 *  anyway. */
const LIGHTBOX_DISMISS_SLOP = 8
const LIGHTBOX_DISMISS_DISTANCE = 96
const LIGHTBOX_DISMISS_TRAVEL = 260

/** Release threshold that commits a horizontal page, deliberately SHORTER than
 *  the dismiss distance. Paging is reversible — the opposite swipe comes back —
 *  while a dismiss destroys the viewing context, so it can commit on less travel.
 *  Distance is the only criterion, for the reason the dismiss path already gives:
 *  the flick a user actually makes travels past it anyway, and a velocity path
 *  would cost per-move rate tracking plus a second threshold. */
const LIGHTBOX_PAGE_DISTANCE = 64

/** How far a drag with nowhere to go still follows the finger: the ends of the
 *  set, and the upward direction of the dismiss drag. Both are gestures that must
 *  not commit but must not feel dead either — a silent no-op reads as broken. */
const LIGHTBOX_RUBBER_BAND_DIVISOR = 4

/** Derive a download filename for a lightbox image. Local images are served
 *  as `/api/file-raw?path=<abs>`, so prefer the basename of that path; for
 *  other URLs fall back to the pathname basename, then the alt text. */
function lightboxFilename(image: LightboxImage): string {
  try {
    const u = new URL(image.src, window.location.href)
    const p = u.searchParams.get('path')
    const fromPath = p ? p.split(/[\\/]/).pop() : ''
    if (fromPath) return fromPath
    const fromName = u.pathname.split('/').pop()
    if (fromName && fromName.includes('.')) return decodeURIComponent(fromName)
  } catch {
    // image.src is not a parseable URL (e.g. a bare data: payload) -- fall through.
  }
  const altName = (image.alt || '').trim().replace(/[^\w.-]+/g, '_').replace(/^_+|_+$/g, '')
  return altName || 'image'
}

/** Download the given lightbox image to the user's machine. Fetches the
 *  already-served bytes (same-origin for /api/file-raw, or data:/blob:) into a
 *  blob and triggers a browser download. If the fetch is blocked (e.g. a
 *  cross-origin remote image with no CORS), falls back to opening the image in
 *  a new tab so the user can save it manually. */
async function downloadLightboxImage(image: LightboxImage): Promise<void> {
  const name = lightboxFilename(image)
  try {
    const res = await fetch(image.src)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const blob = await res.blob()
    downloadBlob(blob, name)
  } catch {
    window.open(image.src, '_blank', 'noopener,noreferrer')
  }
}

/** Build the lightbox payload for an image click. The set is "all images
 *  inside the nearest [data-image-scope] ancestor"; for markdown messages
 *  that's a MarkdownRenderer instance (one per chat message), and for the
 *  chat-input thumbnail strip it's the strip's outer div. */
export function dispatchLightbox(target: HTMLImageElement): void {
  const scope = target.closest('[data-image-scope]') as HTMLElement | null
  let detail: LightboxDetail = { images: [{ src: target.src, alt: target.alt }], index: 0 }
  if (scope) {
    const els = Array.from(scope.querySelectorAll<HTMLImageElement>('img[data-lightbox-image]'))
    if (els.length > 0) {
      detail = {
        images: els.map(el => ({ src: el.src, alt: el.alt })),
        index: Math.max(0, els.indexOf(target)),
      }
    }
  }
  window.dispatchEvent(new CustomEvent('lightbox', { detail }))
}

/** Lightbox overlay -- mount once in the app, listens for 'lightbox' custom
 *  events. Escape closes; ArrowLeft/ArrowRight navigate within the image set
 *  (clamped at the ends). Accepts both the structured { images, index }
 *  payload and the legacy { src, alt } single-image shape. */
export function Lightbox() {
  const [state, setState] = useState<LightboxDetail | null>(null)
  // A fresh mirror of `state`, so handlers subscribed once per open — the global
  // keydown listener's download shortcut, and the paging gesture's read of the
  // set's size and position — see the current value rather than a stale closure.
  const stateRef = useRef<LightboxDetail | null>(null)
  stateRef.current = state
  const imgRef = useRef<HTMLImageElement>(null)
  /** The overlay root. Separate from `imgRef` because the transform target is the
   *  image while the surface a user perceives as "the viewer" is the whole
   *  backdrop — see the `containRef` note on the pinch hook. */
  const overlayRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef({ startX: 0, startY: 0, baseX: 0, baseY: 0, moved: 0, active: false, dragging: false })
  const [dragging, setDragging] = useState(false)
  // `suppressClick` makes the click that follows a real gesture a no-op, so a
  // spring-back or a finished pinch does not also close via the backdrop handler.
  // Declared before the hook because `onPinchEnd` sets it.
  const suppressClickRef = useRef(false)
  // Armed only when setPointerCapture THROWS on pointer-down (see the <img>
  // handler below). An uncaptured drag gets no retargeting and no
  // lostpointercapture (capture never existed), so once the pointer leaves
  // the image the element hears nothing again: without a fallback,
  // `dragging` stays true and the lightbox sits in pan mode with no contact
  // held. Window-level up/cancel listeners for that specific pointerId are
  // the one place the terminal event can still be heard — the same
  // acquisition-side fallback the shared usePointerDrag hook arms.
  //
  // Keyed by pointerId, NOT a single slot: two uncaptured contacts can be live
  // at once (a pan pointer whose capture threw, then a second image press that
  // seats a pinch), and each must keep its own window listeners until ITS OWN
  // up/cancel. A single slot let a second arm — or any disarm — evict a live
  // pointer's listeners, stranding that contact in the pinch tracker so the
  // next touch seated a ghost pinch. Each entry disposes on its pointer's own
  // terminal event, on a same-pointer re-press, and on unmount.
  const panFallbacksRef = useRef<Map<number, () => void>>(new Map())
  const armPanFallback = useCallback((pointerId: number, dispose: () => void) => {
    // A stale entry for this same pointerId (id reuse across gestures) is
    // replaced; sibling pointers' fallbacks are left untouched.
    panFallbacksRef.current.get(pointerId)?.()
    panFallbacksRef.current.set(pointerId, dispose)
  }, [])
  const disposePanFallback = useCallback((pointerId: number) => {
    const dispose = panFallbacksRef.current.get(pointerId)
    if (dispose) { dispose(); panFallbacksRef.current.delete(pointerId) }
  }, [])
  const disposeAllPanFallbacks = useCallback(() => {
    panFallbacksRef.current.forEach(dispose => dispose())
    panFallbacksRef.current.clear()
  }, [])
  // Zoom (enlarge) factor and pan offset for the current image, plus the pinch
  // gesture that drives them. 1 = fit-to-screen; larger values scale the fit box
  // up so the image overflows the viewport and can be panned. Reset to fit
  // whenever the shown image changes (see effect below).
  //
  // The gesture lives in `usePinchZoom` because this is not the only surface that
  // owns its own magnification — `DiagramLightbox` is the other, and shipping the
  // math twice is how the two diverge.
  const {
    zoom, setZoom, pan, setPan, pinching, zoomRef, clampPan,
    trackPointerDown, trackPointerMove, trackPointerUp, reset: resetZoom,
  } = usePinchZoom({
    targetRef: imgRef,
    // Claim the gesture anywhere in the overlay, not just over the `<img>`. A
    // small image leaves most of the full-screen backdrop unclaimed, and a pinch
    // there would fall through to browser page zoom: the viewer is fit-invariant
    // so nothing appears to happen, and the user closes it to find the dashboard
    // behind it at a different zoom with no visible cause.
    containRef: overlayRef,
    // Only while an image is open. This component mounts ONCE for the app's
    // lifetime and returns null when closed, so without this a non-passive
    // `wheel` listener would sit on `window` forever — making the compositor wait
    // on main-thread dispatch for every scroll in the app, viewer or not.
    enabled: state !== null,
    min: LIGHTBOX_ZOOM_MIN,
    max: LIGHTBOX_ZOOM_MAX,
    onPinchStart: () => {
      // Both one-finger gestures lose their claim: a dismiss-drag would read the
      // pinch's vertical component as pull-to-close, and the <img> pan would fight
      // the scale over the same two contacts.
      abortSwipeRef.current?.()
      lastTapRef.current = { t: 0, x: 0, y: 0 }
      const d = dragRef.current
      if (d.active) { d.active = false; d.dragging = false; setDragging(false) }
      // Tear down the PAN, but do NOT disarm the window fallback here. When the
      // pan pointer was uncaptured its release only reaches the window listener,
      // and that listener is the sole place its contact gets dropped from the
      // pinch hook. Disarming it now — while that pointer is still down — would
      // strand the contact if the finger then lifts outside the overlay, seating
      // a ghost pinch on the next single touch. Each pointer's fallback lives in
      // the per-pointer registry and self-disposes on that pointer's own
      // up/cancel (onWindowEnd -> terminatePan(pointerId)), a same-pointer
      // re-press, or unmount.
    },
    // A finished pinch is not a tap. Without this the click synthesised after the
    // last finger lifts reaches the backdrop handler and closes the viewer the
    // user just spent the gesture zooming into.
    onPinchEnd: () => { suppressClickRef.current = true },
  })
  const zoomIn = useCallback(() => setZoom(z => Math.min(LIGHTBOX_ZOOM_MAX, +(z + LIGHTBOX_ZOOM_STEP).toFixed(2))), [setZoom])
  const zoomOut = useCallback(() => setZoom(z => Math.max(LIGHTBOX_ZOOM_MIN, +(z - LIGHTBOX_ZOOM_STEP).toFixed(2))), [setZoom])
  /** `onPinchStart` fires from inside the hook, which is constructed before
   *  `abortSwipe` exists — the ref is what lets the callback reach the later
   *  definition without reordering the whole component around it. */
  const abortSwipeRef = useRef<(() => void) | null>(null)

  // End a drag on either pointerup OR pointercancel (touch/pen interrupted, or
  // capture lost) so `active`/`dragging` never latch on with no contact held.
  // Shared by the element handlers and the window fallback below, so an
  // uncaptured drag terminates down the same path.
  //
  // `pointerId` disposes only THAT pointer's window fallback (a sibling
  // uncaptured contact keeps its own until its own terminal event). Omitted =
  // no fallback to drop (e.g. a captured drag, whose element handler ends it).
  const terminatePan = useCallback((pointerId?: number) => {
    const d = dragRef.current
    d.active = false
    if (pointerId !== undefined) disposePanFallback(pointerId)
    if (d.dragging) { d.dragging = false; setDragging(false) }
  }, [disposePanFallback])
  const endDrag = useCallback((e: React.PointerEvent<HTMLImageElement>) => {
    const d = dragRef.current
    if (d.active) { try { e.currentTarget.releasePointerCapture(e.pointerId) } catch { /* no capture */ } }
    terminatePan(e.pointerId)
  }, [terminatePan])
  // If the component unmounts mid-uncaptured-drag, no window listener may
  // outlive it — drop every armed fallback.
  useEffect(() => disposeAllPanFallbacks, [disposeAllPanFallbacks])
  // ── one-finger overlay drag: dismiss down, page sideways ─────────────────
  // A touch drag anywhere over the overlay locks an AXIS once it crosses the
  // slop, then either pulls the image down to dismiss or sideways to page
  // through the set. Both are gated to fit zoom (above it the same drag already
  // means "pan", handled on the <img>) and to non-mouse pointers, so the desktop
  // click-backdrop-to-close behaviour is untouched.
  //
  // The horizontal half exists because the set was otherwise reachable only from
  // ArrowLeft/ArrowRight: on a phone every image after the first was unreachable.
  // Owning that axis is safe here for a reason worth stating — the app-wide nav
  // drawer claims horizontal drags everywhere else, and yields only to an element
  // whose computed `touch-action` is `none`. The overlay's `touch-none` (already
  // there to take page zoom) is what makes this gesture ours rather than a fight.
  const [swipeY, setSwipeY] = useState(0)
  const [swipeX, setSwipeX] = useState(0)
  const [swiping, setSwiping] = useState(false)
  // `engaged` flips once SLOP is crossed, fixing `axis` for the rest of the
  // gesture; until then it is still a candidate tap. Locking the axis is what
  // keeps a diagonal drag from both dimming the backdrop and paging.
  // `suppressClick` makes the click that follows a real drag a no-op, so a
  // spring-back does not also close via the backdrop handler.
  //
  // `pointerId` is what keeps a PINCH from reading as a drag. Every finger
  // raises its own pointerdown/move/up, so without an id the second finger
  // rewrites the gesture's origin and a two-finger zoom attempt walks the image
  // down and closes the viewer the user was zooming into.
  const swipeRef = useRef({ pointerId: -1, startX: 0, startY: 0, active: false, engaged: false, axis: '' as '' | 'x' | 'y' })
  // Abandon the in-flight gesture and return the image to rest. Used by the
  // multi-touch bail-out and by pointercancel.
  const abortSwipe = useCallback(() => {
    const s = swipeRef.current
    s.active = false
    if (s.engaged) { s.engaged = false; setSwiping(false); suppressClickRef.current = true }
    s.axis = ''
    s.pointerId = -1
    setSwipeY(0)
    setSwipeX(0)
  }, [])
  // Publish it for the hook's `onPinchStart`, which is constructed above this.
  abortSwipeRef.current = abortSwipe

  // ── double-tap to zoom (touch) ───────────────────────────────────────────
  const lastTapRef = useRef({ t: 0, x: 0, y: 0 })
  const onDoubleTap = useCallback((e: React.PointerEvent<HTMLElement>): boolean => {
    if (e.pointerType === 'mouse') return false
    if ((e.target as HTMLElement | null)?.closest('button')) return false
    const now = Date.now()
    const last = lastTapRef.current
    const isDouble = now - last.t < DOUBLE_TAP_MS && Math.hypot(e.clientX - last.x, e.clientY - last.y) < DOUBLE_TAP_SLOP
    lastTapRef.current = { t: now, x: e.clientX, y: e.clientY }
    if (!isDouble) return false
    lastTapRef.current = { t: 0, x: 0, y: 0 }
    suppressClickRef.current = true
    abortSwipe()
    const d = dragRef.current
    if (d.active) { d.active = false; d.dragging = false; setDragging(false) }
    // A double-tap resets the whole gesture (zoom snaps), so every uncaptured
    // contact's fallback goes with it.
    disposeAllPanFallbacks()
    if (zoomRef.current > LIGHTBOX_ZOOM_MIN) {
      setZoom(LIGHTBOX_ZOOM_MIN)
      setPan({ x: 0, y: 0 })
      return true
    }
    const cx = window.innerWidth / 2
    const cy = window.innerHeight / 2
    const z = DOUBLE_TAP_ZOOM
    setZoom(z)
    setPan(clampPan((e.clientX - cx) * (1 - z), (e.clientY - cy) * (1 - z), z))
    return true
  }, [abortSwipe, clampPan, setPan, setZoom, zoomRef, disposeAllPanFallbacks])
  // ── pinch-to-zoom (touch, two fingers) ───────────────────────────────────
  // Browser page zoom is off on touch across the shell (viewport meta in
  // index.html, root `touch-action` in index.css, `gesturestart` suppression in
  // utils/pageZoom.ts), because magnifying a fixed-height app shell strands the
  // user in a layout with no scroll axis to reach what moved off-screen. This
  // viewer is the surface where magnifying IS the point, so it owns the gesture
  // instead of borrowing the browser's — and it drives the SAME `zoom` state the
  // toolbar and keyboard drive, so pan clamping, the reset on image change and
  // the `zoomed` cursor keep working with no parallel code path.
  //
  // The gesture itself is `usePinchZoom` (contact tracking, focal anchoring, pan
  // clamping); what stays here is only the part that is specific to THIS viewer —
  // which one-finger gesture yields to a pinch, and what a finished pinch means
  // for the click that follows.
  const onOverlayPointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    // Every click in this subtree is preceded by a pointerdown, so clearing here
    // is what keeps the flag from latching when the click is swallowed upstream
    // (the <img> stops propagation, so the overlay's own handler never runs).
    suppressClickRef.current = false
    if (e.pointerType === 'mouse') return
    // Record the contact BEFORE any bail-out below. A pinch is only knowable from
    // two tracked contacts, and every branch that follows returns early — so
    // recording last would mean the second finger is never seen in exactly the
    // cases (drag live, already zoomed) a pinch is most likely to start from.
    // The hook records the contact and, when a pinch seats, calls `onPinchStart`
    // (which drops the swipe and the <img> drag) and returns true.
    if (trackPointerDown(e)) {
      lastTapRef.current = { t: 0, x: 0, y: 0 }
      return
    }
    // Toolbar taps must stay taps — never start a gesture from a control.
    if ((e.target as HTMLElement | null)?.closest('button')) return
    // A consumed double-tap changes zoom synchronously through the live ref's
    // owner but React publishes that new value on the next render. Return now
    // instead of consulting the still-fit ref and re-arming swipe-to-dismiss.
    if (onDoubleTap(e)) return
    if (zoomRef.current > LIGHTBOX_ZOOM_MIN) return // the <img> pan owns this gesture
    swipeRef.current = { pointerId: e.pointerId, startX: e.clientX, startY: e.clientY, active: true, engaged: false, axis: '' }
  }, [trackPointerDown, onDoubleTap, zoomRef])
  const onOverlayPointerMove = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    // A live pinch consumes the move (scale + focal-anchored pan).
    if (trackPointerMove(e)) return
    const s = swipeRef.current
    if (!s.active || e.pointerId !== s.pointerId) return
    const dx = e.clientX - s.startX
    const dy = e.clientY - s.startY
    const cur = stateRef.current
    const total = cur ? cur.images.length : 0
    if (!s.engaged) {
      if (Math.hypot(dx, dy) < LIGHTBOX_DISMISS_SLOP) return
      const axis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y'
      // Paging needs somewhere to go. A single image has no neighbours, so the
      // horizontal gesture is dropped outright rather than rubber-banding an
      // image whose set cannot move — which is what it did before paging existed.
      if (axis === 'x' && total < 2) { s.active = false; return }
      s.axis = axis
      s.engaged = true
      setSwiping(true)
      lastTapRef.current = { t: 0, x: 0, y: 0 }
    }
    if (s.axis === 'x') {
      // Mid-set the image tracks the finger 1:1; at either end it is rubber-banded,
      // which is what says "no more images this way" instead of looking broken.
      const blocked = (dx > 0 && cur?.index === 0) || (dx < 0 && cur?.index === total - 1)
      setSwipeX(blocked ? dx / LIGHTBOX_RUBBER_BAND_DIVISOR : dx)
      return
    }
    // Downward travel tracks the finger 1:1; upward is rubber-banded, since
    // pulling up is not a dismiss but should not feel dead either.
    setSwipeY(dy >= 0 ? dy : dy / LIGHTBOX_RUBBER_BAND_DIVISOR)
  }, [trackPointerMove])
  const endSwipe = useCallback((e: React.PointerEvent<HTMLDivElement>, cancelled: boolean) => {
    // The hook drops the contact and ends the pinch on the FIRST lift (rather than
    // the last), which is what stops the finger still down from being re-read as a
    // one-finger pan whose origin is wherever the pinch happened to leave it.
    trackPointerUp(e)
    const s = swipeRef.current
    if (!s.active || e.pointerId !== s.pointerId) return
    if (cancelled) { abortSwipe(); return }
    s.active = false
    s.pointerId = -1
    if (!s.engaged) return
    s.engaged = false
    const axis = s.axis
    s.axis = ''
    setSwiping(false)
    suppressClickRef.current = true
    if (axis === 'x') {
      // Clamped the same way the arrow keys are, so a drag that reached the
      // threshold at either end springs back instead of paging off the set.
      const dx = e.clientX - s.startX
      if (dx <= -LIGHTBOX_PAGE_DISTANCE) {
        setState(cur => (cur && cur.index < cur.images.length - 1 ? { ...cur, index: cur.index + 1 } : cur))
      } else if (dx >= LIGHTBOX_PAGE_DISTANCE) {
        setState(cur => (cur && cur.index > 0 ? { ...cur, index: cur.index - 1 } : cur))
      }
      setSwipeX(0)
      return
    }
    if (e.clientY - s.startY > LIGHTBOX_DISMISS_DISTANCE) setState(null)
    else setSwipeY(0)
  }, [abortSwipe, trackPointerUp])
  const onOverlayPointerUp = useCallback((e: React.PointerEvent<HTMLDivElement>) => endSwipe(e, false), [endSwipe])
  const onOverlayPointerCancel = useCallback((e: React.PointerEvent<HTMLDivElement>) => endSwipe(e, true), [endSwipe])
  const onOverlayClick = useCallback(() => {
    if (suppressClickRef.current) { suppressClickRef.current = false; return }
    setState(null)
  }, [])
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as Partial<LightboxDetail> & Partial<LightboxImage> | undefined
      if (!detail) { setState(null); return }
      if (Array.isArray(detail.images) && detail.images.length > 0) {
        const raw = Number.isInteger(detail.index) ? (detail.index as number) : 0
        const idx = Math.max(0, Math.min(raw, detail.images.length - 1))
        setState({ images: detail.images, index: idx })
      } else if (typeof detail.src === 'string') {
        setState({ images: [{ src: detail.src, alt: detail.alt || '' }], index: 0 })
      }
    }
    window.addEventListener('lightbox', handler)
    return () => window.removeEventListener('lightbox', handler)
  }, [])
  const isOpen = state !== null
  // Reset the zoom whenever the lightbox opens/closes or the shown image
  // changes, so each image starts fit-to-screen rather than inheriting the
  // previous one's zoom. The dismiss offset resets with it — a viewer reopened
  // right after a spring-back must not start half-dragged.
  useEffect(() => {
    setSwipeY(0)
    setSwipeX(0)
    setSwiping(false)
    lastTapRef.current = { t: 0, x: 0, y: 0 }
    swipeRef.current.active = false
    swipeRef.current.engaged = false
    swipeRef.current.axis = ''
    swipeRef.current.pointerId = -1
    // Contacts do not survive the viewer: closing mid-pinch (or an image change
    // driven from the keyboard while fingers are down) must not leave a stale
    // pair behind for the next open to scale against. `resetZoom` clears the
    // contact map and the pinch baseline along with the zoom and pan.
    resetZoom()
  }, [isOpen, state?.index, resetZoom])
  // On any zoom change, recentre at fit and otherwise re-clamp the existing pan
  // to the new (smaller/larger) bounds — zooming out must not strand the image
  // off-screen. Runs post-layout, so offsetWidth already reflects the new box.
  useEffect(() => { setPan(p => (zoom <= LIGHTBOX_ZOOM_MIN ? { x: 0, y: 0 } : clampPan(p.x, p.y))) }, [zoom, clampPan, setPan])
  useEffect(() => {
    if (!isOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        setState(null)
      } else if (e.key === 'ArrowLeft') {
        e.preventDefault()
        setState(s => (s && s.index > 0 ? { ...s, index: s.index - 1 } : s))
      } else if (e.key === 'ArrowRight') {
        e.preventDefault()
        setState(s => (s && s.index < s.images.length - 1 ? { ...s, index: s.index + 1 } : s))
      } else if ((e.key === '+' || e.key === '=') && !isEditableTarget(e) && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault()
        zoomIn()
      } else if ((e.key === '-' || e.key === '_') && !isEditableTarget(e) && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault()
        zoomOut()
      } else if (e.key === '0' && !isEditableTarget(e) && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault()
        setZoom(LIGHTBOX_ZOOM_MIN)
      } else if ((e.key === 'd' || e.key === 'D') && !isEditableTarget(e)) {
        e.preventDefault()
        const cur = stateRef.current
        if (cur) void downloadLightboxImage(cur.images[cur.index])
      }
    }
    // CAPTURE phase, matching DiagramLightbox: dialog panels (Modal, the Radix
    // ui/dialog family) stop bubble-phase keydown propagation so the page's
    // global shortcuts don't fire under them, and this viewer opens ABOVE
    // those dialogs (a README image inside SkillBrowserModal / McpBrowserModal
    // etc). With focus still inside the dialog panel, a bubble-phase listener
    // here never sees the key — arrows/zoom go dead while Escape still works.
    // Capture runs before any panel handler. It also fixes Escape ordering
    // over a Modal: this handler's preventDefault now lands BEFORE Modal's
    // bubble-phase window listener, so its defaultPrevented skip keeps the
    // modal open and Escape closes only the viewer.
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [isOpen, zoomIn, zoomOut, setZoom])
  if (!state) return null
  const img = state.images[state.index]
  const zoomed = zoom > LIGHTBOX_ZOOM_MIN
  // 0 → untouched, 1 → full dismiss feedback. Downward pull only; the
  // rubber-banded upward direction keeps the backdrop at full strength.
  const swipeProgress = Math.min(1, Math.max(0, swipeY) / LIGHTBOX_DISMISS_TRAVEL)
  // The axis is locked for the whole gesture, so only one of the two offsets is
  // ever live. Paging carries no shrink and no backdrop fade: it is not a
  // dismiss, and dimming on the way to another image of the same set would read
  // as the viewer leaving.
  const swipeTransform = swipeX !== 0
    ? `translateX(${swipeX.toFixed(1)}px)`
    : swipeY !== 0
      ? `translateY(${swipeY.toFixed(1)}px) scale(${(1 - swipeProgress * 0.15).toFixed(3)})`
      : undefined
  return (
    <Clickable
      ref={overlayRef}
      className={`fixed inset-0 z-[9999] bg-black/80 flex items-center justify-center overflow-hidden cursor-pointer touch-none ${swiping ? '' : 'transition-colors duration-200'}`}
      // Inline background wins over the class only while a drag is live, so the
      // default (and every non-touch) render keeps the plain bg-black/80 paint.
      style={swipeProgress > 0 ? { backgroundColor: `rgba(0, 0, 0, ${(0.8 * (1 - swipeProgress * 0.75)).toFixed(3)})` } : undefined}
      onClick={onOverlayClick}
      onPointerDown={onOverlayPointerDown}
      onPointerMove={onOverlayPointerMove}
      onPointerUp={onOverlayPointerUp}
      onPointerCancel={onOverlayPointerCancel}
    >
      {/* Inner wrapper centres the image; when enlarged, the image is dragged
          around via a translate transform (see pointer handlers) rather than
          scrollbars — a flex-centred overflow container can't scroll to its
          hidden top/left edges, so drag-to-pan is the reliable mechanism.
          This wrapper also carries the swipe-to-dismiss offset, kept off the
          <img> so it composes with (rather than fights) the pan/zoom transform. */}
      <div
        className={`flex items-center justify-center w-full h-full ${swiping ? '' : 'transition-transform duration-200'}`}
        style={swipeTransform ? { transform: swipeTransform } : undefined}
      >
        {/* The image is a drag surface for panning when zoomed; zoom itself
            lives in the toolbar + keyboard. A plain click only stops the
            backdrop-close from firing (clicking the image should not dismiss
            the viewer). Escape / the toolbar buttons are the keyboard paths,
            so this presentational <img> needs no key handler. */}
        {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions */}
        <img
          ref={imgRef}
          src={img.src}
          alt={img.alt}
          draggable={false}
          className={`select-none object-contain rounded-lg shadow-2xl ${dragging || pinching ? '' : 'transition-transform duration-150'} ${zoomed ? (dragging ? 'cursor-grabbing' : 'cursor-grab') : 'cursor-default'}`}
          style={{ maxWidth: '90vw', maxHeight: '90vh', transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`, transformOrigin: 'center' }}
          onDragStart={e => e.preventDefault()}
          onPointerDown={e => {
            if (zoom <= LIGHTBOX_ZOOM_MIN) return // nothing to pan at fit
            e.preventDefault()
            // A re-press of THIS pointerId replaces its own stale fallback; a
            // sibling uncaptured contact keeps its listeners (it ends on its
            // own up/cancel), so a second image press no longer strands the
            // first contact.
            disposePanFallback(e.pointerId)
            let captured = true
            try { e.currentTarget.setPointerCapture(e.pointerId) } catch { captured = false }
            if (!captured) {
              // Capture is best-effort for liveness (the pan still starts),
              // but the gesture must remain terminable: without retargeting,
              // a release outside the image never reaches it.
              const pointerId = e.pointerId
              const onWindowEnd = (ev: PointerEvent) => {
                if (ev.pointerId !== pointerId) return
                // The pan pointer-down bubbles to `onOverlayPointerDown`, which
                // records this contact in the pinch hook. On the captured path
                // `endSwipe` drops it via `trackPointerUp`; on this uncaptured
                // path the overlay never hears the release, so without this the
                // contact latches forever and the next single touch seats a
                // spurious pinch against the stale point. Drop it first, then
                // terminate the pan for this pointer only.
                trackPointerUp(ev)
                terminatePan(pointerId)
              }
              window.addEventListener('pointerup', onWindowEnd)
              window.addEventListener('pointercancel', onWindowEnd)
              armPanFallback(pointerId, () => {
                window.removeEventListener('pointerup', onWindowEnd)
                window.removeEventListener('pointercancel', onWindowEnd)
              })
            }
            dragRef.current = { startX: e.clientX, startY: e.clientY, baseX: pan.x, baseY: pan.y, moved: 0, active: true, dragging: false }
          }}
          onPointerMove={e => {
            const d = dragRef.current
            if (!d.active) return
            const dx = e.clientX - d.startX
            const dy = e.clientY - d.startY
            d.moved = Math.max(d.moved, Math.hypot(dx, dy))
            if (d.moved > 4 && !d.dragging) {
              d.dragging = true
              setDragging(true)
              lastTapRef.current = { t: 0, x: 0, y: 0 }
            }
            setPan(clampPan(d.baseX + dx, d.baseY + dy))
          }}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
          onClick={e => { e.stopPropagation() }}
        />
      </div>
      {/* Control cluster sits on its own translucent, blurred pill so the
          white icons stay legible even when a light/enlarged image is panned
          up behind the toolbar. */}
      <div className="fixed top-safe-offset-4 right-safe-offset-4 flex items-center gap-0.5 rounded-full bg-black/60 backdrop-blur-md ring-1 ring-white/15 shadow-lg px-1 py-1">
        {/* Zoom segment: − / reset (magnifier) / + always visible as a group. */}
        <button
          aria-label={i18nT('components.markdownRenderer.zoom_out')}
          title={i18nT('components.markdownRenderer.zoom_out')}
          disabled={zoom <= LIGHTBOX_ZOOM_MIN}
          className="text-white/90 hover:text-white p-1.5 rounded-full hover:bg-white/15 transition-colors disabled:opacity-40 disabled:hover:bg-transparent"
          onClick={(e) => { e.stopPropagation(); zoomOut() }}
        >
          <Minus className="lucide-inline" aria-hidden="true" />
        </button>
        <button
          aria-label={i18nT('components.markdownRenderer.reset_zoom')}
          title={i18nT('components.markdownRenderer.reset_zoom')}
          disabled={zoom <= LIGHTBOX_ZOOM_MIN}
          className="text-white/90 hover:text-white p-1.5 rounded-full hover:bg-white/15 transition-colors disabled:opacity-40 disabled:hover:bg-transparent"
          onClick={(e) => { e.stopPropagation(); setZoom(LIGHTBOX_ZOOM_MIN) }}
        >
          <Search className="lucide-inline" aria-hidden="true" />
        </button>
        <button
          aria-label={i18nT('components.markdownRenderer.zoom_in')}
          title={i18nT('components.markdownRenderer.zoom_in')}
          disabled={zoom >= LIGHTBOX_ZOOM_MAX}
          className="text-white/90 hover:text-white p-1.5 rounded-full hover:bg-white/15 transition-colors disabled:opacity-40 disabled:hover:bg-transparent"
          onClick={(e) => { e.stopPropagation(); zoomIn() }}
        >
          <Plus className="lucide-inline" aria-hidden="true" />
        </button>
        <span className="w-px h-5 bg-white/20 mx-0.5" aria-hidden="true" />
        <button
          aria-label={i18nT('components.markdownRenderer.download_image')}
          title={i18nT('components.markdownRenderer.download_d')}
          className="text-white/90 hover:text-white p-1.5 rounded-full hover:bg-white/15 transition-colors"
          onClick={(e) => { e.stopPropagation(); void downloadLightboxImage(img) }}
        >
          <Download className="lucide-inline" aria-hidden="true" />
        </button>
        <button
          aria-label={i18nT('components.markdownRenderer.close')}
          className="text-white/90 hover:text-white p-1.5 rounded-full hover:bg-white/15 transition-colors"
          onClick={() => setState(null)}
        >
          <X className="lucide-inline" aria-hidden="true" />
        </button>
      </div>
      {/* Position in the set. Without it the swipe is invisible — nothing on
          screen says a set exists, which is how every image after the first came
          to be unreachable on touch while the keyboard could still reach them.
          `aria-live` carries the same fact to a screen reader as the image
          changes, which nothing did before. Rendered LAST so the overlay's first
          child stays the wrapper the drag transform is written to. Matches the
          toolbar's own treatment because the scrim is dark in every theme. */}
      {state.images.length > 1 && (
        <div
          className="fixed bottom-safe-offset-4 left-1/2 -translate-x-1/2 rounded-full bg-black/60 backdrop-blur-md ring-1 ring-white/15 shadow-lg px-3 py-1 text-sm text-white/90 tabular-nums"
          aria-live="polite"
        >
          {i18nT('components.markdownRenderer.image_position', {
            index: fmtNumber(state.index + 1),
            total: fmtNumber(state.images.length),
          })}
        </div>
      )}
    </Clickable>
  )
}
