import { ApiError } from '../api/apiError'
import { safeSetItem } from '../utils/safeStorage'

/**
 * What a Browser-panel target may be, and where it may go: input coercion
 * (`normalizeUrl`), the loopback, cookie-isolation and embed rules the panel
 * frames by, the gateway self-origin check, the launcher's URL-shape refusal,
 * and the session feed other surfaces write a target through
 * (`setSessionPreviewUrl` / `setSessionPreviewPending`). Pure apart from the
 * feed's storage write and window event. `WebPreviewPanel.tsx` is the only
 * import path for consumers; it re-exports the public names.
 */

export const URL_KEY_PREFIX = 'mc-webpreview-url:'
/** localStorage prefix for a chat-fed URL that is PENDING an explicit "Load
 *  preview" click — surfaced as a card, never auto-navigated (click-to-load). */
export const PENDING_KEY_PREFIX = 'mc-webpreview-pending:'
/** Window event that feeds a URL into a mounted panel from outside (ChatPage). */
export const PREVIEW_URL_EVENT = 'kirocrew-web-preview-url'
/** Window event feeding a PENDING url (shown as a Load-preview card, NOT
 *  navigated) — the click-to-load counterpart of PREVIEW_URL_EVENT. */
export const PREVIEW_PENDING_EVENT = 'kirocrew-web-preview-pending'

/** Coerce free-form input into a safe http(s) URL, or null if unusable. A bare
 *  host gets a scheme: `http://` for a loopback host or anything carrying an
 *  explicit port or IP literal (`localhost:5173`, `192.168.1.4:3000` — the
 *  dev-server shapes, which are plain http), `https://` for a bare public host
 *  (`google.com`), which is what a real site answers on and what a browser's own
 *  address bar would try. Any explicit scheme that isn't http(s) (javascript:,
 *  file:, data:, ftp://, …) is rejected so the iframe can't be pointed at a
 *  dangerous target. */
export function normalizeUrl(raw: string): string | null {
  const trimmed = raw.trim()
  if (!trimmed) return null
  // Reject dangerous / non-web schemes up front (these have no `//` authority,
  // so the scheme:// check below wouldn't catch them).
  if (/^(javascript|data|file|vbscript|blob|about):/i.test(trimmed)) return null
  const schemeSep = trimmed.match(/^([a-zA-Z][a-zA-Z0-9+.-]*):\/\//)
  let candidate = trimmed
  if (schemeSep) {
    // An explicit scheme:// — only http(s) allowed (rejects ftp://, ws://, …).
    if (!/^https?$/i.test(schemeSep[1])) return null
  } else {
    // No scheme (e.g. `localhost:5173`, where `localhost` is a host, not a
    // scheme). Parse once as http to read the host and port back, then decide.
    let probe: URL
    try { probe = new URL(`http://${trimmed}`) } catch { return null }
    candidate = `${defaultSchemeFor(probe)}://${trimmed}`
  }
  try {
    const u = new URL(candidate)
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null
    return u.toString()
  } catch {
    return null
  }
}

/** `https` for a bare PUBLIC hostname, `http` for everything a dev server looks
 *  like: a loopback host, an IP literal, or any host with an explicit port. A
 *  `host:port` typed bare is a local or LAN server in practice, and a public site
 *  is never typed with one. */
function defaultSchemeFor(u: URL): 'http' | 'https' {
  if (u.port) return 'http'
  const h = u.hostname
  if (isLoopbackHost(h)) return 'http'
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(h) || h.startsWith('[')) return 'http'
  return 'https'
}

/** Loopback hostnames that share cookies by host (port-agnostic). */
const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '0.0.0.0', '[::1]', '::1'])

/** True for any host that resolves to the local machine and can therefore share
 *  host-scoped cookies with a same-host dashboard — the loopback IPs/names plus
 *  the `*.localhost` reserved TLD (e.g. `kirocrew.localhost`, which the desktop
 *  app and tunnels use). */
export function isLoopbackHost(h: string): boolean {
  return LOOPBACK_HOSTS.has(h) || h === 'localhost' || h.endsWith('.localhost')
}

/** Loopback hosts an `http://` preview may be FRAMED on while the dashboard
 *  itself is served over `https`. Two conditions, and a host has to meet both:
 *
 *  1. **The engine must not block the frame as mixed content.** `127.0.0.1` and
 *     `localhost` are potentially trustworthy origins under W3C Secure Contexts,
 *     so an `http://` document on them is exempt. `0.0.0.0` is the unspecified
 *     address rather than loopback, so it is not exempt and really is blocked.
 *  2. **The dashboard's own CSP must admit the origin.** `frame-src` carries
 *     `http://127.0.0.1:*` and `http://localhost:*` unconditionally (`server.py`
 *     `_LOOPBACK_FRAME_SRC`), while `http://*.localhost:*` is added only in
 *     instances mode, and no wildcard-port entry for the rest of `127.0.0.0/8`
 *     or for a bracketed IPv6 literal exists at all — the latter is invalid CSP
 *     grammar.
 *
 *  A host missing either condition keeps the explanatory card, which is strictly
 *  better than a frame the engine or the CSP silently blanks. IPv6 loopback still
 *  reaches the frame: `isolatePreviewHost` canonicalizes `[::1]` to `127.0.0.1`
 *  before the target is ever framed.
 *
 *  `WebPreviewPanel.frameSrcPin.test.ts` reads `_LOOPBACK_FRAME_SRC` out of
 *  `server.py` and fails if any host here lacks its `http://<host>:*` entry. */
export const EMBEDDABLE_LOOPBACK_HOSTS: ReadonlySet<string> = new Set(['127.0.0.1', 'localhost'])

/** Whether an `http://` document on *h* is a potentially trustworthy origin under
 *  W3C Secure Contexts — `localhost`, the `*.localhost` reserved TLD, all of
 *  `127.0.0.0/8`, and IPv6 loopback — so the ENGINE does not block it as mixed
 *  content inside an `https` dashboard. `0.0.0.0` is the unspecified address,
 *  not loopback, and is deliberately absent: the engine really does block it.
 *
 *  This is condition 1 of `EMBEDDABLE_LOOPBACK_HOSTS`, split out so the refusal
 *  card can name the right blocker: a trustworthy host that is still not
 *  embeddable fails only condition 2, the dashboard's own `frame-src`. */
export function isPotentiallyTrustworthyHost(h: string): boolean {
  if (h === 'localhost' || h.endsWith('.localhost')) return true
  if (h === '[::1]' || h === '::1') return true
  return /^127\.(?:\d{1,3})\.(?:\d{1,3})\.(?:\d{1,3})$/.test(h)
}

/** Why an `http://` target cannot be framed inside an `https` dashboard, or
 *  `null` when it can (an embeddable loopback host) or the question does not
 *  arise (an `http` dashboard, an `https` target).
 *
 *  - `'browser'`: the host is not potentially trustworthy, so the engine blocks
 *    the frame as mixed content. Nothing on the gateway side can change that.
 *  - `'policy'`: the engine would allow it, but this dashboard does not embed
 *    it: `EMBEDDABLE_LOOPBACK_HOSTS` is pinned to the hosts the gateway's CSP
 *    `frame-src` carries in every mode, and this host is not one of them (a
 *    `*.localhost` entry exists only in instances mode, so the panel refuses it
 *    even there rather than guess). That is a gateway policy, not a browser one.
 *
 *  One sentence used to cover both, and blamed the browser for the second — which
 *  sent a reader into a mixed-content investigation for a CSP problem. */
export function httpEmbedRefusal(url: string, dashboardProtocol: string): 'browser' | 'policy' | null {
  if (dashboardProtocol !== 'https:' || !url.startsWith('http://')) return null
  const host = hostnameOf(url)
  if (EMBEDDABLE_LOOPBACK_HOSTS.has(host)) return null
  return isPotentiallyTrustworthyHost(host) ? 'policy' : 'browser'
}

/** A URL's hostname, or `''` when it will not parse — so a caller deciding
 *  whether to RELAX a guard fails closed instead of throwing during render. */
function hostnameOf(url: string): string {
  try { return new URL(url).hostname } catch { return '' }
}

/** A URL's port as the browser resolves it, so `:80` and an absent port on
 *  `http://` compare equal. */
function effectivePort(u: URL): string {
  if (u.port) return u.port
  return u.protocol === 'https:' ? '443' : '80'
}

/**
 * True when `url` points back at the gateway serving this dashboard.
 *
 * Such a target can never render in the preview iframe, and the panel would
 * otherwise show a blank frame with no explanation. The gateway answers every
 * request with `X-Frame-Options: SAMEORIGIN` and CSP `frame-ancestors 'self'`,
 * and `isolatePreviewHost` deliberately swaps the host to the *other* loopback
 * name for cookie isolation — so the frame is always cross-origin to the
 * dashboard by construction, and always refused. (Keeping the host identical to
 * pass `'self'` is not an option: that is the cookie leak the swap exists to
 * prevent.) Reporting it beats framing it.
 *
 * Both sides must be loopback: when the dashboard is reached over a tunnel or a
 * LAN address, a loopback target is the *user's own* machine — an ordinary dev
 * server — not this gateway. Ports are compared because the host strings
 * legitimately differ (`localhost` vs `127.0.0.1`) for one and the same server.
 * `dashboardOrigin` defaults to the current document location (overridable for
 * tests).
 *
 * Same port on loopback therefore means the same server, with one exception: a
 * dev server bound to a DIFFERENT loopback interface (`::1`) on the gateway's own
 * port is a distinct listener that this reports as self. That target degrades to
 * the explanatory state plus its open-in-browser link rather than to a blank
 * frame, so the misread costs a click and never silently swallows a page.
 */
export function isDashboardOrigin(url: string, dashboardOrigin?: string): boolean {
  let target: URL
  try { target = new URL(url) } catch { return false }
  let origin = dashboardOrigin
  if (origin == null) {
    try { origin = typeof window !== 'undefined' ? window.location.href : '' } catch { origin = '' }
  }
  if (!origin) return false
  let self_: URL
  try { self_ = new URL(origin) } catch { return false }
  if (!isLoopbackHost(target.hostname) || !isLoopbackHost(self_.hostname)) return false
  return effectivePort(target) === effectivePort(self_)
}

/**
 * Normalize a loopback preview URL's host so it is both reachable under the
 * dashboard CSP and cookie-isolated from the dashboard. Two rewrites, in order:
 *
 * 1. **IPv6 loopback → IPv4.** The dashboard CSP admits loopback preview origins
 *    (see `server.py` `_LOOPBACK_FRAME_SRC`), but a bracketed IPv6 literal with a
 *    wildcard port — `http://[::1]:*` — is INVALID CSP grammar, so Chromium drops
 *    the whole source and can never admit it. The panel's no-cors liveness probe
 *    to `[::1]` is therefore refused and a perfectly healthy dev server shows as
 *    "Preview server not reachable" (while it opens fine in the OS browser, whose
 *    top-level navigation is not bound by the dashboard CSP). `127.0.0.1` is the
 *    same loopback and IS admitted, so canonicalize `[::1]`/`::1` to it. This runs
 *    independent of the dashboard host — even when it is unknown — because the CSP
 *    gap has nothing to do with cookie isolation.
 *
 * 2. **Cookie isolation.** Cookies are scoped by host but NOT by port, so an
 *    iframe pointed at `http://localhost:5173` while the dashboard is served from
 *    the same host (`localhost`, `127.0.0.1`, or a `*.localhost` alias like
 *    `kirocrew.localhost`) would send the dashboard's host-scoped auth cookie to
 *    the previewed dev server (which could read/replay it). When the preview host
 *    matches the dashboard host and both are loopback, swap it to a
 *    guaranteed-distinct loopback host (`127.0.0.1`, or `localhost` when the
 *    dashboard already IS `127.0.0.1`) — a different host string, so no dashboard
 *    cookie is ever sent to the framed server.
 *
 * Non-loopback hosts and already-distinct hosts skip the isolation swap.
 * `dashboardHost` defaults to the current document host (overridable for tests).
 */
export function isolatePreviewHost(url: string, dashboardHost?: string): string {
  let u: URL
  try { u = new URL(url) } catch { return url }
  // (1) IPv6 loopback canonicalization — unconditional (the CSP gap is
  // orthogonal to the dashboard host, so it must fire even when host is unknown).
  // A parsed URL exposes an IPv6 host in bracketed form (`[::1]`); guard the bare
  // form too, defensively.
  if (u.hostname === '[::1]' || u.hostname === '::1') u.hostname = '127.0.0.1'
  // (2) Cookie isolation — needs to know the dashboard host to compare against.
  let host = dashboardHost
  if (host == null) {
    try { host = typeof window !== 'undefined' ? window.location.hostname : '' } catch { host = '' }
  }
  if (!host) return u.toString()
  const h = u.hostname
  if (!isLoopbackHost(h)) return u.toString()  // real host / tunnel — leave it
  if (h !== host) return u.toString()          // already a distinct host → isolated
  u.hostname = h === '127.0.0.1' ? 'localhost' : '127.0.0.1'
  return u.toString()
}

/**
 * Isolate a host only for a target the panel will actually FRAME.
 *
 * The host swap in `isolatePreviewHost` exists to keep the dashboard's
 * host-scoped cookie out of a framed server. A target that is this gateway is
 * never framed — it renders the explanatory state instead — so the swap buys no
 * safety there and actively misreports the target: someone who typed
 * `127.0.0.1:6776` would be told `localhost:6776` "is the dashboard's own
 * server", at the exact moment the panel is explaining itself. Show what they
 * entered.
 */
export function isolateFrameTarget(url: string): string {
  return isDashboardOrigin(url) ? url : isolatePreviewHost(url)
}

/**
 * Feed a URL into a session's Web Preview tab from OUTSIDE the panel — e.g.
 * ChatPage auto-detecting a dev-server URL (or the agent's `kirocrew:preview`
 * marker) in chat. Persists it as the session's preview target (so the panel
 * picks it up on mount if the tab isn't open yet) AND — when ``open`` is true —
 * notifies an already-mounted panel to load it live. Pass ``open=false`` to
 * pre-fill WITHOUT auto-loading (the heuristic "offer" path: it loads only when
 * the user actually opens the tab, never as a drive-by GET). Returns the
 * normalized+isolated URL, or null if unusable.
 */
export function setSessionPreviewUrl(sessionKey: string, rawUrl: string, open = true): string | null {
  if (!sessionKey) return null
  const norm = normalizeUrl(rawUrl)
  if (!norm) return null
  const isolated = isolateFrameTarget(norm)
  safeSetItem(`${URL_KEY_PREFIX}${sessionKey}`, isolated)
  if (open) {
    window.dispatchEvent(new CustomEvent(PREVIEW_URL_EVENT, { detail: { slot: sessionKey, url: isolated } }))
  }
  return isolated
}

/**
 * Feed a PENDING preview URL for a session — the agent's `kirocrew:preview`
 * marker or a heuristic localhost URL detected in chat. Unlike
 * `setSessionPreviewUrl`, this NEVER loads the iframe: it persists the URL under
 * the pending key and notifies a mounted panel to show a **"Load preview"** card.
 * The GET only fires when the user clicks Load (an explicit gesture) — closing
 * the auto-navigation vector where agent output could drive a scripted iframe to
 * an arbitrary host with no user consent.
 *
 * Chat-fed URLs are additionally **loopback-only**: agent output (which can be
 * prompt-injected by browsed/read content) can only ever offer a LOCAL dev
 * server, never an external host — the feature's whole purpose is local
 * previews, so this costs nothing. A URL pointing back at this gateway is
 * rejected too: it can never be framed (see `isDashboardOrigin`), so offering it
 * would only produce a Load button that leads to a blank panel. The manual URL
 * bar is not restricted (typing a URL is the user's own action). Returns the
 * normalized+isolated URL, or null when unusable, non-loopback, or self.
 */
export function setSessionPreviewPending(sessionKey: string, rawUrl: string): string | null {
  if (!sessionKey) return null
  const norm = normalizeUrl(rawUrl)
  if (!norm) return null
  try {
    if (!isLoopbackHost(new URL(norm).hostname)) return null
  } catch {
    return null
  }
  if (isDashboardOrigin(norm)) return null
  const isolated = isolatePreviewHost(norm)
  safeSetItem(`${PENDING_KEY_PREFIX}${sessionKey}`, isolated)
  window.dispatchEvent(new CustomEvent(PREVIEW_PENDING_EVENT, { detail: { slot: sessionKey, url: isolated } }))
  return isolated
}

/** Whether the launcher would refuse *url* for its shape alone (see `refused`).
 * Mirrors the gateway's `validate_url`: a non-empty query or fragment. A bare
 * trailing `?` or `#` carries nothing and passes there, so it passes here. */
export function hasQueryOrFragment(url: string): boolean {
  try {
    const u = new URL(url)
    return u.search !== '' || u.hash !== ''
  } catch {
    return false
  }
}

/** The gateway's own refusal of a URL shape (`code: "invalid_url"` on a 400). */
export function isInvalidUrlRefusal(err: unknown): boolean {
  if (!(err instanceof ApiError) || err.status !== 400) return false
  try {
    return (JSON.parse(err.body) as { code?: unknown }).code === 'invalid_url'
  } catch {
    return false
  }
}
