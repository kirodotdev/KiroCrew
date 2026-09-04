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
 * Resolve a manifest art path the way the installed-app surfaces resolve
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
 * Honour a manifest's ``iconUrl``/``iconUrlDark`` for what the contract says it IS:
 * a BUILTIN's absolute client-local path, whose bytes the client already ships.
 *
 * A RELATIVE value is refused rather than turned into an art-route URL, and that
 * asymmetry is the point. The backend's declared-field set
 * (``_ART_MANIFEST_FIELDS``) carries ``iconPath``, not ``iconUrl`` — because for a
 * FETCHED app ``iconUrl`` is ignored by design, so that the publisher cannot name a
 * host in a field the client would load. So building ``/apps/<name>/art/<relative>``
 * out of an ``iconUrl`` produces a URL the route refuses by construction: a
 * guaranteed 404 dressed as a fallback. One side has to own that contract, and the
 * manifest contract already does.
 *
 * Named for the shape it accepts rather than the field it reads, because that is
 * what the caller is choosing: "this value is only usable if it is already a path
 * this origin serves."
 */
export function clientLocalArt(path: unknown): string {
  return classifyManifestArt(path) === 'same-origin' ? asParserSees(path as string) : ''
}

/**
 * Resolve ONE art path for an app that is INSTALLED, against its own files.
 *
 * The bytes of an installed app's icon, hero and screenshots are already on
 * local disk, inside the directory the install created. Reaching them through
 * ``/api/apps/blob`` instead means a git clone gated by an SSRF
 * allowlist — so a catalog-listed app's art could 403 on a cold load, because
 * that allowlist is warmed by a network fetch the Library render can outrun
 * (its card list gates on the installed-apps query alone) and an ``<img>`` does
 * not retry. ``/apps/{name}/art/…`` reads the file the gateway itself wrote:
 * no network, no ordering, no host in the request.
 *
 * A manifest is untrusted content, so a cross-origin value is refused by
 * :func:`classifyManifestArt` rather than handed to ``<img>``, and anything
 * unusable answers ``''`` so a caller keeps degrading to the
 * gradient. The leading ``./`` is stripped to match the backend, which compares
 * the request against the manifest's declared paths in that normalized form.
 *
 * Reads the fields the backend's declared set actually carries — ``iconPath``,
 * ``heroImage*``, ``screenshots*``. For ``iconUrl``/``iconUrlDark`` use
 * :func:`clientLocalArt`: a relative value there would build a URL the route
 * refuses by construction.
 *
 * Segments are encoded individually: the path is a manifest-declared value and
 * may contain a space, which must not arrive as a raw space in the URL, while
 * the ``/`` separators must survive.
 */
export function installedArt(path: unknown, appName: string | undefined): string {
  const kind = classifyManifestArt(path)
  if (kind === 'refused') return ''
  const value = asParserSees(path as string)
  if (kind === 'same-origin') return value
  if (!appName) return ''
  const rel = value.startsWith('./') ? value.slice(2) : value
  const encoded = rel.split('/').map(encodeURIComponent).join('/')
  return `/apps/${encodeURIComponent(appName)}/art/${encoded}`
}

/**
 * Resolve a LIST of an installed app's art paths, dropping every refused entry.
 *
 * ``unknown`` rather than ``string[]`` because the array's TYPE is as untrusted as
 * its entries: the installed-app normalizer coerces ``screenshots`` but not
 * ``screenshotsDark``, so an ``app.json`` declaring ``"screenshotsDark": {}``
 * would reach a bare ``.map`` and throw.
 */
export function installedArtList(paths: unknown, appName: string | undefined): string[] {
  if (!Array.isArray(paths)) return []
  return paths.map(p => installedArt(p, appName)).filter(Boolean)
}

/**
 * Like :func:`installedArtList` but INDEX-ALIGNED with the declared list:
 * refused entries become ``''`` placeholders instead of being dropped.
 *
 * Exists for callers that pair this list positionally with ANOTHER resolution
 * of the same declared field (the registry row's blob-proxy rewrite, in the
 * detail page's screenshot fallback). With the filtered variant, a refused
 * middle entry shifts every later index down by one, so thumbnail N silently
 * pairs with the art for thumbnail N-1 — a wrong image, which is worse than a
 * hidden one. Callers must skip the ``''`` placeholders at use time.
 */
export function installedArtListAligned(paths: unknown, appName: string | undefined): string[] {
  if (!Array.isArray(paths)) return []
  return paths.map(p => installedArt(p, appName))
}

/**
 * An installed app's ICON, resolving the two fields that can declare one.
 *
 * This exists because the two-term rule was spelled out at eight call sites and
 * they diverged: the rail and detail page resolved ``iconPath`` first while the
 * Library card and Updates list resolved ``iconUrl`` first, so a manifest
 * declaring BOTH wore one icon in the rail and a different one on its own card.
 * The order is only observable for that manifest, which is exactly why four
 * copies of it drifted without anything going red.
 *
 * ``iconPath`` wins because it is the field that addresses a file inside the
 * install directory — the app's own art, on local disk. ``iconUrl`` is the
 * client-local ABSOLUTE path a builtin declares (see ``clientLocalArt``, which
 * refuses a relative one), so it is the fallback rather than the primary.
 */
export function installedIcon(
  path: unknown,
  url: unknown,
  appName: string | undefined,
): string {
  return installedArt(path, appName) || clientLocalArt(url)
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
