/**
 * AppIcon — shared icon component for app cards and detail pages.
 *
 * Two rendering paths:
 *  1. iconUrl SVGs (builtin apps, served from /app-assets/) are fetched and
 *     inlined so the theme's CSS variables cascade into them. Each icon paints
 *     with two tokens driven by `selected`:
 *       idle      → --ico-a: var(--muted)  --ico-b: var(--accent)
 *       selected  → --ico-a: var(--accent) --ico-b: var(--text)
 *     Non-app-asset iconUrls (e.g. registry blob proxy) render as a plain <img>.
 *  2. A lucide-react icon from ICON_MAP (falls back to Package).
 *
 * Both paths honour ``iconUrlDark``: the inline path rarely needs it (theme
 * tokens already repaint first-party SVGs), but a raster icon has fixed bytes,
 * so an app that wants to read well on both backgrounds ships two files.
 *
 * An optional ``iconUrlFallback``/``iconUrlFallbackDark`` pair gives the raster
 * path a second chance when the resolved URL fails to LOAD (as opposed to not
 * being declared): the detail page hands an installed app's own local art route
 * here, so an unreachable registry CDN degrades to the app's real mark instead
 * of the generic glyph. Omitted, the pair is inert and behaviour is unchanged.
 *
 * The two paths also differ in GEOMETRY, which is what ``rasterFill`` exists
 * for: a glyph is line art that needs air around it and is always inset at
 * ``size``, while an app-supplied raster icon is a finished square tile that a
 * plate-drawing caller wants bled to the edges. The flag reaches only the
 * ``<img>`` branches, so no caller can accidentally crop a glyph with it.
 */
import { useEffect, useId, useMemo, useState } from 'react'
import DOMPurify from 'dompurify'
import {
  Shield, Bot, Search, Tag, Users, Zap, Star, Package, Cat,
} from 'lucide-react'
import { useTheme } from '../hooks/useTheme'

const ICON_MAP: Record<string, typeof Shield> = {
  Shield, Bot, Search, Tag, Users, Zap, Star, Package, Cat,
}

// In-memory cache of fetched inline SVG markup, keyed by url.
const svgCache = new Map<string, string>()

/**
 * True only for our own first-party themeable builtin icons that use the
 * --ico-a/--ico-b tokens. Deliberately strict: exactly two clean path
 * segments under /app-assets/ ending in .svg, with NO '.' or '/' inside a
 * segment — so traversal payloads like `/app-assets/../apps/evil/ui/icon.svg`
 * (which pass a naive startsWith check but normalize elsewhere in the browser)
 * are rejected. Anything else takes the plain <img> path.
 */
const APP_ASSET_ICON_RE = /^\/app-assets\/[a-zA-Z0-9_-]+\/[a-zA-Z0-9_-]+\.svg$/
function isAppAssetSvg(url?: string): url is string {
  return !!url && APP_ASSET_ICON_RE.test(url)
}

/**
 * Prefix every `id="x"` (and its `url(#x)` references) with a per-instance
 * token so multiple inlined copies of the same icon don't collide on ids
 * like the file-explorer overlap mask.
 */
function uniquifyIds(markup: string, prefix: string): string {
  const ids = new Set<string>()
  markup.replace(/\bid="([^"]+)"/g, (_m, id) => { ids.add(id); return _m })
  let out = markup
  ids.forEach((id) => {
    const safe = id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    out = out
      .replace(new RegExp(`id="${safe}"`, 'g'), `id="${prefix}-${id}"`)
      .replace(new RegExp(`url\\(#${safe}\\)`, 'g'), `url(#${prefix}-${id})`)
  })
  return out
}

export default function AppIcon({
  icon,
  iconUrl,
  iconUrlDark,
  iconUrlFallback,
  iconUrlFallbackDark,
  size = 20,
  rasterFill = false,
  selected = false,
}: {
  icon?: string
  iconUrl?: string
  /**
   * Optional dark-appearance variant. Resolution mirrors ``useHeroArt``:
   * prefer the current theme's art, fall back to the other one. Falling back
   * in BOTH directions matters — an app that ships only a dark icon should
   * render it in light mode rather than dropping to the lucide glyph, which
   * would read as "this app has no icon".
   *
   * First-party ``/app-assets/`` SVGs do not need this: they are inlined and
   * painted from the --ico-a/--ico-b theme tokens, so one file already covers
   * both appearances. It exists for the raster path, where the bytes are fixed.
   */
  iconUrlDark?: string
  /**
   * Optional second-chance URL pair, consulted only when the resolved primary
   * URL fails to LOAD. The detail page passes the installed app's own on-disk
   * art route here, so an offline dashboard still shows the app's real mark —
   * while the registry's immutable content-addressed asset stays the primary
   * ``src`` and keeps its cache-forever win (a precedence flip is exactly what
   * issue #6804 rejects). Theme resolution mirrors ``iconUrl``/``iconUrlDark``,
   * falling back in both directions. When the fallback also fails, the
   * component degrades to the lucide glyph exactly as before. The fallback
   * always renders on the raster ``<img>`` path — an ``/app-assets/`` SVG value
   * here is not inlined, so it paints with its literal fills, not theme tokens.
   */
  iconUrlFallback?: string
  iconUrlFallbackDark?: string
  size?: number
  /**
   * Let a RASTER icon bleed to the edges of its container instead of sitting
   * inset at ``size``. An app-supplied icon file is a finished tile — the
   * publishing guide asks for a 512x512 opaque square — so on any surface that
   * already draws a rounded plate for it the icon IS the plate, and rendering it
   * inset leaves the app looking like a small sticker stuck on that plate.
   *
   * Deliberately scoped to the raster ``<img>`` branches, so it CANNOT change
   * the two glyph paths: a first-party ``/app-assets/`` SVG and a lucide
   * ``ICON_MAP`` fallback are line-art marks drawn to be read with air around
   * them, and bleeding those to the edge would crop their strokes against the
   * plate's border. That split is the whole reason this is one flag on the
   * component rather than a size the caller raises to the container's width.
   *
   * Fills by ``object-cover``, so an off-spec non-square icon is centre-cropped
   * rather than letterboxed.
   *
   * CALLER CONTRACT — the plate must be BOTH ``relative`` and
   * ``overflow-hidden``. The image is absolutely inset, so an unpositioned plate
   * hands it to the nearest positioned ancestor instead and the icon escapes the
   * plate entirely (on the store's spotlight that ancestor is the 16:9 art
   * panel, so the icon would render as hero art); and the clip is what makes the
   * bled image take the plate's own radius. A test pins both classes at every
   * call site that passes this flag, because neither failure is visible in the
   * diff — only in the render.
   */
  rasterFill?: boolean
  /** Lit (accent-dominant) vs idle (muted + accent highlight). */
  selected?: boolean
}) {
  const { theme } = useTheme()
  const url = (theme === 'dark'
    ? (iconUrlDark || iconUrl)
    : (iconUrl || iconUrlDark)) || undefined
  const fallbackUrl = (theme === 'dark'
    ? (iconUrlFallbackDark || iconUrlFallback)
    : (iconUrlFallback || iconUrlFallbackDark)) || undefined
  const [imgFailed, setImgFailed] = useState(false)
  const [fallbackFailed, setFallbackFailed] = useState(false)
  const [markup, setMarkup] = useState<string | null>(
    isAppAssetSvg(url) ? svgCache.get(url) ?? null : null,
  )
  const rawId = useId()
  // React's useId yields ':r0:' style tokens; sanitize for use in SVG ids.
  const idPrefix = `ai${rawId.replace(/[^a-zA-Z0-9]/g, '')}`
  // Sanitize the fetched SVG (strips <script>/<foreignObject onload> etc.)
  // BEFORE inlining — required by the `frontend-security` lint rule and a
  // defense-in-depth backstop on top of the strict isAppAssetSvg allowlist.
  // The SVG profile preserves the <mask>/url(#…)/fill markup these icons need.
  const scopedMarkup = useMemo(() => {
    if (!markup) return null
    const clean = DOMPurify.sanitize(markup, {
      USE_PROFILES: { svg: true, svgFilters: true },
    })
    return uniquifyIds(clean, idPrefix)
  }, [markup, idPrefix])

  useEffect(() => {
    // Reset per-URL state so a reused AppIcon instance never shows a stale
    // icon or a sticky failure when its icon changes — including a THEME flip,
    // which changes ``url`` without any prop the parent re-keys on. Hydrate
    // synchronously from cache when available; otherwise clear and fetch below.
    // A changed icon clears BOTH failure latches: an app update rewrites the
    // local file in place, so a stale fallback latch would be the same
    // sticky-failure bug this reset exists to prevent.
    setImgFailed(false)
    setFallbackFailed(false)
    const cached = isAppAssetSvg(url) ? svgCache.get(url) ?? null : null
    setMarkup(cached)
    if (!isAppAssetSvg(url) || svgCache.has(url)) return
    let cancelled = false
    fetch(url)
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error('fetch failed'))))
      .then((text) => {
        if (text.trim().startsWith('<svg')) {
          svgCache.set(url, text)
          if (!cancelled) setMarkup(text)
        }
      })
      .catch(() => { if (!cancelled) setImgFailed(true) })
    return () => { cancelled = true }
  }, [url])

  useEffect(() => {
    // The same per-URL reset discipline for the fallback latch alone: the
    // fallback candidate can change independently of the primary (a theme flip
    // where only the fallback pair has a dark variant, an install completing
    // while the page is open) and must never inherit a stale failure.
    setFallbackFailed(false)
  }, [fallbackUrl])

  // Raster geometry, shared by BOTH <img> branches so the second-chance
  // fallback can never render at a different size than the icon it stands in
  // for. Inset at ``size`` is the default; under ``rasterFill`` the image is
  // absolutely inset to the container's edges and carries NO radius of its own
  // — the container is ``overflow-hidden`` and already has one, and a second
  // radius here would disagree with it (the Library's tile is 15px, the store's
  // rows are 8px).
  const rasterClass = rasterFill
    ? 'absolute inset-0 w-full h-full object-cover'
    : 'rounded-lg object-contain'
  const rasterStyle = rasterFill ? undefined : { width: size, height: size }

  // Themeable inline SVG path. The `.app-icon` class sets idle tokens
  // (--ico-a: muted, --ico-b: accent); `data-selected` OR an ancestor
  // `.group:hover` promotes to the lit accent-dominant state (see index.css).
  if (isAppAssetSvg(url) && !imgFailed) {
    if (scopedMarkup) {
      return (
        <span
          aria-hidden
          data-selected={selected || undefined}
          className="app-icon inline-flex shrink-0 [&>svg]:w-full [&>svg]:h-full"
          style={{ width: size, height: size }}
          dangerouslySetInnerHTML={{ __html: scopedMarkup }}
        />
      )
    }
    // While fetching (or before sanitize), reserve space to avoid layout shift.
    return <span className="inline-flex shrink-0" style={{ width: size, height: size }} />
  }

  // Non-app-asset image (e.g. registry blob proxy). Raster art cannot repaint
  // from theme tokens, which is the whole reason ``iconUrlDark`` exists.
  if (url && !imgFailed) {
    return (
      // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- onError is an image-load lifecycle handler (degrade to the fallback URL, then the glyph), not a user interaction; there is nothing here for a keyboard to reach
      <img
        src={url}
        alt=""
        className={rasterClass}
        style={rasterStyle}
        onError={() => setImgFailed(true)}
      />
    )
  }

  // Second chance: the primary URL failed to LOAD and the caller supplied a
  // fallback (an installed app's own local bytes). Skipped when it matches the
  // failed primary — retrying the identical URL that just errored is a second
  // doomed request — and once it has itself failed, so the escape below stays
  // the terminal state and no broken-image frame is ever left on screen.
  if (imgFailed && fallbackUrl && fallbackUrl !== url && !fallbackFailed) {
    return (
      // onError is an image-load lifecycle handler (degrade to the glyph), the
      // same non-interactive use the hero image documents on AppDetailPage.
      // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
      <img
        src={fallbackUrl}
        alt=""
        className={rasterClass}
        style={rasterStyle}
        onError={() => setFallbackFailed(true)}
      />
    )
  }

  const Icon = icon && ICON_MAP[icon] ? ICON_MAP[icon] : Package
  return <Icon size={size} />
}
