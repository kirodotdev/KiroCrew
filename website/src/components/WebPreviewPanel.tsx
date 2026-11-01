import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Globe, RotateCw, ExternalLink, ArrowLeft, ArrowRight, Expand, Minimize, Smartphone, Monitor, Check, Crop, Info, Sparkles, ShieldCheck } from 'lucide-react'

import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from './ui/dropdown-menu'
import ErrorNotice from './ErrorNotice'
import { safeSetItem } from '../utils/safeStorage'
import { Btn } from './ui'
import { SettingRef } from './settingRef/SettingRef'
import { api } from '../api/client'
import { isScreenSnipSupported } from '../hooks/useScreenSnip'
import { useIsMobile } from '../hooks/useIsMobile'
import { useNativeBrowser } from '../hooks/useNativeBrowser'
import { normalizeLocalOrigin, previewAllowsUrl } from '../utils/localPreviewOrigins'
import { sendErrorToChat } from '../utils/errorReport'
import {
  PENDING_KEY_PREFIX, PREVIEW_PENDING_EVENT, PREVIEW_URL_EVENT, URL_KEY_PREFIX,
  isDashboardOrigin,
  isolateFrameTarget, normalizeUrl,
} from './webPreviewPanelUrl'
import { useWebPreviewPanelAnnotate } from './useWebPreviewPanelAnnotate'
import { useFrameNavigationBlock } from './useFrameNavigationBlock'
import WebPreviewPanelNativeSurface from './WebPreviewPanelNativeSurface'

import { i18nT } from '../i18n/t'

export {
  EMBEDDABLE_LOOPBACK_HOSTS, httpEmbedRefusal, isDashboardOrigin, isPotentiallyTrustworthyHost, isolatePreviewHost, normalizeUrl,
  setSessionPreviewPending, setSessionPreviewUrl,
} from './webPreviewPanelUrl'

/**
 * WebPreviewPanel — the side panel's **Preview** tab: a docked, session-scoped
 * live preview of a dev server the user runs locally, in a sandboxed iframe.
 *
 * It opens ONLY the origins listed in `dashboard.browser_local_origins`
 * (Settings -> Browser -> Local preview). A loopback address outside the list is
 * answered with a one-click "Allow" that adds it; an outside URL is answered with
 * "Open this in your Chrome, or ask Kiro" — the agent's browser is the live card
 * in the chat (RFC browser-live-harness), and the agent never drives this tab.
 *
 * In the desktop app the page opens in the NATIVE Chromium view instead
 * (`useNativeBrowser`, composited over the panel by the main process), which is
 * what makes page annotation possible: a front-end dev picks elements on the
 * real page and hands the notes to the chat (`useWebPreviewPanelAnnotate`). The
 * native surface accepts the same allowlist and nothing else; a plain browser
 * (no preload bridge) keeps the iframe. Nothing drives either surface but the
 * person at the keyboard.
 *
 * The iframe is a real embedded browser view: the dev server's own HMR
 * live-reloads it as the user edits, and Reload covers static servers. There is
 * no mixed-content card any more: every address the list can admit is a loopback
 * spelling the engine treats as potentially trustworthy and the dashboard's own
 * frame-src always carries (`[::1]` is canonicalized to 127.0.0.1 before it is
 * framed), so the refusal `httpEmbedRefusal` describes cannot arise here.
 *
 * Session-scoped: the chosen URL is remembered PER chat slot (`sessionKey`), so
 * each session keeps its own preview target. Frontend-only — no gateway round
 * trip; the browser fetches the URL directly, so only servers reachable from the
 * user's browser can be previewed.
 */

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

/** The slice of `GET /api/config/kirocrew` the Preview tab reads. */
interface PreviewOriginsConfig {
  dashboard?: { browser_local_origins?: unknown }
}

/**
 * The configured Preview origins, and the one write this tab performs (adding
 * an origin the user asked to allow). Shares its query key with Settings ->
 * Browser so a change on either surface shows on the other.
 */
function usePreviewOrigins() {
  const qc = useQueryClient()
  const q = useQuery<PreviewOriginsConfig>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig() as Promise<PreviewOriginsConfig>,
  })
  const origins = useMemo(() => {
    const raw = q.data?.dashboard?.browser_local_origins
    return Array.isArray(raw) ? raw.filter((o): o is string => typeof o === 'string') : []
  }, [q.data])
  const allow = useMutation({
    mutationFn: (origin: string) => api.patchConfig('dashboard.browser_local_origins', [...origins, origin]),
    onSettled: () => { void qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) },
  })
  return { origins, loaded: q.isSuccess || q.isError, loadError: q.isError, allow }
}

/** The `host:port` an allow click would add for *url*, or null when no list
 *  entry could ever admit it (not one of the three loopback spellings the
 *  gateway keeps; `0.0.0.0` and `*.localhost` are not). */
function originToAllow(url: string): string | null {
  try {
    const u = new URL(url)
    return normalizeLocalOrigin(u.host)
  } catch {
    return null
  }
}

export default function WebPreviewPanel({ sessionKey, active = true }: { sessionKey?: string | null; active?: boolean }) {
  const storageKey = sessionKey ? `${URL_KEY_PREFIX}${sessionKey}` : null
  const pendingKey = sessionKey ? `${PENDING_KEY_PREFIX}${sessionKey}` : null
  const [nav, setNav] = useState<NavState>(EMPTY_NAV)
  const [draft, setDraft] = useState('')
  // A chat-fed URL awaiting an explicit "Load preview" click (null = none).
  const [pending, setPending] = useState<string | null>(null)
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

  const { origins, loaded: originsLoaded, loadError: originsError, allow } = usePreviewOrigins()

  // ── Native surface (desktop app) ──
  // Scoped by sessionKey: each Preview tab owns its own native view. `enabled`
  // is `active`, so switching side-panel tabs hides -- never destroys -- the
  // view. The view is "open" once an allowed page has been loaded into it;
  // until then the panel shows the ordinary iframe states.
  const native = useNativeBrowser(sessionKey || '', active)
  const nativeOpen = native.available && !!native.state?.open
  const annotate = useWebPreviewPanelAnnotate(sessionKey, nativeOpen)
  // The native address-bar input. The view-URL sync effect reads its focus so a
  // view-initiated navigation never overwrites the field while the user types.
  const nativeInputRef = useRef<HTMLInputElement>(null)
  /** The one gate both surfaces share: listed origins only, once the list has answered. */
  const allowsNow = useCallback(
    (target: string) => originsLoaded && previewAllowsUrl(target, origins),
    [originsLoaded, origins],
  )

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
    // Both are the USER-SELECTED targets (what the allowlist is checked against);
    // the cookie-isolation rewrite is applied where a target is framed, below.
    let pend = ''
    if (pendingKey) {
      try { pend = localStorage.getItem(pendingKey) || '' } catch { /* ignore */ }
    }
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

  const commit = useCallback((raw: string) => {
    const norm = normalizeUrl(raw)
    if (!norm) return
    // Every target goes through the same door: the body decides what to show for
    // it (the frame, or the card that says why not). Nothing is sent anywhere.
    // What is kept, shown and AUTHORIZED is the target the user chose (`norm`);
    // the cookie-isolation rewrite (`isolateFrameTarget`) is applied only to the
    // URL actually loaded (`framed`, below). Checking the list against the
    // rewritten host refused `localhost:5173` for a user who listed exactly that.
    setNav(n => pushNav(n, norm))
    setDraft(norm)
    persist(norm)
    setReloadKey(k => k + 1)
    // Loading supersedes any pending offer for this session.
    setPending(null)
    if (pendingKey) { try { localStorage.removeItem(pendingKey) } catch { /* ignore */ } }
    // An ALLOWED target goes to the native view when one is available: the real
    // page, where annotation works. A refused target never reaches it; the
    // iframe path shows the card that says why.
    if (native.available && allowsNow(norm)) native.open(isolateFrameTarget(norm))
  }, [persist, pendingKey, native, allowsNow])

  // Navigation while the NATIVE surface is showing. The iframe `commit` above
  // re-points a hidden iframe, so this drives the view the user is looking at and
  // keeps the address bar / persistence in step with it. The same gate applies:
  // a target outside the list is not navigated to; the bar keeps the typed text
  // and the view keeps its page.
  const commitNative = useCallback((raw: string) => {
    const norm = normalizeUrl(raw)
    if (!norm) return
    setDraft(norm)
    if (!allowsNow(norm)) return
    persist(norm)
    native.navigate(isolateFrameTarget(norm))
  }, [persist, native, allowsNow])

  // Reflect navigation the view performs on its own (in-page link clicks,
  // redirects) into the native address bar, never while the user is editing it.
  // Display only: `native.state` carries no session tag, so persisting here on a
  // slot switch could write the previous slot's URL into the new slot's storage.
  useEffect(() => {
    const u = native.state?.url
    if (!nativeOpen || !u) return
    if (document.activeElement === nativeInputRef.current) return
    setDraft(u)
  }, [nativeOpen, native.state?.url])

  const dismissPending = useCallback(() => {
    setPending(null)
    if (pendingKey) { try { localStorage.removeItem(pendingKey) } catch { /* ignore */ } }
  }, [pendingKey])

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

  // The URL the panel actually LOADS: the user's target with the cookie-isolation
  // host swap applied (a `localhost` dev server is framed as `127.0.0.1`, so the
  // dashboard's host-scoped cookie never reaches it). `url` itself stays the
  // user's spelling everywhere it is user-visible (URL bar, "Open in browser",
  // persistence) and is what the allowlist judges; `framed` is what the iframe,
  // the liveness probe and the native view get.
  const framed = useMemo(() => (url ? isolateFrameTarget(url) : ''), [url])
  // Varied per reload so a remount is a real request.
  const frameSrc = useMemo(() => withCacheBuster(framed, reloadKey), [framed, reloadKey])

  // A URL pointing back at this gateway is refused by its own frame-ancestors
  // policy, which the liveness probe below cannot see (the server is up, so the
  // probe passes) — detect it so we explain instead of framing a blank page.
  const selfOrigin = useMemo(() => !!url && isDashboardOrigin(url), [url])

  // The allowlist: the one gate that is this tab's own. `null` while the list
  // has not answered yet, so a frame is never shown on a guess and never refused
  // on one either.
  const allowed: boolean | null = useMemo(() => {
    if (!url) return null
    if (!originsLoaded) return null
    return previewAllowsUrl(url, origins)
  }, [url, origins, originsLoaded])
  const allowable = useMemo(() => (url && allowed === false ? originToAllow(url) : null), [url, allowed])

  // Liveness of the framed dev server: probed only while a URL is actually
  // framed (loaded, allowed, embeddable, tab active); two strikes unmount the
  // stale iframe in favour of the stopped state, a later success restores it.
  const unreachable = useLivenessProbe(
    framed,
    !!url && allowed === true && !selfOrigin && !pending && active,
    reloadKey,
  )

  /**
   * True exactly when the body renders the preview iframe — the one state in
   * which a target that refuses to be framed leaves a blank rectangle and the
   * panel says nothing about it.
   *
   * The conditions mirror the body's branch order (pending offer, no url, the
   * allowlist, the two embed refusals, unreachable server, then the frame), and
   * they are spelled here rather than re-derived at the use site so the hint
   * below the URL bar cannot disagree with whether a frame is actually on screen.
   */
  const framing = !!url && !pending && allowed === true && !selfOrigin && !unreachable

  // A navigation inside the frame that the dashboard's own `frame-src` refused
  // (an OAuth redirect to a sign-in page, most often). The frame is left blank
  // and the app's button spins, so a notice above it names the refused origin
  // and the way out. Cleared by a new URL, a reload or its dismiss.
  const frameRef = useRef<HTMLIFrameElement>(null)
  // `framing` is in the reset key because a frame that left the screen (an
  // unreachable server, a refusal card) remounts on a fresh load; `active`
  // is not, because a tab switch keeps the same blank frame mounted.
  const frameOrigin = useMemo(() => {
    try { return framed ? new URL(framed).origin : '' } catch { return '' }
  }, [framed])
  const [frameBlocked, dismissFrameBlocked] = useFrameNavigationBlock(
    framing && active, frameRef, `${reloadKey}:${url}:${framing}`, frameOrigin,
  )

  const askKiro = useCallback((target: string) => {
    // Stages the prompt in a chat composer and navigates there; the agent's
    // browser is the live card in the chat, never this tab.
    sendErrorToChat(i18nT('components.webPreviewPanel.ask_kiro_prompt', { url: target }))
  }, [])

  const iconBtn = 'flex items-center justify-center w-7 h-7 rounded-md text-muted hover:text-text '
    + 'hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 '
    + 'disabled:opacity-40 disabled:cursor-default disabled:hover:bg-transparent disabled:hover:text-muted'

  return (
    <div className="relative flex flex-col h-full min-h-0 bg-bg" data-testid="web-preview-panel" data-origins-loaded={originsLoaded ? 'true' : 'false'}>
      {/* Preview subtree stays MOUNTED while the native surface overlays it, so
          the iframe document and any unsaved form/SPA state survive the view
          being shown -- visually hidden, never unmounted. */}
      <div className={`flex flex-col h-full min-h-0 ${nativeOpen ? 'invisible pointer-events-none' : ''}`} aria-hidden={nativeOpen || undefined}>
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
        {/* Device-size presets behind one trigger, so the row stays at 4 sibling
            controls (back / forward / expand / sizes): `max-two-buttons-per-row`
            grandfathers a 4-control row but forbids growth. The trigger icon
            reflects the active device size so the state is visible unopened. */}
        <DropdownMenu open={moreOpen} onOpenChange={setMoreOpen}>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              className={`${iconBtn} ${isDeviceSized ? 'text-accent' : ''}`}
              title={i18nT('components.webPreviewPanel.preview_size')}
              aria-label={i18nT('components.webPreviewPanel.preview_size')}
            >
              {isDeviceSized ? <Smartphone size={14} /> : <Monitor size={14} />}
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-[210px]">
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
      {/* Why a page can be blank here, said up front instead of detected after
          the fact: no client-side signal separates a framed page from one that
          refused to be framed (see useFrameNavigationBlock for the one case that
          CAN be seen). One quiet standing line above the frame, never over it,
          carrying the open-in-browser link that is the actual way out. Only
          while a frame is really on screen, so it never contradicts one of the
          explanatory cards below. Info glyph and "Note:", not the warning mark
          the stateful cards use. */}
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
                className="inline-flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-md bg-accent text-accent-fg hover:opacity-90 transition-opacity cursor-pointer border-none"
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
            {/* The configured origins as one-click starting points; with none
                configured, the way to add one. */}
            {origins.length > 0 ? (
              <div className="flex flex-wrap gap-1.5 justify-center pt-1" data-testid="web-preview-origin-picks">
                {origins.map(o => (
                  <button
                    key={o}
                    type="button"
                    onClick={() => commit(`http://${o}`)}
                    className="text-[11px] font-mono px-2 py-1 rounded-md border border-border bg-transparent text-muted hover:text-text hover:bg-bg-hover hover:border-border-strong transition-colors cursor-pointer"
                  >
                    {o}
                  </button>
                ))}
              </div>
            ) : originsError ? (
              // A failed read is not "none configured": say it failed.
              <ErrorNotice
                variant="inline"
                message={i18nT('components.webPreviewPanel.origins_unavailable')}
                className="max-w-[360px] text-left"
              />
            ) : originsLoaded && (
              <div className="flex flex-col items-center gap-1.5 pt-1" data-testid="web-preview-no-origins">
                <span className="text-[11px] text-muted max-w-[300px] leading-snug">
                  {i18nT('components.webPreviewPanel.no_origins_yet')}
                </span>
                <SettingRef configKey="dashboard.browser_local_origins" />
              </div>
            )}
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
        ) : allowed === null ? (
          // The list has not answered yet: show nothing on a guess.
          <div className="flex items-center justify-center h-full bg-bg" aria-busy />
        ) : allowed === false ? (
          allowable ? (
            // A loopback address outside the list. Nothing failed: the panel's
            // own gate stopped it, so this is a hint with the one-click fix, not
            // an error. Plain text, no alert role.
            <div className="flex flex-col items-center justify-center h-full gap-3 px-6 text-center bg-bg">
              <ShieldCheck size={22} className="text-muted" />
              <div className="text-[13px] font-medium text-text">{i18nT('components.webPreviewPanel.origin_not_allowed_title')}</div>
              <div className="text-[11px] text-muted max-w-[320px] leading-snug" data-testid="web-preview-origin-refused">
                {i18nT('components.webPreviewPanel.origin_not_allowed_body')}
              </div>
              <code className="text-[11px] font-mono px-2 py-1 rounded bg-bg-elevated text-text break-all max-w-[320px]">
                {allowable}
              </code>
              <div className="flex flex-col items-center gap-2 pt-1">
                <Btn primary onClick={() => allow.mutate(allowable)} disabled={allow.isPending || originsError} data-testid="web-preview-allow-origin" className="whitespace-nowrap">
                  <Check size={13} className="lucide-inline" aria-hidden />
                  {i18nT('components.webPreviewPanel.allow_origin', { origin: allowable })}
                </Btn>
                <SettingRef configKey="dashboard.browser_local_origins" />
              </div>
              {/* No hand-off: the address bar above holds an unsaved draft URL. */}
              {allow.isError && (
                <ErrorNotice
                  variant="inline"
                  message={allow.error instanceof Error && allow.error.message ? allow.error.message : i18nT('components.webPreviewPanel.allow_origin_failed')}
                  className="max-w-[360px] text-left"
                />
              )}
              {originsError && (
                <ErrorNotice
                  variant="inline"
                  message={i18nT('components.webPreviewPanel.origins_unavailable')}
                  className="max-w-[360px] text-left"
                />
              )}
            </div>
          ) : (
            // An outside URL. This tab is for your own dev server; a real site
            // belongs in your Chrome, or in the agent's browser via the chat,
            // where its live card is.
            <div className="flex flex-col items-center justify-center h-full gap-3 px-6 text-center bg-bg">
              <Globe size={22} className="text-muted" />
              <div className="text-[13px] font-medium text-text">{i18nT('components.webPreviewPanel.outside_url_title')}</div>
              <div className="text-[11px] text-muted max-w-[320px] leading-snug" data-testid="web-preview-outside-refused">
                {i18nT('components.webPreviewPanel.outside_url_body')}
              </div>
              <code className="text-[11px] font-mono px-2 py-1 rounded bg-bg-elevated text-text break-all max-w-[320px]">
                {url}
              </code>
              <div className="flex items-center gap-2 pt-1">
                <a
                  href={url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-md border border-border text-text hover:bg-bg-hover transition-colors no-underline"
                >
                  <ExternalLink size={13} /> {i18nT('components.webPreviewPanel.open_in_your_chrome')}
                </a>
                <Btn onClick={() => askKiro(url)} data-testid="web-preview-ask-kiro">
                  <Sparkles size={13} className="lucide-inline" aria-hidden />
                  {i18nT('components.webPreviewPanel.ask_kiro')}
                </Btn>
              </div>
            </div>
          )
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
      {nativeOpen && (
        <WebPreviewPanelNativeSurface
          native={native}
          annotate={annotate}
          draft={draft}
          setDraft={setDraft}
          commitNative={commitNative}
          nativeInputRef={nativeInputRef}
        />
      )}
    </div>
  )
}
