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
 */
import { useEffect, useId, useMemo, useState } from 'react'
import DOMPurify from 'dompurify'
import {
  Shield, Bot, Search, Tag, Users, Zap, Star, Package, Cat,
} from 'lucide-react'

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
  size = 20,
  selected = false,
}: {
  icon?: string
  iconUrl?: string
  size?: number
  /** Lit (accent-dominant) vs idle (muted + accent highlight). */
  selected?: boolean
}) {
  const [imgFailed, setImgFailed] = useState(false)
  const [markup, setMarkup] = useState<string | null>(
    isAppAssetSvg(iconUrl) ? svgCache.get(iconUrl) ?? null : null,
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
    // icon or a sticky failure when its iconUrl changes. Hydrate synchronously
    // from cache when available; otherwise clear and fetch below.
    setImgFailed(false)
    const cached = isAppAssetSvg(iconUrl) ? svgCache.get(iconUrl) ?? null : null
    setMarkup(cached)
    if (!isAppAssetSvg(iconUrl) || svgCache.has(iconUrl)) return
    let cancelled = false
    fetch(iconUrl)
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error('fetch failed'))))
      .then((text) => {
        if (text.trim().startsWith('<svg')) {
          svgCache.set(iconUrl, text)
          if (!cancelled) setMarkup(text)
        }
      })
      .catch(() => { if (!cancelled) setImgFailed(true) })
    return () => { cancelled = true }
  }, [iconUrl])

  // Themeable inline SVG path. The `.app-icon` class sets idle tokens
  // (--ico-a: muted, --ico-b: accent); `data-selected` OR an ancestor
  // `.group:hover` promotes to the lit accent-dominant state (see index.css).
  if (isAppAssetSvg(iconUrl) && !imgFailed) {
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

  // Non-app-asset image (e.g. registry blob proxy).
  if (iconUrl && !imgFailed) {
    return (
      <img
        src={iconUrl}
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
