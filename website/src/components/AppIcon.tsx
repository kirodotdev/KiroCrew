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
  size = 20,
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
  size?: number
  /** Lit (accent-dominant) vs idle (muted + accent highlight). */
  selected?: boolean
}) {
  const { theme } = useTheme()
  const url = (theme === 'dark'
    ? (iconUrlDark || iconUrl)
    : (iconUrl || iconUrlDark)) || undefined
  const [imgFailed, setImgFailed] = useState(false)
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
    setImgFailed(false)
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
      <img
        src={url}
        alt=""
        className="rounded-lg object-contain"
        style={{ width: size, height: size }}
        onError={() => setImgFailed(true)}
      />
    )
  }

  const Icon = icon && ICON_MAP[icon] ? ICON_MAP[icon] : Package
  return <Icon size={size} />
}
