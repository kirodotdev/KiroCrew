import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Globe, RotateCw, ExternalLink, ArrowLeft, ArrowRight, Maximize2, Minimize2, Smartphone, Monitor, Check, Crop } from 'lucide-react'

import { safeSetItem } from '../utils/safeStorage'
import { isScreenSnipSupported } from '../hooks/useScreenSnip'
import { useIsMobile } from '../hooks/useIsMobile'

/**
 * WebPreviewPanel — a docked, session-scoped **live web preview** of a URL the
 * user is serving locally (a dev server / static server for the project they're
 * working on).
 *
 * Opened from the side panel's + menu ("Web Preview"), it loads the URL in a
 * sandboxed iframe. Unlike the agent-browse mirror (a read-only screenshot
 * stream), this is a real embedded browser view: the dev server's own HMR
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
/** Common local dev-server ports offered as one-click starting points. */
const COMMON_PORTS = [3000, 5173, 8080, 4321, 8000]
/** iframe sandbox — permissive enough for real apps + HMR, but still a sandbox. */
const SANDBOX = 'allow-scripts allow-same-origin allow-forms allow-popups allow-modals allow-downloads'

/** Preview viewport presets. `w`/`h` absent = responsive desktop (fill panel);
 *  present = a fixed device-sized frame (mobile/tablet). */
interface DevicePreset {
  id: string
  label: string
  w?: number
  h?: number
}
const DEVICE_PRESETS: DevicePreset[] = [
  { id: 'responsive', label: 'Responsive (desktop)' },
  { id: 'iphone-se', label: 'iPhone SE', w: 375, h: 667 },
  { id: 'iphone-15', label: 'iPhone 15 / 14', w: 390, h: 844 },
  { id: 'iphone-15-pro-max', label: 'iPhone 15 Pro Max', w: 430, h: 932 },
  { id: 'pixel-7', label: 'Pixel 7', w: 412, h: 915 },
  { id: 'galaxy-s20', label: 'Galaxy S20', w: 360, h: 800 },
  { id: 'ipad-mini', label: 'iPad Mini', w: 768, h: 1024 },
]

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
 * Isolate a loopback preview URL onto a hostname DISTINCT from the dashboard's.
 *
 * Cookies are scoped by host but NOT by port, so an iframe pointed at
 * `http://localhost:5173` while the dashboard is served from the same host
 * (`localhost`, `127.0.0.1`, or a `*.localhost` alias like `kirocrew.localhost`)
 * would send the dashboard's host-scoped auth cookie to the previewed dev server
 * (which could read/replay it). When the preview host matches the dashboard host
 * and both are loopback, swap it to a guaranteed-distinct loopback host
 * (`127.0.0.1`, or `localhost` when the dashboard already IS `127.0.0.1`) — a
 * different host string, so no dashboard cookie is ever sent to the framed
 * server. Non-loopback hosts and already-distinct hosts are returned unchanged.
 * `dashboardHost` defaults to the current document host (overridable for tests).
 */
export function isolatePreviewHost(url: string, dashboardHost?: string): string {
  let host = dashboardHost
  if (host == null) {
    try { host = typeof window !== 'undefined' ? window.location.hostname : '' } catch { host = '' }
  }
  if (!host) return url
  let u: URL
  try { u = new URL(url) } catch { return url }
  const h = u.hostname
  if (!isLoopbackHost(h)) return url        // real host / tunnel — leave it
  if (h !== host) return url                // already a distinct host → isolated
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
  // Bumping this key remounts the iframe — a hard reload that works even for
  // static servers with no HMR.
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
  }, [persist, pendingKey])

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

  return (
    <div className="flex flex-col h-full min-h-0 bg-bg">
      {/* URL bar: [back][forward]  ( [reload] input )  [open][expand] | [device] */}
      <form
        className="flex items-center gap-1 px-2 py-1.5 border-b border-border shrink-0"
        onSubmit={e => { e.preventDefault(); commit(draft) }}
      >
        {/* Back / forward — outside the URL container, enabled only when the
            history stack has somewhere to go. */}
        <button type="button" onClick={back} disabled={!canBack} className={iconBtn} title="Back" aria-label="Back">
          <ArrowLeft size={15} />
        </button>
        <button type="button" onClick={forward} disabled={!canForward} className={iconBtn} title="Forward" aria-label="Forward">
          <ArrowRight size={15} />
        </button>
        {/* URL container — reload lives inside on the left. */}
        <div className="flex-1 min-w-0 flex items-center gap-0.5 h-7 pl-0.5 pr-1 rounded-md bg-bg-elevated border border-border focus-within:border-accent transition-colors">
          <button
            type="button"
            onClick={reload}
            disabled={!url}
            className="flex items-center justify-center w-6 h-6 rounded text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 disabled:opacity-40 disabled:cursor-default disabled:hover:bg-transparent disabled:hover:text-muted"
            title="Reload"
            aria-label="Reload preview"
          >
            <RotateCw size={13} />
          </button>
          <input
            type="text"
            value={draft}
            onChange={e => setDraft(e.target.value)}
            placeholder="localhost:5173"
            aria-label="Preview URL"
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
            title="Open in browser"
            aria-label="Open in browser"
          >
            <ExternalLink size={13} />
          </a>
        </div>
        <button
          type="button"
          onClick={toggleExpand}
          className={`${iconBtn} ${expanded ? 'text-accent bg-accent-subtle hover:text-accent' : ''}`}
          title={expanded ? 'Exit expanded preview' : 'Expand preview (hide nav & sessions)'}
          aria-label={expanded ? 'Exit expanded preview' : 'Expand preview'}
          aria-pressed={expanded}
        >
          {expanded ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
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
            title={`Preview size: ${device.label}`}
            aria-label="Preview size"
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
                  <span className="flex-1">{d.label}</span>
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
            title="Screenshot an area into the chat"
            aria-label="Screenshot an area into the chat"
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
            <div className="text-[13px] font-medium text-text">Preview ready</div>
            <div className="text-[11px] text-muted max-w-[320px] leading-snug">
              The agent started a local preview. Load it to view the site in this panel.
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
                <Globe size={13} /> Load preview
              </button>
              <button
                type="button"
                onClick={dismissPending}
                className="text-[12px] px-3 py-1.5 rounded-md border border-border text-muted hover:text-text hover:bg-bg-hover transition-colors cursor-pointer bg-transparent"
              >
                Dismiss
              </button>
            </div>
          </div>
        ) : !url ? (
          <div className="flex flex-col items-center justify-center h-full gap-3 px-6 text-center bg-bg">
            <Globe size={22} className="text-muted" />
            <div className="text-[13px] font-medium text-text">Preview a local web server</div>
            <div className="text-[11px] text-muted max-w-[300px] leading-snug">
              Enter your dev-server URL above (e.g. a Vite or static server running on localhost) to
              preview the site you&apos;re working on — it live-reloads as you edit.
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
            <div className="text-[13px] font-medium text-text">Can&apos;t embed an http:// page here</div>
            <div className="text-[11px] text-muted max-w-[320px] leading-snug">
              This dashboard is served over HTTPS, so the browser blocks embedding an
              <span className="font-mono"> http:// </span>
              page (mixed content). Open it in a new tab instead.
            </div>
            <a
              href={url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-md border border-border text-text hover:bg-bg-hover transition-colors no-underline"
            >
              <ExternalLink size={13} /> Open {url}
            </a>
          </div>
        ) : unreachable ? (
          // The loaded dev server stopped responding — show a stopped state
          // instead of the stale last-rendered page (the iframe is unmounted).
          <div className="flex flex-col items-center justify-center h-full gap-3 px-6 text-center bg-bg">
            <Globe size={22} className="text-muted" />
            <div className="text-[13px] font-medium text-text">Preview server not reachable</div>
            <div className="text-[11px] text-muted max-w-[320px] leading-snug">
              The server at <span className="font-mono break-all">{url}</span> stopped responding.
              It&apos;ll reconnect automatically when it&apos;s back — or reload now.
            </div>
            <button
              type="button"
              onClick={() => { setUnreachable(false); reload() }}
              className="inline-flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-md border border-border text-text hover:bg-bg-hover transition-colors cursor-pointer bg-transparent"
            >
              <RotateCw size={13} /> Reload
            </button>
          </div>
        ) : isDeviceSized ? (
          // Fixed device size — center the frame in a scrollable, muted backdrop.
          <div className="absolute inset-0 overflow-auto bg-bg-elevated flex items-start justify-center p-4">
            <iframe
              key={reloadKey}
              src={url}
              title="Web preview"
              style={{ width: device.w, height: device.h }}
              className="shrink-0 border border-border rounded-lg bg-white shadow-sm"
              sandbox={SANDBOX}
            />
          </div>
        ) : (
          <iframe
            key={reloadKey}
            src={url}
            title="Web preview"
            className="absolute inset-0 w-full h-full border-0 bg-white"
            sandbox={SANDBOX}
          />
        )}
      </div>
    </div>
  )
}
