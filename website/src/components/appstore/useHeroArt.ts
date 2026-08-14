/**
 * useHeroArt — theme-aware hero image resolution for store surfaces.
 *
 * Resolution order: prefer the current theme's artwork, fall
 * back to the opposite theme, then the first screenshot. Callers pair the
 * returned ``src`` with ``failed``/``onError`` so a 404'd hero degrades to the
 * gradient instead of rendering a blank panel.
 */
import { useEffect, useState } from 'react'
import { useTheme } from '../../hooks/useTheme'
import type { RegistryApp } from './types'

type HeroFields = Pick<RegistryApp, 'heroImage' | 'heroImageDark' | 'screenshots' | 'repo'>

/** Matches a URL scheme prefix ("https:", "data:", …) — such paths are never repo-relative. */
const SCHEME_RE = /^[a-z][a-z0-9+.-]*:/i

/**
 * Resolve a manifest art path the way ``InstalledAppCard`` resolves
 * ``iconPath``: a repo-relative path (registry apps declare art relative to
 * their repo root) is routed through the blob proxy, while absolute paths
 * (``/app-assets/...`` built-ins) and full URLs pass through untouched so
 * shipping apps keep working byte-for-byte. Server-enriched registry rows
 * already arrive as ``/api/apps/blob?...`` URLs and start with ``/``, so they
 * are naturally left alone rather than double-wrapped.
 */
export function resolveArtPath(path: string, repo?: string): string {
  if (!path || !repo) return path
  if (path.startsWith('/') || SCHEME_RE.test(path)) return path
  // The blob proxy rejects "." path segments; "./assets/x.png" means the same
  // repo-relative path as "assets/x.png", so normalize the common form.
  const rel = path.startsWith('./') ? path.slice(2) : path
  return `/api/apps/blob?repo=${encodeURIComponent(repo)}&path=${encodeURIComponent(rel)}`
}

/**
 * True when the app ships ANY art ``useHeroArt`` could render (either theme's
 * hero, or a screenshot). Featured ranking uses this so a dark-only or
 * screenshot-only app is not treated as art-less.
 */
export function hasHeroArt(app: HeroFields): boolean {
  return !!(app.heroImage || app.heroImageDark || app.screenshots?.[0])
}

/**
 * *app* is optional so a caller can hold the hook call unconditional while still
 * declining to render: a surface whose app list came from a published document
 * may legitimately have nothing to show, and React forbids skipping the hook to
 * handle that. No app means no art, which is the same answer as an app shipping
 * none.
 */
export function useHeroArt(app?: HeroFields): { src: string; onError: () => void } {
  const { theme } = useTheme()
  const dark = theme === 'dark'
  const chosen = (dark
    ? (app?.heroImageDark || app?.heroImage)
    : (app?.heroImage || app?.heroImageDark)) || app?.screenshots?.[0] || ''
  // Repo-relative manifest paths (all three fields: heroImage, heroImageDark,
  // screenshots) resolve through the blob proxy; absolute paths pass through.
  const resolved = resolveArtPath(chosen, app?.repo)
  const [failed, setFailed] = useState('')
  // Reset the failure latch when the resolved art changes (theme flip, or a
  // re-fetch that filled in metadata) so a new URL gets a fresh attempt.
  useEffect(() => { setFailed('') }, [resolved])
  return {
    src: failed === resolved ? '' : resolved,
    onError: () => setFailed(resolved),
  }
}
