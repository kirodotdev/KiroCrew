import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Globe, RotateCw, ExternalLink, ArrowLeft, ArrowRight, Expand, Minimize, Smartphone, Monitor, Check, Crop, Play, Loader2, AlertTriangle, Info, MoreHorizontal, X } from 'lucide-react'

import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
  DropdownMenuSub, DropdownMenuSubTrigger, DropdownMenuSubContent, DropdownMenuSeparator,
} from './ui/dropdown-menu'
import ErrorNotice from './ErrorNotice'
import { safeSetItem } from '../utils/safeStorage'
import { Btn } from './ui'
import { SettingRef } from './settingRef/SettingRef'
import { isScreenSnipSupported } from '../hooks/useScreenSnip'
import { useIsMobile } from '../hooks/useIsMobile'
import { useBrowserView } from '../hooks/useBrowserView'
import { useNativeBrowser } from '../hooks/useNativeBrowser'
import {
  PENDING_KEY_PREFIX, PREVIEW_PENDING_EVENT, PREVIEW_URL_EVENT, URL_KEY_PREFIX,
  hasQueryOrFragment, httpEmbedRefusal, isDashboardOrigin, isInvalidUrlRefusal, isLoopbackHost,
  isolateFrameTarget, isolatePreviewHost, normalizeUrl,
} from './webPreviewPanelUrl'
import { useWebPreviewPanelAnnotate } from './useWebPreviewPanelAnnotate'
import { useFrameNavigationBlock } from './useFrameNavigationBlock'
import { useRelayCookieRefusal } from './useRelayCookieRefusal'
import WebPreviewPanelNativeSurface from './WebPreviewPanelNativeSurface'

import { i18nT } from '../i18n/t'

export {
  EMBEDDABLE_LOOPBACK_HOSTS, httpEmbedRefusal, isDashboardOrigin, isPotentiallyTrustworthyHost, isolatePreviewHost, normalizeUrl,
  setSessionPreviewPending, setSessionPreviewUrl,
} from './webPreviewPanelUrl'

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
 * On that second transport the address bar is also how a human opens a REAL
 * website: an external host cannot render in the iframe (sites refuse to be
 * framed, and the dashboard CSP refuses to frame them), so `commit` hands it to
 * the gateway (`POST /api/browser/open`), which opens it in the gateway host's
 * Playwright CLI browser and the panel shows that through the CLI view — whose
 * own URL bar, tab bar and input then carry the navigation from there.
 * The dev-server iframe is a real embedded browser view: the dev server's own
 * HMR live-reloads it as the user edits, and Reload covers static servers.
 *
 * Session-scoped: the chosen URL is remembered PER chat slot (`sessionKey`), so
 * each session keeps its own preview target. The dev-server preview is
 * frontend-only — no gateway round trip; the browser fetches the URL directly,
 * so only servers reachable from the user's browser (localhost dev servers) can
 * be previewed. The launcher above is the one gateway round trip, and only for a
 * non-loopback host.
 */

/** localStorage flag: the reader dismissed the padlock hint under the browser
 *  view once. Per browser, not per chat -- the hint explains a control that is
 *  the same in every chat, so one reading is enough. */
const PADLOCK_HINT_DISMISSED_KEY = 'mc-webpreview-padlock-hint-dismissed'
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

/** Common local dev-server ports offered as one-click starting points. */
const COMMON_PORTS = [3000, 5173, 8080, 4321, 8000]
/** iframe sandbox — permissive enough for real apps + HMR, but still a sandbox. */
const SANDBOX = 'allow-scripts allow-same-origin allow-forms allow-popups allow-modals allow-downloads'
/** Browser-view iframe sandbox for the RELAY PATH ONLY — deliberately WITHOUT
 * `allow-same-origin`. The relay serves the view SPA on the dashboard's OWN
 * origin, so granting the frame a real origin would let that content (or
 * anything squatting the relay's loopback target port) reach the parent DOM
 * and drive the dashboard's cookie-authed APIs. The opaque origin — reinforced
 * server-side by the relay's CSP `sandbox` stamp — keeps the view exactly as
 * isolated as its old loopback origin was. An opaque origin sends no cookies,
 * which is why the relay path embeds its own capability token.
 * `allow-popups` matches the direct path: without
 * `allow-popups-to-escape-sandbox` a popup INHERITS these flags — opaque
 * origin included — so parity costs no isolation (the view SPA has no popup
 * affordance today; this keeps one added later from no-op'ing only via relay).
 * The direct-URL fallback (older gateway, no relay `path`) keeps the plain
 * SANDBOX instead: it is already a foreign loopback origin, and only relayed
 * documents get the storage shim — sandboxing the direct frame opaque would
 * crash the view SPA's boot on its bare localStorage access. */
const VIEW_SANDBOX = 'allow-scripts allow-forms allow-popups allow-modals allow-downloads'

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

/** Probe interval and the per-probe deadline of `useLivenessProbe`. */
const PROBE_EVERY_MS = 5000
const PROBE_TIMEOUT_MS = 2500

/**
 * Whether a framed server has stopped answering. A cross-origin iframe cannot
 * tell us its server died (it keeps showing the last document, or the browser's
 * own error page), so while `enabled` the URL is polled with a no-cors GET; a
 * connection failure throws. Two consecutive failures ⇒ unreachable; a later
 * success auto-restores. (Two strikes tolerates a brief HMR/dev-server restart.)
 * Any change of `url` or `resetKey` starts a fresh, reachable-until-proven-
 * otherwise cycle — which is how a Reload clears the state.
 *
 * Used for BOTH framed servers: the dev-server preview and the Playwright CLI
 * view, which is served on the GATEWAY host's loopback and is unreachable from a
 * browser on another machine unless its port is pinned and forwarded.
 */
function useLivenessProbe(url: string, enabled: boolean, resetKey = 0): boolean {
  const [unreachable, setUnreachable] = useState(false)
  useEffect(() => {
    setUnreachable(false)
    if (!url || !enabled) return
    let fails = 0
    let cancelled = false
    const probe = async () => {
      const ctrl = new AbortController()
      const t = setTimeout(() => ctrl.abort(), PROBE_TIMEOUT_MS)
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
    const id = setInterval(() => { void probe() }, PROBE_EVERY_MS)
    return () => { cancelled = true; clearInterval(id) }
  }, [url, enabled, resetKey])
  return unreachable
}

/** The address bar's launcher flow on the non-native transport, while a URL is
 *  being opened in the gateway host's browser or after that failed. `error` is
 *  the gateway's text — the CLI's own words — rendered verbatim. */
interface LaunchState {
  url: string
  status: 'opening' | 'failed'
  error: string | null
  /** The URL's shape is one the launcher refuses: credentials, a `?` query,
   * or a `#` fragment would enter host-visible CLI argv. `local` means the
   * panel caught query/fragment data before any request — a plain validation
   * hint. `gateway` means the request went out and the gateway answered
   * `invalid_url` — a real failed request whose own bounded reason is shown.
   * Neither offers retry: the same address would be refused again. */
  refused?: 'local' | 'gateway'
}

export default function WebPreviewPanel({ sessionKey, active = true }: { sessionKey?: string | null; active?: boolean }) {
  const storageKey = sessionKey ? `${URL_KEY_PREFIX}${sessionKey}` : null
  const pendingKey = sessionKey ? `${PENDING_KEY_PREFIX}${sessionKey}` : null
  const [nav, setNav] = useState<NavState>(EMPTY_NAV)
  const [draft, setDraft] = useState('')
  // A chat-fed URL awaiting an explicit "Load preview" click (null = none).
  const [pending, setPending] = useState<string | null>(null)
  // The address bar's launcher flow (non-native transport, external host): the
  // URL is being opened in the gateway host's browser, or that failed and the
  // gateway's own words explain why. Null = no launch in progress or failed.
  const [launch, setLaunch] = useState<LaunchState | null>(null)
  // The one guard against a stale launch answer: only the LATEST launch may
  // paint. Every launchInBrowser call takes the next number, both of its
  // callbacks paint only while that number is still current, and the
  // slot-change effect below bumps it. That drops a late failure of launch A
  // after launch B succeeded on the same slot (mistype, retype fast) and an
  // answer for a slot the user has left, with one mechanism.
  const launchSeqRef = useRef(0)
  // The CLI session THIS slot's launches land in (`panel-<owner6>-<slot8>`), from the
  // gateway's answer. Shown in the view's header: the framed dashboard lists
  // every browser session on the host by name, and the name is the only thing
  // that tells this chat's session apart from the others.
  const [launchedSession, setLaunchedSession] = useState<string | null>(null)
  // Whether the framed dashboard attached its viewport to that session, from the
  // same answer. False is the one case where the reader is looking at the
  // frame's session grid with no page, so the panel names the session to pick;
  // null = no launch answered yet.
  const [launchedAttached, setLaunchedAttached] = useState<boolean | null>(null)
  // The padlock hint under the view header, dismissed once per browser.
  const [padlockHintDismissed, setPadlockHintDismissed] = useState<boolean>(() => {
    try { return localStorage.getItem(PADLOCK_HINT_DISMISSED_KEY) === '1' } catch { return false }
  })
  const dismissPadlockHint = useCallback(() => {
    setPadlockHintDismissed(true)
    safeSetItem(PADLOCK_HINT_DISMISSED_KEY, '1')
  }, [])
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
  // Validate the server-reported target before it becomes an iframe `src`.
  //
  // Preferred: the gateway's same-origin relay `path`
  // (`/browser-view/<token>/`). A relative path rides whatever origin delivers
  // the dashboard itself — an SSH forward, a tunnel — so the view is reachable
  // remotely with no pinned port and no second forward. The embedded
  // capability token is the relay's auth (the frame below is an opaque-origin
  // sandbox that sends no cookies). The shape check (single leading slash)
  // keeps a malformed or hostile value from becoming a protocol-relative
  // (`//host`) navigation.
  //
  // Fallback: the absolute `url` from an older gateway that predates the relay.
  // The contract promises `http://127.0.0.1:<port>/`, and `normalizeUrl`
  // rejects anything that is not http(s) — so a malformed or hostile value
  // degrades to the explanatory state instead of being navigated.
  //
  // The fallback is deliberately NOT run through `isolatePreviewHost`: that
  // swaps a loopback host matching the dashboard's onto the other alias, and
  // the CLI binds ONE interface (`--host 127.0.0.1`). Swapping `127.0.0.1` to
  // `localhost` can resolve to `::1`, where nothing is listening — the exact
  // IPv6 trap the explicit bind exists to avoid. The view's URL is used
  // verbatim.
  const viewUrl = useMemo(
    () => {
      const relayPath = view.data?.path
      if (typeof relayPath === 'string' && relayPath.startsWith('/') && !relayPath.startsWith('//')) {
        return relayPath
      }
      return view.data?.url ? normalizeUrl(view.data.url) : null
    },
    [view.data?.path, view.data?.url],
  )
  // Which sandbox the view frame needs tracks HOW the URL was resolved. A relay
  // path is same-origin (relative, guaranteed above to start with a single '/'),
  // so it must be framed WITHOUT `allow-same-origin`; the direct loopback URL
  // (older gateway, no `path`) is a foreign origin where `allow-same-origin`
  // grants only that origin — and it MUST keep real storage access, because the
  // relay's localStorage shim is injected only into relayed documents, so an
  // opaque-origin direct frame crashes the view SPA's boot.
  const viewIsRelay = !!viewUrl && viewUrl.startsWith('/')
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

  const annotate = useWebPreviewPanelAnnotate(sessionKey, nativeOpen)

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
    // Same for an in-flight or failed launch: it names a URL typed for another
    // slot. Bumping the sequence retires any launch still in flight for the old
    // slot, so its late answer paints nothing here.
    launchSeqRef.current += 1
    setLaunch(null)
    setLaunchedSession(null)
    setLaunchedAttached(null)
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

  /**
   * Open `target` in the gateway host's browser and show it through the CLI view.
   *
   * The non-native transport's only way to render an external site: the
   * dashboard CSP refuses to frame it, so the preview iframe and its liveness
   * probe are left exactly as they were and the URL goes to the gateway instead,
   * which starts the view if needed and drives the CLI. On success the view
   * takes the panel (its status came back with the answer, so it frames at
   * once). On failure the gateway's text — the CLI's own words — is shown in the
   * panel, never a blank frame and never the dev-server "stopped responding" copy.
   */
  const openInView = view.open
  // A launch resolves seconds later. By then the user may have typed another
  // address on this slot, or switched chats; either way only the newest launch
  // may paint, so each callback checks that its sequence number is still the
  // current one (see launchSeqRef) and drops a stale answer.
  const launchInBrowser = useCallback((target: string) => {
    if (!sessionKey) return
    const seq = ++launchSeqRef.current
    const isLatest = () => launchSeqRef.current === seq
    setDraft(target)
    // The gateway refuses a `?` query or `#` fragment (the URL becomes CLI argv,
    // readable by same-host accounts, and those parts are where a token
    // travels). Refuse the same shape here, before the round trip, and say
    // where such a link goes instead of showing the refusal text.
    if (hasQueryOrFragment(target)) {
      setLaunch({ url: target, status: 'failed', error: null, refused: 'local' })
      setViewOverride(false)
      return
    }
    setLaunch({ url: target, status: 'opening', error: null })
    openInView(target, sessionKey).then(
      (data) => {
        if (!isLatest()) return
        if (data.ok) {
          setLaunch(null)
          setLaunchedSession(data.session || null)
          // Only an explicit false means "not attached": an older gateway that
          // does not answer the field is treated as attached, so no line appears.
          setLaunchedAttached(data.attached !== false)
          setViewOverride(true)
        } else {
          // The explanatory card lives in the preview body, so make sure the
          // view overlay is not covering it.
          setLaunch({ url: target, status: 'failed', error: data.error || null })
          setViewOverride(false)
        }
      },
      (err: unknown) => {
        if (!isLatest()) return
        // A caller that skipped the local query/fragment check may still reach
        // the gateway with credentials or another refused shape. Preserve the
        // gateway's bounded reason: only it knows which invariant rejected the
        // request, and substituting query/hash copy misdiagnoses userinfo.
        if (isInvalidUrlRefusal(err)) {
          setLaunch({
            url: target,
            status: 'failed',
            error: err instanceof Error ? err.message : String(err),
            refused: 'gateway',
          })
        } else {
          setLaunch({ url: target, status: 'failed', error: err instanceof Error ? err.message : String(err) })
        }
        setViewOverride(false)
      },
    )
  }, [sessionKey, openInView])

  const commit = useCallback((raw: string) => {
    const norm = normalizeUrl(raw)
    if (!norm) return
    let host = ''
    try { host = new URL(norm).hostname } catch { /* normalizeUrl validated it */ }
    const external = !!host && !isLoopbackHost(host)
    // A NON-loopback target on the NON-native transport (remote gateway / plain
    // browser) cannot render in the iframe: external sites refuse to be framed
    // and the dashboard CSP refuses to frame them anyway. Hand it to the gateway
    // host's browser instead — and do not touch the iframe or its probe, which
    // would otherwise report the site as a dead dev server.
    if (external && !native.available) {
      launchInBrowser(norm)
      return
    }
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
    if (external && native.available) native.open(isolated)
  }, [persist, pendingKey, native, launchInBrowser])

  // Bumped by the unreachable-view card's retry, restarting that probe.
  const [viewProbeKey, setViewProbeKey] = useState(0)

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

  // An http:// frame inside an https:// dashboard (remote/tunnel) may be blocked
  // — detect it so we can explain + offer the open-in-new-tab fallback instead
  // of rendering a silently-blank frame.
  //
  // The scheme alone does not decide it: a loopback target is a potentially
  // trustworthy origin the engine does NOT block, so a scheme-only test refuses
  // a dev server (or the CLI browser view) that would have loaded, and blames the
  // browser for a refusal that is ours. See `EMBEDDABLE_LOOPBACK_HOSTS` for which
  // hosts qualify, and `httpEmbedRefusal` for WHICH blocker stops the rest — the
  // card names it, so a CSP refusal is never explained as mixed content.
  const embedRefusal = useMemo(
    () => (typeof window !== 'undefined' ? httpEmbedRefusal(url, window.location.protocol) : null),
    [url],
  )

  // A URL pointing back at this gateway is refused by its own frame-ancestors
  // policy, which the liveness probe below cannot see (the server is up, so the
  // probe passes) — detect it so we explain instead of framing a blank page.
  const selfOrigin = useMemo(() => !!url && isDashboardOrigin(url), [url])

  // Liveness of the framed dev server: probed only while a URL is actually
  // framed (loaded, embeddable, tab active); two strikes unmount the stale iframe
  // in favour of the stopped state, a later success restores it.
  const unreachable = useLivenessProbe(
    url,
    !!url && embedRefusal === null && !selfOrigin && !pending && active,
    reloadKey,
  )

  // Liveness of the framed CLI view, from THIS browser's point of view — for
  // the DIRECT-URL fallback only. That URL is served on the gateway host's
  // loopback, so from a browser on another machine (a laptop reaching a remote
  // gateway over an SSH tunnel) the frame is dead unless
  // `dashboard.browser_view_port` is pinned and that port forwarded too. The
  // gateway's status says "running" either way — it is reachable from where IT
  // stands — so the panel has to find out for itself and explain the pin +
  // forward instead of showing the browser's own error page in the frame.
  // Same probe as the dev server's, and CSP admits it (loopback connect-src).
  // The relay path NEVER gets this probe: it is same-origin, so its health is
  // the dashboard's own — and a no-cors probe against it would tell us nothing
  // (the response is opaque) while costing the relay's per-request ownership
  // proofs every 5 seconds for every open panel.
  const viewUnreachable = useLivenessProbe(
    viewUrl || '',
    !!viewUrl && !viewIsRelay && viewRunning && showBrowserView && active,
    viewProbeKey,
  )
  // The relay path's own failure: a cookie-checking front door refuses the
  // opaque-origin frame's asset fetches, so the view never boots (#23180).
  const viewCookieRefused = useRelayCookieRefusal(
    viewUrl || '',
    viewIsRelay && viewRunning && showBrowserView && active,
    viewProbeKey,
  )

  /**
   * True exactly when the body renders the preview iframe — the one state in
   * which a target that refuses to be framed leaves a blank rectangle and the
   * panel says nothing about it.
   *
   * The conditions mirror the body's branch order (launcher, pending offer, no
   * url, the two embed refusals, unreachable server, then the frame), and they
   * are spelled here rather than re-derived at the use site so the hint below
   * the URL bar cannot disagree with whether a frame is actually on screen. Each
   * of those earlier branches already states what it is showing, and a line
   * warning about a blank frame would contradict the card under it.
   *
   * Note the asymmetry with the liveness probe above, which is deliberate rather
   * than an oversight: the probe ignores `launch` because a launcher flow leaves
   * the previous target framed and its server worth polling, whereas the body
   * replaces the frame with the launcher card, so the hint must not show.
   */
  const framing = !!url && !launch && !pending && !embedRefusal && !selfOrigin && !unreachable

  // A navigation inside the frame that the dashboard's own `frame-src` refused
  // (an OAuth redirect to a sign-in page, most often). The frame is left blank
  // and the app's button spins, so a notice above it names the refused origin
  // and the way out. Cleared by a new URL, a reload or its dismiss.
  const frameRef = useRef<HTMLIFrameElement>(null)
  // `framing` is in the reset key because a frame that left the screen (an
  // unreachable server, the launcher card) remounts on a fresh load; `active`
  // is not, because a tab switch keeps the same blank frame mounted.
  const frameOrigin = useMemo(() => {
    try { return url ? new URL(url).origin : '' } catch { return '' }
  }, [url])
  const [frameBlocked, dismissFrameBlocked] = useFrameNavigationBlock(
    framing && active, frameRef, `${reloadKey}:${url}:${framing}`, frameOrigin,
  )

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
        {/* The dot is what THIS browser sees, not gateway-side truth: green while
            the view answers from here, danger-coloured while the probe below says
            it does not — a green dot beside an "unreachable" card contradicted it. */}
        {viewRunning && (
          <span
            className={`inline-block w-1.5 h-1.5 rounded-full ${viewUnreachable || viewCookieRefused ? '' : 'animate-pulse'}`}
            style={{ backgroundColor: viewUnreachable || viewCookieRefused ? 'var(--danger)' : 'var(--ok)' }}
            data-testid="web-preview-view-dot"
            aria-hidden
          />
        )}
        {/* What THIS chat launched, by the session name the framed dashboard
            lists it under — the sidebar lists the gateway's own browser sessions,
            and the name is what tells this one apart.

            A LAUNCH FACT, deliberately not an ownership claim (#5940). The reveal
            that points the dashboard's single viewport at a session is one
            machine-wide switch, so anything else that launches — an agent or the
            CLI for another chat's session, a second dashboard tab — moves the
            frame without this panel hearing about it. A header reading "this
            chat's browser" then described a page the reader was not looking at.
            "Opened from this chat" stays true under every reveal, and the panel
            has no way to say more: `/api/browser/view` answers status, url, port
            and reason, and the frame is cross-origin.

            The label is visible text, not a tooltip, and deliberately does not
            say "session": that word is already what the app calls its chats and
            what the frame's own list is headed, so the name chip carries the
            identity. Rendered only once a launch has named it. The group is the
            row's only flexible item: at narrow widths (320px) it gives way first
            — label, then name, each truncating — so the controls to its right,
            including the only way back to the preview bar, always fit. */}
        {launchedSession && (
          <span className="flex min-w-0 flex-1 items-center gap-1 text-[11px] text-muted overflow-hidden" data-testid="web-preview-session-label">
            <span className="min-w-0 truncate">{i18nT('components.webPreviewPanel.opened_from_this_chat')}</span>
            <code className="font-mono px-1.5 py-0.5 rounded bg-bg-elevated text-text truncate shrink min-w-[6ch] max-w-[180px]" data-testid="web-preview-session-name">
              {launchedSession}
            </code>
          </span>
        )}
        <div className={launchedSession ? 'shrink' : 'flex-1'} />
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
      {/* One sentence naming the two ways to the next page, because both are
          behind controls whose meaning a first-time reader cannot see: the
          framed dashboard's own address bar unlocks with its padlock (which
          only turns on typing and clicking inside that browser), and the
          panel's bar comes back with the toggle above. Dismissed once per
          browser: the controls are the same in every chat, so a second reading
          buys nothing and daily chrome should not carry how-to prose. Chrome
          above the frame, never on it. Only where this view IS the browser for
          an external site. */}
      {!native.available
        && launchedSession
        && launchedAttached !== false
        && viewRunning
        && !viewUnreachable
        && !viewCookieRefused
        && !padlockHintDismissed && (
        <div
          className="flex items-center gap-2 px-3 py-1 border-b border-border shrink-0 text-[11px] text-muted leading-snug"
          data-testid="web-preview-padlock-hint"
        >
          <span className="min-w-0 flex-1">
            {i18nT('components.webPreviewPanel.to_open_another_site_unlock_the_view_s_address_bar')}
          </span>
          <Btn aria-label={i18nT('app.dismiss')} onClick={dismissPadlockHint} className="shrink-0">
            <X className="lucide-inline" />
          </Btn>
        </div>
      )}
      {/* The framed dashboard did not attach to the launched session (no shared
          socket root, an unsupported bundle layout, Windows), so the reader is
          looking at its session grid with no page: name the session to pick.
          Said only in that case -- an attached view needs no instruction. */}
      {!native.available && launchedSession && launchedAttached === false && (
        <div
          className="px-3 py-1 border-b border-border shrink-0 text-[11px] text-muted leading-snug"
          data-testid="web-preview-pick-session"
        >
          {i18nT('components.webPreviewPanel.your_page_is_in_pick_it_in_the_sidebar', { session: launchedSession })}
        </div>
      )}
      <div className="relative flex-1 min-h-0 bg-bg-elevated">
        {view.pending || view.starting ? (
          <div className="flex flex-col items-center justify-center h-full gap-2 px-6 text-center">
            <Loader2 size={18} className="text-muted animate-spin" aria-hidden />
            <span className="text-[11px] text-muted">{i18nT('components.webPreviewPanel.loading_the_browser_view')}</span>
          </div>
        ) : viewRunning && viewUnreachable ? (
          // Running on the gateway, dead from HERE. The view binds the gateway
          // host's loopback, so a browser on another machine reaches it only
          // through a forwarded port — explain the pin + forward instead of
          // leaving the browser's own connection-refused page in the frame.
          // Hand-off on: there is no draft to lose (the address bar's URL is
          // state, not an unsaved form), and the side panel stays open across
          // the chat navigation the hand-off performs.
          <div className="flex flex-col items-center justify-center h-full gap-3 px-6 text-center bg-bg">
            <Monitor size={22} className="text-muted" />
            <ErrorNotice
              title={i18nT('components.webPreviewPanel.browser_view_can_t_be_reached_from_this_browser')}
              message={i18nT('components.webPreviewPanel.the_view_runs_on_the_gateway_host_pin_and_forward')}
              className="max-w-[420px] text-left"
              testId="web-preview-view-unreachable"
              askAgent
            />
            <code className="text-[11px] font-mono px-2 py-1 rounded bg-bg-elevated text-text break-all max-w-[360px]">
              {viewUrl}
            </code>
            {/* The pin lives in config.json; the chip links to the setting where
                the UI edits it, or shows the CLI command where it does not. */}
            <SettingRef configKey="dashboard.browser_view_port" />
            <button
              type="button"
              onClick={() => setViewProbeKey(k => k + 1)}
              className="inline-flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-md border border-border text-text hover:bg-bg-hover transition-colors cursor-pointer bg-transparent"
            >
              <RotateCw size={13} /> {i18nT('components.webPreviewPanel.try_again')}
            </button>
          </div>
        ) : viewRunning && viewCookieRefused ? (
          // Something in front of the dashboard wants a cookie the sandboxed
          // frame cannot send, so the frame would stay blank: say so, and name
          // the way that works (a direct forward with no such front door).
          <div className="flex flex-col items-center justify-center h-full gap-3 px-6 text-center bg-bg">
            <Monitor size={22} className="text-muted" />
            <ErrorNotice
              title={i18nT('components.webPreviewPanel.browser_view_can_t_load_its_files_here')}
              message={i18nT('components.webPreviewPanel.a_sign_in_proxy_refuses_the_view_s_files')}
              className="max-w-[420px] text-left"
              testId="web-preview-view-cookie-refused"
              askAgent
            />
            <button
              type="button"
              onClick={() => setViewProbeKey(k => k + 1)}
              className="inline-flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-md border border-border text-text hover:bg-bg-hover transition-colors cursor-pointer bg-transparent"
            >
              <RotateCw size={13} /> {i18nT('components.webPreviewPanel.try_again')}
            </button>
          </div>
        ) : viewRunning ? (
          // The CLI dashboard, framed. It brings its own session grid, tab bar,
          // navigation and remote input, so the panel adds no controls of its own.
          <iframe
            src={viewUrl as string}
            title={i18nT('components.webPreviewPanel.live_browser_session')}
            className="absolute inset-0 w-full h-full border-0 bg-bg-elevated"
            sandbox={viewIsRelay ? VIEW_SANDBOX : SANDBOX}
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

  const nativeSurface = <WebPreviewPanelNativeSurface native={native} annotate={annotate} draft={draft} setDraft={setDraft} commitNative={commitNative} nativeInputRef={nativeInputRef} />

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

      {/* Why a page can be blank here, said up front instead of detected after
          the fact. No client-side signal separates a framed page from a refused
          one: a cross-origin frame fires `load` for a document blocked by
          X-Frame-Options or frame-ancestors just as it does for a healthy one,
          and sooner (milliseconds, against seconds for a cold dev server), never
          fires `error`, exposes no readable contentDocument even when healthy,
          and reports an opaque zero-status resource timing entry. The only tell
          is a console CSP violation, which a page cannot read and which
          X-Frame-Options does not emit at all. So a timeout heuristic would fire
          on slow servers and stay silent on refusals -- backwards -- and
          `useSilentLoadWatch`, whose verdict is "load never arrived", cannot see
          this case either.
          One quiet line above the frame, never over it (the placement the
          browser view's padlock hint uses), carrying the open-in-browser link
          that is the actual way out. Only while a frame is really on screen, so
          it never contradicts one of the explanatory cards below.
          The copy describes a CLASS of pages ("Some pages ...") and makes no
          claim about the frame on screen, because the line is unconditional: any
          wording that points at "this preview" asks a reader whose page rendered
          fine to evaluate a problem they do not have. A fact about the class is
          true above a healthy frame and is the explanation the reader wants
          above a blank one. It says "embedding", not "framed", to match the
          vocabulary the sibling cards in this same panel already use.
          Whether a line is a live warning about the reader's page or a note
          that is always there is a fact about its PERSISTENCE, which no single
          view of the panel shows, so two things say it. The glyph is Info and
          deliberately NOT the AlertTriangle the explanatory cards use, since
          those are stateful and this row is standing, and their warning mark
          would say the opposite. The copy also opens with the word "Note:",
          because a glyph carries a convention only to a reader who
          already knows it, while an unlabelled line of muted text above a frame
          reads as a warning that has not cleared. */}
      {/* A navigation inside the frame the dashboard's own `frame-src` refused.
          Said ABOVE the frame and never instead of it: the report cannot always
          be tied to this frame (see useFrameNavigationBlock), so a wrong guess
          must cost one dismissable line, never the previewed app's state.
          Sign-in providers refuse framing by their own policy too, so the way
          out is the app itself in a real tab, where sign-in runs top-level. */}
      {framing && frameBlocked !== null && (
        <div className="px-3 py-2 border-b border-border shrink-0" data-testid="web-preview-frame-blocked">
          {/* No hand-off: the address bar above this notice holds an unsaved
              draft URL, and the agent cannot lift a sign-in provider's own
              refusal to be framed anyway. */}
          <ErrorNotice
            title={i18nT('components.webPreviewPanel.frame_navigation_blocked_title')}
            message={i18nT('components.webPreviewPanel.frame_navigation_blocked_body')}
            messagePlacement="below"
            onDismiss={dismissFrameBlocked}
            testId="web-preview-frame-blocked-notice"
            footer={(
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 pt-0.5 text-[12px]">
                {frameBlocked && (
                  <span className="min-w-0 break-all" data-testid="web-preview-frame-blocked-origin">
                    {i18nT('components.webPreviewPanel.frame_navigation_blocked_origin', { origin: frameBlocked })}
                  </span>
                )}
                <a
                  href={url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 shrink-0 font-medium text-danger-text underline"
                >
                  <ExternalLink size={12} aria-hidden /> {i18nT('components.webPreviewPanel.open_in_browser')}
                </a>
              </div>
            )}
          />
        </div>
      )}
      {framing && frameBlocked === null && (
        <div
          className="flex items-center gap-2 px-3 py-1 border-b border-border shrink-0 text-[11px] text-muted leading-snug"
          data-testid="web-preview-frame-hint"
        >
          <Info size={11} className="shrink-0" aria-hidden />
          <span className="min-w-0 flex-1">
            {i18nT('components.webPreviewPanel.some_pages_refuse_embedding_and_appear_blank')}
          </span>
          <a
            href={url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 shrink-0 text-muted hover:text-text transition-colors no-underline"
          >
            <ExternalLink size={11} aria-hidden />
            <span>{i18nT('components.webPreviewPanel.open_in_browser')}</span>
          </a>
        </div>
      )}

      {/* Body */}
      <div className="relative flex-1 min-h-0 bg-white">
        {launch ? (
          // The address bar's launcher (non-native transport, external host):
          // the URL is being opened in the gateway host's browser, or that
          // failed. The gateway's text is the CLI's own words and is shown
          // verbatim — it names the real cause (a Chromium sandbox refusal, a
          // missing browser build) and, for the sandbox case, the operator's
          // remedy. Never a blank frame, never the dev-server copy.
          <div className="flex flex-col items-center justify-center h-full gap-3 px-6 text-center bg-bg">
            {launch.status === 'opening' ? (
              <>
                <Loader2 size={18} className="text-muted animate-spin" aria-hidden />
                <div className="text-[13px] font-medium text-text">{i18nT('components.webPreviewPanel.opening_in_the_browser')}</div>
                <code className="text-[11px] font-mono px-2 py-1 rounded bg-bg-elevated text-text break-all max-w-[320px]">
                  {launch.url}
                </code>
              </>
            ) : launch.refused === 'local' ? (
              <>
                <Globe size={22} className="text-muted" />
                <code className="text-[11px] font-mono px-2 py-1 rounded bg-bg-elevated text-text break-all max-w-[320px]">
                  {launch.url}
                </code>
                {/* Nothing failed here: the panel's own check stopped a URL shape
                    the launcher refuses before any request went out. That is a
                    validation hint, not an error, so it is plain text — no
                    ErrorNotice, no alert role, no danger colour — that says where
                    such a link goes. No retry: the same address would be refused
                    again. */}
                <div
                  className="flex items-start gap-2 max-w-[420px] text-left text-[12px] text-text rounded-md border border-border bg-bg-elevated px-3 py-2"
                  data-testid="web-preview-launch-refused"
                >
                  <span className="min-w-0 flex-1 leading-snug">
                    {i18nT('components.webPreviewPanel.links_with_a_query_or_hash_part_go_in_the_frame_s_own_address_bar')}
                  </span>
                  <Btn aria-label={i18nT('app.dismiss')} onClick={() => setLaunch(null)} className="shrink-0">
                    <X className="lucide-inline" />
                  </Btn>
                </div>
              </>
            ) : (
              <>
                <Globe size={22} className="text-muted" />
                <code className="text-[11px] font-mono px-2 py-1 rounded bg-bg-elevated text-text break-all max-w-[320px]">
                  {launch.url}
                </code>
                {/* The gateway's own words when it gave any (never translated —
                    they report the real cause, whether a URL-credential refusal,
                    a Chromium sandbox refusal and its remedy, or another launch
                    failure); our generic copy only when it did not. No hand-off:
                    the address bar beside this notice holds an unsaved draft URL. */}
                <ErrorNotice
                  title={i18nT('components.webPreviewPanel.couldn_t_open_this_page_in_the_browser')}
                  message={launch.error || i18nT('components.webPreviewPanel.the_gateway_could_not_open_a_browser_for_this_page')}
                  // The title is the plain-language cause. Gateway validation
                  // reasons keep body styling; verbatim CLI diagnostics are
                  // visibly demoted (muted, smaller monospace) as evidence.
                  messageClassName={
                    launch.refused === 'gateway'
                      ? 'block text-[12px] text-text pt-1 text-left'
                      : 'block font-mono text-[10.5px] text-muted pt-1 max-h-[30vh] overflow-auto text-left'
                  }
                  className="max-w-[420px] text-left"
                  onDismiss={() => setLaunch(null)}
                  testId="web-preview-launch-error"
                />
                {/* One action in the row; the notice's own dismiss is the way out.
                    Same outlined style as the unreachable card's retry: one look
                    for one job. No retry for a refused shape: the same address
                    would be refused again. */}
                {launch.refused !== 'gateway' && (
                  <button
                    type="button"
                    onClick={() => launchInBrowser(launch.url)}
                    className="inline-flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-md border border-border text-text hover:bg-bg-hover transition-colors cursor-pointer bg-transparent"
                  >
                    <RotateCw size={13} /> {i18nT('components.webPreviewPanel.try_again')}
                  </button>
                )}
              </>
            )}
          </div>
        ) : pending ? (
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
        ) : embedRefusal ? (
          // The explanation follows the blocker, not the scheme: `'browser'` is
          // the engine's mixed-content rule (a public or `0.0.0.0` host),
          // `'policy'` is the dashboard's own embed allowlist (a `*.localhost` or
          // other trustworthy host outside it). Both keep the
          // open-in-browser link — it is what makes the state recoverable.
          <div className="flex flex-col items-center justify-center h-full gap-3 px-6 text-center bg-bg">
            <Globe size={22} className="text-muted" />
            <div className="text-[13px] font-medium text-text">{i18nT('components.webPreviewPanel.can_t_embed_an_http_page_here')}</div>
            <div
              className="text-[11px] text-muted max-w-[320px] leading-snug"
              data-testid={`web-preview-embed-refusal-${embedRefusal}`}
            >
              {embedRefusal === 'policy' ? (
                i18nT('components.webPreviewPanel.dashboard_policy_blocks_embedding_http_page')
              ) : (
                <>
                  {i18nT('components.webPreviewPanel.this_dashboard_is_served_over_https_so_the_brows')}
                  <span className="font-mono"> {i18nT('components.webPreviewPanel.http')} </span>
                  {i18nT('components.webPreviewPanel.page_mixed_content')}
                </>
              )}
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
              onClick={reload}
              className="inline-flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-md border border-border text-text hover:bg-bg-hover transition-colors cursor-pointer bg-transparent"
            >
              <RotateCw size={13} /> {i18nT('components.webPreviewPanel.reload')}
            </button>
          </div>
        ) : isDeviceSized ? (
          // Fixed device size — center the frame in a scrollable, muted backdrop.
          <div className="absolute inset-0 overflow-auto bg-bg-elevated flex items-start justify-center p-4">
            <iframe
              ref={frameRef}
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
            ref={frameRef}
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
