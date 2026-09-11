import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Globe, RotateCw, ExternalLink, ArrowLeft, ArrowRight, Expand, Minimize, Smartphone, Monitor, Check, Crop, Play, Loader2, AlertTriangle, MoreHorizontal, MousePointerClick, Pencil, X, Plus } from 'lucide-react'

import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
  DropdownMenuSub, DropdownMenuSubTrigger, DropdownMenuSubContent, DropdownMenuSeparator,
} from './ui/dropdown-menu'
import ErrorNotice from './ErrorNotice'
import { safeSetItem } from '../utils/safeStorage'
import {
  OPAQUE_ROLE_KEYS, PREVIEW_ANNOTATE_EVENT, annotationScreenshotFile, annotationStamp, describeAnnotationTarget,
  type AnnotationItem, type PreviewAnnotateDetail,
} from '../utils/browserAnnotations'
import { formatAnnotationDraft } from '../utils/browserAnnotations.prompt'
import { isScreenSnipSupported } from '../hooks/useScreenSnip'
import { useIsMobile } from '../hooks/useIsMobile'
import { useBrowserView } from '../hooks/useBrowserView'
import { useNativeBrowser } from '../hooks/useNativeBrowser'

import { i18nT } from '../i18n/t'
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
 * plain browser) the Playwright CLI's own loopback dashboard is framed
 * (`showBrowserView`, driven by `useBrowserView`). Unlike the native view, that
 * one is served by another process — but it is NOT read-only: the CLI dashboard
 * carries its own tab bar, navigation and full remote mouse/keyboard input, so
 * the frame is the control surface and nothing may be layered over it.
 * The dev-server iframe is a real embedded browser view: the dev server's own
 * HMR live-reloads it as the user edits, and Reload covers static servers.
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
 * Window event: enter/leave the preview expand mode. App collapses the
 * left nav and ChatPage hides the session list + maximizes the side panel, so
 * the preview gets maximum room and the chat pane shrinks to its minimum. A
 * plain window event (not prop-drilling) keeps this leaf panel decoupled from
 * the two ancestors that own that layout.
 */
export const PREVIEW_EXPAND_EVENT = 'kirocrew-preview-expand'
/**
 * Window event: request an area screenshot into the chat input. ChatPage owns
 * the capture pipeline (getDisplayMedia in the browser / the same path routed
 * through Electron's setDisplayMediaRequestHandler in the desktop app → the
 * SnipOverlay crop surface → attach the PNG to the composer), so the preview's
 * crop button just asks for it via this event rather than duplicating capture.
 */
export const PREVIEW_SNIP_EVENT = 'kirocrew-web-preview-snip'

/** The panel's mirror of the in-page pick overlay plus the notes it owns,
 *  keyed by the slot it belongs to (see the Annotate section in the component). */
interface AnnotateMirror {
  slot: string
  /** An overlay session is alive on the page (poll it). */
  live: boolean
  picking: boolean
  /** Picks on the current page, as the overlay reports them. */
  targets: BrowserAnnotationTarget[]
  /** Notes by pick id. Panel-owned; never sent to the page. */
  notes: Record<number, string>
  /** Noted picks kept from a page that navigated away (detached). */
  retained: AnnotationItem[]
  page: { url: string; title: string }
  /** The note editor: which pick, and the text being typed. */
  editing: { id: number; text: string } | null
}
const EMPTY_TARGETS: BrowserAnnotationTarget[] = []
const EMPTY_ANNOTATE: AnnotateMirror = {
  slot: '', live: false, picking: false, targets: EMPTY_TARGETS, notes: {}, retained: [], page: { url: '', title: '' }, editing: null,
}
/** One mirror per chat slot. Kept as a map (not "the current slot's mirror")
 *  so switching sessions and coming back finds the notes where they were. */
type AnnotateMirrors = Record<string, AnnotateMirror>
/** sessionStorage key: typed prose must survive a dashboard reload too (the
 *  native view -- and the overlay in it -- outlives the renderer). Session
 *  scoped, not local: notes are about a page that is open right now. */
const ANNOTATE_STORE_KEY = 'mc-browser-annotations'
function loadAnnotateMirrors(): AnnotateMirrors {
  try {
    const raw = sessionStorage.getItem(ANNOTATE_STORE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as unknown
    if (!parsed || typeof parsed !== 'object') return {}
    const out: AnnotateMirrors = {}
    for (const [slot, m] of Object.entries(parsed as Record<string, Partial<AnnotateMirror>>)) {
      if (!m || typeof m !== 'object') continue
      out[slot] = {
        slot,
        live: !!m.live,
        // Pick mode is a live gesture, never resumed from storage.
        picking: false,
        targets: Array.isArray(m.targets) ? m.targets : EMPTY_TARGETS,
        notes: m.notes && typeof m.notes === 'object' ? m.notes : {},
        retained: Array.isArray(m.retained) ? m.retained : [],
        page: m.page && typeof m.page === 'object' ? { url: String(m.page.url ?? ''), title: String(m.page.title ?? '') } : { url: '', title: '' },
        editing: m.editing && typeof m.editing === 'object' && typeof m.editing.id === 'number' ? { id: m.editing.id, text: String(m.editing.text ?? '') } : null,
      }
    }
    return out
  } catch {
    return {}
  }
}
/** Keep the text of an open editor as its pick's note (live picks only; a
 *  retained pick's draft is written by the caller). Never drops prose. */
function flushDraft(
  editing: AnnotateMirror['editing'],
  notes: Record<number, string>,
  targets: BrowserAnnotationTarget[],
): Record<number, string> {
  if (!editing) return notes
  const text = editing.text.replace(/\s+/g, ' ').trim()
  if (!text || !targets.some(t => t.id === editing.id)) return notes
  return { ...notes, [editing.id]: text }
}
/** The page went away (navigation, view lost): stop the live session but keep
 *  every noted pick -- including a note still being typed -- as detached. */
function detachAll(prev: AnnotateMirror): AnnotateMirror {
  const notes = { ...prev.notes }
  // Text in the open editor is the user's too; an uncommitted draft is kept
  // as that pick's note rather than lost with the page.
  if (prev.editing && prev.editing.text.trim() && prev.targets.some(t => t.id === prev.editing!.id)) {
    notes[prev.editing.id] = prev.editing.text.replace(/\s+/g, ' ').trim()
  }
  return {
    ...prev,
    live: false,
    picking: false,
    editing: prev.editing && prev.retained.some(a => a.id === prev.editing!.id) ? prev.editing : null,
    retained: [
      ...prev.retained,
      ...prev.targets.filter(t => (notes[t.id] ?? '').trim()).map(t => ({ ...t, note: notes[t.id] ?? '', detached: true })),
    ],
    targets: EMPTY_TARGETS,
    notes: {},
  }
}
function saveAnnotateMirrors(mirrors: AnnotateMirrors): void {
  try {
    const kept = Object.fromEntries(Object.entries(mirrors).filter(([, m]) => m.live || m.targets.length || m.retained.length || m.editing))
    if (Object.keys(kept).length) sessionStorage.setItem(ANNOTATE_STORE_KEY, JSON.stringify(kept))
    else sessionStorage.removeItem(ANNOTATE_STORE_KEY)
  } catch {
    // Storage unavailable or full: the in-memory map still covers this tab.
  }
}

type AnnotateResult = Awaited<ReturnType<NonNullable<BrowserAPI['annotate']>>>
/** Explicit guards, not `in` narrowing: the bridge returns a wide union and
 *  `tsc -b` under the app config does not narrow `'items' in res` usefully. */
function isAnnotatePoll(res: AnnotateResult): res is BrowserAnnotatePoll {
  return !!res && res.ok === true && Array.isArray((res as { items?: unknown }).items) && typeof (res as { picking?: unknown }).picking === 'boolean'
}
function isAnnotateCapture(res: AnnotateResult): res is BrowserAnnotateCapture {
  return !!res && res.ok === true && typeof (res as { png?: unknown }).png === 'string'
}
/** Plain names for the opaque ARIA roles, in the UI language (the on-page
 *  hover tag gets the same map, since the page realm has no catalog). */
function annotateRoleNames(): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [role, key] of Object.entries(OPAQUE_ROLE_KEYS)) out[role] = i18nT(key)
  return out
}
function annotateFailureText(res: AnnotateResult): string {
  return res && res.ok === false && typeof res.error === 'string' ? res.error : ''
}
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
function isolateFrameTarget(url: string): string {
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
  // Preview expand mode — reflected in the toggle icon; broadcast to
  // App/ChatPage which collapse the surrounding chrome.
  const [expanded, setExpanded] = useState(false)
  // Viewport preset (responsive desktop vs a fixed device size).
  const [deviceId, setDeviceId] = useState('responsive')
  const [moreOpen, setMoreOpen] = useState(false)
  // The native address-bar input. The view-URL sync effect reads its focus so a
  // view-initiated navigation never overwrites the field while the user types.
  const nativeInputRef = useRef<HTMLInputElement>(null)

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

  // ── Playwright CLI browser view ───────────────────────────────────────────
  // The same Browser panel also frames the Playwright CLI's own loopback
  // dashboard (`show --port`), which is where an agent's browsing is watched AND
  // driven. There is no per-frame push to subscribe to any more: the view is a
  // process with a URL, so the panel polls its status and frames the URL.
  const view = useBrowserView(active)
  // Validate the server-reported URL before it becomes an iframe `src`. The
  // contract promises `http://127.0.0.1:<port>/`, and `normalizeUrl` rejects
  // anything that is not http(s) — so a malformed or hostile value degrades to
  // the explanatory state instead of being navigated.
  //
  // Deliberately NOT run through `isolatePreviewHost`: that swaps a loopback host
  // matching the dashboard's onto the other alias, and the CLI binds ONE
  // interface (`--host 127.0.0.1`). Swapping `127.0.0.1` to `localhost` can
  // resolve to `::1`, where nothing is listening — the exact IPv6 trap the
  // explicit bind exists to avoid. The view's URL is used verbatim.
  const viewUrl = useMemo(
    () => (view.data?.url ? normalizeUrl(view.data.url) : null),
    [view.data?.url],
  )
  const viewRunning = view.data?.status === 'running' && !!viewUrl
  // Whether the browser view OWNS the panel. Null = follow the view itself (it
  // takes over as soon as it is running, which is how the old frame mirror
  // behaved); true/false = the user overrode that with the toggle, so they can
  // get back to a dev-server preview while a browser session is up, and can open
  // the view deliberately to see WHY it is not running.
  const [viewOverride, setViewOverride] = useState<boolean | null>(null)
  // ── Transport selection ──
  // The browsing surface has two backends, chosen by WHERE the browser actually
  // runs — and the NATIVE view wins whenever it exists:
  //
  //   • A real Chromium view is available in THIS process (Electron shell) ->
  //     embed it natively. A chat-opened page lands in the real, human- and
  //     agent-operable browser, not a read-only screenshot. Native OWNS the
  //     panel; the CLI view is suppressed even if its server is up.
  //   • Otherwise (remote gateway / plain browser, where `useNativeBrowser`
  //     reports `available: false`) -> the browser lives in another process, and
  //     the Playwright CLI's own dashboard is what shows AND drives it.
  //
  // Scoped by sessionKey: each Browser panel owns its own native view. `enabled`
  // is just `active` (native no longer yields to streaming frames), so switching
  // side-panel tabs hides — never destroys — the view. Authorization for the
  // agent to drive the view is Browser Mode itself (the Settings toggle),
  // enforced in the Electron main process (browser-control.js `canAgentControl`);
  // the panel no longer carries a per-session agent-act toggle.
  const native = useNativeBrowser(sessionKey || '', active)
  // Native wins when available. Its view is "open" once a page has been loaded
  // into it; until then the panel shows the ordinary iframe preview.
  const nativeOpen = native.available && !!native.state?.open
  // The CLI browser view is the surface whenever no native view is actually
  // OWNING the panel. `!nativeOpen` is the real condition (not
  // `!native.available`): availability only means Electron's preload bridge
  // exists, so on the desktop app before any page is opened natively the native
  // view has nothing to show and gating on availability left a blank panel.
  const showBrowserView = !nativeOpen && (viewOverride ?? viewRunning)
  /** Flip the browser view on or off, taking over from whatever it follows now. */
  const toggleBrowserView = useCallback(() => {
    setViewOverride(v => !(v ?? viewRunning))
  }, [viewRunning])

  // ── Annotate: element notes on the live page ──────────────────────────────
  // The user points at elements IN the native page (hover highlight, click to
  // select); that surface is an overlay injected into the page by the main
  // process, because the native view is composited above this DOM. The NOTE
  // is typed HERE, in the panel: the page is external content, and a text
  // input inside it would let any page script read the user's prose. So the
  // overlay reports picked elements (id, number, ref, description, liveness)
  // and the panel owns the notes, the editor, the list and Send/Clear.
  // Native-only by construction: the remote/CLI transport has no in-process
  // view to inject into, so the button is not rendered there.
  const annotateBridge = window.browserAPI?.annotate
  const canAnnotate = nativeOpen && typeof annotateBridge === 'function'
  // One mirror per slot, read through the CURRENT slot key in the same
  // render, so a session switch never shows -- or sends -- another session's
  // notes, and coming back finds this session's notes where they were. The
  // map also lives in sessionStorage so a dashboard reload keeps typed prose.
  const [annotateMirrors, setAnnotateMirrors] = useState<AnnotateMirrors>(loadAnnotateMirrors)
  const mirror = annotateMirrors[sessionKey || ''] ?? EMPTY_ANNOTATE
  useEffect(() => { saveAnnotateMirrors(annotateMirrors) }, [annotateMirrors])
  const mirrorsRef = useRef(annotateMirrors)
  mirrorsRef.current = annotateMirrors
  const annotateOn = mirror.live
  const picking = mirror.picking
  const editing = mirror.editing
  /** Everything listed: notes kept from a page that navigated away, then the
   *  current page's picks with their (panel-owned) notes. */
  const annotations = useMemo<AnnotationItem[]>(() => [
    ...mirror.retained,
    ...mirror.targets.map(t => ({ ...t, note: mirror.notes[t.id] ?? '' })),
  ], [mirror.retained, mirror.targets, mirror.notes])
  /** What Send hands over: only picks that actually carry a note. */
  const noted = useMemo(() => annotations.filter(a => a.note.trim()), [annotations])
  /** Add to chat has something to send once a note is saved -- or typed and
   *  still open in the editor (it is committed on the way out). */
  const canSend = noted.length > 0 || !!editing?.text.trim()
  const [annotateBusy, setAnnotateBusy] = useState(false)
  const [annotateError, setAnnotateError] = useState('')
  /** The agent hand-off is offered on errors it could act on -- not on the
   *  capture-failed notice, which already says the notes were added and what
   *  did not happen; there is nothing for the agent to fix there. */
  const annotateErrorAskAgent = annotateError !== i18nT('components.webPreviewPanel.annotate_capture_failed')
  /** Brief confirmation after Add to chat: the result lands in the composer,
   *  away from the button, so say here that nothing was sent yet. */
  const [addedAck, setAddedAck] = useState(false)
  /** Two-step Clear: first click arms (label restates the count), second within
   *  the window executes. Typed prose must not vanish on one click. */
  const [clearArmed, setClearArmed] = useState(false)
  const clearTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  /** Consecutive poll failures that are NOT "the overlay is gone". */
  const pollFailures = useRef(0)
  /**
   * Picks the poll updater abandons (nothing typed, editor moved on) and the
   * page has not been told to drop yet, keyed by the slot they belong to. The
   * updater fills it; the effect below drains it after the commit. React runs
   * a state updater lazily whenever the hook already has an update queued, so
   * a decision made inside it is not readable on the line after setState --
   * only after the render it produces. The slot travels with the id because
   * that render can also be the one that switches sessions: a remove bound to
   * the session current at drain time would reach the other slot's page, where
   * the same marker number may be a live pick.
   */
  const abandonedPicks = useRef<Map<string, Set<number>>>(new Map())
  const noteInputRef = useRef<HTMLInputElement>(null)

  const callAnnotate = useCallback(async (op: BrowserAnnotateOp, args?: Record<string, unknown>) => {
    if (!sessionKey || typeof annotateBridge !== 'function') return null
    return annotateBridge(sessionKey, op, args)
  }, [annotateBridge, sessionKey])

  /** Update ONE slot's mirror; every other slot's state is left alone. */
  const patchSlot = useCallback((slot: string, fn: (prev: AnnotateMirror) => AnnotateMirror) => {
    setAnnotateMirrors(prev => {
      const cur = prev[slot] ?? { ...EMPTY_ANNOTATE, slot }
      const next = fn(cur)
      return next === cur ? prev : { ...prev, [slot]: next }
    })
  }, [])
  /** Update the current slot's mirror. */
  const patchMirror = useCallback((fn: (prev: AnnotateMirror) => AnnotateMirror) => {
    patchSlot(sessionKey || '', fn)
  }, [patchSlot, sessionKey])

  // Tell each slot's overlay to drop the picks the poll updater abandoned. This
  // runs after the commit that removed them from the list, so list, count and
  // page highlight agree, and it addresses the bridge by the recorded slot
  // rather than through callAnnotate, which is bound to whichever session is
  // current now. The Set dedupes the updater's second run under StrictMode.
  useEffect(() => {
    if (!abandonedPicks.current.size) return
    const pending = [...abandonedPicks.current]
    abandonedPicks.current.clear()
    if (typeof annotateBridge !== 'function') return
    for (const [slot, ids] of pending) for (const id of ids) void annotateBridge(slot, 'remove', { id })
  }, [annotateMirrors, annotateBridge])

  // A session switch resets the per-turn transients: poll failure count and an
  // armed Clear -- a confirmation armed for one session's notes must never fire
  // on another session's.
  useEffect(() => {
    pollFailures.current = 0
    setClearArmed(false)
    if (clearTimer.current) { clearTimeout(clearTimer.current); clearTimer.current = null }
  }, [sessionKey])

  /** Toggle pick mode. First use installs the overlay and starts polling. */
  const toggleAnnotate = useCallback(async () => {
    if (annotateBusy) return
    setAnnotateError('')
    if (annotateOn && picking) {
      // Only a confirmed stop flips the local state; a failed one is surfaced
      // and `picking` is left for the poll to reconcile with the page.
      const stopped = await callAnnotate('stop')
      if (!stopped || !stopped.ok) {
        setAnnotateError(annotateFailureText(stopped) || i18nT('components.webPreviewPanel.annotate_update_failed'))
        return
      }
      patchMirror(prev => ({ ...prev, picking: false }))
      return
    }
    // Continue numbering after notes retained from a page that navigated
    // away, so on-page markers, the list and the draft agree, and ids never
    // collide with the retained ones.
    const res = await callAnnotate('start', {
      editHint: i18nT('components.webPreviewPanel.annotate_click_to_edit'),
      roleNames: annotateRoleNames(),
      seq: mirror.retained.reduce((m, a) => Math.max(m, a.n), 0),
      idStart: mirror.retained.reduce((m, a) => Math.max(m, a.id), 0),
    })
    if (!res || !res.ok) {
      setAnnotateError(annotateFailureText(res) || i18nT('components.webPreviewPanel.annotate_failed'))
      return
    }
    const page = { url: res.url ?? '', title: res.title ?? '' }
    pollFailures.current = 0
    // Starting never wipes: picks and notes from a paused session (Done,
    // Add to chat, or a transient poll failure) stay exactly as they are. If
    // the overlay really is gone, the first poll answers `no_overlay` and
    // `detachAll` moves the noted picks to `retained` -- nothing typed is
    // discarded on any path.
    patchMirror(prev => ({ ...prev, live: true, picking: true, page }))
  }, [annotateBusy, annotateOn, picking, callAnnotate, patchMirror, mirror.retained])

  // Mirror the overlay while a session is alive. The overlay lives in the
  // page, so the picks the user makes there reach this panel only by asking;
  // 150ms keeps it feeling live without measurable cost (one tiny
  // executeJavaScript per tick; state is replaced only when something
  // changed). Two failure shapes, two answers:
  //   • `no_overlay` -- the document navigated and took the overlay with it.
  //     Polling stops, but the notes the user typed are KEPT (moved to
  //     `retained`, marked detached), so Send still works: the draft flags
  //     them, no screenshot. Annotating a dev app the agent is editing means
  //     HMR reloads mid-session are routine; wiping typed prose on each one
  //     would be the wrong default.
  //   • anything else (timeout, exception) -- transient. Retry; after three in
  //     a row surface it and pause polling, still keeping the notes.
  useEffect(() => {
    if (!annotateOn || !canAnnotate) return
    let stopped = false
    const slot = sessionKey || ''
    const tick = async () => {
      const res = await callAnnotate('poll')
      if (stopped) return
      if (isAnnotatePoll(res)) {
        pollFailures.current = 0
        patchSlot(slot, prev => {
          // A live pick can never share an id with a retained note (the overlay
          // is started past the highest retained id); one that does is a
          // page-controlled reply and is ignored rather than aliasing a note.
          const incoming = prev.retained.length
            ? res.items.filter(t => !prev.retained.some(a => a.id === t.id))
            : res.items
          const sameTargets = JSON.stringify(incoming) === JSON.stringify(prev.targets)
          let targets = sameTargets ? prev.targets : incoming
          let notes = prev.notes
          let retained = prev.retained
          if (!sameTargets) {
            // A pick the reply no longer lists: the honest overlay only drops
            // one on the user's own remove/clear (both already handled here),
            // so anything else -- a page tampering with the reply -- must not
            // cost a note. Noted picks that went missing are kept as detached.
            const keep = new Set(targets.map(t => t.id))
            const missingNoted = prev.targets.filter(t => !keep.has(t.id) && (prev.notes[t.id] ?? '').trim() && !retained.some(a => a.id === t.id))
            if (missingNoted.length) retained = [...retained, ...missingNoted.map(t => ({ ...t, note: prev.notes[t.id], detached: true }))]
            notes = Object.fromEntries(Object.entries(prev.notes).filter(([id]) => keep.has(Number(id))))
          }
          let editing = prev.editing
          const moveTo = res.picked !== undefined ? res.picked : res.edit
          if (moveTo !== undefined && (!editing || editing.id !== moveTo)) {
            // The editor moves to another pick. Whatever was typed for the one
            // it leaves is kept as that pick's note -- live or retained -- and
            // never dropped (same rescue as detachAll on navigation). A pick
            // left with NOTHING typed is abandoned: it goes, like Esc does, so
            // the list, the count and the draft never disagree about it.
            notes = flushDraft(editing, notes, targets)
            if (editing && editing.text.trim() && retained.some(a => a.id === editing!.id)) {
              const text = editing.text.replace(/\s+/g, ' ').trim()
              retained = retained.map(a => (a.id === editing!.id ? { ...a, note: text } : a))
            }
            if (editing && !editing.text.trim() && !(notes[editing.id] ?? '').trim() && targets.some(t => t.id === editing!.id) && editing.id !== moveTo) {
              const ids = abandonedPicks.current.get(slot) ?? new Set<number>()
              ids.add(editing.id)
              abandonedPicks.current.set(slot, ids)
              targets = targets.filter(t => t.id !== editing!.id)
            }
            editing = { id: moveTo, text: notes[moveTo] ?? '' }
          }
          else if (editing && !targets.some(t => t.id === editing!.id) && !retained.some(a => a.id === editing!.id)) editing = null
          const page = { url: res.url, title: res.title }
          if (sameTargets && notes === prev.notes && retained === prev.retained && editing === prev.editing && prev.picking === res.picking
            && prev.page.url === page.url && prev.page.title === page.title) return prev
          return { ...prev, picking: res.picking, targets, notes, retained, editing, page }
        })
        return
      }
      // The overlay is gone (navigation) or the view itself is (closed,
      // destroyed): either way the page is not coming back for these picks.
      if (res && !res.ok && (res.code === 'no_overlay' || res.code === 'no_view')) {
        patchSlot(slot, detachAll)
        return
      }
      pollFailures.current += 1
      if (pollFailures.current >= 3) {
        setAnnotateError(annotateFailureText(res) || i18nT('components.webPreviewPanel.annotate_failed'))
        patchSlot(slot, prev => ({ ...prev, live: false, picking: false }))
      }
    }
    void tick()
    const t = setInterval(() => { void tick() }, 150)
    return () => {
      stopped = true
      clearInterval(t)
    }
  }, [annotateOn, canAnnotate, callAnnotate, sessionKey, patchSlot])

  // The pick happened in the page; the note is typed here. Focus the editor
  // as soon as it opens (the main process has already handed keyboard focus
  // back to this renderer on the poll that reported the pick).
  const editingId = editing?.id
  useEffect(() => {
    if (editingId === undefined) return
    const el = noteInputRef.current
    if (!el) return
    el.focus()
    el.select()
  }, [editingId])

  // Losing the native view (transport switch, panel close) ends the live
  // session; noted picks are kept as detached until cleared.
  useEffect(() => {
    if (canAnnotate) return
    patchMirror(prev => (prev.live ? detachAll(prev) : prev))
  }, [canAnnotate, patchMirror])
  // Unmount (the panel itself closes) tears every live overlay down. A session
  // SWITCH does not: the other session's native view -- and the overlay in it
  // -- stays alive, so its picks are still there when the user comes back.
  useEffect(() => () => {
    if (typeof annotateBridge !== 'function') return
    for (const [slot, m] of Object.entries(mirrorsRef.current)) {
      if (slot && m.live) void annotateBridge(slot, 'teardown')
    }
  }, [annotateBridge])

  useEffect(() => {
    if (!annotateError) return
    const t = setTimeout(() => setAnnotateError(''), 8000)
    return () => clearTimeout(t)
  }, [annotateError])
  useEffect(() => {
    if (!addedAck) return
    const t = setTimeout(() => setAddedAck(false), 5000)
    return () => clearTimeout(t)
  }, [addedAck])
  useEffect(() => () => { if (clearTimer.current) clearTimeout(clearTimer.current) }, [])

  const dropLocally = useCallback((id: number) => {
    patchMirror(prev => {
      const isTarget = prev.targets.some(t => t.id === id)
      const notes = { ...prev.notes }
      delete notes[id]
      return {
        ...prev,
        notes,
        targets: isTarget ? prev.targets.filter(t => t.id !== id) : prev.targets,
        retained: prev.retained.filter(a => a.id !== id),
        editing: prev.editing?.id === id ? null : prev.editing,
      }
    })
  }, [patchMirror])
  /** Remove a pick. On a live page the overlay is asked FIRST and the note is
   *  dropped only once it confirms: a refused removal must not leave a marker
   *  on the page whose note has already vanished from here. */
  const removeAnnotation = useCallback(async (id: number) => {
    const live = annotateOn && mirror.targets.some(t => t.id === id)
    if (!live) {
      dropLocally(id)
      return
    }
    const res = await callAnnotate('remove', { id })
    if (res && res.ok) dropLocally(id)
    else setAnnotateError(annotateFailureText(res) || i18nT('components.webPreviewPanel.annotate_update_failed'))
  }, [annotateOn, mirror.targets, callAnnotate, dropLocally])
  const editAnnotation = useCallback((id: number) => {
    patchMirror(prev => {
      if (prev.editing?.id === id) return prev
      // Moving the editor keeps the in-flight text of the pick it leaves.
      const notes = flushDraft(prev.editing, prev.notes, prev.targets)
      const retained = prev.editing && prev.editing.text.trim() && prev.retained.some(a => a.id === prev.editing!.id)
        ? prev.retained.map(a => (a.id === prev.editing!.id ? { ...a, note: prev.editing!.text.replace(/\s+/g, ' ').trim() } : a))
        : prev.retained
      const existing = notes[id] ?? retained.find(a => a.id === id)?.note ?? ''
      return { ...prev, notes, retained, editing: { id, text: existing } }
    })
  }, [patchMirror])
  const setEditingText = useCallback((text: string) => {
    patchMirror(prev => (prev.editing ? { ...prev, editing: { ...prev.editing, text } } : prev))
  }, [patchMirror])
  /** Enter / Save: keep the note; an empty note on a fresh pick removes the pick. */
  const commitNote = useCallback(() => {
    const cur = editing
    if (!cur) return
    const text = cur.text.replace(/\s+/g, ' ').trim()
    if (!text) {
      const had = mirror.notes[cur.id] ?? mirror.retained.find(a => a.id === cur.id)?.note ?? ''
      if (!had) void removeAnnotation(cur.id)
      else patchMirror(prev => ({ ...prev, editing: null }))
      return
    }
    patchMirror(prev => ({
      ...prev,
      notes: prev.targets.some(t => t.id === cur.id) ? { ...prev.notes, [cur.id]: text } : prev.notes,
      retained: prev.retained.map(a => (a.id === cur.id ? { ...a, note: text } : a)),
      editing: null,
    }))
  }, [editing, mirror.notes, mirror.retained, removeAnnotation, patchMirror])
  /** Esc / Cancel: drop the edit; a fresh pick with nothing typed is removed. */
  const cancelNote = useCallback(() => {
    const cur = editing
    if (!cur) return
    const had = mirror.notes[cur.id] ?? mirror.retained.find(a => a.id === cur.id)?.note ?? ''
    if (!had) void removeAnnotation(cur.id)
    else patchMirror(prev => ({ ...prev, editing: null }))
  }, [editing, mirror.notes, mirror.retained, removeAnnotation, patchMirror])
  const clearAnnotations = useCallback(() => {
    if (!clearArmed) {
      setClearArmed(true)
      if (clearTimer.current) clearTimeout(clearTimer.current)
      // Long enough to read the restated label, short enough that a stale
      // armed state cannot ambush a later click.
      clearTimer.current = setTimeout(() => setClearArmed(false), 4000)
      return
    }
    if (clearTimer.current) clearTimeout(clearTimer.current)
    setClearArmed(false)
    const wipe = () => patchMirror(prev => ({ ...prev, targets: EMPTY_TARGETS, notes: {}, retained: [], editing: null }))
    if (!annotateOn) {
      wipe()
      return
    }
    // Live page: clear the markers first; only a confirmed clear drops the notes.
    void callAnnotate('clear').then(res => {
      if (res && res.ok) wipe()
      else setAnnotateError(annotateFailureText(res) || i18nT('components.webPreviewPanel.annotate_update_failed'))
    })
  }, [clearArmed, annotateOn, callAnnotate, patchMirror])

  /** Add to chat: screenshot with the markers + the numbered draft, both into
   *  the composer of THIS session (nothing is sent). Notes stay so the user
   *  can keep adding and add again. */
  const sendAnnotations = useCallback(async () => {
    if (annotateBusy) return
    // Text still open in the editor is the user's newest words for that pick:
    // commit it first so the draft carries it, not the previously saved note.
    const openText = editing?.text.replace(/\s+/g, ' ').trim() ?? ''
    const outgoing = annotations
      .map(a => (editing && a.id === editing.id && openText ? { ...a, note: openText } : a))
      .filter(a => a.note.trim())
    if (!outgoing.length) return
    if (editing && openText) {
      patchMirror(prev => ({
        ...prev,
        notes: prev.targets.some(t => t.id === editing.id) ? { ...prev.notes, [editing.id]: openText } : prev.notes,
        retained: prev.retained.map(a => (a.id === editing.id ? { ...a, note: openText } : a)),
        editing: null,
      }))
    }
    setAnnotateBusy(true)
    setAnnotateError('')
    try {
      const cap = annotateOn ? await callAnnotate('capture') : null
      const files: File[] = []
      let page = mirror.page
      let screenshotName: string | undefined
      if (isAnnotateCapture(cap)) {
        const file = annotationScreenshotFile(cap.png, annotationStamp())
        files.push(file)
        screenshotName = file.name
        page = { url: cap.url, title: cap.title }
      } else if (annotateOn) {
        // The notes still go into the composer; say precisely what did not.
        setAnnotateError(i18nT('components.webPreviewPanel.annotate_capture_failed'))
      }
      const draft = formatAnnotationDraft(outgoing, { url: page.url, title: page.title, screenshotName })
      const detail: PreviewAnnotateDetail = { slot: sessionKey || '', files, draft }
      window.dispatchEvent(new CustomEvent(PREVIEW_ANNOTATE_EVENT, { detail }))
      setAddedAck(true)
      if (annotateOn && picking) {
        // Pick mode ends only when the page confirms; otherwise say so and let
        // the poll reconcile `picking` with the page's real state.
        const stopped = await callAnnotate('stop')
        if (stopped && stopped.ok) patchMirror(prev => ({ ...prev, picking: false }))
        else setAnnotateError(annotateFailureText(stopped) || i18nT('components.webPreviewPanel.annotate_update_failed'))
      }
    } catch (e) {
      setAnnotateError(e instanceof Error && e.message ? e.message : i18nT('components.webPreviewPanel.annotate_failed'))
    } finally {
      setAnnotateBusy(false)
    }
  }, [annotateBusy, annotations, editing, annotateOn, picking, mirror.page, callAnnotate, sessionKey, patchMirror])

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
    saved = saved ? isolateFrameTarget(saved) : ''
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
    // Drop any manual show/hide of the browser view: the override is a decision
    // about THIS session's panel, and carrying it into another session would hide
    // a running view (or pin an explanatory card) the user never asked for there.
    setViewOverride(null)
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

  // Reflect view-initiated navigation (agent browser_navigate, in-page link
  // clicks, redirects) into the native address bar. `did-navigate` already
  // updates `native.state.url`, but the input is bound to `draft`, which
  // otherwise only changes on typing or back/forward — so an agent-opened page
  // left the bar empty and showing its placeholder. Mirror the live URL into
  // `draft`, but never while the user is editing the field, so a slow redirect
  // cannot overwrite what they are typing.
  //
  // Display only — deliberately does NOT persist. `native.state` carries no
  // session tag, and on a slot switch this component instance is reused (its
  // `sessionKey` changes) while `useNativeBrowser` may still hold the previous
  // slot's state until `getState` resolves; persisting here would write that
  // stale URL into the NEW slot's storage. User-typed navigation persists via
  // `commitNative`; view-initiated navigation stays display-only, as before.
  useEffect(() => {
    const u = native.state?.url
    if (!nativeOpen || !u) return
    if (document.activeElement === nativeInputRef.current) return
    setDraft(u)
  }, [nativeOpen, native.state?.url])

  const commit = useCallback((raw: string) => {
    const norm = normalizeUrl(raw)
    if (!norm) return
    const isolated = isolateFrameTarget(norm)
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

  const broadcastExpanded = useCallback((expanded: boolean) => {
    window.dispatchEvent(new CustomEvent(PREVIEW_EXPAND_EVENT, { detail: { expanded } }))
  }, [])
  const toggleExpand = useCallback(() => {
    setExpanded(v => { broadcastExpanded(!v); return !v })
  }, [broadcastExpanded])
  // Leaving the preview (tab switched away or panel/tab closed) drops expand mode
  // so the chrome isn't left collapsed with the toggle out of reach.
  useEffect(() => {
    if (!active && expanded) { setExpanded(false); broadcastExpanded(false) }
  }, [active, expanded, broadcastExpanded])
  useEffect(() => () => { broadcastExpanded(false) }, [broadcastExpanded])

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

  // A URL pointing back at this gateway is refused by its own frame-ancestors
  // policy, which the liveness probe below cannot see (the server is up, so the
  // probe passes) — detect it so we explain instead of framing a blank page.
  const selfOrigin = useMemo(() => !!url && isDashboardOrigin(url), [url])

  // Liveness probe: a cross-origin iframe cannot tell us its server died — it
  // just keeps showing the last document. While a URL is actually framed (loaded,
  // embeddable, tab active) poll it with a no-cors GET; a connection-refused error
  // throws. Two consecutive failures ⇒ the dev server stopped, so we mark it
  // unreachable and unmount the iframe (clearing the stale page). A later success
  // auto-restores it. (Two strikes tolerates a brief HMR/dev-server restart.)
  useEffect(() => {
    if (!url || mixedContent || selfOrigin || pending || !active) return
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
  }, [url, reloadKey, mixedContent, selfOrigin, pending, active])

  const iconBtn = 'flex items-center justify-center w-7 h-7 rounded-md text-muted hover:text-text '
    + 'hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 '
    + 'disabled:opacity-40 disabled:cursor-default disabled:hover:bg-transparent disabled:hover:text-muted'

  // The Playwright CLI browser view — the agent-browse surface for the case where
  // the browser runs in another process. Rendered as an OVERLAY (not an early
  // return) so the preview subtree below stays mounted; its iframe document + any
  // unsaved form/SPA state survive the view taking over mid-preview.
  //
  // Nothing is layered over the frame. The CLI dashboard's own remote mouse and
  // keyboard input IS the control surface, so a scrim, a hint bar or a
  // click-to-focus catcher over it would swallow exactly the events that make the
  // view useful. Chrome goes in the header above it, never on top.
  const browserView = (
    <div className="absolute inset-0 z-10 flex flex-col h-full min-h-0 bg-bg">
      <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border shrink-0" style={{ backgroundColor: 'var(--bg-elevated)' }}>
        <Monitor size={14} className="shrink-0 text-muted" />
        <span className="shrink-0 text-[13px] font-medium text-text">{i18nT('components.webPreviewPanel.browser_view')}</span>
        {viewRunning && (
          <span className="inline-block w-1.5 h-1.5 rounded-full animate-pulse" style={{ backgroundColor: 'var(--ok)' }} aria-hidden />
        )}
        <div className="flex-1" />
        {viewUrl && (
          <a
            href={viewUrl}
            target="_blank"
            rel="noreferrer"
            className="flex items-center justify-center w-6 h-6 rounded text-muted hover:text-text hover:bg-bg-hover transition-colors shrink-0 no-underline"
            title={i18nT('components.webPreviewPanel.open_in_browser')}
            aria-label={i18nT('components.webPreviewPanel.open_in_browser')}
          >
            <ExternalLink size={13} />
          </a>
        )}
        {/* Same toggle as the one in the URL bar, in its pressed state — the bar
            is inert while this overlay is up, so the way back has to live here. */}
        <button
          type="button"
          onClick={toggleBrowserView}
          className="flex items-center justify-center w-6 h-6 rounded text-accent bg-accent-subtle hover:text-accent transition-colors border-none cursor-pointer shrink-0"
          title={i18nT('components.webPreviewPanel.browser_view')}
          aria-label={i18nT('components.webPreviewPanel.browser_view')}
          aria-pressed
        >
          <Monitor size={13} />
        </button>
      </div>
      <div className="relative flex-1 min-h-0 bg-bg-elevated">
        {view.pending || view.starting ? (
          <div className="flex flex-col items-center justify-center h-full gap-2 px-6 text-center">
            <Loader2 size={18} className="text-muted animate-spin" aria-hidden />
            <span className="text-[11px] text-muted">{i18nT('components.webPreviewPanel.loading_the_browser_view')}</span>
          </div>
        ) : viewRunning ? (
          // The CLI dashboard, framed. It brings its own session grid, tab bar,
          // navigation and remote input, so the panel adds no controls of its own.
          <iframe
            src={viewUrl as string}
            title={i18nT('components.webPreviewPanel.live_browser_session')}
            className="absolute inset-0 w-full h-full border-0 bg-bg-elevated"
            sandbox={SANDBOX}
          />
        ) : view.data?.status === 'stopped' ? (
          <div className="flex flex-col items-center justify-center h-full gap-3 px-6 text-center bg-bg">
            <Monitor size={22} className="text-muted" />
            <div className="text-[13px] font-medium text-text">{i18nT('components.webPreviewPanel.browser_view_is_not_running')}</div>
            <div className="text-[11px] text-muted max-w-[320px] leading-snug">
              {i18nT('components.webPreviewPanel.start_it_to_watch_and_drive_the_agent_s_browser')}
            </div>
            {view.data.reason && (
              <div className="text-[11px] text-muted max-w-[320px] leading-snug">{view.data.reason}</div>
            )}
            <button
              type="button"
              onClick={view.start}
              className="inline-flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-md bg-accent text-white hover:opacity-90 transition-opacity cursor-pointer border-none"
            >
              <Play size={13} /> {i18nT('components.webPreviewPanel.start_browser_view')}
            </button>
            {(!!view.startError || view.startDidNotTake) && (
              <div className="flex items-center gap-1.5 text-[11px] max-w-[320px] leading-snug" style={{ color: 'var(--error)' }}>
                <AlertTriangle size={13} className="shrink-0" aria-hidden />
                <span>
                  {i18nT('components.webPreviewPanel.couldn_t_start_the_browser_view')}
                  {view.startError instanceof Error ? ` ${view.startError.message}` : ''}
                </span>
              </div>
            )}
          </div>
        ) : (
          // `unavailable`, an unusable URL on a `running` status, or a failed
          // status read. All three mean the same thing to the user — there is
          // nothing to frame — and all three must SAY so rather than leave a black
          // rectangle that reads as a hung browser.
          <div className="flex flex-col items-center justify-center h-full gap-3 px-6 text-center bg-bg">
            <Monitor size={22} className="text-muted" />
            <div className="text-[13px] font-medium text-text">{i18nT('components.webPreviewPanel.browser_view_unavailable')}</div>
            <div className="text-[11px] text-muted max-w-[320px] leading-snug">
              {/* The server's own words when it gave any (never translated — it
                  reports the real cause, e.g. that the CLI is not installed);
                  our generic copy only when it did not. */}
              {view.data?.reason
                || (view.error instanceof Error ? view.error.message : null)
                || i18nT('components.webPreviewPanel.the_browser_view_isn_t_available_on_this_gateway')}
            </div>
            <button
              type="button"
              onClick={view.refresh}
              className="inline-flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-md border border-border text-text hover:bg-bg-hover transition-colors cursor-pointer bg-transparent"
            >
              <RotateCw size={13} /> {i18nT('components.webPreviewPanel.reload')}
            </button>
          </div>
        )}
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
        {/* Annotate: pick elements on the page and type a note on each. Lives
            on the native header (which had no actions); the preview toolbar's
            4-control row is untouched. Labelled, not icon-only: the verb is new
            to users. While picking, the same button reads "Done" -- one control,
            two states, so there is always a visible way out besides Esc. */}
        {canAnnotate && (
          <button
            type="button"
            onClick={() => { void toggleAnnotate() }}
            disabled={annotateBusy}
            aria-pressed={picking}
            className={`inline-flex items-center gap-1.5 h-6 px-2 rounded-md text-[12px] font-medium border transition-colors cursor-pointer shrink-0 disabled:opacity-60 disabled:cursor-default ${
              picking
                ? 'text-accent bg-accent-subtle border-accent hover:bg-accent-subtle'
                : 'text-text bg-transparent border-border hover:bg-bg-hover hover:border-border-strong'
            }`}
            title={picking ? i18nT('components.webPreviewPanel.annotate_done_tooltip') : i18nT('components.webPreviewPanel.annotate_tooltip')}
            data-testid="browser-annotate"
          >
            <MousePointerClick size={13} aria-hidden />
            <span>{picking ? i18nT('components.webPreviewPanel.annotate_done') : i18nT('components.webPreviewPanel.annotate')}</span>
          </button>
        )}
      </div>
      {/* Annotate errors get their own row under the header: inline in the
          header they would wrap into a sliver beside the button. askAgent is
          safe here -- the notes live in the panel's mirror (persisted per slot),
          so the hand-off destroys nothing. */}
      {annotateError && (
        <div className="px-3 py-1.5 border-b border-border shrink-0" style={{ backgroundColor: 'var(--bg-elevated)' }}>
          <ErrorNotice message={annotateError} variant="inline" askAgent={annotateErrorAskAgent} onDismiss={() => setAnnotateError('')} />
        </div>
      )}
      {/* Address bar. The preview subtree below (which owns the other URL form)
          is hidden while the native surface is up, so without this the user
          could reach a page and then have no way to leave it. */}
      <form
        className="flex items-center gap-1 px-2 py-1.5 border-b border-border shrink-0"
        onSubmit={e => { e.preventDefault(); commitNative(draft) }}
      >
        <div className="flex-1 min-w-0 flex items-center gap-0.5 h-7 pl-1 pr-1 rounded-md bg-bg-elevated border border-border focus-within:border-accent transition-colors">
          <input
            ref={nativeInputRef}
            type="text"
            value={draft}
            onChange={e => setDraft(e.target.value)}
            placeholder={i18nT('components.webPreviewPanel.localhost_5173')}
            aria-label={i18nT('components.webPreviewPanel.preview_url')}
            spellCheck={false}
            autoCorrect="off"
            autoCapitalize="off"
            className="flex-1 min-w-0 h-full bg-transparent border-none text-[12px] text-text placeholder:text-muted px-1"
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
      {/* Annotation list: mirrors the notes typed on the page. Below the view so
          the address bar and the page stay together; the host above shrinks to
          make room, which the bounds report picks up. Shown while a session is
          alive (picking, or notes exist). */}
      {(picking || annotations.length > 0) && (
        <div className="shrink-0 border-t border-border" style={{ backgroundColor: 'var(--bg-elevated)' }} data-testid="browser-annotations">
          <div className="flex items-center gap-2 px-3 py-1.5">
            <span className="text-[12px] font-medium text-text">
              {i18nT('components.webPreviewPanel.annotations_count', { count: annotations.length })}
            </span>
            <div className="flex-1" />
            <button
              type="button"
              onClick={clearAnnotations}
              disabled={!annotations.length || annotateBusy}
              className={`text-[12px] px-2 h-6 rounded-md border transition-colors cursor-pointer bg-transparent disabled:opacity-40 disabled:cursor-default ${
                clearArmed ? 'text-danger border-danger hover:bg-danger/10' : 'text-text border-border hover:bg-bg-hover hover:border-border-strong'
              }`}
              aria-label={clearArmed
                ? i18nT('components.webPreviewPanel.annotate_clear_confirm', { count: annotations.length })
                : i18nT('components.webPreviewPanel.annotate_clear')}
            >
              {clearArmed
                ? i18nT('components.webPreviewPanel.annotate_clear_confirm', { count: annotations.length })
                : i18nT('components.webPreviewPanel.annotate_clear')}
            </button>
            <button
              type="button"
              onClick={() => { void sendAnnotations() }}
              disabled={!canSend || annotateBusy}
              // Disabled = muted fill, not accent-at-40%: on dark themes a faded
              // accent still reads as clickable, and this button is disabled
              // exactly while the first note is being typed.
              className="inline-flex items-center gap-1.5 text-[12px] px-2.5 h-6 rounded-md bg-accent text-white hover:opacity-90 transition-opacity cursor-pointer border-none disabled:bg-bg-hover disabled:text-muted disabled:cursor-default disabled:hover:opacity-100"
              title={i18nT('components.webPreviewPanel.annotate_send_tooltip')}
              data-testid="browser-annotations-send"
            >
              {annotateBusy ? <Loader2 size={12} className="animate-spin" aria-hidden /> : <Plus size={12} aria-hidden />}
              {i18nT('components.webPreviewPanel.annotate_send')}
            </button>
          </div>
          {addedAck && (
            <div className="px-3 pb-1.5 text-[11px] leading-snug text-accent" role="status" data-testid="browser-annotations-added">
              {i18nT('components.webPreviewPanel.annotate_added_ack')}
            </div>
          )}
          {annotations.some(a => a.detached) && (
            <div className="px-3 pb-1.5 text-[11px] text-muted leading-snug">
              {i18nT('components.webPreviewPanel.annotate_detached')}
            </div>
          )}
          {annotations.length === 0 ? (
            <div className="px-3 pb-2 text-[11px] text-muted leading-snug">
              {i18nT('components.webPreviewPanel.annotate_empty_hint')}
            </div>
          ) : (
            <ul className="m-0 p-0 list-none max-h-40 overflow-y-auto border-t border-border">
              {annotations.map(a => (
                <li
                  key={a.id}
                  // Wraps: the editor takes a full row of its own, so a 320px
                  // panel still shows badge + descriptor above a usable input.
                  className={`flex flex-wrap items-center gap-x-2 gap-y-1 px-3 py-1 border-b border-border last:border-b-0 text-[12px] ${a.detached ? 'opacity-70' : ''}`}
                  title={a.detached ? i18nT('components.webPreviewPanel.annotate_detached') : undefined}
                  data-testid={editing?.id === a.id ? 'browser-annotation-editing' : undefined}
                >
                  <span
                    className="shrink-0 min-w-[18px] h-[18px] px-1.5 rounded-full text-[11px] font-semibold text-white text-center leading-[18px]"
                    // Marker identity colour, matching the on-page badges; not
                    // the danger hue, which belongs to Remove and the armed Clear.
                    style={{ backgroundColor: 'var(--accent)' }}
                    aria-hidden
                  >
                    {a.n}
                  </span>
                  <span className={`shrink-0 font-mono text-[11px] text-muted truncate ${editing?.id === a.id ? 'max-w-[calc(100%-1.75rem)]' : 'max-w-[40%]'}`} title={`${a.ref} · ${a.selector}`}>
                    {describeAnnotationTarget(a, annotateRoleNames())}
                  </span>
                  {editing?.id === a.id ? (
                    // The note editor. Typed HERE, not in the page: the page is
                    // external content and must never see the user's prose.
                    // Enter saves, Esc cancels (a fresh pick with nothing typed
                    // is dropped), Save/Remove for pointer users.
                    <div className="flex items-center gap-2 basis-full min-w-0">
                      <input
                        ref={noteInputRef}
                        type="text"
                        value={editing.text}
                        onChange={e => setEditingText(e.target.value)}
                        onKeyDown={e => {
                          if (e.key === 'Enter') { e.preventDefault(); commitNote() }
                          else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancelNote() }
                        }}
                        placeholder={i18nT('components.webPreviewPanel.annotate_note_placeholder')}
                        aria-label={i18nT('components.webPreviewPanel.annotate_note_for_n', { n: a.n })}
                        className="flex-1 min-w-0 h-6 px-2 rounded-md bg-bg-elevated border border-border text-[12px] text-text placeholder:text-muted focus-ring"
                        data-testid="browser-annotation-note-input"
                      />
                      <button
                        type="button"
                        onClick={commitNote}
                        className="text-[12px] px-2 h-6 rounded-md bg-accent text-white border-none cursor-pointer hover:opacity-90 transition-opacity shrink-0"
                      >
                        {i18nT('components.webPreviewPanel.annotate_save')}
                      </button>
                      <button
                        type="button"
                        onClick={() => { void removeAnnotation(a.id) }}
                        className="flex items-center justify-center w-6 h-6 rounded text-muted hover:text-danger hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0"
                        title={i18nT('components.webPreviewPanel.annotate_remove')}
                        aria-label={i18nT('components.webPreviewPanel.annotate_remove_n', { n: a.n })}
                      >
                        <X size={12} />
                      </button>
                    </div>
                  ) : (
                    <>
                      <span className="flex-1 min-w-0 truncate text-text" title={a.note}>{a.note}</span>
                      <button
                        type="button"
                        onClick={() => editAnnotation(a.id)}
                        className="flex items-center justify-center w-6 h-6 rounded text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0"
                        title={i18nT('components.webPreviewPanel.annotate_edit_note')}
                        aria-label={i18nT('components.webPreviewPanel.annotate_edit_note_n', { n: a.n })}
                      >
                        <Pencil size={12} />
                      </button>
                      <button
                        type="button"
                        onClick={() => { void removeAnnotation(a.id) }}
                        className="flex items-center justify-center w-6 h-6 rounded text-muted hover:text-danger hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0"
                        title={i18nT('components.webPreviewPanel.annotate_remove')}
                        aria-label={i18nT('components.webPreviewPanel.annotate_remove_n', { n: a.n })}
                      >
                        <X size={12} />
                      </button>
                    </>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )

  return (
    <div className="relative flex flex-col h-full min-h-0 bg-bg">
      {/* Preview subtree stays MOUNTED even while the browser view overlays it,
          so the iframe document + unsaved form/SPA state survive the view being
          shown — visually hidden, never unmounted. */}
      <div className={`flex flex-col h-full min-h-0 ${showBrowserView || nativeOpen ? 'invisible pointer-events-none' : ''}`} aria-hidden={showBrowserView || nativeOpen || undefined}>
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
            className="flex-1 min-w-0 h-full bg-transparent border-none text-[12px] text-text placeholder:text-muted px-1"
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
        {/* Expand toggle — a frequently used action, stays in the row. */}
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
        {/* Overflow menu — collapses browser-view toggle AND device-size presets
            into a single trigger so the row stays at 4 sibling controls
            (back / forward / expand / overflow), matching the base-branch count.
            `max-two-buttons-per-row` grandfathers a 4-control row but forbids
            growth; collapsing is the prescribed remedy. The trigger icon reflects
            the active device size so the state is visible without opening. */}
        <DropdownMenu open={moreOpen} onOpenChange={setMoreOpen}>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              className={`${iconBtn} ${viewRunning || isDeviceSized ? 'text-accent' : ''}`}
              title={i18nT('components.webPreviewPanel.more_actions')}
              aria-label={i18nT('components.webPreviewPanel.more_actions')}
            >
              {isDeviceSized ? <Smartphone size={14} /> : <MoreHorizontal size={14} />}
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-[210px]">
            <DropdownMenuItem onSelect={toggleBrowserView}>
              <Monitor size={13} className="shrink-0 text-muted" />
              <span>{i18nT('components.webPreviewPanel.browser_view')}</span>
              {viewRunning && <Check size={13} className="ml-auto shrink-0 text-accent" />}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuSub>
              <DropdownMenuSubTrigger>
                {isDeviceSized ? <Smartphone size={13} className="shrink-0 text-muted" /> : <Monitor size={13} className="shrink-0 text-muted" />}
                <span>{i18nT('components.webPreviewPanel.preview_size')}</span>
              </DropdownMenuSubTrigger>
              <DropdownMenuSubContent className="min-w-[210px]">
                {DEVICE_PRESETS.map(d => (
                  <DropdownMenuItem
                    key={d.id}
                    role="menuitemradio"
                    aria-checked={d.id === deviceId}
                    onSelect={() => setDeviceId(d.id)}
                  >
                    <span className="text-muted shrink-0">{d.w ? <Smartphone size={14} /> : <Monitor size={14} />}</span>
                    <span className="flex-1">{deviceLabel(d)}</span>
                    {d.w && <span className="text-[10px] text-muted font-mono shrink-0">{d.w}×{d.h}</span>}
                    {d.id === deviceId && <Check size={13} className="ml-auto text-accent shrink-0" />}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuSubContent>
            </DropdownMenuSub>
          </DropdownMenuContent>
        </DropdownMenu>
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
              {i18nT('components.webPreviewPanel.page_mixed_content')}
            </div>
            <code className="text-[11px] font-mono px-2 py-1 rounded bg-bg-elevated text-text break-all max-w-[320px]">
              {url}
            </code>
            <a
              href={url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-md border border-border text-text hover:bg-bg-hover transition-colors no-underline"
            >
              <ExternalLink size={13} /> {i18nT('components.webPreviewPanel.open_in_browser')}
            </a>
          </div>
        ) : selfOrigin ? (
          // Points back at this gateway, which refuses to be framed. The probe
          // can't surface this (the server is up), so say so here rather than
          // leaving an unexplained blank frame.
          <div className="flex flex-col items-center justify-center h-full gap-3 px-6 text-center bg-bg">
            <Globe size={22} className="text-muted" />
            <div className="text-[13px] font-medium text-text">{i18nT('components.webPreviewPanel.can_t_preview_this_dashboard_here')}</div>
            <div className="text-[11px] text-muted max-w-[320px] leading-snug">
              {i18nT('components.webPreviewPanel.this_url_is_the_dashboard_s_own_server_which_ref')}
            </div>
            <code className="text-[11px] font-mono px-2 py-1 rounded bg-bg-elevated text-text break-all max-w-[320px]">
              {url}
            </code>
            <a
              href={url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-md border border-border text-text hover:bg-bg-hover transition-colors no-underline"
            >
              <ExternalLink size={13} /> {i18nT('components.webPreviewPanel.open_in_browser')}
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
      {nativeOpen ? nativeSurface : showBrowserView ? browserView : null}
    </div>
  )
}
