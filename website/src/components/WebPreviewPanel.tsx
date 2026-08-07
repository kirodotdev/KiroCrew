import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Globe, RotateCw, ExternalLink, ArrowLeft, ArrowRight, Expand, Minimize, Smartphone, Monitor, Check, Crop, MousePointerClick } from 'lucide-react'

import { safeSetItem } from '../utils/safeStorage'
import { isScreenSnipSupported } from '../hooks/useScreenSnip'
import { useIsMobile } from '../hooks/useIsMobile'
import { useBrowserFrame } from '../hooks/useBrowserFrame'
import { useNativeBrowser } from '../hooks/useNativeBrowser'

import { i18nT } from '../i18n/t'
import { fmtTimeNumeric } from '../i18n/format'
/**
 * WebPreviewPanel — a docked, session-scoped **live web preview** of a URL the
 * user is serving locally (a dev server / static server for the project they're
 * working on).
 *
 * Opened from the side panel's + menu ("Browser"), it loads the URL in a
 * sandboxed iframe. It ALSO hosts the agent-browse surface, with two transports
 * chosen by where the browser runs: when a native Chromium view is available
 * (Electron shell) it OWNS the panel (`nativeOpen`) — a chat-opened page lands
 * in the real, human- and agent-operable browser; otherwise (remote gateway /
 * plain browser) the read-only Playwright screenshot mirror is the FALLBACK
 * (`showMirror`, gated on `isLive` AND no native view). Unlike either agent
 * surface, the iframe is a real embedded browser view: the dev server's own HMR
 * live-reloads it as the user edits, and Reload covers static servers.
 *
 * Session-scoped: the chosen URL is remembered PER chat slot (`sessionKey`), so
 * each session keeps its own preview target. Frontend-only — no gateway round
 * trip; the browser fetches the URL directly, so only servers reachable from
 * the user's browser (localhost dev servers) can be previewed.
 */

const URL_KEY_PREFIX = 'mc-webpreview-url:'
/** localStorage prefix for a chat-fed URL that is PENDING an explicit "Load
 *  preview" click — surfaced as a card, never auto-navigated (click-to-load). */
const PENDING_KEY_PREFIX = 'mc-webpreview-pending:'
/** Window event that feeds a URL into a mounted panel from outside (ChatPage). */
const PREVIEW_URL_EVENT = 'kirocrew-web-preview-url'
/** Window event feeding a PENDING url (shown as a Load-preview card, NOT
 *  navigated) — the click-to-load counterpart of PREVIEW_URL_EVENT. */
const PREVIEW_PENDING_EVENT = 'kirocrew-web-preview-pending'
/**
 * Window event: enter/leave preview "focus" (expand) mode. App collapses the
 * left nav and ChatPage hides the session list + maximizes the side panel, so
 * the preview gets maximum room and the chat pane shrinks to its minimum. A
 * plain window event (not prop-drilling) keeps this leaf panel decoupled from
 * the two ancestors that own that layout.
 */
export const PREVIEW_FOCUS_EVENT = 'kirocrew-preview-focus'
/**
 * Window event: request an area screenshot into the chat input. ChatPage owns
 * the capture pipeline (getDisplayMedia in the browser / the same path routed
 * through Electron's setDisplayMediaRequestHandler in the desktop app → the
 * SnipOverlay crop surface → attach the PNG to the composer), so the preview's
 * crop button just asks for it via this event rather than duplicating capture.
 */
export const PREVIEW_SNIP_EVENT = 'kirocrew-web-preview-snip'
/**
 * Window event: the panel requests enabling "Let the agent use the browser" (operate mode) so the
 * agent may actively drive the browser. ChatPage listens and turns browse mode
 * on for the active slot. Fired by the live mirror's "Let the agent act" button.
 */
export const PREVIEW_ENABLE_BROWSE_EVENT = 'kirocrew-enable-browse'
/**
 * Window event: ChatPage broadcasts the current per-slot browse-mode state so
 * the panel can reflect it — the "Let the agent act" button shows only while
 * browse mode is OFF.
 */
export const BROWSE_MODE_EVENT = 'kirocrew-browse-mode'
/**
 * How long after the last agent-browse frame we keep the live mirror on screen
 * before falling back to the preview body. Frames arrive only when the agent
 * screenshots, so a generous window keeps the view up between captures.
 */
const LIVE_FRAME_TTL_MS = 90_000
/** Common local dev-server ports offered as one-click starting points. */
const COMMON_PORTS = [3000, 5173, 8080, 4321, 8000]
/** iframe sandbox — permissive enough for real apps + HMR, but still a sandbox. */
const SANDBOX = 'allow-scripts allow-same-origin allow-forms allow-popups allow-modals allow-downloads'

/** Preview viewport presets. `w`/`h` absent = responsive desktop (fill panel);
 *  present = a fixed device-sized frame (mobile/tablet). */
interface DevicePreset {
  id: string
  /** Device NAME, verbatim — a hardware product name, never translated. Absent
   *  for the responsive preset, whose label is descriptive copy and therefore
   *  lives in `DEVICE_LABEL_KEY` instead. */
  label?: string
  w?: number
  h?: number
}
const DEVICE_PRESETS: DevicePreset[] = [
  { id: 'responsive' },
  { id: 'iphone-se', label: 'iPhone SE', w: 375, h: 667 },
  { id: 'iphone-15', label: 'iPhone 15 / 14', w: 390, h: 844 },
  { id: 'iphone-15-pro-max', label: 'iPhone 15 Pro Max', w: 430, h: 932 },
  { id: 'pixel-7', label: 'Pixel 7', w: 412, h: 915 },
  { id: 'galaxy-s20', label: 'Galaxy S20', w: 360, h: 800 },
  { id: 'ipad-mini', label: 'iPad Mini', w: 768, h: 1024 },
]

/**
 * Catalog KEY for the presets whose label is descriptive copy rather than a
 * hardware product name — only the responsive one, which describes a BEHAVIOUR
 * ("fill the panel") and not a device.
 *
 * A key rather than the string itself: `DEVICE_PRESETS` is module-level data
 * evaluated at import, so an `i18nT()` call in it would freeze the boot language.
 * `deviceLabel()` does the lookup during render. Flat `Record` of full literal
 * keys, indexed inline at the call, because that is the shape
 * `scripts/check-i18n-keys.mjs` can resolve statically.
 */
export const DEVICE_LABEL_KEY: Record<string, string> = {
  responsive: 'components.webPreviewPanel.responsive_desktop',
}

/** A preset's display label for the CURRENT language. Product-named presets fall
 *  through to their verbatim `label`. */
function deviceLabel(d: DevicePreset): string {
  // `hasOwnProperty`, not `in`: guards against a preset id colliding with an
  // inherited Object.prototype member and handing a function to i18next.
  return Object.prototype.hasOwnProperty.call(DEVICE_LABEL_KEY, d.id)
    ? i18nT(DEVICE_LABEL_KEY[d.id])
    : (d.label ?? d.id)
}

/** Coerce free-form input into a safe http(s) URL, or null if unusable. A bare
 *  `host:port` / `localhost:5173` gets an `http://` scheme; any explicit scheme
 *  that isn't http(s) (javascript:, file:, data:, ftp://, …) is rejected so the
 *  iframe can't be pointed at a dangerous target. */
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
    // scheme) — default to http.
    candidate = `http://${trimmed}`
  }
  try {
    const u = new URL(candidate)
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null
    return u.toString()
  } catch {
    return null
  }
}

/** Query param carrying the reload counter (see `withCacheBuster`). Deliberately
 *  namespaced so setting it can't clobber a param the previewed app owns. */
const RELOAD_PARAM = '_kcreload'

/**
 * Return `url` with the reload counter appended as a query param, so each
 * panel-initiated load is a distinct URL.
 *
 * Remounting the iframe (bumping its React key) re-navigates to the SAME URL,
 * which the browser may satisfy from its HTTP cache — a remount is not a
 * revalidation. Dev servers that send `Last-Modified` with no `Cache-Control`
 * (`python -m http.server`, many static servers) get *heuristic* freshness, so
 * the browser can skip even the conditional request and Reload would re-show the
 * pre-edit page. Varying the URL is what makes it a genuinely new request.
 *
 * Scope, deliberately narrow: this forces a fresh **top-level document** only.
 * Subresources that document references (bundles, CSS) are requested at their
 * own unchanged URLs and can still be served from cache — a parent frame cannot
 * tell a cross-origin iframe to bypass its cache, so that half isn't reachable
 * from here.
 */
export function withCacheBuster(url: string, key: number): string {
  if (!url || !key) return url
  try {
    const u = new URL(url)
    u.searchParams.set(RELOAD_PARAM, String(key))
    return u.toString()
  } catch {
    // Unparseable (shouldn't happen — everything is normalized first). Load it
    // as-is rather than not at all.
    return url
  }
}

/** Loopback hostnames that share cookies by host (port-agnostic). */
const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '0.0.0.0', '[::1]', '::1'])

/** True for any host that resolves to the local machine and can therefore share
 *  host-scoped cookies with a same-host dashboard — the loopback IPs/names plus
 *  the `*.localhost` reserved TLD (e.g. `kirocrew.localhost`, which the desktop
 *  app and tunnels use). */
function isLoopbackHost(h: string): boolean {
  return LOOPBACK_HOSTS.has(h) || h === 'localhost' || h.endsWith('.localhost')
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
  const isolated = isolatePreviewHost(norm)
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
 * previews, so this costs nothing. The manual URL bar is not restricted (typing
 * a URL is the user's own action). Returns the normalized+isolated URL, or null
 * when unusable or non-loopback.
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
  const isolated = isolatePreviewHost(norm)
  safeSetItem(`${PENDING_KEY_PREFIX}${sessionKey}`, isolated)
  window.dispatchEvent(new CustomEvent(PREVIEW_PENDING_EVENT, { detail: { slot: sessionKey, url: isolated } }))
  return isolated
}

/** URL-bar navigation history. `index` points at the current entry in `stack`.
 *  (An iframe's own cross-origin page history isn't readable, so back/forward
 *  step through URLs committed via the bar / quick-picks / chat feed, not the
 *  site's internal link navigations.) */
interface NavState {
  stack: string[]
  index: number
}
const EMPTY_NAV: NavState = { stack: [], index: -1 }

/** Push `url` as a new current entry, dropping any forward history (browser
 *  semantics). No-op when it already equals the current entry. */
function pushNav(n: NavState, url: string): NavState {
  const cur = n.index >= 0 ? n.stack[n.index] : null
  if (cur === url) return n
  const stack = [...n.stack.slice(0, n.index + 1), url]
  return { stack, index: stack.length - 1 }
}

export default function WebPreviewPanel({ sessionKey, active = true }: { sessionKey?: string | null; active?: boolean }) {
  const storageKey = sessionKey ? `${URL_KEY_PREFIX}${sessionKey}` : null
  const pendingKey = sessionKey ? `${PENDING_KEY_PREFIX}${sessionKey}` : null
  const [nav, setNav] = useState<NavState>(EMPTY_NAV)
  const [draft, setDraft] = useState('')
  // A chat-fed URL awaiting an explicit "Load preview" click (null = none).
  const [pending, setPending] = useState<string | null>(null)
  // The loaded dev server stopped responding (connection refused / down). A
  // cross-origin iframe keeps showing its last document after its server dies,
  // so we probe liveness and unmount it here instead of showing a stale page.
  const [unreachable, setUnreachable] = useState(false)
  // Reload counter. Bumping it remounts the iframe AND varies its src (see
  // `withCacheBuster`) — the remount alone would re-request the same URL and the
  // browser could answer from cache, which is no reload at all for a static
  // server with no HMR. 0 = the initial mount-restored load, kept pristine.
  const [reloadKey, setReloadKey] = useState(0)
  // Preview "focus" (expand) mode — reflected in the toggle icon; broadcast to
  // App/ChatPage which collapse the surrounding chrome.
  const [expanded, setExpanded] = useState(false)
  // Viewport preset (responsive desktop vs a fixed device size).
  const [deviceId, setDeviceId] = useState('responsive')
  const [deviceMenuOpen, setDeviceMenuOpen] = useState(false)
  const deviceMenuRef = useRef<HTMLDivElement>(null)

  const url = nav.index >= 0 ? nav.stack[nav.index] : ''
  const canBack = nav.index > 0
  const canForward = nav.index >= 0 && nav.index < nav.stack.length - 1
  const device = DEVICE_PRESETS.find(d => d.id === deviceId) ?? DEVICE_PRESETS[0]
  const isDeviceSized = !!device.w
  const isMobile = useIsMobile()
  // Gate the crop→chat snip on the actual capability it uses (getDisplayMedia)
  // and never on mobile. Gating on the real mechanism — rather than a macOS
  // guess — also avoids exposing a failing action on a Mac browser driving a
  // non-macOS remote gateway (whose native /api/screenshot fallback wouldn't work).
  const canSnip = isScreenSnipSupported() && !isMobile

  // ── Live agent-browse mirror ──────────────────────────────────────────────
  // The same Browser panel also shows the read-only Playwright screenshot stream
  // (the `web-browse` skill / [BROWSE] mode). No manual mode toggle: when browse
  // frames are arriving, the live mirror takes precedence over the iframe; when
  // they go stale (LIVE_FRAME_TTL_MS) we fall back to the preview body.
  const { frame, lastTs, sessionKey: frameSessionKey, sessionName } = useBrowserFrame()
  const [nowTick, setNowTick] = useState(() => Date.now())
  // Whether "Let the agent use the browser" (operate) is currently on — broadcast by ChatPage so
  // the mirror can show "Let the agent act" only while it's off.
  const [browseOn, setBrowseOn] = useState(false)
  useEffect(() => {
    const onMode = (e: Event) => setBrowseOn(!!(e as CustomEvent<{ on?: boolean }>).detail?.on)
    window.addEventListener(BROWSE_MODE_EVENT, onMode)
    return () => window.removeEventListener(BROWSE_MODE_EVENT, onMode)
  }, [])
  // While frames are live, re-evaluate staleness on an interval so the mirror
  // auto-retires after the agent stops browsing (frames only push on capture).
  useEffect(() => {
    if (!frame || !lastTs) return
    setNowTick(Date.now())
    const id = setInterval(() => {
      const now = Date.now()
      setNowTick(now)
      // Stop ticking once the mirror has gone stale — a new frame (lastTs
      // change) re-runs this effect and re-arms the interval. Prevents a
      // perpetual 5s re-render for the panel's lifetime after the first frame.
      if (now - lastTs >= LIVE_FRAME_TTL_MS) clearInterval(id)
    }, 5000)
    return () => clearInterval(id)
  }, [frame, lastTs])
  // Session-scoped: only surface the mirror when the streaming frame belongs to
  // THIS panel's session. useBrowserFrame is global (latest frame from ANY
  // session); without this check a background session's browse would render in —
  // and, via "Let the agent act" below, authorize [BROWSE] on — the wrong
  // session's panel. `sessionKey` must be present and equal the frame's key.
  const isLive = !!frame && !!lastTs && !!sessionKey && frameSessionKey === sessionKey
    && nowTick - lastTs < LIVE_FRAME_TTL_MS

  // ── Transport selection ──
  // The browsing surface has two backends, chosen by WHERE the browser actually
  // runs — and the NATIVE view wins whenever it exists:
  //
  //   • A real Chromium view is available in THIS process (Electron shell) ->
  //     embed it natively. A chat-opened page lands in the real, human- and
  //     agent-operable browser, not a read-only screenshot. Native OWNS the
  //     panel; the mirror is suppressed even if a stray Playwright frame arrives.
  //   • Otherwise (remote gateway / plain browser, where `useNativeBrowser`
  //     reports `available: false`) -> the browser lives in another process and
  //     only streamed frames can show it, so the mirror is the FALLBACK.
  //
  // `agentActEnabled: browseOn` wires the Globe toggle ("Let the agent use the
  // browser") to the native view's agent-control gate: on -> acquire (LIGHT),
  // off -> release. Scoped by sessionKey: each Browser panel owns its own view
  // and authorization. `enabled` is just `active` now (native no longer yields
  // to streaming frames), so switching side-panel tabs hides — never destroys —
  // the view.
  const native = useNativeBrowser(sessionKey || '', active, {
    agentActEnabled: browseOn,
  })
  // Native wins when available. Its view is "open" once a page has been loaded
  // into it; until then the panel shows the ordinary iframe preview.
  const nativeOpen = native.available && !!native.state?.open
  // The live screenshot mirror is the fallback whenever no native view is
  // actually OWNING the surface. Gating it on `!native.available` was wrong:
  // availability only means Electron's preload bridge exists, so on the desktop
  // app before any page is opened natively (and for a remote gateway's frames
  // arriving into a desktop shell) the mirror was suppressed while the native
  // view had nothing to show -- a blank panel. `!nativeOpen` is the real
  // condition: a native view that is open owns the surface, otherwise mirror.
  const showMirror = isLive && !nativeOpen
  const requestInteraction = useCallback(() => {
    // Target the session whose page is on screen — which is THIS panel's
    // session, since isLive required a session_key match — never a global or
    // active-slot fallback. Carrying the slot keeps the grant attributed to the
    // browsing session even if the active slot differs.
    window.dispatchEvent(new CustomEvent(PREVIEW_ENABLE_BROWSE_EVENT, { detail: { slot: sessionKey } }))
  }, [sessionKey])

  const persist = useCallback((u: string) => {
    if (storageKey && u) safeSetItem(storageKey, u)
  }, [storageKey])

  // Load this session's persisted URL on mount / slot change so the preview
  // target follows the session (and survives panel collapse + reloads). Starts a
  // fresh single-entry history at that URL, and resets the viewport to responsive.
  useEffect(() => {
    let saved = ''
    if (storageKey) {
      try { saved = localStorage.getItem(storageKey) || '' } catch { /* ignore locked storage */ }
    }
    // Isolate defensively in case a legacy same-host value was persisted before
    // isolatePreviewHost existed.
    saved = saved ? isolatePreviewHost(saved) : ''
    let pend = ''
    if (pendingKey) {
      try { pend = localStorage.getItem(pendingKey) || '' } catch { /* ignore */ }
    }
    pend = pend ? isolatePreviewHost(pend) : ''
    setPending(pend || null)
    // A pending (chat-fed, not-yet-loaded) URL takes display precedence: the
    // body shows a Load-preview card at that URL and the iframe stays unloaded.
    // Otherwise load the session's last committed URL.
    if (pend) {
      setNav(EMPTY_NAV)
      setDraft(pend)
    } else {
      setNav(saved ? { stack: [saved], index: 0 } : EMPTY_NAV)
      setDraft(saved)
    }
    setDeviceId('responsive')
  }, [storageKey, pendingKey])

  // Live external feed: ChatPage (or any caller of setSessionPreviewUrl) can
  // push a URL for this session; load it immediately when the tab is already
  // open. When the tab is NOT yet mounted, the persisted value above is read on
  // mount instead — so both paths converge on the same URL.
  useEffect(() => {
    const onExternal = (e: Event) => {
      const d = (e as CustomEvent<{ slot?: string; url?: string }>).detail
      if (!d?.url || d.slot !== sessionKey) return
      setNav(n => pushNav(n, d.url as string))
      setDraft(d.url)
      setReloadKey(k => k + 1)
    }
    window.addEventListener(PREVIEW_URL_EVENT, onExternal)
    return () => window.removeEventListener(PREVIEW_URL_EVENT, onExternal)
  }, [sessionKey])

  // Live PENDING feed: ChatPage pushes a chat-detected URL for this session as a
  // Load-preview card (never navigated). The user clicks Load to fire the GET.
  useEffect(() => {
    const onPending = (e: Event) => {
      const d = (e as CustomEvent<{ slot?: string; url?: string }>).detail
      if (!d?.url || d.slot !== sessionKey) return
      setPending(d.url)
      setDraft(d.url)
    }
    window.addEventListener(PREVIEW_PENDING_EVENT, onPending)
    return () => window.removeEventListener(PREVIEW_PENDING_EVENT, onPending)
  }, [sessionKey])

  // Close the device menu on outside click.
  useEffect(() => {
    if (!deviceMenuOpen) return
    const onDown = (e: MouseEvent) => {
      if (deviceMenuRef.current && !deviceMenuRef.current.contains(e.target as Node)) setDeviceMenuOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [deviceMenuOpen])

  const commit = useCallback((raw: string) => {
    const norm = normalizeUrl(raw)
    if (!norm) return
    const isolated = isolatePreviewHost(norm)
    setNav(n => pushNav(n, isolated))
    setDraft(isolated)
    persist(isolated)
    setReloadKey(k => k + 1)
    // Loading supersedes any pending offer for this session.
    setPending(null)
    if (pendingKey) { try { localStorage.removeItem(pendingKey) } catch { /* ignore */ } }
    // A NON-loopback target goes to the native browser view when one is
    // available. External sites routinely refuse to be framed
    // (X-Frame-Options / frame-ancestors), so the iframe path renders them
    // blank — a real embedded browser is the only thing that can show them.
    // Loopback dev servers keep using the iframe: that is the local-preview
    // feature, and framing works there.
    if (!native.available) return
    let host = ''
    try { host = new URL(isolated).hostname } catch { /* normalizeUrl validated it */ }
    if (host && !isLoopbackHost(host)) native.open(isolated)
  }, [persist, pendingKey, native])

  const dismissPending = useCallback(() => {
    setPending(null)
    if (pendingKey) { try { localStorage.removeItem(pendingKey) } catch { /* ignore */ } }
  }, [pendingKey])

  // Navigation while the NATIVE surface is showing. The iframe `commit` above is
  // the wrong path here: it re-points a hidden iframe and only forwards
  // non-loopback targets to the native view, so a loopback URL typed while the
  // native browser is on screen would appear to do nothing. This drives the view
  // the user is actually looking at, and keeps the address bar / persistence in
  // step with it.
  const commitNative = useCallback((raw: string) => {
    const norm = normalizeUrl(raw)
    if (!norm) return
    setDraft(norm)
    persist(norm)
    native.navigate(norm)
  }, [persist, native])

  const back = useCallback(() => {
    setNav(n => {
      if (n.index <= 0) return n
      const target = n.stack[n.index - 1]
      setDraft(target)
      persist(target)
      return { ...n, index: n.index - 1 }
    })
  }, [persist])

  const forward = useCallback(() => {
    setNav(n => {
      if (n.index >= n.stack.length - 1) return n
      const target = n.stack[n.index + 1]
      setDraft(target)
      persist(target)
      return { ...n, index: n.index + 1 }
    })
  }, [persist])

  const reload = useCallback(() => setReloadKey(k => k + 1), [])

  const broadcastFocus = useCallback((focused: boolean) => {
    window.dispatchEvent(new CustomEvent(PREVIEW_FOCUS_EVENT, { detail: { focused } }))
  }, [])
  const toggleExpand = useCallback(() => {
    setExpanded(v => { broadcastFocus(!v); return !v })
  }, [broadcastFocus])
  // Leaving the preview (tab switched away or panel/tab closed) drops focus mode
  // so the chrome isn't left collapsed with the toggle out of reach.
  useEffect(() => {
    if (!active && expanded) { setExpanded(false); broadcastFocus(false) }
  }, [active, expanded, broadcastFocus])
  useEffect(() => () => { broadcastFocus(false) }, [broadcastFocus])

  // What the iframe actually loads: the nav URL, varied per reload so a remount
  // is a real request. `url` itself stays pristine everywhere it's user-visible
  // (URL bar, "Open in browser", persistence) and is what the liveness probe hits.
  const frameSrc = useMemo(() => withCacheBuster(url, reloadKey), [url, reloadKey])

  // An http:// frame inside an https:// dashboard (remote/tunnel) is blocked by
  // the browser as mixed content — detect it so we can explain + offer the
  // open-in-new-tab fallback instead of rendering a silently-blank frame.
  const mixedContent = useMemo(
    () => typeof window !== 'undefined'
      && window.location.protocol === 'https:'
      && url.startsWith('http://'),
    [url],
  )

  // Liveness probe: a cross-origin iframe cannot tell us its server died — it
  // just keeps showing the last document. While a URL is actually framed (loaded,
  // embeddable, tab active) poll it with a no-cors GET; a connection-refused error
  // throws. Two consecutive failures ⇒ the dev server stopped, so we mark it
  // unreachable and unmount the iframe (clearing the stale page). A later success
  // auto-restores it. (Two strikes tolerates a brief HMR/dev-server restart.)
  useEffect(() => {
    if (!url || mixedContent || pending || !active) return
    setUnreachable(false)
    let fails = 0
    let cancelled = false
    const probe = async () => {
      const ctrl = new AbortController()
      const t = setTimeout(() => ctrl.abort(), 2500)
      try {
        await fetch(url, { mode: 'no-cors', cache: 'no-store', signal: ctrl.signal })
        if (cancelled) return
        fails = 0
        setUnreachable(false)
      } catch {
        if (cancelled) return
        fails += 1
        if (fails >= 2) setUnreachable(true)
      } finally {
        clearTimeout(t)
      }
    }
    void probe()
    const id = setInterval(() => { void probe() }, 5000)
    return () => { cancelled = true; clearInterval(id) }
  }, [url, reloadKey, mixedContent, pending, active])

  const iconBtn = 'flex items-center justify-center w-7 h-7 rounded-md text-muted hover:text-text '
    + 'hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 '
    + 'disabled:opacity-40 disabled:cursor-default disabled:hover:bg-transparent disabled:hover:text-muted'

  // Live agent-browse mirror — the FALLBACK transport, shown only when no native
  // view is available (see `showMirror`). Rendered as an OVERLAY (not an early
  // return) so the preview subtree below stays mounted; its iframe document +
  // any unsaved form/SPA state survive a browse frame arriving mid-preview.
  const liveMirror = (
    <div className="absolute inset-0 z-10 flex flex-col h-full min-h-0 bg-bg">
        <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border shrink-0" style={{ backgroundColor: 'var(--bg-elevated)' }}>
          <Monitor size={14} className="shrink-0 text-muted" />
          <span className="shrink-0 text-[13px] font-medium text-text">{i18nT('components.webPreviewPanel.browser_live')}</span>
          <span className="inline-block w-1.5 h-1.5 rounded-full animate-pulse" style={{ backgroundColor: 'var(--ok)' }} aria-hidden />
          {sessionName ? (
            <span className="flex-1 min-w-0 truncate text-[12px] text-muted" title={sessionName}>· {sessionName}</span>
          ) : (
            <div className="flex-1" />
          )}
          {browseOn ? (
            <span className="shrink-0 inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full bg-accent/12 text-accent font-medium" title={i18nT('components.webPreviewPanel.the_agent_can_click_type_and_navigate_this_page')}>
              <MousePointerClick size={12} /> {i18nT('components.webPreviewPanel.agent_can_act')}
            </span>
          ) : (
            <button
              type="button"
              onClick={requestInteraction}
              className="shrink-0 inline-flex items-center gap-1 text-[12px] px-2.5 py-1 rounded-md border border-border text-text hover:bg-bg-hover transition-colors cursor-pointer bg-transparent"
              title={i18nT('components.webPreviewPanel.lets_the_agent_click_type_and_navigate_this_page')}
            >
              <MousePointerClick size={13} /> {i18nT('components.webPreviewPanel.let_the_agent_act')}
            </button>
          )}
        </div>
        <div className="relative bg-black flex-1 min-h-0 flex items-center justify-center">
          {frame ? (
            <img src={frame} alt={i18nT('components.webPreviewPanel.live_browser_session')} className="max-w-full max-h-full object-contain" />
          ) : (
            <div className="flex flex-col items-center gap-2 py-8 text-muted">
              <Monitor size={18} />
              <span className="text-[11px]">{i18nT('components.webPreviewPanel.waiting_for_the_browser_to_take_a_screenshot')}</span>
            </div>
          )}
        </div>
        <div className="px-3 py-1.5 border-t border-border text-[11px] text-muted flex items-center justify-between gap-2">
          <span className="truncate">{i18nT('components.webPreviewPanel.view_only_clicks_here_don_t_reach_the_page')}</span>
          {lastTs && <span className="shrink-0">{i18nT('components.webPreviewPanel.updated')} {fmtTimeNumeric(lastTs)}</span>}
        </div>
      </div>
  )

  // Native browse surface — the counterpart to `liveMirror` for the case where
  // the browser runs in THIS process. The page is a real Chromium view the main
  // process composites over `hostRef`'s rectangle, so the div below is
  // deliberately empty: it exists to be measured, not painted. Nothing may be
  // rendered inside it, because the native layer would cover it.
  const nativeSurface = (
    <div className="absolute inset-0 z-10 flex flex-col h-full min-h-0 bg-bg">
      <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border shrink-0" style={{ backgroundColor: 'var(--bg-elevated)' }}>
        <Monitor size={14} className="shrink-0 text-muted" />
        <span className="shrink-0 text-[13px] font-medium text-text">{i18nT('components.webPreviewPanel.browser_live')}</span>
        <span className="inline-block w-1.5 h-1.5 rounded-full" style={{ backgroundColor: 'var(--ok)' }} aria-hidden />
        <div className="flex-1" />
        {/* Globe toggle ("Let the agent use the browser") wiring: turning it on
            acquires in-process (LIGHT) agent control of THIS native view; off
            releases it. `requestInteraction` asks ChatPage to flip the toggle on
            for this session — the hook (agentActEnabled: browseOn) then drives
            setControlOwner. Reuses the live-mirror's keys, no new strings. */}
        {browseOn ? (
          <span className="shrink-0 inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full bg-accent/12 text-accent font-medium" title={i18nT('components.webPreviewPanel.the_agent_can_click_type_and_navigate_this_page')}>
            <MousePointerClick size={12} /> {i18nT('components.webPreviewPanel.agent_can_act')}
          </span>
        ) : (
          <button
            type="button"
            onClick={requestInteraction}
            className="shrink-0 inline-flex items-center gap-1 text-[12px] px-2.5 py-1 rounded-md border border-border text-text hover:bg-bg-hover transition-colors cursor-pointer bg-transparent"
            title={i18nT('components.webPreviewPanel.lets_the_agent_click_type_and_navigate_this_page')}
          >
            <MousePointerClick size={13} /> {i18nT('components.webPreviewPanel.let_the_agent_act')}
          </button>
        )}
      </div>
      {/* Address bar. The preview subtree below (which owns the other URL form)
          is hidden while the native surface is up, so without this the user
          could reach a page and then have no way to leave it. */}
      <form
        className="flex items-center gap-1 px-2 py-1.5 border-b border-border shrink-0"
        onSubmit={e => { e.preventDefault(); commitNative(draft) }}
      >
        <div className="flex-1 min-w-0 flex items-center gap-0.5 h-7 pl-1 pr-1 rounded-md bg-bg-elevated border border-border focus-within:border-accent transition-colors">
          <input
            type="text"
            value={draft}
            onChange={e => setDraft(e.target.value)}
            placeholder={i18nT('components.webPreviewPanel.localhost_5173')}
            aria-label={i18nT('components.webPreviewPanel.preview_url')}
            spellCheck={false}
            autoCorrect="off"
            autoCapitalize="off"
            className="flex-1 min-w-0 h-full bg-transparent border-none text-[12px] text-text placeholder:text-muted focus:outline-none px-1"
          />
          <a
            href={native.state?.url || undefined}
            target="_blank"
            rel="noreferrer"
            aria-disabled={!native.state?.url}
            className={`flex items-center justify-center w-6 h-6 rounded text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 no-underline ${!native.state?.url ? 'opacity-40 pointer-events-none' : ''}`}
            title={i18nT('components.webPreviewPanel.open_in_browser')}
            aria-label={i18nT('components.webPreviewPanel.open_in_browser')}
          >
            <ExternalLink size={13} />
          </a>
        </div>
      </form>
      {/* Measured host for the native view. Empty by design. */}
      <div ref={native.hostRef} className="flex-1 min-h-0 bg-black" />
    </div>
  )

  return (
    <div className="relative flex flex-col h-full min-h-0 bg-bg">
      {/* Preview subtree stays MOUNTED even while the live mirror overlays it,
          so the iframe document + unsaved form/SPA state survive an isLive
          toggle — visually hidden, never unmounted. */}
      <div className={`flex flex-col h-full min-h-0 ${showMirror || nativeOpen ? 'invisible pointer-events-none' : ''}`} aria-hidden={showMirror || nativeOpen || undefined}>
      {/* URL bar: [back][forward]  ( [reload] input )  [open][expand] | [device] */}
      <form
        className="flex items-center gap-1 px-2 py-1.5 border-b border-border shrink-0"
        onSubmit={e => { e.preventDefault(); commit(draft) }}
      >
        {/* Back / forward — outside the URL container, enabled only when the
            history stack has somewhere to go. */}
        <button type="button" onClick={back} disabled={!canBack} className={iconBtn} title={i18nT('components.webPreviewPanel.back')} aria-label={i18nT('components.webPreviewPanel.back')}>
          <ArrowLeft size={15} />
        </button>
        <button type="button" onClick={forward} disabled={!canForward} className={iconBtn} title={i18nT('components.webPreviewPanel.forward')} aria-label={i18nT('components.webPreviewPanel.forward')}>
          <ArrowRight size={15} />
        </button>
        {/* URL container — reload lives inside on the left. */}
        <div className="flex-1 min-w-0 flex items-center gap-0.5 h-7 pl-0.5 pr-1 rounded-md bg-bg-elevated border border-border focus-within:border-accent transition-colors">
          <button
            type="button"
            onClick={reload}
            disabled={!url}
            className="flex items-center justify-center w-6 h-6 rounded text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 disabled:opacity-40 disabled:cursor-default disabled:hover:bg-transparent disabled:hover:text-muted"
            title={i18nT('components.webPreviewPanel.reload')}
            aria-label={i18nT('components.webPreviewPanel.reload_preview')}
          >
            <RotateCw size={13} />
          </button>
          <input
            type="text"
            value={draft}
            onChange={e => setDraft(e.target.value)}
            placeholder={i18nT('components.webPreviewPanel.localhost_5173')}
            aria-label={i18nT('components.webPreviewPanel.preview_url')}
            spellCheck={false}
            autoCorrect="off"
            autoCapitalize="off"
            className="flex-1 min-w-0 h-full bg-transparent border-none text-[12px] text-text placeholder:text-muted focus:outline-none px-1"
          />
          <a
            href={url || undefined}
            target="_blank"
            rel="noreferrer"
            aria-disabled={!url}
            className={`flex items-center justify-center w-6 h-6 rounded text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 no-underline ${!url ? 'opacity-40 pointer-events-none' : ''}`}
            title={i18nT('components.webPreviewPanel.open_in_browser')}
            aria-label={i18nT('components.webPreviewPanel.open_in_browser')}
          >
            <ExternalLink size={13} />
          </a>
        </div>
        <button
          type="button"
          onClick={toggleExpand}
          className={`${iconBtn} ${expanded ? 'text-accent bg-accent-subtle hover:text-accent' : ''}`}
          title={expanded ? i18nT('components.webPreviewPanel.exit_expanded_preview') : i18nT('components.webPreviewPanel.expand_preview_hide_nav_sessions')}
          aria-label={expanded ? i18nT('components.webPreviewPanel.exit_expanded_preview') : i18nT('components.webPreviewPanel.expand_preview')}
          aria-pressed={expanded}
        >
          {expanded ? <Minimize size={14} /> : <Expand size={14} />}
        </button>
        {/* Divider */}
        <span aria-hidden="true" className="w-px h-5 bg-border shrink-0 mx-0.5" />
        {/* Dimension selector: responsive desktop vs a fixed device size. */}
        <div className="relative shrink-0" ref={deviceMenuRef}>
          <button
            type="button"
            onClick={() => setDeviceMenuOpen(v => !v)}
            className={`flex items-center h-7 px-1.5 rounded-md transition-colors bg-transparent border-none cursor-pointer ${
              isDeviceSized ? 'text-accent bg-accent-subtle hover:text-accent' : 'text-muted hover:text-text hover:bg-bg-hover'
            }`}
            title={`${i18nT('components.webPreviewPanel.preview_size')}: ${deviceLabel(device)}`}
            aria-label={i18nT('components.webPreviewPanel.preview_size')}
            aria-haspopup="menu"
            aria-expanded={deviceMenuOpen}
          >
            {isDeviceSized ? <Smartphone size={14} /> : <Monitor size={14} />}
          </button>
          {deviceMenuOpen && (
            <div
              role="menu"
              className="absolute top-9 right-0 z-50 min-w-[210px] py-1.5 rounded-xl bg-bg-elevated border border-border shadow-lg animate-rise"
            >
              {DEVICE_PRESETS.map(d => (
                <button
                  key={d.id}
                  type="button"
                  role="menuitemradio"
                  aria-checked={d.id === deviceId}
                  className="flex items-center gap-2.5 w-full px-3 py-2 text-[13px] text-text hover:bg-bg-hover focus:bg-bg-hover focus:outline-none transition-colors bg-transparent border-none cursor-pointer text-left"
                  onClick={() => { setDeviceId(d.id); setDeviceMenuOpen(false) }}
                >
                  <span className="text-muted shrink-0">{d.w ? <Smartphone size={14} /> : <Monitor size={14} />}</span>
                  <span className="flex-1">{deviceLabel(d)}</span>
                  {d.w && <span className="text-[10px] text-muted font-mono shrink-0">{d.w}×{d.h}</span>}
                  {d.id === deviceId && <Check size={13} className="text-accent shrink-0" />}
                </button>
              ))}
            </div>
          )}
        </div>
        {/* Screenshot an area of the preview into the chat input. Gated to
            clients where the snip pipeline actually works (see canSnip). */}
        {canSnip && (
          <button
            type="button"
            onClick={() => window.dispatchEvent(new CustomEvent(PREVIEW_SNIP_EVENT))}
            className={iconBtn}
            title={i18nT('components.webPreviewPanel.screenshot_an_area_into_the_chat')}
            aria-label={i18nT('components.webPreviewPanel.screenshot_an_area_into_the_chat')}
          >
            <Crop size={14} />
          </button>
        )}
      </form>

      {/* Body */}
      <div className="relative flex-1 min-h-0 bg-white">
        {pending ? (
          // Click-to-load: a chat-fed URL is surfaced here but NOT navigated
          // until the user explicitly clicks Load (no auto-GET from agent output).
          <div className="flex flex-col items-center justify-center h-full gap-3 px-6 text-center bg-bg">
            <Globe size={22} className="text-accent" />
            <div className="text-[13px] font-medium text-text">{i18nT('components.webPreviewPanel.preview_ready')}</div>
            <div className="text-[11px] text-muted max-w-[320px] leading-snug">
              {i18nT('components.webPreviewPanel.the_agent_started_a_local_preview_load_it_to_vie')}
            </div>
            <code className="text-[11px] font-mono px-2 py-1 rounded bg-bg-elevated text-text break-all max-w-[320px]">
              {pending}
            </code>
            <div className="flex items-center gap-2 pt-1">
              <button
                type="button"
                onClick={() => commit(pending)}
                className="inline-flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-md bg-accent text-white hover:opacity-90 transition-opacity cursor-pointer border-none"
              >
                <Globe size={13} /> {i18nT('components.webPreviewPanel.load_preview')}
              </button>
              <button
                type="button"
                onClick={dismissPending}
                className="text-[12px] px-3 py-1.5 rounded-md border border-border text-muted hover:text-text hover:bg-bg-hover transition-colors cursor-pointer bg-transparent"
              >
                {i18nT('components.webPreviewPanel.dismiss')}
              </button>
            </div>
          </div>
        ) : !url ? (
          <div className="flex flex-col items-center justify-center h-full gap-3 px-6 text-center bg-bg">
            <Globe size={22} className="text-muted" />
            <div className="text-[13px] font-medium text-text">{i18nT('components.webPreviewPanel.preview_a_local_web_server')}</div>
            <div className="text-[11px] text-muted max-w-[300px] leading-snug">
              {i18nT('components.webPreviewPanel.enter_your_dev_server_url_above_e_g_a_vite_or_st')}
            </div>
            <div className="flex flex-wrap gap-1.5 justify-center pt-1">
              {COMMON_PORTS.map(p => (
                <button
                  key={p}
                  type="button"
                  onClick={() => commit(`http://localhost:${p}`)}
                  className="text-[11px] font-mono px-2 py-1 rounded-md border border-border bg-transparent text-muted hover:text-text hover:bg-bg-hover hover:border-border-strong transition-colors cursor-pointer"
                >
                  :{p}
                </button>
              ))}
            </div>
          </div>
        ) : mixedContent ? (
          <div className="flex flex-col items-center justify-center h-full gap-3 px-6 text-center bg-bg">
            <Globe size={22} className="text-muted" />
            <div className="text-[13px] font-medium text-text">{i18nT('components.webPreviewPanel.can_t_embed_an_http_page_here')}</div>
            <div className="text-[11px] text-muted max-w-[320px] leading-snug">
              {i18nT('components.webPreviewPanel.this_dashboard_is_served_over_https_so_the_brows')}
              <span className="font-mono"> {i18nT('components.webPreviewPanel.http')} </span>
              {i18nT('components.webPreviewPanel.page_mixed_content_open_it_in_a_new_tab_instead')}
            </div>
            <a
              href={url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-md border border-border text-text hover:bg-bg-hover transition-colors no-underline"
            >
              <ExternalLink size={13} /> {i18nT('components.webPreviewPanel.open')} {url}
            </a>
          </div>
        ) : unreachable ? (
          // The loaded dev server stopped responding — show a stopped state
          // instead of the stale last-rendered page (the iframe is unmounted).
          <div className="flex flex-col items-center justify-center h-full gap-3 px-6 text-center bg-bg">
            <Globe size={22} className="text-muted" />
            <div className="text-[13px] font-medium text-text">{i18nT('components.webPreviewPanel.preview_server_not_reachable')}</div>
            <div className="text-[11px] text-muted max-w-[320px] leading-snug">
              {i18nT('components.webPreviewPanel.the_server_at')} <span className="font-mono break-all">{url}</span> {i18nT('components.webPreviewPanel.stopped_responding_it_ll_reconnect_automatically')}
            </div>
            <button
              type="button"
              onClick={() => { setUnreachable(false); reload() }}
              className="inline-flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-md border border-border text-text hover:bg-bg-hover transition-colors cursor-pointer bg-transparent"
            >
              <RotateCw size={13} /> {i18nT('components.webPreviewPanel.reload')}
            </button>
          </div>
        ) : isDeviceSized ? (
          // Fixed device size — center the frame in a scrollable, muted backdrop.
          <div className="absolute inset-0 overflow-auto bg-bg-elevated flex items-start justify-center p-4">
            <iframe
              key={reloadKey}
              src={frameSrc}
              title={i18nT('components.webPreviewPanel.web_preview')}
              style={{ width: device.w, height: device.h }}
              className="shrink-0 border border-border rounded-lg bg-white shadow-sm"
              sandbox={SANDBOX}
            />
          </div>
        ) : (
          <iframe
            key={reloadKey}
            src={frameSrc}
            title={i18nT('components.webPreviewPanel.web_preview')}
            className="absolute inset-0 w-full h-full border-0 bg-white"
            sandbox={SANDBOX}
          />
        )}
      </div>
      </div>
      {nativeOpen ? nativeSurface : showMirror ? liveMirror : null}
    </div>
  )
}
