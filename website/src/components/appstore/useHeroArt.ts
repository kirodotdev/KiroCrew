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
 * How a manifest-declared art path may be used.
 *
 * `'same-origin'` is fetchable exactly as written and cannot leave this origin —
 * a built-in's ``/app-assets/…``, or a store row's own ``/api/apps/blob?…`` URL.
 * `'relative'` needs a base, and what it is relative TO differs per field, so
 * the caller supplies it. `'refused'` is a value this surface must not request
 * at all.
 */
export type ArtPathKind = 'same-origin' | 'relative' | 'refused'

/**
 * A same-origin base to resolve a candidate art path against.
 *
 * Escaping an origin is a property of the VALUE's own syntax, not of the base —
 * so a value that leaves this (unreachable) origin would equally leave the
 * dashboard's, and one that stays inside it stays inside the dashboard's. Using
 * a fixed base instead of `window.location` keeps the rule deterministic and
 * testable, and means the classifier does not need a DOM.
 */
const ORIGIN_PROBE_BASE = 'https://origin-probe.invalid/apps/detail/probe'
const ORIGIN_PROBE_ORIGIN = 'https://origin-probe.invalid'

/**
 * The URL parser's own preprocessing, reproduced so the value we HAND to
 * ``<img>`` is the value we classified.
 *
 * The parser removes every ASCII tab and newline anywhere in the input and trims
 * leading C0 controls and spaces, all BEFORE parsing. Measured: against a
 * same-origin base, ``/<TAB>/host/x``, ``/<LF>/host/x``, ``<TAB>//host/x`` and
 * ``<SPACE>//host/x`` all resolve to ``https://host`` — so no test on the raw
 * string's first characters can decide anything. (A space or form feed MID-value
 * is not stripped and stays on-origin, which is why this mirrors the spec's exact
 * set rather than "all whitespace".)
 */
function asParserSees(path: string): string {
  return path.replace(/[\t\n\r]/g, '').replace(/^[\u0000-\u0020]+/, '')
}

/**
 * Classify one art path read off an installed app's ``app.json``.
 *
 * The parameter is ``unknown`` because a manifest is JSON from disk and its
 * field TYPES are not guaranteed either: the installed-app normalizer coerces
 * some list fields but passes unknown keys through verbatim, so an ``app.json``
 * declaring ``"iconPath": {}`` arrives here as an object. A bare ``startsWith``
 * would throw and take the whole surface down, so anything that is not a
 * non-empty string is refused.
 *
 * An installed manifest is untrusted content: honouring an absolute URL out of
 * it would let a third party point the store's ``<img>`` at any host, so merely
 * rendering the app would leak the viewer's address and headers to that host.
 * The rule is therefore POSITIVE — a value is accepted only when the URL parser
 * itself says it lands on our own origin — rather than a list of forbidden
 * spellings. Three spellings defeated three successive prefix tests here
 * (protocol-relative ``//``, the backslash forms the parser reads as slashes,
 * and a tab or leading space splitting the two slashes), which is the evidence
 * that the parser has to be the authority and not a regex approximating it.
 *
 * This mirrors the backend, which honours only the repo-relative ``iconPath``
 * when it builds a store row and never a manifest-declared ``iconUrl``.
 */
export function classifyManifestArt(path: unknown): ArtPathKind {
  if (typeof path !== 'string' || !path) return 'refused'
  const value = asParserSees(path)
  if (!value) return 'refused'
  // Parses on its own => it carries a scheme, so it is not ours to honour.
  try {
    new URL(value)
    return 'refused'
  } catch {
    // Relative: keep going and let the origin check decide.
  }
  try {
    if (new URL(value, ORIGIN_PROBE_BASE).origin !== ORIGIN_PROBE_ORIGIN) return 'refused'
  } catch {
    return 'refused'
  }
  return value.startsWith('/') ? 'same-origin' : 'relative'
}

/**
 * Resolve ONE art path read straight off an installed app's ``app.json``.
 *
 * Store rows arrive server-enriched (the backend rewrites ``iconPath`` and the
 * hero/screenshot fields into ``/api/apps/blob?…`` URLs), so a surface that
 * renders a row needs no resolution. A surface that falls back to the manifest
 * does: a local-directory install has no row at all, and a row built from a
 * cached manifest predating the release that added the art carries those fields
 * empty while the manifest on disk has them.
 *
 * Unlike :func:`resolveArtPath`, an unusable path answers ``''`` rather than
 * passing through — both a repo-relative path with no repo to resolve against
 * and a refused cross-origin one. Passing through is right for a store row (the
 * value may already be absolute) but wrong here: the browser would resolve
 * ``assets/hero.webp`` against the current route, get the SPA shell, and the
 * ``<img>`` would fail silently instead of degrading to the gradient.
 */
export function manifestArt(path: unknown, repo: string | undefined): string {
  const kind = classifyManifestArt(path)
  if (kind === 'refused') return ''
  // The normalized form, not the raw one: classifying one string and emitting
  // another is exactly the gap a tab-splitting value walks through.
  const value = asParserSees(path as string)
  if (kind === 'same-origin') return value
  return repo ? resolveArtPath(value, repo) : ''
}

/**
 * Resolve a LIST of manifest art paths, dropping every entry the rules refuse.
 *
 * The array itself is ``unknown`` for the same reason each entry is: the
 * installed-app normalizer coerces ``screenshots`` but not ``screenshotsDark``,
 * so an ``app.json`` declaring ``"screenshotsDark": {}`` would reach a bare
 * ``.map`` and throw.
 */
export function manifestArtList(paths: unknown, repo: string | undefined): string[] {
  if (!Array.isArray(paths)) return []
  return paths.map(p => manifestArt(p, repo)).filter(Boolean)
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
