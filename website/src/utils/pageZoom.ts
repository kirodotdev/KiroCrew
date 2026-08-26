/** WebKit-only page-zoom suppression for the touch shell.
 *
 *  Why this file exists at all: the viewport meta in `index.html` and the root
 *  `touch-action` in `index.css` are the other two mechanisms, and neither reaches
 *  iOS. WebKit has deliberately ignored the viewport zoom keys **for user gestures**
 *  since iOS 10 (an accessibility decision), and it does not treat `touch-action` as
 *  governing page zoom either. What it does expose is the non-standard Safari gesture
 *  events, and cancelling `gesturestart` is the one path that stops a two-finger pinch
 *  from scaling the whole shell.
 *
 *  Cancelling the gesture events does NOT suppress touch or pointer events, so a
 *  surface that owns its own pinch keeps working — the image viewer scales its own
 *  transform off two pointers (see Lightbox in `MarkdownRenderer.tsx`) and is
 *  unaffected by anything here.
 *
 *  Scoped to coarse pointers. Desktop Safari raises the same events for a trackpad
 *  pinch, where zooming a page is a convention this has no business taking away.
 *
 *  For the policy and the accessibility trade behind it, see the page-zoom section of
 *  `website/docs/page-layout.md` — the authoritative copy.
 */

/** Safari's non-standard gesture event. `scale` is the pinch factor since the
 *  gesture began (1 = unchanged); the type is declared locally because no lib.dom
 *  definition exists for it. */
type SafariGestureEvent = Event & { scale?: number }

const GESTURE_EVENTS = ['gesturestart', 'gesturechange', 'gestureend'] as const

/** True when the primary input is touch. Read once at install time rather than
 *  per event: the listeners are cheap and a device does not change its pointer
 *  class mid-session (a laptop gaining a touchscreen would need a reload either
 *  way, and the desktop it protects is the common case). */
function isCoarsePointer(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia('(pointer: coarse)').matches
}

/** Install the suppression. Returns a teardown so tests (and any future
 *  runtime toggle) can remove the listeners; the app installs once and never
 *  uninstalls. Safe to call in a non-touch or non-WebKit environment — it
 *  simply installs nothing and hands back a no-op. */
export function installPageZoomSuppression(): () => void {
  if (typeof document === 'undefined' || !isCoarsePointer()) return () => {}

  const cancel = (e: Event) => e.preventDefault()

  // A pinch that STARTS as a one-finger scroll and gains a second finger does not
  // always raise `gesturestart` in WebKit, so the zoom slips through the handler
  // above. `touchmove` is the only signal left at that point. The guard is kept
  // as narrow as the hole it plugs — two or more contacts AND a scale WebKit has
  // already moved off 1 — so an ordinary two-finger scroll, and every gesture a
  // component reads through pointer events, are left alone.
  const cancelMultiTouchScale = (e: TouchEvent) => {
    const scale = (e as unknown as SafariGestureEvent).scale
    if (e.touches.length > 1 && typeof scale === 'number' && scale !== 1) e.preventDefault()
  }

  // `passive: false` is mandatory: listeners on document default to passive for
  // touch events in every current engine, and a passive listener's
  // preventDefault() is ignored (with a console warning) rather than honoured.
  for (const type of GESTURE_EVENTS) document.addEventListener(type, cancel, { passive: false })
  document.addEventListener('touchmove', cancelMultiTouchScale, { passive: false })

  return () => {
    for (const type of GESTURE_EVENTS) document.removeEventListener(type, cancel)
    document.removeEventListener('touchmove', cancelMultiTouchScale)
  }
}
