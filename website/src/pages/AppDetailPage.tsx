/**
 * AppDetailPage — unified detail view for both installed and registry apps.
 *
 * Route: /apps/detail/:name
 * Fetches from both /api/apps/{name} (installed) and /api/apps/registry (browse).
 * Shows full description, features, screenshots, tags, and action buttons.
 */
import { useEffect, useState, useCallback, useRef } from 'react'
import { useReducedMotion } from 'framer-motion'
import { useParams, useNavigate, useLocation } from 'react-router-dom'
import {
  ArrowLeft, Download, Check, Loader2, Power, PowerOff,
  Trash2, RefreshCw, Bot, Zap, ArrowUp,
  Clock, ChevronLeft, ChevronRight, X, Monitor, Copy, Terminal,
  Sparkles, Target, Settings2, Star,
} from 'lucide-react'
import { needsDesktopApp } from '../lib/electron'
import { api } from '../api/client'
import { PageHeader, Card, CardTitle, Badge, Btn } from '../components/ui'
import AppIcon from '../components/AppIcon'
import TrustAppModal, { APP_EXECUTION_DENIED, isTrustDeniedError, useTrustGate } from '../components/appstore/TrustAppModal'
import { isRegistrySourced, sanitizeStargazersCount } from '../components/appstore/types'
import { recordEvent } from '../rum'
import { useTheme } from '../hooks/useTheme'
import { DOUBLE_TAP_MS, DOUBLE_TAP_SLOP, DOUBLE_TAP_ZOOM, usePinchZoom } from '../hooks/usePinchZoom'
import AskAgentButton from '../components/AskAgentButton'

import { i18nT } from '../i18n/t'
import type { AppContributor } from '../types'
import {
  appDisplayName, appDescription, appHighlights, appUseCases, appConfiguration,
} from '../components/appstore/appManifest'
import { isBuiltinServerRow, mergeBuiltinRow } from '../components/appstore/mergeBuiltinRow'
import { classifyManifestArt, installedArt, installedArtList, installedArtListAligned, installedIcon } from '../components/appstore/useHeroArt'
import { fmtDateNumeric, fmtCompact, fmtNumber } from '../i18n/format'
type AppInfo = {
  name: string
  displayName: string
  description: string
  version: string
  author: string
  icon?: string
  iconUrl?: string
  iconUrlDark?: string
  // Second-chance icon art: an INSTALLED app's own local route, consulted by
  // AppIcon only when the primary (usually registry) URL fails to load.
  iconUrlFallback?: string
  iconUrlFallbackDark?: string
  tags?: string[]
  highlights?: string[]
  useCases?: string[]
  configuration?: string[]
  screenshots?: string[]
  screenshotsDark?: string[]
  heroImage?: string
  heroImageDark?: string
  heroImageDetail?: string
  heroImageDetailDark?: string
  // Second-chance hero/screenshot art (#6864): an INSTALLED app's own local
  // routes, consulted only when the primary (usually registry) URL fails to
  // load. Optional because only the installed branch sets them — a
  // not-installed app has no local bytes, so hide-on-error stays its terminal
  // state. The screenshot lists are index-aligned with their primaries.
  heroImageFallback?: string
  heroImageDarkFallback?: string
  heroImageDetailFallback?: string
  heroImageDetailDarkFallback?: string
  screenshotsFallback?: string[]
  screenshotsDarkFallback?: string[]
  repo?: string
  trustRepository?: string
  branch?: string
  /**
   * GitHub star count baked into git-type third-party rows by the publisher.
   * Display-only and server-sanitized; built-ins never carry it, so presence
   * is the display gate.
   */
  stargazersCount?: number
  // Installed state
  installed: boolean
  installedVersion?: string
  enabled?: boolean
  managed?: string
  source?: string
  installedAt?: string
  updateAvailable?: boolean
  // Three-axis classification
  origin?: string     // "builtin" | "registry" | "local" | "external"
  resources?: string  // "gateway" | "app"
  lifecycle?: string  // "gateway" | "app" | "locked"
  // Platform
  platform?: { os?: string[]; installMode?: string; clientInstall?: { shell?: string; postInstall?: string }
    // Set when the app's UI needs the Electron shell (native windows,
    // global shortcuts, tray). A UX gate only — the marker is client-side.
    requiresDesktopApp?: boolean }
  // Manifest (from installed app)
  manifest?: AppManifest
}

interface McpServerConfig {
  url?: string
  command?: string
  autoApprove?: string[]
  [key: string]: unknown
}

interface AppPermissions {
  api?: string[]
  events?: string[]
  mcpTools?: string[]
  storage?: boolean
  cron?: boolean
  network?: boolean
  memory?: boolean | string
  [key: string]: unknown
}

/** A registry app entry from /api/apps/registry — a superset of the fields we
 *  read here, spread into AppInfo when there's no installed app. */
interface RegistryEntry extends Partial<AppInfo> {
  name: string
  updateAvailable?: boolean
}

interface AppManifest {
  displayName?: string
  description?: string
  version?: string
  author?: string
  tags?: string[]
  highlights?: string[]
  useCases?: string[]
  configuration?: string[]
  screenshots?: string[]
  screenshotsDark?: string[]
  // Store-listing metadata. For built-in apps these live on the manifest
  // (preserved through AppManifest.extra) rather than on a registry entry —
  // built-ins are not part of the /api/apps/registry feed.
  iconUrl?: string
  iconUrlDark?: string
  // Repo-relative icon paths. An external app declares these (the backend
  // rewrites them into blob-proxy URLs on a registry row); `iconUrl` is the
  // built-in spelling.
  iconPath?: string
  iconPathDark?: string
  // The repo an external app's art paths are relative to, when the manifest
  // declares it.
  repo?: string
  heroImage?: string
  heroImageDark?: string
  heroImageDetail?: string
  heroImageDetailDark?: string
  ui?: { pages?: { route?: string; label?: string; icon?: string; iconUrl?: string }[] }
  // Installed apps carry the platform config here (a registry/catalog entry
  // exposes it top-level instead); `needsDesktopApp` reads both. Without this the
  // desktop requirement was invisible on surfaces that pass the manifest shape.
  platform?: { requiresDesktopApp?: boolean; os?: string[] }
  agents?: string[]
  skills?: string[]
  crons?: { name: string }[]
  mcpServers?: Record<string, McpServerConfig>
  permissions?: AppPermissions
  minKiroCrewVersion?: string
}

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

  // ── screenshot magnification (issue #6162) ────────────────────────────────
  // This lightbox is the third full-viewport magnify overlay, bound by the same
  // own-your-zoom rule as the image viewer and DiagramLightbox (page-layout.md):
  // page zoom is off on touch shell-wide, so a phone user has no other way to
  // inspect a fit-scaled screenshot. Unlike those two, the surface also owns
  // prev/next navigation and click-to-dismiss, so the shared hook supplies only
  // the gesture math while the page keeps those interactions.
  const dialogRef = useRef<HTMLDivElement | null>(null)
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
        <div className="flex gap-3 overflow-x-auto pb-2 scrollbar-none">
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
                />
              </button>
            )
          })}
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
          // Modal backdrop: click-to-dismiss is a mouse affordance; keyboard users
          // dismiss/navigate via the onKeyDown handler (Escape / arrows) below.
          // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
          <div
            className="fixed inset-0 z-[9999] flex items-center justify-center bg-bg/80 backdrop-blur-sm"
            onClick={() => {
              // A pinch, drag or double-tap just finished — that click is gesture
              // residue and must not dismiss the viewer the user just zoomed into.
              if (suppressClickRef.current) { suppressClickRef.current = false; return }
              setSelected(null)
            }}
            onKeyDown={e => {
              if (e.key === 'Escape') setSelected(null)
              if (e.key === 'ArrowRight' && nextVisible !== undefined) setSelected(nextVisible)
              if (e.key === 'ArrowLeft' && prevVisible !== undefined) setSelected(prevVisible)
            }}
            tabIndex={-1}
            ref={el => {
              dialogRef.current = el
              el?.focus()
            }}
            role="dialog"
            aria-modal="true"
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
          </div>
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

export default function AppDetailPage() {
  const { name } = useParams<{ name: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const { theme: resolvedMode } = useTheme()
  const [app, setApp] = useState<AppInfo | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  /**
   * Success reflection for an in-place sync. This page otherwise has only an
   * error surface, so a successful ``update`` re-rendered a byte-identical page:
   * re-copying a source directory normally carries the same version, which makes
   * silence indistinguishable from a no-op. The list card states the outcome for
   * the same reason, and both paths this fix wires need to say it.
   */
  const [successMsg, setSuccessMsg] = useState('')
  const clearError = useCallback(() => {
    setError('')
  }, [])
  const [actionLoading, setActionLoading] = useState<string | null>(null)
  const [installLog, setInstallLog] = useState('')
  const [showInstallLog, setShowInstallLog] = useState(false)
  const [installDone, setInstallDone] = useState(false)
  const installLogRef = useRef<HTMLPreElement>(null)
  const installAbortRef = useRef<AbortController | null>(null)
  const [clientInstall, setClientInstall] = useState<{ shell?: string; postInstall?: string } | null>(null)
  const [copied, setCopied] = useState(false)
  const [serverHostname, setServerHostname] = useState('')
  const [showUninstallConfirm, setShowUninstallConfirm] = useState(false)
  const [keepData, setKeepData] = useState(true)

  // Contributors row (Details panel). Resolved GitHub repo URL for the app's
  // source, its top contributors, and the fetch's loading state. The row hides
  // entirely unless a GitHub repo URL resolves and the fetch yields entries.
  const [repoUrl, setRepoUrl] = useState('')
  const [contributors, setContributors] = useState<AppContributor[]>([])
  const [contribLoading, setContribLoading] = useState(false)

  useEffect(() => {
    // Prefer the registry/manifest repo, then the git URL the app was trusted
    // from, then its source. GitHub-only in v1; a non-GitHub value leaves the
    // row hidden (the backend also returns [] for one).
    const candidates = [app?.repo, app?.manifest?.repo, app?.trustRepository, app?.source]
    const url = candidates.find(
      (v): v is string => typeof v === 'string' && /^https:\/\/github\.com\//i.test(v),
    ) || ''
    setRepoUrl(url)
    if (!url) { setContributors([]); setContribLoading(false); return }
    let cancelled = false
    setContribLoading(true)
    api.appContributors(url)
      .then(res => { if (!cancelled) setContributors(res.contributors || []) })
      .catch(() => { if (!cancelled) setContributors([]) })
      .finally(() => { if (!cancelled) setContribLoading(false) })
    return () => { cancelled = true }
  }, [app])

  // Helper: open chat with a pre-filled message (same mechanism as useChatLauncher from app-sdk)
  const openChatWithMessage = useCallback((message: string) => {
    ;(window as Window & { __mc_chat_launch?: { message: string; ts: number } }).__mc_chat_launch = { message, ts: Date.now() }
    navigate('/chat')
  }, [navigate])

  // Abort in-flight streaming install on unmount
  useEffect(() => () => { installAbortRef.current?.abort() }, [])

  const load = useCallback(async () => {
    if (!name) return
    setLoading(true)
    clearError()
    try {
      // Try installed app first
      const installed = await api.getApp(name).catch(() => null)
      // Also check registry for richer metadata (screenshots, highlights)
      const registryData = await api.listRegistry().catch(() => ({ apps: [], serverPlatform: { os: '', arch: '' } }))
      const registryList = (registryData.apps || []) as RegistryEntry[]

      // Fetch server hostname for client install template variables
      const sysInfo = await api.system().catch(() => ({ hostname: '' }))
      if (sysInfo.hostname) setServerHostname(sysInfo.hostname)
      const registryEntry = registryList.find((r) => r.name === name)

      if (installed) {
        const m = installed.manifest || {}
        // A built-in the registry response carries goes through the SAME merge
        // the browse list uses. Spelled separately, the two chains disagreed:
        // this page preferred the manifest (`m.author || registryEntry?.author`)
        // while the list preferred the row, so one app could read
        // "Kiro Crew · Developer Tools" in the list and "kirocrew · Productivity"
        // one click later. The catalog is the store's inventory on both surfaces
        // or on neither.
        if (registryEntry && isBuiltinServerRow(registryEntry)) {
          setApp({
            ...mergeBuiltinRow(registryEntry, { ...m, version: installed.version }),
            name: installed.name,
            installed: true,
            installedVersion: installed.version,
            enabled: installed.enabled,
            managed: installed.managed,
            source: installed.source,
            installedAt: installed.installedAt,
            origin: installed.origin,
            resources: installed.resources,
            lifecycle: installed.lifecycle,
            updateAvailable: registryEntry.updateAvailable || false,
            // Built-ins never carry a star count (they have no repository of
            // their own) — enforce the invariant here rather than trusting the
            // spread above, since mergeBuiltinRow copies the raw server row.
            stargazersCount: undefined,
            manifest: m,
          })
        } else {
          // A non-built-in installed app may have no registry row carrying art
          // at all — a local-directory install has none, and a row built from a
          // cached manifest older than the release that added the art carries
          // those fields empty. The manifest on disk still has the paths, and
          // since the app IS installed those paths resolve against its own
          // install directory through `installedArt` — no repo identifier, no
          // clone, no network.
          // A page's own icon ships inside the app's UI bundle, not at the repo
          // root, so a relative value resolves against the app's UI asset route —
          // the same base the rail and the command palette use. A cross-origin
          // value is refused here for the same reason it is everywhere else on
          // this path: the manifest is untrusted, and requesting it would leak the
          // viewer to whatever host it names.
          const pageIcon: unknown = m.ui?.pages?.[0]?.iconUrl
          const pageIconKind = classifyManifestArt(pageIcon)
          const pageIconUrl = pageIconKind === 'same-origin' ? pageIcon as string
            : pageIconKind === 'relative' ? `/apps/${installed.name}/ui/${pageIcon as string}`
              : ''
          setApp({
            name: installed.name,
            displayName: installed.displayName || m.displayName || installed.name,
            description: m.description || '',
            // The installed record wins, and the registry row is the LAST
            // resort. Version is the one field where the catalog does not get
            // to speak for the machine: the row is fetched from the network and
            // cached, so it can name an older version than the clone installed
            // here — a repo publishing 0.1.0 while this machine runs 0.2.0. The
            // built-in branch above resolves it the same way
            // (`mergeBuiltinRow(registryEntry, { ...m, version: installed.version })`,
            // whose contract names version as its one reversed field), so both
            // branches agree rather than disagreeing the way the comment there
            // records for `author`.
            version: installed.version || m.version || registryEntry?.version || '0.0.0',
            author: m.author || registryEntry?.author || '',
            icon: registryEntry?.icon || m.ui?.pages?.[0]?.icon || '',
            // `iconPath` is preferred over a manifest-declared `iconUrl` for the
            // same reason the backend honours only `iconPath`: a repo-relative
            // path stays on our own proxy, which enforces the extension
            // allowlist and the trusted-repo gate. The `iconUrl` fallback goes
            // through the same resolver rather than straight to `<img>`, so a
            // manifest naming an external host is refused on this surface too.
            iconUrl: registryEntry?.iconUrl
              || installedIcon(m.iconPath, m.iconUrl, installed.name) || pageIconUrl || '',
            iconUrlDark: registryEntry?.iconUrlDark
              || installedIcon(m.iconPathDark, m.iconUrlDark, installed.name) || '',
            // The app IS installed on this branch, so its icon bytes are on
            // local disk: carry that route as a LOAD-failure fallback for
            // AppIcon. Deliberately not a precedence change — the registry's
            // immutable content-addressed asset above stays the primary `src`
            // and keeps its cache-forever win (#6804 rejects a flip); the local
            // candidate is consulted only when that src errors (offline,
            // captive portal, blocked host). When the local candidate itself
            // won the precedence above, AppIcon skips the identical URL.
            iconUrlFallback: installedIcon(m.iconPath, m.iconUrl, installed.name) || '',
            iconUrlFallbackDark: installedIcon(m.iconPathDark, m.iconUrlDark, installed.name) || '',
            tags: m.tags || registryEntry?.tags || [],
            highlights: m.highlights || registryEntry?.highlights || [],
            useCases: m.useCases || registryEntry?.useCases || [],
            configuration: m.configuration || registryEntry?.configuration || [],
            // `||` would be wrong for the list fields: an empty array is truthy,
            // so a declared-but-unresolvable list would short-circuit the
            // blob-proxy fallback instead of falling through to it.
            screenshots: registryEntry?.screenshots
              || installedArtList(m.screenshots, installed.name),
            screenshotsDark: registryEntry?.screenshotsDark
              || installedArtList(m.screenshotsDark, installed.name),
            heroImage: registryEntry?.heroImage || installedArt(m.heroImage, installed.name),
            heroImageDark: registryEntry?.heroImageDark
              || installedArt(m.heroImageDark, installed.name),
            heroImageDetail: registryEntry?.heroImageDetail
              || installedArt(m.heroImageDetail, installed.name),
            heroImageDetailDark: registryEntry?.heroImageDetailDark
              || installedArt(m.heroImageDetailDark, installed.name),
            // The app IS installed on this branch, so its hero/screenshot
            // bytes are on local disk: carry those routes as LOAD-failure
            // fallbacks, the same shape as the icon pair above. Deliberately
            // not a precedence change — the registry assets stay the primary
            // `src` (#6804 rejects a flip); these are consulted only when a
            // primary errors (offline, captive portal, blocked host) (#6864).
            heroImageFallback: installedArt(m.heroImage, installed.name),
            heroImageDarkFallback: installedArt(m.heroImageDark, installed.name),
            heroImageDetailFallback: installedArt(m.heroImageDetail, installed.name),
            heroImageDetailDarkFallback: installedArt(m.heroImageDetailDark, installed.name),
            // The screenshot fallbacks pair with their primaries BY INDEX, so
            // they use the aligned resolver (refused entries stay as ''
            // placeholders) — the filtered list would shift every entry after
            // a refusal and pair a thumbnail with its neighbour's art. Set
            // only when the registry supplied the primary list: when the
            // local list won the precedence above, the primary already IS the
            // local route (filtered, so aligned indices would not match), and
            // retrying an identical URL is a guaranteed second failure.
            screenshotsFallback: registryEntry?.screenshots
              ? installedArtListAligned(m.screenshots, installed.name)
              : undefined,
            screenshotsDarkFallback: registryEntry?.screenshotsDark
              ? installedArtListAligned(m.screenshotsDark, installed.name)
              : undefined,
            // Left as the row's own value: this field also names the repo in the
            // trust-consent prompt and the details list, and widening those to a
            // fallback identifier is a separate decision from resolving art.
            repo: registryEntry?.repo || '',
            trustRepository: installed.trustRepository,
            stargazersCount: sanitizeStargazersCount(registryEntry?.stargazersCount),
            installed: true,
            installedVersion: installed.version,
            enabled: installed.enabled,
            managed: installed.managed,
            source: installed.source,
            installedAt: installed.installedAt,
            origin: installed.origin,
            resources: installed.resources,
            lifecycle: installed.lifecycle,
            updateAvailable: registryEntry?.updateAvailable || false,
            manifest: m,
          })
        }
      } else if (registryEntry) {
        setApp({
          ...registryEntry,
          // Required AppInfo fields — registry entries normally carry these, but
          // fall back so the object always satisfies AppInfo.
          name: registryEntry.name,
          displayName: registryEntry.displayName || registryEntry.name,
          description: registryEntry.description || '',
          version: registryEntry.version || '0.0.0',
          author: registryEntry.author || '',
          // The spread above copies the RAW listRegistry payload, which never
          // went through normalizeRegistryApp — sanitize the display-only star
          // count explicitly so a hostile/older gateway cannot render NaN/-1
          // or a layout-breaking 1e308 here (the list path is already covered).
          stargazersCount: sanitizeStargazersCount(registryEntry.stargazersCount),
          // Preserve install status from registry (set by detectInstalled)
          installed: registryEntry.installed ?? false,
          platform: registryEntry.platform,
        })
      } else {
        setError(i18nT('pages.appDetailPage.app_not_found_2', { name }))
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : i18nT('pages.appDetailPage.failed_to_load_app'))
    } finally {
      setLoading(false)
    }
  }, [name, clearError])

  useEffect(() => { load() }, [load])

  // Auto-trigger install/update after an IN-APP action navigated here.
  //
  // The trigger is router state (``location.state.autoAction``) and ONLY that:
  // a query param is attacker-reachable — a cross-site page can navigate an
  // authenticated browser to a detail URL and the Lax session cookie rides
  // along, so any URL-driven trigger (install OR update, since update installs
  // an absent app) would run third-party setup code with gateway privileges
  // without user intent. Router state can only be produced by an in-app
  // navigate() call, so it cannot be forged from outside the app.
  const autoActionTriggered = useRef(false)
  useEffect(() => {
    if (!app || autoActionTriggered.current) return
    const stateAction = (location.state as { autoAction?: string } | null)?.autoAction
    if (stateAction !== 'install' && stateAction !== 'update') return
    if (stateAction === 'install' && app.installed) return
    autoActionTriggered.current = true
    // Clear the state so a refresh or Back/Forward doesn't re-fire it.
    navigate(`${location.pathname}${location.search}`, { replace: true, state: null })
    // An installed app whose bytes came from a directory on this machine has no
    // registry row to install from — its refresh is the update endpoint, which
    // re-copies the source directory recorded at install. The streaming registry
    // install is for everything else: a registry-sourced app, and an app not
    // installed at all.
    if (stateAction === 'update' && app.installed && !isRegistrySourced(app)) {
      handleAction('update')
      return
    }
    handleInstall()
  }, [app, location]) // eslint-disable-line react-hooks/exhaustive-deps

  /**
   * The registry install itself. Resolves `'trust-required'` when the gateway
   * refused it for missing execution trust instead of failing it, so the CALLER
   * owns the consent modal — which is what lets this same function BE the retry
   * the modal re-runs once the grant lands.
   *
   * `'failed'` is reported separately from `'done'` for every unsuccessful
   * install. The distinction is load-bearing rather than cosmetic: when this
   * function runs AS the trust retry, the modal rolls its fresh grant back only if
   * the retry rejects, and collapsing an ordinary `{ok:false}` failure into
   * `'done'` therefore left a grant standing over a name no app occupies — a
   * consent bypass for whatever gets installed under that name next.
   */
  const runInstall = async (): Promise<'done' | 'trust-required' | 'failed' | 'aborted'> => {
    if (!app) return 'done'
    setActionLoading('install')
    setInstallLog('')
    setInstallDone(false)
    setShowInstallLog(true)
    clearError()
    setClientInstall(null)
    installAbortRef.current?.abort()
    const controller = new AbortController()
    installAbortRef.current = controller
    try {
      const result = await api.installFromRegistryStream(
        app.name,
        (line) => {
          setInstallLog(prev => prev + (prev ? '\n' : '') + line)
          // Auto-scroll to bottom
          requestAnimationFrame(() => {
            if (installLogRef.current) {
              installLogRef.current.scrollTop = installLogRef.current.scrollHeight
            }
          })
        },
        controller.signal,
      )
      // Server says this app needs client-side installation
      //
      // Deliberately `'done'` and NOT a non-terminal outcome, unlike the abort and
      // failure paths below. Nothing is on disk yet, so the rollback probe would
      // 404 and withdraw the grant — but the grant is exactly what the user just
      // consented to so they can complete the client-side install being shown to
      // them. Withdrawing it here would break the flow it was granted for. The
      // residual window (user consents, then abandons the client install, leaving
      // a grant with no owner) is real but is a product question about that flow,
      // not a defect in this one: revoking is available in Settings, and the
      // uninstall path refuses to leave the grant behind if they finish later.
      if (result.needsClientInstall) {
        setClientInstall(result.clientInstall || app.platform?.clientInstall || {})
        setShowInstallLog(false)
        setActionLoading(null)
        return 'done'
      }
      // A refused install is a consent prompt, not an error. The gate runs
      // BEFORE the clone, so nothing landed on disk and the log holds nothing
      // the user needs to read — drop the log panel and hand the refusal back.
      // The stream RESOLVES this refusal (SSE `done` carries the code), so it is
      // checked on the result, not only in the catch below.
      if (isTrustDeniedError(result)) {
        setShowInstallLog(false)
        return 'trust-required'
      }
      setInstallDone(true)
      if (result.ok) {
        recordEvent('app_install', { app: app.name, source: 'registry', version: app.version })
        await load()
        window.dispatchEvent(new Event('mc:apps-changed'))
      } else {
        setError(result.error || i18nT('pages.appDetailPage.install_failed'))
        return 'failed'
      }
    } catch (e: unknown) {
      // An ABORT is not a completed install, and reporting it as `'done'` was the
      // third way a grant could be orphaned (after an ordinary `{ok:false}` and
      // after a second trust refusal). Navigating away aborts the stream, so:
      // confirm trust -> navigate -> the install never lands -> the retry resolved
      // -> nothing rejected -> the fresh grant stayed over a name no app occupies.
      // Reported as its own outcome so the retry rejects and the rollback probe
      // decides from what is ACTUALLY installed: if the abort raced a completed
      // install the app is there and the grant is rightly kept, and if it did not
      // land the 404 withdraws it.
      if (e instanceof Error && e.name === 'AbortError') return 'aborted'
      // The non-streaming install route answers 403 with the same code.
      if (isTrustDeniedError(e)) {
        setShowInstallLog(false)
        return 'trust-required'
      }
      setInstallDone(true)
      setError(e instanceof Error ? e.message : i18nT('pages.appDetailPage.install_failed'))
      return 'failed'
    } finally {
      // Only clear loading if this is still the active install —
      // compare by identity to avoid the race where a second invocation
      // replaces the ref before this finally runs.
      if (installAbortRef.current === controller) {
        setActionLoading(null)
      }
    }
    return 'done'
  }

  /** The single enable path — shared by the action buttons and the trust retry. */
  const runEnable = useCallback(async (name: string) => {
    await api.enableApp(name)
    recordEvent('app_enable', { app: name, version: app?.installedVersion || app?.version })
    await load()
    window.dispatchEvent(new Event('mc:apps-changed'))
  }, [app, load])

  const trust = useTrustGate(runEnable)

  /**
   * Get / Install / Update entry point — owns the consent modal.
   *
   * Every install surface funnels here (the Get button, the two Update buttons,
   * and the `autoAction` navigation the App Store's Get uses), so the refusal is
   * handled once no matter which one triggered it. The retry re-runs the INSTALL,
   * not the enable: this refusal came from the registry-install gate and there is
   * nothing installed yet to enable.
   */
  const handleInstall = async () => {
    if (!app) return
    if (await runInstall() !== 'trust-required') return
    trust.open(
      {
        name: app.name,
        displayName: app.displayName,
        trustRepository: app.trustRepository,
        origin: app.origin,
      },
      async () => {
        // ANY unsuccessful retry must REJECT, not resolve. `useTrustGate` rolls the
        // fresh grant back on rejection (and only then), so resolving here on an
        // ordinary install failure left the grant orphaned over a name no app
        // occupies. A second trust refusal additionally means the grant did not
        // take effect, which the modal reports inline rather than closing on a
        // silent no-op.
        const outcome = await runInstall()
        if (outcome === 'trust-required') throw new Error(APP_EXECUTION_DENIED)
        if (outcome !== 'done') throw new Error(i18nT('pages.appDetailPage.install_failed'))
      },
    )
  }

  const handleAction = async (action: 'enable' | 'disable' | 'uninstall' | 'update') => {
    if (!app) return
    // Intercept uninstall to show confirmation modal
    if (action === 'uninstall') {
      setShowUninstallConfirm(true)
      setKeepData(true)
      return
    }
    setActionLoading(action)
    clearError()
    setSuccessMsg('')
    try {
      if (action === 'enable') { await runEnable(app.name); return }
      if (action === 'disable') await api.disableApp(app.name)
      else if (action === 'update') await api.updateApp(app.name)
      if (action === 'disable') {
        recordEvent('app_disable', { app: app.name, version: app.installedVersion || app.version })
      }
      await load()
      // `load()` clears the error but does not speak to success, and a same-version
      // re-copy changes nothing visible, so say it explicitly. The Sync button that
      // reaches here is not gated on source, and `handle_update_app` re-clones a
      // registry-sourced app from the registry rather than copying a directory, so
      // each case has to name where the update actually came from.
      if (action === 'update') {
        setSuccessMsg(isRegistrySourced(app)
          ? i18nT('pages.appsPage.updated_from_the_registry', { name: appDisplayName(app) })
          : i18nT('pages.appsPage.synced_from_its_source_directory', { name: appDisplayName(app) }))
        setTimeout(() => setSuccessMsg(''), 4000)
      }
      window.dispatchEvent(new Event('mc:apps-changed'))
    } catch (e: unknown) {
      // A third-party app that has not been granted execution trust yet is a
      // consent prompt, not an error — branch on the machine-readable code, and
      // let the modal grant it inline instead of sending the user to a blanket
      // switch. Every OTHER failure still renders its own prose.
      if (action === 'enable' && isTrustDeniedError(e)) {
        trust.open({
          name: app.name,
          displayName: app.displayName,
          trustRepository: app.trustRepository,
          origin: app.origin,
        })
      } else {
        setError(e instanceof Error ? e.message : i18nT('pages.appDetailPage.failed_to', { action }))
      }
    } finally {
      setActionLoading(null)
    }
  }

  const confirmUninstall = async () => {
    if (!app) return
    setActionLoading('uninstall')
    clearError()
    try {
      const res = await api.uninstallApp(app.name, keepData)
      if (res.uninstall_log) setInstallLog(res.uninstall_log)
      recordEvent('app_uninstall', { app: app.name, version: app.installedVersion || app.version })
      window.dispatchEvent(new Event('mc:apps-changed'))
      navigate('/apps')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : i18nT('pages.appDetailPage.failed_to_uninstall'))
    } finally {
      setActionLoading(null)
      setShowUninstallConfirm(false)
    }
  }

  if (loading) {
    return (
      <>
        <PageHeader title={i18nT('pages.appDetailPage.apps')} subtitle={i18nT('pages.appDetailPage.loading')} />
        <div className="flex-1 flex items-center justify-center text-muted text-sm">
          <Loader2 size={16} className="animate-spin mr-2" /> {i18nT('pages.appDetailPage.loading_app_details')}
        </div>
      </>
    )
  }

  if (!app) {
    return (
      <>
        <PageHeader title={i18nT('pages.appDetailPage.app_not_found')} subtitle={error || i18nT('pages.appDetailPage.doesnt_exist', { name })} />
        <div className="flex-1 flex items-center justify-center p-8">
          <Btn onClick={() => navigate('/apps')}><ArrowLeft size={14} /> {i18nT('pages.appDetailPage.back_to_apps')}</Btn>
        </div>
      </>
    )
  }

  const isSelfManaged = app.resources === 'app'
  const isBuiltin = app.origin === 'builtin'
  // An app can declare that its UI only works inside the Electron shell
  // (native always-on-top windows, global shortcuts, tray). Browser sessions
  // are told to use the desktop app instead of being handed a broken UI.
  // UX gate only: the marker is client-side, so nothing security-relevant may
  // rest on it (see PlatformConfig.requiresDesktopApp in manifest.py).
  const desktopOnly = needsDesktopApp(app)
  const canUpdate = app.lifecycle === 'gateway'
  const canUninstall = app.lifecycle !== 'locked'
  // Resource lists, derived once. `manifest` is absent for a registry-only app,
  // and `normalizeInstalledApp` fills the lists for an installed one — so the
  // fallback here is the registry case, not a defence against a partial
  // manifest, and nothing below re-asserts past it (#3689).
  const agents = app.manifest?.agents || []
  const skills = app.manifest?.skills || []
  const crons = app.manifest?.crons || []
  // Theme-aware hero banner source (mirrors the Browse card resolution).
  // Prefer the wide detail-ratio banner (heroImageDetail*); fall back to the
  // Browse hero, then the opposite theme.
  const heroDetailSrc = resolvedMode === 'dark'
    ? (app.heroImageDetailDark || app.heroImageDetail || '')
    : (app.heroImageDetail || app.heroImageDetailDark || '')
  const heroBrowseSrc = resolvedMode === 'dark'
    ? (app.heroImageDark || app.heroImage || '')
    : (app.heroImage || app.heroImageDark || '')
  const heroSrc = heroDetailSrc || heroBrowseSrc
  // When a dedicated detail banner is shown, size the container to its
  // 1200x288 (25:6) ratio so object-cover doesn't horizontally crop the art
  // on viewports narrower than 1200px. Fall back to 16:9 for the Browse hero.
  const heroIsDetail = Boolean(heroDetailSrc)
  // Local-art fallback candidate (#6864): the SAME two-level choice
  // re-evaluated over the fallback fields. The detail-vs-Browse order cannot
  // put detail-ratio art into the 16:9 container: a non-empty detail FALLBACK
  // implies a detail PRIMARY (the primary resolution above already falls back
  // to the same local candidate when the registry has none), so whenever the
  // first term below is non-empty, heroIsDetail is true and the container is
  // already sized 25:6. The reachable cross-tier case is the converse — a
  // registry detail banner failing with only local Browse art on disk — where
  // borrowing the other tier's art beats no art, the ratio stays keyed on the
  // primary (heroIsDetail above), and object-cover crops rather than distorts.
  const heroDetailFallback = resolvedMode === 'dark'
    ? (app.heroImageDetailDarkFallback || app.heroImageDetailFallback || '')
    : (app.heroImageDetailFallback || app.heroImageDetailDarkFallback || '')
  const heroBrowseFallback = resolvedMode === 'dark'
    ? (app.heroImageDarkFallback || app.heroImageFallback || '')
    : (app.heroImageFallback || app.heroImageDarkFallback || '')
  const heroSrcFallback = heroDetailFallback || heroBrowseFallback
  // Resolve untrusted registry metadata once and use the same normalized arrays
  // for both visibility and content. Reading the raw field for visibility would
  // render an empty titled card when a third-party index supplied a string or a
  // mixed array that the resolver correctly rejects.
  const useCases = appUseCases(app)
  const configuration = appConfiguration(app)

  return (
    <>
      <PageHeader title={i18nT('pages.appDetailPage.apps')} subtitle={appDisplayName(app)} />
      <div className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        {/* Back link */}
        <button className="flex items-center gap-1.5 text-[13px] text-muted hover:text-text mb-5 bg-transparent border-none cursor-pointer p-0 font-body transition-colors" onClick={() => navigate('/apps')}>
          <ArrowLeft size={14} /> {i18nT('pages.appDetailPage.back_to_apps')}
        </button>

        {/* In-place sync succeeded. Stated because nothing else on the page
            changes when a re-copy carries the same version. No dismiss control:
            unlike the error box below — which persists until cleared and so needs
            one — this clears itself, and a close button on a self-closing notice
            is a control whose only outcome is to race the timer. */}
        {successMsg && (
          <div className="mb-4 bg-ok/10 border border-ok/20 rounded-lg p-3 animate-rise">
            <span className="text-ok text-sm block">{successMsg}</span>
          </div>
        )}

        {/* Error */}
        {error && (
          <div className="mb-4 bg-danger/10 border border-danger/20 rounded-lg p-3 flex items-start gap-3 animate-rise">
            <div className="flex-1 min-w-0">
              {/* No special execution-policy branch here any more: an untrusted
                  third-party app is refused with `app_execution_denied`, and that
                  refusal is now resolved INLINE by the consent modal (granting
                  this one app) rather than by sending the user off to flip a
                  blanket switch. Everything that still reaches this box is an
                  unrecognized backend failure, so it renders the prose — better
                  than swallowing it — plus a hand-off to the agent, since raw
                  backend prose is otherwise a dead end. */}
              <span className="text-danger text-sm block">{error}</span>
              <div className="mt-2">
                <AskAgentButton message={error} />
              </div>
            </div>
            <button aria-label={i18nT('pages.appDetailPage.dismiss_error')} className="text-danger/60 hover:text-danger text-sm shrink-0" onClick={clearError}><X className="lucide-inline" /></button>
          </div>
        )}

        {/* Third-party execution-trust consent. Opened when an enable OR a
            registry install is refused with code `app_execution_denied`, instead
            of surfacing the raw backend string in the error card above. */}
        <TrustAppModal
          app={trust.target}
          pending={trust.pending}
          failed={trust.failed}
          granted={trust.granted}
          onCancel={trust.cancel}
          onConfirm={trust.confirm}
        />

        {/* Uninstall confirmation modal */}
        {showUninstallConfirm && app && (
          // Modal backdrop: click-to-dismiss is a mouse affordance; keyboard
          // users dismiss via the Escape handler in onKeyDown below.
          // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
          <div className="fixed inset-0 z-[100] flex items-center justify-center bg-bg/60 backdrop-blur-sm animate-rise"
            onClick={() => setShowUninstallConfirm(false)}
            onKeyDown={e => { if (e.key === 'Escape') setShowUninstallConfirm(false) }}
            tabIndex={-1} ref={el => el?.focus()} role="dialog" aria-modal="true" aria-label={i18nT('pages.appDetailPage.confirm_uninstall')}
          >
            {/* Presentational wrapper: stops backdrop-dismiss on inner clicks. */}
            {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events */}
            <div className="bg-card border border-border rounded-xl p-6 max-w-sm w-full mx-4 shadow-xl" onClick={e => e.stopPropagation()}>
              <div className="flex items-center gap-3 mb-4">
                <div className="w-10 h-10 rounded-xl bg-danger/10 flex items-center justify-center">
                  <Trash2 size={20} className="text-danger" />
                </div>
                <div>
                  <div className="font-medium text-text">{i18nT('pages.appDetailPage.uninstall')} {appDisplayName(app)}?</div>
                  <div className="text-[12px] text-muted">{i18nT('pages.appDetailPage.v')}{app.installedVersion || app.version}</div>
                </div>
              </div>

              <p className="text-[13px] text-muted mb-4">{i18nT('pages.appDetailPage.this_will_remove_the_app_and_all_its_registered')}</p>

              <label htmlFor="keep-app-data" className="flex items-center gap-2 text-[13px] text-muted mb-5 cursor-pointer select-none">
                <input id="keep-app-data" type="checkbox" checked={keepData} onChange={e => setKeepData(e.target.checked)} className="rounded" aria-label={i18nT('pages.appDetailPage.keep_app_data')} />
                {i18nT('pages.appDetailPage.keep_app_data')}
              </label>

              <div className="flex items-center gap-2 justify-end">
                <Btn onClick={() => setShowUninstallConfirm(false)}>{i18nT('pages.appDetailPage.cancel')}</Btn>
                <Btn danger onClick={confirmUninstall} disabled={actionLoading === 'uninstall'}>
                  {actionLoading === 'uninstall' ? i18nT('pages.appDetailPage.removing') : i18nT('pages.appDetailPage.uninstall')}
                </Btn>
              </div>
            </div>
          </div>
        )}

        {/* Hero banner (only when the app ships one, or its local fallback survives a load failure) */}
        <HeroBanner src={heroSrc} fallbackSrc={heroSrcFallback || undefined} isDetail={heroIsDetail} />

        {/* Hero */}
        <div className="flex items-start gap-5 mb-6">
          {/* `relative` is load-bearing, not decoration: `rasterFill` absolutely
              insets the image, so without a positioned plate the icon resolves
              against the nearest positioned ancestor — there is none above this
              hero row, so it would escape to a page-level box. `overflow-hidden`
              is what makes the bled image take this plate's `rounded-2xl`. */}
          <div className="w-24 h-24 rounded-2xl bg-accent/10 flex items-center justify-center shrink-0 overflow-hidden relative">
            <AppIcon icon={app.icon} iconUrl={app.iconUrl} iconUrlDark={app.iconUrlDark} iconUrlFallback={app.iconUrlFallback} iconUrlFallbackDark={app.iconUrlFallbackDark} size={64} rasterFill />
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-3 mb-1 flex-wrap">
              <span className="text-xl font-medium text-text">{appDisplayName(app)}</span>
              {app.installed && isBuiltin && <Badge variant="aim">{i18nT('pages.appDetailPage.built_in')}</Badge>}
              {app.installed && isBuiltin && <Badge variant={app.enabled ? 'ok' : 'warn'}>{app.enabled ? i18nT('pages.appDetailPage.enabled') : i18nT('pages.appDetailPage.disabled')}</Badge>}
              {app.installed && isSelfManaged && !isBuiltin && <Badge variant="ok">{i18nT('pages.appDetailPage.self_managed')}</Badge>}
              {app.installed && !isSelfManaged && !isBuiltin && <Badge variant={app.enabled ? 'ok' : 'warn'}>{app.enabled ? i18nT('pages.appDetailPage.enabled') : i18nT('pages.appDetailPage.disabled')}</Badge>}
            </div>
            <div className="text-[13px] text-muted mb-3 flex items-center gap-1 flex-wrap">
              <span>{app.author} {i18nT('pages.appDetailPage.v_2')}{app.version}</span>
              {typeof app.stargazersCount === 'number' && (
                <span className="inline-flex items-center gap-0.5">
                  · <Star size={13} className="shrink-0" role="img" aria-label={i18nT('pages.appDetailPage.github_stars')} />
                  {fmtCompact(app.stargazersCount)}
                </span>
              )}
            </div>

            {/* Actions */}
            <div className="flex items-center gap-2 flex-wrap">
              {!app.installed && !clientInstall && (
                <Btn primary onClick={handleInstall} disabled={actionLoading === 'install'}>
                  {actionLoading === 'install' ? <><Loader2 size={14} className="animate-spin" /> {i18nT('pages.appDetailPage.installing')}</> : <><Download size={14} /> {i18nT('pages.appDetailPage.install')}</>}
                </Btn>
              )}
              {!app.installed && clientInstall && (
                <div className="text-[13px] text-muted flex items-center gap-1.5"><Monitor size={14} /> {i18nT('pages.appDetailPage.requires_local_install')}</div>
              )}
              {app.installed && isBuiltin && (
                <>
                  {app.enabled ? (
                    <Btn onClick={() => handleAction('disable')} disabled={actionLoading === 'disable'}><PowerOff size={14} /> {i18nT('pages.appDetailPage.disable')}</Btn>
                  ) : (
                    /* Enable stays available in a browser: enabling is a
                       server-side state change (backend, agents and crons run
                       in the gateway) and only the app's own window needs the
                       desktop shell. The requirement is shown in TEXT here (not
                       only a hover title): a tooltip is unreachable by touch or
                       keyboard, and the bare "Desktop app" label reads as a
                       category tag rather than a requirement — and this detail
                       page is where a user decides to enable the app. The compact
                       store row / feature card keep the badge alone (the decision
                       does not happen there). */
                    <>
                    <Btn onClick={() => handleAction('enable')} disabled={actionLoading === 'enable'}><Power size={14} /> {i18nT('pages.appDetailPage.enable')}</Btn>
                    {desktopOnly && (
                    <span className="text-[13px] text-muted flex items-center gap-1.5">
                      <Monitor size={14} /> {i18nT('pages.appDetailPage.desktop_app_hint')}
                    </span>
                  )}
                    </>
                  )}
                </>
              )}
              {app.installed && isSelfManaged && !isBuiltin && (
                <>
                  <div className="text-[13px] text-ok flex items-center gap-1.5"><Check size={14} /> {i18nT('pages.appDetailPage.installed_version', { version: app.installedVersion })}</div>
                  {app.updateAvailable && <Btn onClick={handleInstall} disabled={actionLoading === 'install'} className="!bg-[var(--info)] !text-white hover:!opacity-80">{actionLoading === 'install' ? <><Loader2 size={14} className="animate-spin" /> {i18nT('pages.appDetailPage.updating')}</> : <><ArrowUp size={14} /> {i18nT('pages.appDetailPage.update')}</>}</Btn>}
                  {canUninstall && <Btn danger onClick={() => handleAction('uninstall')} disabled={actionLoading === 'uninstall'} title={i18nT('pages.appDetailPage.removes_kirocrew_metadata_only_the_app_itself_is')}><Trash2 size={14} /> {i18nT('pages.appDetailPage.uninstall')}</Btn>}
                </>
              )}
              {app.installed && !isSelfManaged && !isBuiltin && (
                <>
                  {app.enabled ? (
                    <Btn onClick={() => handleAction('disable')} disabled={actionLoading === 'disable'}><PowerOff size={14} /> {i18nT('pages.appDetailPage.disable')}</Btn>
                  ) : (
                    /* Enable stays available in a browser: enabling is a
                       server-side state change (backend, agents and crons run
                       in the gateway) and only the app's own window needs the
                       desktop shell. Replacing the button with a static claim
                       left every browser user at a dead end — and this page is
                       where store rows land, so it was the common path. Same
                       pattern as AppListRow / FeaturedSpotlight rows. */
                    <>
                    <Btn onClick={() => handleAction('enable')} disabled={actionLoading === 'enable'}><Power size={14} /> {i18nT('pages.appDetailPage.enable')}</Btn>
                    {desktopOnly && (
                    /* The consequence is VISIBLE here, not only in `title`. A
                       hover tooltip does not exist on touch and is not reachable
                       by keyboard, and on its own the bare "Desktop app" label
                       reads as a category tag rather than a requirement — so the
                       one place a user decides whether to enable the app was the
                       one place the requirement could go unread. The compact
                       store row and feature card keep the badge alone: every
                       other badge in those components works that way, and the
                       decision does not happen there. */
                    <span className="text-[13px] text-muted flex items-center gap-1.5">
                      <Monitor size={14} /> {i18nT('pages.appDetailPage.desktop_app_hint')}
                    </span>
                  )}
                    </>
                  )}
                  {canUpdate && app.updateAvailable && <Btn onClick={handleInstall} disabled={actionLoading === 'install'} className="!bg-[var(--info)] !text-white hover:!opacity-80">{actionLoading === 'install' ? <><Loader2 size={14} className="animate-spin" /> {i18nT('pages.appDetailPage.updating')}</> : <><ArrowUp size={14} /> {i18nT('pages.appDetailPage.update')}</>}</Btn>}
                  {canUpdate && !app.updateAvailable && <Btn onClick={() => handleAction('update')} disabled={actionLoading === 'update'} title={i18nT('pages.appDetailPage.sync_app_from_its_source_directory')}><RefreshCw size={14} /> {i18nT('pages.appDetailPage.sync')}</Btn>}
                  {canUninstall && <Btn danger onClick={() => handleAction('uninstall')} disabled={actionLoading === 'uninstall'}><Trash2 size={14} /> {i18nT('pages.appDetailPage.uninstall')}</Btn>}
                </>
              )}
            </div>
          </div>
        </div>

        {/* Install log (inline, between hero and description) */}
        {showInstallLog && (
          <Card>
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-2">
                {!installDone && <Loader2 size={14} className="animate-spin text-accent" />}
                {installDone && !error && <Check size={14} className="text-ok" />}
                {installDone && error && <X size={14} className="text-danger" />}
                <CardTitle>
                  {!installDone ? i18nT('pages.appDetailPage.installing') : error ? i18nT('pages.appDetailPage.install_failed') : i18nT('pages.appDetailPage.install_complete')}
                </CardTitle>
              </div>
              <div className="flex items-center gap-2">
                {installDone && error && (
                  <Btn onClick={() => {
                    const appSourcePath = `~/.kiro/crew/app-sources/${app?.name || name}/`
                    const msg = [
                      `App "${app?.displayName || name}" installation failed. Error log:`,
                      '',
                      '```',
                      installLog.slice(-2000),
                      '```',
                      '',
                      `The app source is at: ${appSourcePath}`,
                      `Read the README.md and any setup instructions in that directory, then fix the environment and complete the installation.`,
                    ].join('\n')
                    openChatWithMessage(msg)
                  }}>
                    <Sparkles size={14} /> {i18nT('pages.appDetailPage.fix_with_ai')}
                  </Btn>
                )}
                {installDone && (
                  <button className="text-muted hover:text-text transition-colors p-1" onClick={() => setShowInstallLog(false)} aria-label={i18nT('pages.appDetailPage.close')}>
                    <X size={14} />
                  </button>
                )}
              </div>
            </div>
            <pre
              ref={installLogRef}
              className="bg-bg border border-border rounded-lg p-3 text-[12px] text-muted whitespace-pre-wrap font-mono max-h-64 overflow-y-auto"
            >{installLog || i18nT('pages.appDetailPage.starting_install')}</pre>
          </Card>
        )}

        {/* Client install instructions */}
        {clientInstall && clientInstall.shell && (() => {
          // Replace template variables with actual values
          const gatewayUrl = window.location.origin
          const gatewayHost = serverHostname || '<your-cloud-desktop-host>'
          const replaceVars = (s: string) => s
            .replace(/\{\{gateway_url\}\}/g, gatewayUrl)
            .replace(/\{\{gateway_host\}\}/g, gatewayHost)
          const resolvedShell = replaceVars(clientInstall.shell!)
          const resolvedPostInstall = replaceVars(clientInstall.postInstall || '')
          return (
          <Card>
            <div className="flex items-start gap-3">
              <div className="w-10 h-10 rounded-xl bg-accent/10 flex items-center justify-center shrink-0 mt-0.5">
                <Terminal size={20} className="text-accent" />
              </div>
              <div className="flex-1 min-w-0">
                <div className="font-medium text-text mb-1">{i18nT('pages.appDetailPage.install_on_your_mac')}</div>
                <p className="text-[13px] text-muted mb-3">
                  {i18nT('pages.appDetailPage.this_app_requires_macos_and_needs_to_be_installe')}
                </p>
                <div className="relative group/cmd">
                  <pre className="bg-bg border border-border rounded-lg p-3 pr-10 text-[13px] font-mono text-text overflow-x-auto whitespace-pre-wrap break-all">{resolvedShell}</pre>
                  <button
                    className="absolute top-2 right-2 p-1.5 rounded-md bg-bg-elevated border border-border text-muted hover:text-text hover:border-accent/40 transition-all opacity-0 group-hover/cmd:opacity-100"
                    aria-label={i18nT('pages.appDetailPage.copy_command')}
                    onClick={() => {
                      navigator.clipboard.writeText(resolvedShell)
                      setCopied(true)
                      setTimeout(() => setCopied(false), 2000)
                    }}
                  >
                    {copied ? <Check size={14} className="text-ok" /> : <Copy size={14} />}
                  </button>
                </div>
                {resolvedPostInstall && (
                  <p className="text-[12px] text-muted mt-2">
                    {i18nT('pages.appDetailPage.after_installation_run')} <code className="bg-bg-elevated px-1.5 py-0.5 rounded text-[12px]">{resolvedPostInstall}</code>
                  </p>
                )}
                <p className="text-[12px] text-muted mt-2">
                  {i18nT('pages.appDetailPage.once_launched_the_app_will_automatically_connect')}
                </p>
              </div>
            </div>
          </Card>
          )
        })()}

        {/* Description */}
        <Card>
          <p className="text-sm text-muted leading-relaxed">{appDescription(app)}</p>
        </Card>

        {/* Screenshots */}
        {(() => {
          const dark = app.screenshotsDark || []
          const light = app.screenshots || []
          const useDark = resolvedMode === 'dark' && dark.length > 0
          // The fallback list must come from the SAME theme family the
          // primary list came from: the two arrays pair by index against the
          // same declared manifest field, so mixing families (dark primary,
          // light fallback) could pair a thumbnail with a different image
          // entirely. When the matching family has no local list, the gallery
          // stays default-inert, exactly as before #6864.
          return (
            <ScreenshotGallery
              screenshots={useDark ? dark : light}
              fallbacks={useDark ? app.screenshotsDarkFallback : app.screenshotsFallback}
            />
          )
        })()}

        {/* Concise operator guidance, kept separate from the marketing feature list. */}
        {(useCases.length > 0 || configuration.length > 0) && (
          <div className="grid grid-cols-[repeat(auto-fit,minmax(280px,1fr))] gap-4 mb-4">
            {useCases.length > 0 && (
              <Card>
                <CardTitle>
                  <Target className="lucide-inline text-accent" />{' '}
                  {i18nT('pages.appDetailPage.use_cases')}
                </CardTitle>
                <div className="grid gap-2 mt-2">
                  {useCases.map((item, i) => (
                    <div key={i} className="flex items-start gap-2.5 text-[13px] text-text">
                      <span className="mt-[7px] size-1.5 rounded-full bg-accent shrink-0" />
                      <span>{item}</span>
                    </div>
                  ))}
                </div>
              </Card>
            )}
            {configuration.length > 0 && (
              <Card>
                <CardTitle>
                  <Settings2 className="lucide-inline text-accent" />{' '}
                  {i18nT('pages.appDetailPage.configuration')}
                </CardTitle>
                <div className="grid gap-2 mt-2">
                  {configuration.map((item, i) => (
                    <div key={i} className="flex items-start gap-2.5 text-[13px] text-text">
                      <span className="mt-[7px] size-1.5 rounded-full bg-accent shrink-0" />
                      <span>{item}</span>
                    </div>
                  ))}
                </div>
              </Card>
            )}
          </div>
        )}

        {/* Features */}
        {(app.highlights || []).length > 0 && (
          <Card>
            <CardTitle>{i18nT('pages.appDetailPage.features')}</CardTitle>
            <div className="grid gap-2 mt-2">
              {appHighlights(app).map((h, i) => (
                <div key={i} className="flex items-start gap-2.5 text-[13px] text-text">
                  <Check size={13} className="text-ok mt-0.5 shrink-0" />
                  <span>{h}</span>
                </div>
              ))}
            </div>
          </Card>
        )}

        {/* Info grid */}
        <div className="grid grid-cols-[repeat(auto-fit,minmax(280px,1fr))] gap-4 mt-4">
          {/* Permissions (transparency) */}
          {app.manifest?.permissions && (
            <Card>
              <CardTitle>{i18nT('pages.appDetailPage.permissions')}</CardTitle>
              <div className="grid gap-2 mt-2 text-[13px]">
                {(app.manifest.permissions.api || []).length > 0 && (
                  <div>
                    <div className="text-muted text-[11px] uppercase tracking-wider mb-1">{i18nT('pages.appDetailPage.api_access')}</div>
                    <div className="flex flex-wrap gap-1">
                      {(app.manifest.permissions.api || []).map((p: string) => (
                        <code key={p} className="bg-bg-elevated border border-border px-1.5 py-0.5 rounded text-[11px] text-text">{p}</code>
                      ))}
                    </div>
                  </div>
                )}
                {(app.manifest.permissions.events || []).length > 0 && (
                  <div>
                    <div className="text-muted text-[11px] uppercase tracking-wider mb-1">{i18nT('pages.appDetailPage.websocket_events')}</div>
                    <div className="flex flex-wrap gap-1">
                      {(app.manifest.permissions.events || []).map((e: string) => (
                        <code key={e} className="bg-bg-elevated border border-border px-1.5 py-0.5 rounded text-[11px] text-text">{e}</code>
                      ))}
                    </div>
                  </div>
                )}
                {(app.manifest.permissions.mcpTools || []).length > 0 && (
                  <div>
                    <div className="text-muted text-[11px] uppercase tracking-wider mb-1">{i18nT('pages.appDetailPage.mcp_tools')}</div>
                    <div className="flex flex-wrap gap-1">
                      {(app.manifest.permissions.mcpTools || []).map((t: string) => (
                        <code key={t} className="bg-ok-subtle border border-ok/20 px-1.5 py-0.5 rounded text-[11px] text-ok">{t}</code>
                      ))}
                    </div>
                  </div>
                )}
                <div className="flex flex-wrap gap-3 text-[12px] text-muted mt-1">
                  {app.manifest.permissions.storage && <span className="flex items-center gap-1">{i18nT('pages.appDetailPage.storage_yes')}</span>}
                  {app.manifest.permissions.cron && <span className="flex items-center gap-1">{i18nT('pages.appDetailPage.cron_yes')}</span>}
                  {app.manifest.permissions.network && <span className="flex items-center gap-1">{i18nT('pages.appDetailPage.network_yes')}</span>}
                  {app.manifest.permissions.memory && <span className="flex items-center gap-1">{i18nT('pages.appDetailPage.memory')} {String(app.manifest.permissions.memory)}</span>}
                </div>
              </div>
            </Card>
          )}

          {/* MCP Servers */}
          {app.manifest?.mcpServers && Object.keys(app.manifest.mcpServers).length > 0 && (
            <Card>
              <CardTitle>{i18nT('pages.appDetailPage.mcp_servers')}</CardTitle>
              <div className="grid gap-2 mt-2 text-[13px]">
                {Object.entries(app.manifest.mcpServers).map(([sName, sConfig]) => (
                  <div key={sName} className="bg-bg-elevated border border-border rounded-md px-2.5 py-2">
                    <div className="font-mono font-medium text-text text-[12px]">{sName}</div>
                    {sConfig.url && <div className="text-muted text-[11px] mt-0.5">{sConfig.url}</div>}
                    {sConfig.command && <div className="text-muted text-[11px] mt-0.5">{sConfig.command}</div>}
                    {(sConfig.autoApprove || []).length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-1.5">
                        {(sConfig.autoApprove || []).map((t: string) => (
                          <span key={t} className="bg-ok-subtle border border-ok/20 px-1 py-0 rounded text-[10px] text-ok">{t}</span>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </Card>
          )}

          {/* Tags */}
          {(app.tags || []).length > 0 && (
            <Card>
              <CardTitle>{i18nT('pages.appDetailPage.tags')}</CardTitle>
              <div className="flex items-center gap-1.5 flex-wrap mt-2">
                {app.tags!.map(t => (
                  <span key={t} className="bg-bg-elevated border border-border px-2 py-0.5 rounded text-[11px] text-muted">{t}</span>
                ))}
              </div>
            </Card>
          )}

          {/* Resources (installed only) */}
          {app.installed && (agents.length > 0 || skills.length > 0 || crons.length > 0) && (
            <Card>
              <CardTitle>{i18nT('pages.appDetailPage.resources')}</CardTitle>
              <div className="grid gap-1.5 mt-2 text-[13px]">
                {agents.length > 0 && (
                  <div className="flex items-start gap-2 text-muted">
                    <Bot size={13} className="mt-0.5 shrink-0" />
                    <div>{agents.map((a: string) => a.split('/').pop()?.replace('.json', '')).join(', ')}</div>
                  </div>
                )}
                {skills.length > 0 && (
                  <div className="flex items-start gap-2 text-muted">
                    <Zap size={13} className="mt-0.5 shrink-0" />
                    <div>{skills.map((s: string) => s.split('/').pop()).join(', ')}</div>
                  </div>
                )}
                {crons.length > 0 && (
                  <div className="flex items-start gap-2 text-muted">
                    <Clock size={13} className="mt-0.5 shrink-0" />
                    <div>{crons.map((c) => c.name).join(', ')}</div>
                  </div>
                )}
              </div>
            </Card>
          )}

          {/* Metadata */}
          <Card>
            <CardTitle>{i18nT('pages.appDetailPage.details')}</CardTitle>
            <div className="grid gap-1.5 mt-2 text-[13px] text-muted">
              {app.repo && <div>{i18nT('pages.appDetailPage.repository')} {app.repo}</div>}
              {typeof app.stargazersCount === 'number' && <div>{i18nT('pages.appDetailPage.github_stars_2', { value: fmtNumber(app.stargazersCount) })}</div>}
              {app.author && <div>{i18nT('pages.appDetailPage.author')} {app.author}</div>}
              {repoUrl && (contribLoading || contributors.length > 0) && (
                <div className="flex items-start gap-2 flex-wrap">
                  <span className="shrink-0">{i18nT('pages.appDetailPage.contributors')}</span>
                  {contribLoading ? (
                    <div className="flex gap-1.5">
                      {[0, 1, 2].map(i => (
                        <span
                          key={i}
                          className="inline-block h-[22px] w-16 rounded-full animate-pulse"
                          style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)' }}
                        />
                      ))}
                    </div>
                  ) : (
                    <div className="flex items-center gap-1.5 flex-wrap">
                      {contributors.slice(0, 6).map(c => (
                        <span
                          key={c.login}
                          title={c.name}
                          className="inline-flex items-center gap-1.5 rounded-full pl-0.5 pr-2 py-0.5"
                          style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', color: 'var(--text)' }}
                        >
                          <span
                            className="relative inline-flex h-[18px] w-[18px] items-center justify-center overflow-hidden rounded-full text-[10px]"
                            style={{ background: 'var(--accent-subtle)', color: 'var(--accent)' }}
                          >
                            <span>{(c.name || c.login).slice(0, 1).toUpperCase()}</span>
                            {c.avatarUrl && /^https:\/\/[^"')(\s]+$/.test(c.avatarUrl) && (
                              <span
                                aria-hidden="true"
                                className="absolute inset-0 rounded-full"
                                style={{ backgroundImage: `url("${c.avatarUrl}")`, backgroundSize: 'cover', backgroundPosition: 'center' }}
                              />
                            )}
                          </span>
                          <span className="text-[12px]">{c.name}</span>
                        </span>
                      ))}
                      <a
                        href={`${repoUrl.replace(/\.git$/, '').replace(/\/$/, '')}/graphs/contributors`}
                        target="_blank"
                        rel="noreferrer noopener"
                        className="inline-flex items-center rounded-full px-2 py-0.5 text-[12px] no-underline"
                        style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', color: 'var(--muted)' }}
                      >
                        {i18nT('pages.appDetailPage.all_contributors')}
                      </a>
                    </div>
                  )}
                </div>
              )}
              {app.installedAt && <div>{i18nT('pages.appDetailPage.installed')} {fmtDateNumeric(app.installedAt)}</div>}
              {app.origin && <div>{i18nT('pages.appDetailPage.origin')} {app.origin} {i18nT('pages.appDetailPage.resources_2')} {app.resources || 'gateway'} {i18nT('pages.appDetailPage.lifecycle')} {app.lifecycle || 'gateway'}</div>}
              {app.manifest?.minKiroCrewVersion && <div>{i18nT('pages.appDetailPage.min_kirocrew_v')}{app.manifest.minKiroCrewVersion}</div>}
              {app.platform?.os && <div>{i18nT('pages.appDetailPage.platform')} {app.platform.os.join(', ')}</div>}
            </div>
          </Card>
        </div>
      </div>
    </>
  )
}
