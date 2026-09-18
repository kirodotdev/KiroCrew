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
 * A path segment the URL parser treats as a DOT SEGMENT during normalization.
 *
 * WHATWG path normalization reads `%2e` as `.` (case-insensitively), so the
 * double-dot set is `..`, `.%2e`, `%2e.` and `%2e%2e`, and the single-dot set
 * is `.` and `%2e`. Matching the parser's own set — not just literal dots — is
 * what keeps a percent spelling from walking through, the same
 * parser-is-the-authority rule the origin probe below follows.
 */
const DOT_SEGMENT_RE = /^(?:\.|%2e){1,2}$/i

/**
 * True when the URL parser would MOVE this path while normalizing it.
 *
 * Judges the value the PARSER will see (:func:`asParserSees` runs first, so a
 * tab or newline inside a dot segment cannot hide it), then splits on BOTH
 * separators: WHATWG converts ``\`` to ``/`` in special-scheme URLs before
 * normalizing, so ``\..\`` is the same escape as ``/../``. A value that
 * carries a dot segment stays same-origin — an origin probe alone passes it —
 * but the browser resolves the segments AFTER a resolver joins its route on,
 * so ``/apps/<name>/art/../../../api/tips/next`` is requested as
 * ``/api/tips/next``: a content-controlled ``<img>`` invoking an authenticated
 * API. One sanctioned LEADING ``./`` is allowed (it means the same
 * repo-relative path and every consumer strips it). Deliberately FAIL-CLOSED on
 * query/fragment text: a raw ``/../`` after ``?`` cannot move path segments, but
 * splitting the value at ``?`` would misjudge relative manifest values (where
 * ``?`` is literal path data once per-segment encoding runs), so a pathological
 * query costs a gradient fallback rather than a widened parser model.
 */
function hasTraversalSegments(path: string): boolean {
  const seen = asParserSees(path)
  const scan = (v: string): boolean => {
    const rel = v.startsWith('./') ? v.slice(2) : v
    return rel.split(/[\\/]/).some(seg => DOT_SEGMENT_RE.test(seg))
  }
  // Two views of the value, refused when EITHER carries a dot segment. The
  // whole-value view covers a relative manifest value, which is re-joined
  // under the art route after per-segment encoding — `?` and `#` are literal
  // path data there, so "a?/../y" would traverse the joined URL. The
  // pre-terminator view covers a verbatim browser-bound value, whose path
  // STOPS at the first `?` or `#` — "/app-assets/..?x" ends in a real dot
  // segment the whole-value split reads as one non-dot "..?x".
  return scan(seen) || scan(seen.split(/[?#]/)[0])
}

/**
 * The one absolute route the SERVER writes into a registry row: the blob-proxy
 * URL its enrichment step builds (``/api/apps/blob?repo=…&path=…``). Not part
 * of :data:`CLIENT_ART_ROUTE_RE` because a MANIFEST never legitimately
 * declares it — only :func:`resolveArtPath`, which consumes enriched rows,
 * accepts it. The literal ``?`` is required: a bare or sub-pathed spelling
 * names nothing the proxy serves.
 */
const ENRICHED_BLOB_ROUTE_RE = /^\/api\/apps\/blob\?./

/**
 * Resolve a manifest art path the way the installed-app surfaces resolve
 * ``iconPath``: a repo-relative path (registry apps declare art relative to
 * their repo root) is routed through the blob proxy, while allowlisted
 * absolute paths and full URLs pass through untouched so shipping apps keep
 * working byte-for-byte.
 *
 * A registry row is third-party content and this function's output lands in
 * ``<img src>`` verbatim, so each branch that passes a value through raw is
 * positively gated rather than open-ended:
 *
 * - ABSOLUTE values pass only on the art allowlist — shipped client assets and
 *   the installed-art route (:data:`CLIENT_ART_ROUTE_RE`), plus the
 *   server-enriched blob route (:data:`ENRICHED_BLOB_ROUTE_RE`) — and only
 *   with no traversal segments. "Same origin" alone is not a safety property:
 *   ``heroImage: "/api/tips/next"`` would fire an authenticated GET the row's
 *   author chose the moment the image renders.
 * - RELATIVE values with a repo are immune by construction: the blob proxy
 *   encodes them whole into a query parameter.
 * - RELATIVE values with NO repo are refused: they would render relative to
 *   the current page, which on a root-mounted SPA is the same reach as an
 *   absolute path, and nothing legitimate used that branch.
 * - FULL URLs pass through: a cross-origin image request carries no
 *   same-origin credentials, and registry rows may name CDNs by design.
 *
 * Branch decisions read the parser's view of the value (:func:`asParserSees`);
 * the clean value returns byte-identical.
 */
export function resolveArtPath(path: string, repo?: string): string {
  if (!path) return path
  const seen = asParserSees(path)
  if (seen.startsWith('/')) {
    if (hasTraversalSegments(seen)) return ''
    return CLIENT_ART_ROUTE_RE.test(seen) || ENRICHED_BLOB_ROUTE_RE.test(seen) ? path : ''
  }
  if (SCHEME_RE.test(seen)) return path
  if (!repo) return ''
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
 * leading AND trailing C0 controls and spaces, all BEFORE parsing. Measured:
 * against a same-origin base, ``/<TAB>/host/x``, ``/<LF>/host/x``,
 * ``<TAB>//host/x`` and ``<SPACE>//host/x`` all resolve to ``https://host`` — so
 * no test on the raw string's first characters can decide anything. The trailing
 * trim matters to the traversal guard for the same reason: ``/app-assets/.. ``
 * ends in what the raw string calls a non-dot segment, and the parser exposes
 * the ``..`` by trimming the space first. (A space or form feed MID-value is not
 * stripped and stays on-origin, which is why this mirrors the spec's exact set
 * rather than "all whitespace".)
 */
function asParserSees(path: string): string {
  return path
    .replace(/[\t\n\r]/g, '')
    .replace(/^[\u0000-\u0020]+/, '')
    .replace(/[\u0000-\u0020]+$/, '')
}

/**
 * The same-origin absolute paths a manifest may point ``<img>`` at: a shipped
 * client asset, or the installed-art route the resolvers themselves emit (the
 * hero/screenshot gates re-classify those outputs before rendering them). A
 * positive route allowlist, because "same origin" alone is not a safety
 * property here: an authenticated API route is same-origin too, and a manifest
 * naming ``/api/...`` outright would have it fetched — with credentials — the
 * moment a fallback ``<img>`` renders. The prefix must be followed by a real
 * asset path, so a bare route names nothing.
 */
const CLIENT_ART_ROUTE_RE = /^\/(?:app-assets|apps\/[^\\/]+\/art)\/[^\\/]/

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
 * Dot segments are the fourth refusal family, in the parser's own spelling set
 * (``..``, ``%2e%2e``, their mixes, and the backslash-separated forms — see
 * :func:`hasTraversalSegments`): a traversal value stays same-origin, so the
 * probe alone passes it, but the browser normalizes the segments AFTER a
 * resolver joins its route on — ``heroImage: "../../../api/tips/next"`` would
 * turn the installed-art route into a manifest-controlled request against an
 * authenticated API endpoint.
 *
 * This mirrors the backend, which honours only the repo-relative ``iconPath``
 * when it builds a store row and never a manifest-declared ``iconUrl``.
 */
export function classifyManifestArt(path: unknown): ArtPathKind {
  if (typeof path !== 'string' || !path) return 'refused'
  const value = asParserSees(path)
  if (!value) return 'refused'
  // TWO leading separators make an AUTHORITY reference: the parser consumes
  // "//host" as a host, not as path segments. A generic host fails the origin
  // comparison below, but a value naming the PROBE's own host would match it
  // exactly — while on the real dashboard origin it still targets that
  // external host. Refused by shape, before any origin math.
  if (/^[\\/]{2}/.test(value)) return 'refused'
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
  if (hasTraversalSegments(value)) return 'refused'
  if (!value.startsWith('/')) return 'relative'
  // Same-origin is necessary but not sufficient: only the client-art routes
  // are a manifest's to name (see CLIENT_ART_ROUTE_RE) — any other absolute
  // path is a request the manifest author chose, not art.
  return CLIENT_ART_ROUTE_RE.test(value) ? 'same-origin' : 'refused'
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
 * The slice of an INSTALLED app a caller hands over so the hook can build a
 * local second-chance candidate from the app's own manifest. The art fields
 * are ``unknown`` because a manifest is JSON from disk — :func:`installedArt`
 * owns the refusal of anything that is not a usable same-origin path, exactly
 * as it does for the detail page's fallbacks.
 */
export type InstalledArtSource = {
  name: string
  manifest?: {
    heroImage?: unknown
    heroImageDark?: unknown
    screenshots?: unknown
  } | null
}

/**
 * *app* is optional so a caller can hold the hook call unconditional while still
 * declining to render: a surface whose app list came from a published document
 * may legitimately have nothing to show, and React forbids skipping the hook to
 * handle that. No app means no art, which is the same answer as an app shipping
 * none.
 *
 * *installed* is the optional local second chance (#6887): when the app is
 * INSTALLED its art bytes are already on local disk, so a registry asset that
 * fails to LOAD (offline, captive portal, blocked host) swaps once to the
 * installed-app art route instead of degrading straight to the gradient — the
 * same two-latch discipline AppIcon (#6804) and the detail page (#6864) run.
 * Deliberately not a precedence change: the registry's asset stays the primary
 * ``src`` and keeps its cache win; the local candidate is consulted only when
 * that src errors, and never retried when it IS the failed primary. When the
 * fallback also fails — or none is supplied, every pre-existing caller — the
 * terminal state is ``''`` (the gradient), exactly as before. Both latches
 * reset when the resolved primary changes (theme flip, a re-fetch filling in
 * metadata); the fallback latch alone resets when the local candidate moves
 * (an install completing under a mounted page). The candidate is same-origin
 * by construction: it only ever comes out of :func:`installedArt`, which
 * refuses anything a third-party manifest could use to point this ``<img>``
 * at another host.
 */
export function useHeroArt(app?: HeroFields, installed?: InstalledArtSource): { src: string; onError: () => void } {
  const { theme } = useTheme()
  const dark = theme === 'dark'
  const chosen = (dark
    ? (app?.heroImageDark || app?.heroImage)
    : (app?.heroImage || app?.heroImageDark)) || app?.screenshots?.[0] || ''
  // Repo-relative manifest paths (all three fields: heroImage, heroImageDark,
  // screenshots) resolve through the blob proxy; absolute paths pass through.
  const resolved = resolveArtPath(chosen, app?.repo)
  // The local candidate mirrors the primary's field choice over the RESOLVED
  // values (a refused field falls through to the next, as the detail page's
  // fallback resolution does), then the first usable local screenshot — any of
  // the app's own art beats the gradient.
  const m = installed?.manifest
  const localLight = installedArt(m?.heroImage, installed?.name)
  const localDark = installedArt(m?.heroImageDark, installed?.name)
  const fallback = (dark ? (localDark || localLight) : (localLight || localDark))
    || installedArtList(m?.screenshots, installed?.name)[0] || ''
  const [failed, setFailed] = useState('')
  const [fallbackFailed, setFallbackFailed] = useState('')
  // Reset the failure latches when the resolved art changes (theme flip, or a
  // re-fetch that filled in metadata) so a new URL gets a fresh attempt. A
  // changed primary clears BOTH: an app update rewrites the local file in
  // place, so a stale fallback latch would be a sticky failure.
  useEffect(() => { setFailed(''); setFallbackFailed('') }, [resolved])
  // The same per-URL reset for the fallback latch alone: the candidate moves
  // independently of the primary and must never inherit a stale failure.
  useEffect(() => { setFallbackFailed('') }, [fallback])
  // '' never counts as a failed primary: with no registry art at all the hook
  // answers '' as it always has — the second chance is for a LOAD failure,
  // not a precedence flip.
  const primaryFailed = resolved !== '' && failed === resolved
  // Skipped when the candidate matches the failed primary (retrying the URL
  // that just errored is a second doomed request) and once it has itself
  // failed, so '' stays the terminal state.
  const secondChance = primaryFailed && fallback !== '' && fallback !== resolved && fallbackFailed !== fallback
  return {
    src: primaryFailed ? (secondChance ? fallback : '') : resolved,
    onError: () => {
      if (!primaryFailed) setFailed(resolved)
      else setFallbackFailed(fallback)
    },
  }
}
