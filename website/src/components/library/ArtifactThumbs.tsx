import { useState, useMemo, useEffect, useRef } from 'react'
import { Cloud, ImageOff, Rocket } from 'lucide-react'
import { widgetHeightKey, getWidgetHeight, setWidgetHeight, estimateWidgetHeight } from '../../utils/widgetHeights'
import { getImageDims, rememberImageDims } from '../../utils/imageDims'
import { sanitize } from '../../api/helpers'
import { useTheme } from '../../hooks/useTheme'
import { sanitizeCssValue } from '../../lib/cssSanitize'
import { framablePreviewUrl } from '../../lib/safeUrl'
import { useCloudDeploymentEnabled } from '../../hooks/useCloudDeploymentEnabled'
import { useAppPreview } from '../WebAppArtifactCard'
import { THEME_VAR_NAMES, buildSrcdoc } from '../../lib/widgetSrcdoc'
import MarkdownRenderer from '../MarkdownRenderer'
import { i18nT } from '../../i18n/t'
import { useSandboxDoc } from '../../hooks/useSandboxDoc'
import { useNearViewport } from '../../hooks/useNearViewport'
import type { Artifact } from '../../types'

/** Read the current computed theme CSS vars (capped to the known set, each
 * value sanitized) so a sandboxed preview iframe matches the dashboard theme.
 * Mirrors the helper in ArtifactDetailPage. */
function readThemeVars(): Record<string, string> {
  if (typeof window === 'undefined' || typeof document === 'undefined') return {}
  const computed = getComputedStyle(document.documentElement)
  const out: Record<string, string> = {}
  for (const name of THEME_VAR_NAMES) {
    const v = sanitizeCssValue(computed.getPropertyValue(name))
    if (v) out[name] = v
  }
  return out
}

/** Key space for cached thumbnail heights. These are measured at BASE_W and
 *  clamped to VIEWPORT_H, so they are not comparable with the heights a
 *  full-width `WidgetFrame` measures and must not share its entries. */
const THUMB_HEIGHT_SPACE = 'thumb900'

/** Live preview of a widget/html artifact, rendered as a scaled-down
 * thumbnail: the iframe lays out at a fixed desktop width (BASE_W) so the
 * widget looks normal, then the whole frame is CSS-scaled to fit the column —
 * a minified webpage, not a cramped narrow render. */
export function WidgetThumb({ content, slug }: { content: string; slug: string }) {
  const BASE_W = 900
  // Fixed iframe viewport height (in BASE_W space). The iframe NEVER grows past
  // this — it only shrinks for genuinely short flow-content. This makes
  // viewport-sized content (height:100vh / 100%, e.g. slide decks) impossible to
  // ratchet: the reported height is clamped to the viewport, so 100vh can't feed
  // itself taller. Tall flow-content (dashboards) is clipped to the viewport top.
  const VIEWPORT_H = 560
  const { theme, colorTheme, themeVersion } = useTheme()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const themeVars = useMemo(() => readThemeVars(), [theme, colorTheme, themeVersion])
  const srcdoc = useMemo(
    () => (content ? buildSrcdoc({ html: content, themeVars, mode: theme, includeHeightReporter: true }) : null),
    [content, themeVars, theme],
  )
  // Reserve the height this content had last time, or the median of thumbnails
  // already measured, before falling back to the viewport ceiling.
  //
  // Seeding from VIEWPORT_H instead makes every thumbnail start at the MAXIMUM
  // and correct downward when the iframe reports — so the error is one-way, and
  // inside a virtualized list the corrections accumulate into a scroller whose
  // total height keeps shrinking as you scroll (measured: 4.1k px of height
  // change and 1.2k px of drift over eight swipes). `WidgetFrame` solved the
  // same problem the same way; the key space is separate because these
  // thumbnails lay out at a fixed BASE_W while a frame lays out at its
  // container's width, so the two sets of heights are not comparable.
  const heightKey = useMemo(() => widgetHeightKey(content, THUMB_HEIGHT_SPACE), [content])
  const [contentH, setContentH] = useState(
    () => getWidgetHeight(heightKey) ?? Math.min(VIEWPORT_H, estimateWidgetHeight(THUMB_HEIGHT_SPACE, VIEWPORT_H)),
  ) // iframe height at BASE_W (≤ VIEWPORT_H)
  const [colW, setColW] = useState(320) // measured column/preview width
  const wrapRef = useRef<HTMLDivElement>(null)
  const iframeRef = useRef<HTMLIFrameElement>(null)

  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const measure = () => setColW(el.clientWidth || 320)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // A gateway-served document, not a `blob:` URL — the same reason the artifact
  // and widget frames moved: some WebKit-based in-app browsers refuse a blob
  // load outright and can take the whole page down with it.
  const near = useNearViewport(wrapRef)
  // Mint only once the thumb is near the viewport. A gallery renders every card
  // it has, and minting for all of them at once pushes a pile of documents the
  // gateway has to hold in flight simultaneously — with image-bearing artifacts
  // that is megabytes, and the stash then has to refuse mints it could otherwise
  // have served. Deferring keeps the pressure proportional to what is on screen.
  const { url: blobUrl, failed } = useSandboxDoc(near ? srcdoc : null)
  // Gated on the FIRST document only, and deliberately NOT reset when a new url
  // lands. A re-mint navigates the frame again, and re-hiding on every new url
  // leaves the thumb blank until the next `load` fires — long enough to read as
  // the preview vanishing when the gateway is reached through a tunnel, and
  // permanent if a further re-mint arrives first. Same reasoning as
  // ArtifactBody's `everLoaded`; keep the two in step.
  const [everLoaded, setEverLoaded] = useState(false)
  // The load listener is bound on the ref rather than via an `onLoad` prop: the
  // a11y lint rule counts any handler prop on a non-interactive element as an
  // interaction, and the repo's eslint ratchet has no room for a new warning.
  useEffect(() => {
    const el = iframeRef.current
    if (!el || !blobUrl) return
    const onLoad = () => setEverLoaded(true)
    el.addEventListener('load', onLoad)
    return () => el.removeEventListener('load', onLoad)
  }, [blobUrl])

  useEffect(() => {
    const handler = (e: MessageEvent) => {
      if (!iframeRef.current || e.source !== iframeRef.current.contentWindow) return
      if (e.data?.type === 'mc-widget-height' && typeof e.data.height === 'number') {
        // Reject non-finite reports outright: `typeof NaN === 'number'`, and
        // NaN flows straight through min/max/round into BOTH the persisted
        // cache and contentH (rendering height:NaN). A misbehaving widget
        // must not be able to corrupt the geometry every later mount reserves
        // from.
        if (!Number.isFinite(e.data.height)) return
        // Clamp to the viewport ceiling so viewport-sized content (100vh) can
        // never grow the iframe — and thus can never grow itself.
        const next = Math.min(VIEWPORT_H, Math.max(80, Math.round(e.data.height)))
        // Remember it before the state update: this is what lets the NEXT mount
        // of the same content reserve the right box and not correct at all.
        setWidgetHeight(heightKey, next)
        setContentH((prev) => (next === prev ? prev : next))
      }
    }
    window.addEventListener('message', handler)
    return () => window.removeEventListener('message', handler)
  }, [heightKey])

  // Re-reserve when the CONTENT changes: the card mounts before its lazy
  // fetch resolves (`hasPreview` gates on kind, not content), so the useState
  // initializer above ran against the EMPTY content's key. Without this
  // resync the persisted height for the real content is ignored and every
  // mount pays one avoidable correction when the iframe reports — the same
  // height-churn class the virtualized gallery works to eliminate.
  useEffect(() => {
    setContentH(getWidgetHeight(heightKey) ?? Math.min(VIEWPORT_H, estimateWidgetHeight(THUMB_HEIGHT_SPACE, VIEWPORT_H)))
  }, [heightKey])

  const scale = colW / BASE_W
  // contentH is already clamped to VIEWPORT_H in the reporter, so the iframe
  // never grows past the fixed viewport — no feedback loop is possible.
  const renderH = contentH
  const scaledH = Math.round(renderH * scale)

  return (
    <div
      ref={wrapRef}
      className="relative w-full overflow-hidden bg-card"
      // The SAME box before and after the iframe exists. Reserving a different
      // placeholder height (and then swapping) is a second height change per
      // card on top of the report, and in a virtualized list every one of those
      // re-lays out everything below it.
      style={{ height: scaledH }}
    >
      {blobUrl ? (
        <iframe
          ref={iframeRef}
          src={blobUrl}
          sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"
          title={i18nT('pages.artifactsPage.preview', { slug })}
          tabIndex={-1}
          className="border-none bg-card block"
          style={{
            width: BASE_W,
            height: renderH,
            // `translateZ(0)` composed onto the scale, not instead of it: the
            // scale is the thumbnail's whole geometry. A 2D transform makes a
            // stacking context but does NOT force this frame onto its own
            // compositing layer, and an engine can then lay the document out,
            // run its scripts and report a correct height while rasterizing
            // nothing -- an empty card behind the same opacity-on-load reveal,
            // which across a grid is the whole gallery blank. The 3D form is
            // what promotes; it adds no visual offset with no perspective set.
            // Same remedy as ArtifactBody / WidgetFrame / RemoteArtifactDetail;
            // keep the four in step.
            transform: `scale(${scale}) translateZ(0)`,
            transformOrigin: 'top left',
            // Hidden until the document reports load: until then the engine may
            // paint its own WHITE canvas over this element's background, which
            // across a grid of cards reads as the page flashing.
            colorScheme: theme,
            opacity: everLoaded ? 1 : 0,
          }}
        />
      ) : failed ? (
        /* A static muted thumb, NOT the pulse: `animate-pulse` asserts progress,
           and after a failed mint nothing is in flight. Repeated across a grid an
           endless pulse reads as a hung page rather than a failed card. The card
           itself is the retry affordance — opening the artifact mints again. */
        <div className="h-full bg-bg-elevated flex items-center justify-center p-2">
          <span className="text-[11px] text-muted text-center leading-tight">
            {i18nT('components.artifactBody.could_not_render')}
          </span>
        </div>
      ) : (
        <div className="h-full bg-bg-elevated animate-pulse" />
      )}
    </div>
  )
}

/** Kind-aware preview for non-iframe artifacts: markdown is rendered, SVG is
 * drawn (sanitized), JSON is pretty-printed, everything else is a raw snippet.
 * All paths are height-capped so cards stay tidy. */
export function ContentThumb({ content, kind }: { content: string; kind: Artifact['kind'] }) {
  if (!content.trim()) return <div className="h-[64px] bg-bg-elevated" />

  if (kind === 'markdown') {
    return (
      <div className="px-3 py-2 max-h-[300px] overflow-hidden bg-card msg-content text-[12px] leading-relaxed">
        <MarkdownRenderer content={content.slice(0, 4000)} />
      </div>
    )
  }

  if (kind === 'svg') {
    const clean = sanitize(content)
    return (
      <div
        className="px-3 py-3 max-h-[300px] overflow-hidden bg-card flex items-center justify-center [&>svg]:max-w-full [&>svg]:max-h-[280px] [&>svg]:h-auto"
        dangerouslySetInnerHTML={{ __html: clean }}
      />
    )
  }

  let body = content
  if (kind === 'json') {
    try { body = JSON.stringify(JSON.parse(content), null, 2) } catch { /* keep raw on parse failure */ }
  }
  return (
    <pre className="m-0 px-3 py-2 text-[11px] leading-snug text-muted font-mono whitespace-pre-wrap break-words max-h-[260px] overflow-hidden bg-bg-elevated">
      {body.slice(0, 1200)}
    </pre>
  )
}

/** Thumbnail for image artifacts: the picture streamed straight from the
 * artifact's asset endpoint (the server sets Content-Type), object-fit
 * contained and height-capped so cards stay uniform. Lazy so off-screen
 * gallery cards don't fetch bytes until scrolled into view. Alt text prefers
 * the stored `image.alt`, falling back to the artifact name. */
export function ImageThumb({ a }: { a: Artifact }) {
  // The asset endpoint can legitimately 404/500 (pruned sidecar, unreadable
  // file, refused mime). A bare <img> would leave the browser's broken-image
  // glyph sitting in an otherwise healthy card with nothing to read.
  const [failed, setFailed] = useState(false)
  // Natural dimensions, best source first: the save-time header sniff in the
  // artifact metadata, else the client-side cache learned from a prior load
  // (legacy artifacts saved before the sniff existed). Either way the browser
  // derives an aspect ratio from the ATTRIBUTES and reserves the final
  // contain-fit box before any bytes arrive — without it the card mounts
  // ~16px tall and grows ~280px when the lazy load lands, shoving everything
  // below it mid-scroll on every pass (the virtualizer's row-height cache
  // sizes placeholders, not a remounted card's own empty <img> box).
  const meta = a.image
  const known = meta?.width && meta?.height ? { w: meta.width, h: meta.height } : getImageDims(a.slug)
  if (failed) {
    return (
      <div className="flex flex-col items-center justify-center gap-1 max-h-[300px] h-[120px] overflow-hidden bg-bg-elevated p-2 text-center">
        <ImageOff size={16} className="text-muted shrink-0" aria-hidden="true" />
        <span className="text-[11px] text-muted">
          {i18nT('pages.artifactsPage.image_could_not_be_loaded')}
        </span>
      </div>
    )
  }
  return (
    <div className="flex items-center justify-center max-h-[300px] overflow-hidden bg-bg-elevated p-2">
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- onLoad/onError are image-load lifecycle handlers (learn the natural size, else degrade to the "could not be loaded" tile), not user interactions; the thumb stays a plain image with nothing for a keyboard to reach */}
      <img
        src={`/api/artifacts/${a.slug}/asset`}
        alt={a.image?.alt || a.name}
        loading="lazy"
        width={known?.w}
        height={known?.h}
        className="max-w-full max-h-[280px] object-contain"
        draggable={false}
        // Learn the natural size on a successful load so the NEXT mount of a
        // legacy image (no sniffed metadata) reserves correctly. Slug-keyed:
        // a re-upload overwrites on its next load.
        onLoad={(e) => {
          if (!meta?.width || !meta?.height) {
            rememberImageDims(a.slug, e.currentTarget.naturalWidth, e.currentTarget.naturalHeight)
          }
        }}
        onError={() => setFailed(true)}
      />
    </div>
  )
}

const WEBAPP_STATUS_DOT: Record<string, string> = {
  live: 'bg-ok',
  deploying: 'bg-warn animate-pulse',
  expired: 'bg-muted-strong',
  error: 'bg-danger',
}

/** Gallery preview for webapp artifacts — a mock browser window so an app
 * card reads as "a website" next to the html/widget iframe thumbs, never as
 * a wall of raw description text. Live CloudFront deployments embed the real
 * site (same scaled-viewport trick as WidgetThumb, no height reporter needed:
 * fixed 16:10 viewport); every other state gets a status hero. `mini` drops
 * the iframe (an 84px folder tile can't render a meaningful site). */
export function WebAppThumb({ art, mini = false }: { art: Artifact; mini?: boolean }) {
  const BASE_W = 1280
  const BASE_H = 800
  const meta = art.webapp_metadata
  const status = meta?.lifecycle?.status ?? 'draft'
  const publicUrl = meta?.deploy_target?.public_url || ''
  // When the platform withholds cloud deployment there is no deploy state worth
  // reporting: every artifact would read "Not deployed" for a capability that is
  // not on offer. The local preview below is unaffected and still renders.
  const cloudDeployEnabled = useCloudDeploymentEnabled()
  // Local-first: serve the app's local copy through the gateway preview
  // channel (works for every lifecycle state); fall back to iframing the
  // live CloudFront deployment; else a status hero.
  const { base: previewBase, remoteFramable } = useAppPreview(art.slug, !mini && !!meta)
  const frameUrl = previewBase
    || (!mini && status === 'live' && remoteFramable ? framablePreviewUrl(publicUrl) : null)
  const urlLabel = (() => {
    if (!publicUrl) return cloudDeployEnabled ? i18nT('pages.artifactsPage.not_deployed') : ''
    try {
      const u = new URL(publicUrl)
      return `${u.host}${u.pathname}`
    } catch {
      return cloudDeployEnabled ? i18nT('pages.artifactsPage.not_deployed') : ''
    }
  })()
  const wrapRef = useRef<HTMLDivElement>(null)
  const [colW, setColW] = useState(320)
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const measure = () => setColW(el.clientWidth || 320)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const scale = colW / BASE_W
  const heroIcon = status === 'expired'
    ? <Cloud size={mini ? 16 : 24} className="text-muted" aria-hidden="true" />
    : <Rocket size={mini ? 16 : 24} className={status === 'deploying' ? 'text-warn animate-pulse' : 'text-accent/70'} aria-hidden="true" />
  const heroLabel = status === 'expired' ? i18nT('pages.artifactsPage.expired') : status === 'deploying' ? i18nT('pages.artifactsPage.deploying') : status === 'live' ? i18nT('pages.artifactsPage.live') : cloudDeployEnabled ? i18nT('pages.artifactsPage.not_deployed_2') : i18nT('pages.artifactsPage.local_preview')
  return (
    <div className="bg-card">
      {/* chrome bar */}
      <div className={`flex items-center gap-1.5 px-2 ${mini ? 'py-1' : 'py-1.5'} bg-bg-elevated border-b border-border`}>
        <div className="flex gap-1 shrink-0" aria-hidden="true">
          <span className="w-1.5 h-1.5 rounded-full bg-danger/40" />
          <span className="w-1.5 h-1.5 rounded-full bg-warn/40" />
          <span className="w-1.5 h-1.5 rounded-full bg-ok/40" />
        </div>
        <div className="flex-1 min-w-0 flex items-center gap-1 px-1.5 py-0.5 rounded bg-card border border-border">
          <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${WEBAPP_STATUS_DOT[status] ?? 'bg-muted-strong'}`} aria-hidden="true" />
          <span className="text-[10px] text-muted truncate font-mono">{urlLabel}</span>
        </div>
      </div>
      {frameUrl ? (
        <div ref={wrapRef} className="relative w-full overflow-hidden bg-card" style={{ height: Math.round(BASE_H * scale) }}>
          <iframe
            src={frameUrl}
            // Local channel (/artifact-app/...): scripts ONLY — the path is
            // dashboard-origin, so allow-same-origin here would hand the app
            // the dashboard's cookies/DOM (the channel's own CSP `sandbox`
            // header enforces an opaque origin as a second layer).
            // Remote CloudFront fallback: allow-same-origin refers to the
            // site's own origin, never the dashboard's.
            sandbox={previewBase ? 'allow-scripts' : 'allow-scripts allow-same-origin'}
            referrerPolicy="no-referrer"
            loading="lazy"
            title={i18nT('pages.artifactsPage.app_preview', { slug: art.slug })}
            tabIndex={-1}
            className="border-none bg-card block"
            style={{ width: BASE_W, height: BASE_H, transform: `scale(${scale})`, transformOrigin: 'top left' }}
          />
        </div>
      ) : (
        <div className={`flex flex-col items-center justify-center gap-1.5 ${mini ? 'py-3' : 'py-8'} bg-gradient-to-br from-accent-subtle via-card to-bg-elevated`}>
          {heroIcon}
          <span className={`${mini ? 'text-[10px]' : 'text-[12px]'} text-muted font-medium`}>{heroLabel}</span>
          {!mini && meta?.architecture && (
            <span className="text-[10px] text-muted">
              {[meta.architecture.frontend && 'frontend', meta.architecture.backend && 'api', meta.architecture.state && 'db'].filter(Boolean).join(' \u00b7 ')}
            </span>
          )}
        </div>
      )}
    </div>
  )
}
