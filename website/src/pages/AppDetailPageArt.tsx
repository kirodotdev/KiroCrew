/**
 * The App Store detail page's art: the hero banner and the screenshot gallery.
 *
 * Both render an app's store-listing images with a local-art second chance:
 * when a primary (usually registry) URL fails to load, an installed app's own
 * same-origin route is tried once, and an image that fails both ways unmounts
 * rather than leaving an empty box. `HeroBanner` renders the wide banner;
 * `ScreenshotGallery` renders the thumbnail strip with its edge cues and the
 * lightbox (prev/next, click-to-dismiss, and pinch / double-tap zoom through
 * `usePinchZoom`). `AppDetailPage.tsx` renders both and re-exports them, and
 * callers import them from there. This module never imports it.
 */
import { useEffect, useState, useCallback, useRef } from 'react'
import { useReducedMotion } from 'framer-motion'
import { ChevronLeft, ChevronRight, X } from 'lucide-react'
import { DOUBLE_TAP_MS, DOUBLE_TAP_SLOP, DOUBLE_TAP_ZOOM, usePinchZoom } from '../hooks/usePinchZoom'
import { useScrollEdges } from '../hooks/useScrollEdges'
import { i18nT } from '../i18n/t'
import { classifyManifestArt } from '../components/appstore/useHeroArt'
import AppStoreDialog from '../components/appstore/AppStoreDialog'

type ScreenshotFailureState = {
  screensKey: string
  fallbacksKey: string
  primary: ReadonlySet<number>
  fallback: ReadonlySet<number>
}

const NO_SCREENSHOT_FAILURES: ReadonlySet<number> = new Set()

function recordScreenshotFailure(
  previous: ScreenshotFailureState,
  screensKey: string,
  fallbacksKey: string,
  index: number,
  tier: 'primary' | 'fallback',
): ScreenshotFailureState {
  // A failure belongs to the generation that rendered the image. The URL lists
  // are re-armed during render (below), so a handler still holding an older
  // generation's keys is by definition superseded: drop it rather than let it
  // resurrect a set the current generation already cleared.
  if (previous.screensKey !== screensKey || previous.fallbacksKey !== fallbacksKey) {
    return previous
  }
  const primary = new Set<number>(previous.primary)
  const fallback = new Set<number>(previous.fallback)
  if (tier === 'primary') primary.add(index)
  else fallback.add(index)
  return { screensKey, fallbacksKey, primary, fallback }
}

// Exported for tests: the per-index latch guards (self-match, '' placeholder
// skip) are not all reachable through the page once the call site gates the
// fallback list on a registry-supplied primary.

/** Screenshot zoom bounds. `1` is fit-to-viewport. The ceiling matches the
 *  image viewer's (5), not the diagram viewer's (8): a screenshot is raster
 *  pixels, and 8x would only blur it, while a vector diagram's labels need the
 *  deeper zoom to become readable. */
const SCREENSHOT_ZOOM_MIN = 1
const SCREENSHOT_ZOOM_MAX = 5
/** Travel a one-finger drag must cover before it counts as a pan rather than a
 *  tap — below it the double-tap path is left alone. */
const DRAG_SLOP = 6

export function ScreenshotGallery({ screenshots, fallbacks }: { screenshots: string[]; fallbacks?: string[] }) {
  const [selected, setSelected] = useState<number | null>(null)
  const [attachEdges, edges, remeasure] = useScrollEdges<HTMLDivElement>()
  // Both lists are TYPED string[] but can arrive as arbitrary JSON at
  // runtime: the registry-only branch spreads the raw (third-party) registry
  // row into the view model, so a malformed row declaring `screenshots: {}`
  // or a colliding `screenshotsFallback` key reaches this component as-is,
  // and a bare `.join`/`.map` would take the whole page down. Same
  // unknown-typed defensiveness as installedArtList (GPT review finding on
  // #6886; the `screenshots` case pre-existed as a `.map` crash).
  const screenList: string[] = Array.isArray(screenshots) ? screenshots : []
  const fallbackList: string[] = Array.isArray(fallbacks) ? fallbacks : []
  // Per-thumbnail failure latches: a thumbnail whose primary errored swaps to
  // ITS OWN fallback; one
  // whose fallback errored too is hidden — the pre-#6864 terminal state.
  // Per-index state, not one flag for the strip: one unreachable asset must
  // not blank its neighbours. `fallbacks` is optional so untouched callers
  // stay default-inert (the contract #6865 locked for AppIcon).
  const screensKey = screenList.join('\n')
  const fallbacksKey = fallbackList.join('\n')
  // The scroller keeps its box while its thumbnails change (list refetch, a
  // thumbnail resolving or going terminal), so only this remeasure refreshes
  // the edge cue; per-image onLoad covers late width settling. The remeasure
  // effect itself lives below, after the failure latches it depends on.
  // Re-arm the latches during render rather than in a passive effect. An image
  // rendered for a new generation can fail BEFORE an effect would run, and the
  // effect's reset would then erase that real failure and re-show the dead URL.
  // Adjusting state while rendering re-arms before the new <img> is committed,
  // and — unlike binding failures to the URL text alone — it clears on EVERY
  // transition, so returning to an earlier list (theme flip back, refetch)
  // retries instead of restoring a stale failure. A primary-list change re-arms
  // both latch sets; a fallback-only change re-arms only the fallback latches.
  const [failures, setFailures] = useState<ScreenshotFailureState>(() => ({
    screensKey,
    fallbacksKey,
    primary: NO_SCREENSHOT_FAILURES,
    fallback: NO_SCREENSHOT_FAILURES,
  }))
  if (failures.screensKey !== screensKey) {
    setFailures({
      screensKey,
      fallbacksKey,
      primary: NO_SCREENSHOT_FAILURES,
      fallback: NO_SCREENSHOT_FAILURES,
    })
  } else if (failures.fallbacksKey !== fallbacksKey) {
    setFailures({
      screensKey,
      fallbacksKey,
      primary: failures.primary,
      fallback: NO_SCREENSHOT_FAILURES,
    })
  }
  const primaryFailed = failures.screensKey === screensKey
    ? failures.primary
    : NO_SCREENSHOT_FAILURES
  const fallbackFailed = failures.screensKey === screensKey
    && failures.fallbacksKey === fallbacksKey
    ? failures.fallback
    : NO_SCREENSHOT_FAILURES

  // Refresh the edge cue when the thumbnail set changes. A thumbnail going
  // terminal UNMOUNTS its button (resolvedAt -> ''), shrinking scrollWidth
  // with no onLoad, no box resize and — at scrollLeft 0 — no scroll event, so
  // the failure-latch sizes are a dep alongside screensKey: an unmount that
  // makes the strip fit must clear a now-stale right-edge fade.
  const failedCount = primaryFailed.size + fallbackFailed.size
  useEffect(() => { remeasure() }, [screensKey, failedCount, remeasure])

  // ── screenshot magnification (issue #6162) ────────────────────────────────
  // This lightbox is the third full-viewport magnify overlay, bound by the same
  // own-your-zoom rule as the image viewer and DiagramLightbox (narrow-viewport.md):
  // page zoom is off on touch shell-wide, so a phone user has no other way to
  // inspect a fit-scaled screenshot. Unlike those two, the surface also owns
  // prev/next navigation and click-to-dismiss, so the shared hook supplies only
  // the gesture math while the page keeps those interactions.
  // The viewer's root, shared with AppStoreDialog: that component moves focus
  // in once when the viewer opens (not on every prev/next, which would pull
  // focus off the arrow button just pressed), keeps Tab inside, and hands focus
  // back to the thumbnail on close.
  const dialogRef = useRef<HTMLDivElement>(null)
  const closeViewer = useCallback(() => setSelected(null), [])
  const imgRef = useRef<HTMLImageElement | null>(null)
  const reduceMotion = useReducedMotion()
  // A finished pinch or double-tap synthesises a click; without suppression it
  // reaches the backdrop handler and closes the viewer the user just zoomed
  // into (mirrors DiagramLightbox's `suppressClickRef`).
  const suppressClickRef = useRef(false)
  const lastTapRef = useRef({ t: 0, x: 0, y: 0 })
  const [dragging, setDragging] = useState(false)
  const dragRef = useRef({ id: -1, startX: 0, startY: 0, baseX: 0, baseY: 0, active: false })
  const open = selected !== null
  const {
    zoom, setZoom, pan, setPan, pinching, clampPan,
    trackPointerDown, trackPointerMove, trackPointerUp, reset,
  } = usePinchZoom({
    targetRef: imgRef,
    // Claim a trackpad gesture anywhere in the overlay, not just over the
    // image: around a small or portrait screenshot most of the backdrop is
    // visually part of the viewer (mirrors DiagramLightbox).
    containRef: dialogRef,
    // Only while the lightbox is open. It unmounts its viewers by returning
    // null elsewhere, but the component itself stays mounted and a non-passive
    // `wheel` listener would otherwise sit on `window` for the page's lifetime.
    enabled: open,
    min: SCREENSHOT_ZOOM_MIN,
    max: SCREENSHOT_ZOOM_MAX,
    onPinchEnd: () => { suppressClickRef.current = true },
  })

  // Reset to fit whenever the selected screenshot changes, so a zoom applied to
  // one screenshot is never inherited by the next (mirrors DiagramLightbox's
  // reset on `svg` change).
  useEffect(() => { reset() }, [selected, reset])

  // Next or Previous unmounts when the set of visible shots past it runs out:
  // the user stepped to an end (`selected`), or a shot failed to load and was
  // hidden (`failedCount`, `screensKey`). Removing the focused button drops
  // focus to <body>, and the dialog's own onKeyDown owns Escape and the
  // arrows, so hand focus back to the dialog in that case only.
  useEffect(() => {
    const dialog = dialogRef.current
    if (dialog && !dialog.contains(document.activeElement)) dialog.focus()
  }, [selected, failedCount, screensKey])

  // Re-clamp the pan after a zoom change: the pannable box is a function of the
  // zoom, so shrinking back toward fit must pull an out-of-range pan back in.
  useEffect(() => { setPan(p => (zoom <= SCREENSHOT_ZOOM_MIN ? { x: 0, y: 0 } : clampPan(p.x, p.y))) }, [zoom, clampPan, setPan])

  /** Double-tap toggles fit <-> DOUBLE_TAP, anchored where the user tapped so
   *  the detail they aimed at is what they get. */
  const onTap = useCallback((e: React.PointerEvent<HTMLImageElement>) => {
    if (e.pointerType === 'mouse') return
    const now = Date.now()
    const last = lastTapRef.current
    const isDouble = now - last.t < DOUBLE_TAP_MS && Math.hypot(e.clientX - last.x, e.clientY - last.y) < DOUBLE_TAP_SLOP
    lastTapRef.current = { t: now, x: e.clientX, y: e.clientY }
    if (!isDouble) return
    lastTapRef.current = { t: 0, x: 0, y: 0 }
    suppressClickRef.current = true
    if (zoom > SCREENSHOT_ZOOM_MIN) { setZoom(SCREENSHOT_ZOOM_MIN); setPan({ x: 0, y: 0 }); return }
    const cx = window.innerWidth / 2
    const cy = window.innerHeight / 2
    const z = DOUBLE_TAP_ZOOM
    setZoom(z)
    setPan(clampPan((e.clientX - cx) * (1 - z), (e.clientY - cy) * (1 - z), z))
  }, [zoom, setZoom, setPan, clampPan])

  const onImgPointerDown = useCallback((e: React.PointerEvent<HTMLImageElement>) => {
    // Clear here (not on the wrapper) because the <img> stops propagation, so
    // a wrapper-level clear would never run for an image click.
    suppressClickRef.current = false
    // A pinch owns the gesture when it seats; neither the tap nor the pan path
    // must also run. The first contact already ran through `onTap` and left a
    // tap candidate, so clear it.
    if (trackPointerDown(e)) {
      lastTapRef.current = { t: 0, x: 0, y: 0 }
      dragRef.current.active = false
      setDragging(false)
      return
    }
    onTap(e)
    if (zoom <= SCREENSHOT_ZOOM_MIN) return
    dragRef.current = { id: e.pointerId, startX: e.clientX, startY: e.clientY, baseX: pan.x, baseY: pan.y, active: true }
  }, [trackPointerDown, onTap, zoom, pan])

  const onImgPointerMove = useCallback((e: React.PointerEvent<HTMLImageElement>) => {
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
      lastTapRef.current = { t: 0, x: 0, y: 0 }
    }
    suppressClickRef.current = true
    setPan(clampPan(d.baseX + dx, d.baseY + dy))
  }, [trackPointerMove, dragging, clampPan, setPan])

  const onImgPointerUp = useCallback((e: React.PointerEvent<HTMLImageElement>) => {
    trackPointerUp(e)
    const d = dragRef.current
    if (d.active && e.pointerId === d.id) { d.active = false; if (dragging) setDragging(false) }
  }, [trackPointerUp, dragging])

  // The effective (post-swap) src for one index — '' when the index is
  // terminal (primary failed and no usable fallback: absent, an '' alignment
  // placeholder, identical to the failed primary, or itself failed). Shared
  // by the thumbnail AND the lightbox, so a thumbnail that swapped to local
  // art never enlarges to the dead primary URL (review finding on #6886).
  //
  // The fallback must be SAME-ORIGIN: for a registry-only app the raw row
  // spread can deliver attacker-chosen fallback keys, and honouring an
  // absolute URL here would let a third-party index point this <img> at any
  // host on load failure, leaking the viewer's address and headers (GPT
  // security finding on #6886). installedArt already emits only same-origin
  // routes, so legitimate installed-app fallbacks always pass.
  const resolvedAt = (i: number): string => {
    const url = screenList[i]
    if (!primaryFailed.has(i)) return url
    const fallback = fallbackList[i] || ''
    if (!fallback || classifyManifestArt(fallback) !== 'same-origin') return ''
    return fallback !== url && !fallbackFailed.has(i) ? fallback : ''
  }

  if (screenList.length === 0) return null
  // When every thumbnail is terminal, drop the whole section: a bare
  // "SCREENSHOTS" header over nothing is the empty-box state this fix removes
  // from the hero, and it stays reachable for non-installed apps.
  if (!screenList.some((_, i) => resolvedAt(i) !== '')) return null

  return (
    <>
      <div className="mb-6">
        <div className="text-[12px] text-muted uppercase tracking-wider mb-3">{i18nT('pages.appDetailPage.screenshots')}</div>
        {/* The wrapper exists for the edge cues: absolutely-positioned children
            of the scroller travel with the scrolled content, so the fades
            anchor to this non-scrolling parent instead. */}
        <div className="relative">
        <div ref={attachEdges} className="flex gap-3 overflow-x-auto pb-2 scrollbar-none">
          {screenList.map((_, i) => {
            // Second chance per thumbnail; a terminal index unmounts its
            // button entirely — the old display:none shape left an invisible,
            // tabbable "Open screenshot N" button that opened a broken
            // lightbox for keyboard users.
            const shown = resolvedAt(i)
            if (!shown) return null
            return (
              <button
                key={i}
                type="button"
                aria-label={i18nT('pages.appDetailPage.open_screenshot', { n: i + 1 })}
                className="p-0 border-none bg-transparent shrink-0 cursor-pointer"
                onClick={() => setSelected(i)}
              >
                {/* onError is an image-load lifecycle handler (swap to local */}
                {/* art, then hide broken images), not a user interaction; */}
                {/* the rule flags onError regardless. */}
                {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
                <img
                  src={shown}
                  alt={i18nT('pages.appDetailPage.screenshot', { n: i + 1 })}
                  className="h-40 rounded-lg border border-border hover:border-accent/40 hover:shadow-md transition-all object-cover"
                  onError={() => {
                    const tier = primaryFailed.has(i) ? 'fallback' : 'primary'
                    setFailures(previous => recordScreenshotFailure(
                      previous, screensKey, fallbacksKey, i, tier,
                    ))
                  }}
                  onLoad={() => remeasure()}
                />
              </button>
            )
          })}
        </div>
        {/* Edge cues: this scroller hides its scrollbar (scrollbar-none), so a
            gradient is the only signal that thumbnails continue past the
            clipped edge. from-bg matches the page surface. */}
        {edges.left && (
          <div aria-hidden="true" data-testid="app-screenshots-cue-left" className="pointer-events-none absolute left-0 top-0 bottom-0 w-6 z-10 bg-gradient-to-r from-bg to-transparent" />
        )}
        {edges.right && (
          <div aria-hidden="true" data-testid="app-screenshots-cue-right" className="pointer-events-none absolute right-0 top-0 bottom-0 w-6 z-10 bg-gradient-to-l from-bg to-transparent" />
        )}
        </div>
      </div>

      {/* Lightbox */}
      {selected !== null && (() => {
        // Navigation walks the VISIBLE subset: terminal indices have no
        // thumbnail, so stepping raw indices would land on a blank slide with
        // a counter that includes the hidden ones (UX review on #6886).
        // `selected` stays a raw index so thumbnail clicks need no mapping.
        const visible = screenList.map((_, i) => i).filter(i => resolvedAt(i) !== '')
        const nextVisible = visible.find(i => i > selected)
        const prevVisible = [...visible].reverse().find(i => i < selected)
        return (
          // Click-to-dismiss on the backdrop is a mouse affordance; keyboard
          // users close with Escape (AppStoreDialog) and step with the arrows.
          <AppStoreDialog
            className="fixed inset-0 z-[9999] flex items-center justify-center bg-bg/80 backdrop-blur-xs"
            dialogRef={dialogRef}
            onClose={closeViewer}
            onBackdropClick={() => {
              // A pinch, drag or double-tap just finished — that click is gesture
              // residue and must not dismiss the viewer the user just zoomed into.
              if (suppressClickRef.current) { suppressClickRef.current = false; return }
              setSelected(null)
            }}
            onKeyDown={e => {
              if (e.key === 'ArrowRight' && nextVisible !== undefined) setSelected(nextVisible)
              if (e.key === 'ArrowLeft' && prevVisible !== undefined) setSelected(prevVisible)
            }}
            aria-label={i18nT('pages.appDetailPage.screenshot_viewer')}
          >
            {/* Presentational wrapper: stops backdrop-dismiss when clicking the image. */}
            <div role="presentation" className="relative max-w-4xl max-h-[80vh] mx-4" onClick={e => e.stopPropagation()}>
              <img
                src={resolvedAt(selected)}
                alt=""
                ref={imgRef}
                className="max-w-full max-h-[80vh] rounded-xl shadow-2xl touch-none"
                style={{
                  transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
                  // No transition during a gesture: a pinch already produces a frame
                  // per move, and easing between them lags the fingers. Nor for a user
                  // who opted out of motion — a double-tap animates a 2.5x scale.
                  transition: pinching || dragging || reduceMotion ? 'none' : 'transform 150ms ease-out',
                  cursor: zoom > SCREENSHOT_ZOOM_MIN ? (dragging ? 'grabbing' : 'grab') : undefined,
                }}
                onPointerDown={onImgPointerDown}
                onPointerMove={onImgPointerMove}
                onPointerUp={onImgPointerUp}
                onPointerCancel={onImgPointerUp}
              />
              <button className="absolute top-2 right-2 bg-bg/80 rounded-full p-1.5 text-muted hover:text-text" onClick={() => setSelected(null)} aria-label={i18nT('pages.appDetailPage.close')}><X size={18} /></button>
              {prevVisible !== undefined && (
                <button className="absolute left-2 top-1/2 -translate-y-1/2 bg-bg/80 rounded-full p-2 text-muted hover:text-text" onClick={e => { e.stopPropagation(); setSelected(prevVisible) }} aria-label={i18nT('pages.appDetailPage.previous')}><ChevronLeft size={20} /></button>
              )}
              {nextVisible !== undefined && (
                <button className="absolute right-2 top-1/2 -translate-y-1/2 bg-bg/80 rounded-full p-2 text-muted hover:text-text" onClick={e => { e.stopPropagation(); setSelected(nextVisible) }} aria-label={i18nT('pages.appDetailPage.next')}><ChevronRight size={20} /></button>
              )}
              <div className="absolute bottom-3 left-1/2 -translate-x-1/2 text-[12px] text-muted bg-bg/80 px-3 py-1 rounded-full">{visible.indexOf(selected) + 1} / {visible.length}</div>
            </div>
          </AppStoreDialog>
        )
      })()}
    </>
  )
}

/**
 * Hero banner with a local-art second chance (#6864).
 *
 * Renders `src`; when it fails to LOAD, swaps once to `fallbackSrc` (an
 * installed app's own local route); when that fails too — or no usable
 * fallback exists — unmounts entirely, so no empty bordered box is left where
 * the banner was (the pre-#6864 terminal state, preserved). Latch discipline
 * mirrors AppIcon (#6804): two latches, per-URL resets so a theme flip
 * re-arms, and the self-match guard skipping a fallback identical to the
 * failed primary (when the local candidate already won the precedence,
 * retrying the URL that just errored is a second doomed request).
 */
// Exported for tests (direct latch-discipline coverage).
export function HeroBanner({ src, fallbackSrc, isDetail }: { src: string; fallbackSrc?: string; isDetail: boolean }) {
  // Same-origin gate on the fallback, mirroring resolvedAt in the gallery:
  // the registry-only raw-row spread can deliver attacker-chosen fallback
  // keys, and an absolute URL honoured on error would leak the viewer's
  // address to a third-party host. installedArt only emits same-origin
  // routes, so real installed-app fallbacks always pass (GPT finding, #6886).
  const safeFallback = fallbackSrc && classifyManifestArt(fallbackSrc) === 'same-origin' ? fallbackSrc : ''
  // Re-arm the latches during render rather than in a passive effect. The image
  // for a new URL can fail BEFORE an effect would run, and the effect's reset
  // would then erase that real failure and re-show the dead URL. Adjusting
  // state while rendering re-arms before the new <img> is committed, and —
  // unlike binding the failure to the URL text alone — it clears on EVERY
  // transition, so a theme flip back to a previously failed URL retries instead
  // of restoring a stale failure. A changed primary re-arms both latches; a
  // fallback that moves on its own (an install completing under a mounted page)
  // re-arms only the fallback latch.
  const [latch, setLatch] = useState<{
    src: string
    fallback: string
    primaryFailed: boolean
    fallbackFailed: boolean
  }>(() => ({ src, fallback: safeFallback, primaryFailed: false, fallbackFailed: false }))
  if (latch.src !== src) {
    setLatch({ src, fallback: safeFallback, primaryFailed: false, fallbackFailed: false })
  } else if (latch.fallback !== safeFallback) {
    setLatch({ src, fallback: safeFallback, primaryFailed: latch.primaryFailed, fallbackFailed: false })
  }
  const primaryFailed = latch.src === src && latch.primaryFailed
  const fallbackFailed = latch.src === src
    && latch.fallback === safeFallback
    && latch.fallbackFailed
  const useFallback = primaryFailed && !!safeFallback && safeFallback !== src && !fallbackFailed
  if (!src || (primaryFailed && !useFallback)) return null
  return (
    <div className={`w-full ${isDetail ? 'aspect-[25/6]' : 'aspect-video'} max-h-72 rounded-2xl border border-border overflow-hidden mb-6 bg-[var(--card)]`}>
      {/* onError is an image-load lifecycle handler (swap to local art, then hide). */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
      <img
        src={useFallback ? safeFallback : src}
        alt=""
        className="w-full h-full object-cover"
        onError={() => {
          setLatch(previous => {
            // Superseded generation: the current render already re-armed these
            // tokens, so this error is about a URL no longer on screen.
            if (previous.src !== src || previous.fallback !== safeFallback) return previous
            return previous.primaryFailed
              ? { ...previous, fallbackFailed: true }
              : { ...previous, primaryFailed: true }
          })
        }}
      />
    </div>
  )
}
