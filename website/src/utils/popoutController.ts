/**
 * Generic cross-window coordination engine for "pop out" features.
 *
 * A "pop out" opens some entity (a chat session, an artifact, …) in a
 * dedicated same-origin browser window. That window is its own JS context — it
 * opens its own `/api/ws` socket and Redux store, so it renders live and
 * independently; the gateway stays the single source of truth. This module
 * carries only the lightweight *coordination* the backend can't: which entities
 * are currently popped out, so the main dashboard can show an indicator, focus
 * an existing popout instead of spawning a duplicate, and bring one back.
 *
 * A single BroadcastChannel (one per feature — see `channelName`) is shared by
 * every same-origin tab/window. Popout windows announce their presence and
 * answer heartbeat pings; the main dashboard maintains an `id -> lastSeen` map
 * and prunes windows that stop responding (closed / crashed). Everything is
 * in-memory + channel messages — no persistence, so a stale entry can never
 * outlive a heartbeat interval.
 *
 * `createPopoutController` returns an independent instance (its own channel +
 * state), so multiple features (chat, artifacts) coordinate on separate
 * channels with zero cross-talk. The pure helpers (message reducer, prune) are
 * exported so the coordination logic is unit-testable without a live
 * BroadcastChannel.
 */

import { i18nT } from '../i18n/t'

/** Heartbeat cadence (ms) for the main window's liveness ping. */
export const HEARTBEAT_MS = 5_000
/** A popout unseen for longer than this is considered gone and pruned. */
export const STALE_MS = 12_000
/**
 * How long a popout waits for a main window to claim a forwarded navigation
 * intent before falling back to opening the destination in a new tab.
 * BroadcastChannel delivery is effectively synchronous between live same-origin
 * windows, so this only needs to absorb event-loop scheduling jitter.
 */
export const NAV_CLAIM_MS = 250

/**
 * A navigation the popout wants performed in the MAIN dashboard window.
 * Popout windows are pinned to their entity — any affordance that would leave
 * it (open a chat session, go back to the library) forwards one of these
 * instead of navigating locally, which would mount the whole dashboard inside
 * the popout window.
 */
export type NavIntent = {
  /** Destination route in the main dashboard, e.g. '/chat' or '/artifacts'. Must not carry a query string. */
  path: string
  /** Chat slot to activate before navigating. */
  slotKey?: string
  /** Composer prefill to seed for a slot (rides the message — sessionStorage is per-window). */
  prefill?: { slotKey: string; prompt: string }
}

export type PopoutMsg =
  | { t: 'open'; id: string }
  | { t: 'close'; id: string }
  | { t: 'ping' }
  | { t: 'pong'; id: string }
  | { t: 'focus'; id: string }
  | { t: 'bring-back'; id: string }
  // Navigation-intent handshake (popout → main). Two-phase claim so that with
  // several main dashboard tabs open, exactly ONE performs the navigation:
  // the popout broadcasts a request, every main offers, the popout picks the
  // first offer and addresses the go at that main's id.
  | { t: 'nav-request'; nonce: string; intent: NavIntent }
  | { t: 'nav-offer'; nonce: string; mainId: string }
  | { t: 'nav-go'; nonce: string; mainId: string; intent: NavIntent }

/** entity id -> epoch ms the window was last seen alive. */
export type PopoutMap = Record<string, number>

/** Fold a channel message into the popped-out map (pure). */
export function applyMessage(map: PopoutMap, msg: PopoutMsg, now: number): PopoutMap {
  switch (msg.t) {
    case 'open':
    case 'pong':
      return { ...map, [msg.id]: now }
    case 'close': {
      if (!(msg.id in map)) return map
      const next = { ...map }
      delete next[msg.id]
      return next
    }
    default:
      return map
  }
}

/** Drop windows unseen for longer than `staleMs` (pure; identity-stable when unchanged). */
export function pruneStale(map: PopoutMap, now: number, staleMs: number = STALE_MS): PopoutMap {
  const next: PopoutMap = {}
  let changed = false
  for (const [id, seen] of Object.entries(map)) {
    if (now - seen <= staleMs) next[id] = seen
    else changed = true
  }
  return changed ? next : map
}

type Listener = () => void

/** Options that specialize a controller for one feature (chat, artifacts, …). */
export interface PopoutControllerOptions {
  /** Unique BroadcastChannel name for this feature (isolates cross-talk). */
  channelName: string
  /** Short label for guarded console diagnostics (e.g. 'chatPopout'). */
  logLabel: string
  /** Build the popout window URL for an entity id. */
  buildUrl: (id: string, title?: string) => string
  /** Stable, filesystem-safe `window.open` name for an entity id (enables dedupe). */
  windowName: (id: string) => string
  /**
   * Main-dashboard URL for an entity — the `returnSelfToMain` fallback target
   * when `window.close()` is refused (deep-linked / restored popouts have no
   * script opener, so close is a spec-level no-op there).
   */
  mainViewUrl: (id: string | null) => string
}

/** The main-window + popout-window API for one feature's popouts. */
export interface PopoutController {
  /** Subscribe a main-window listener (for useSyncExternalStore). Starts the heartbeat lazily. */
  subscribe(listener: Listener): () => void
  /** Current set of popped-out ids (stable identity until membership changes). */
  getSnapshot(): ReadonlySet<string>
  /** Open (or focus, if already open) an entity in its own browser window. */
  openPopout(id: string, title?: string): void
  /** Focus the popout window for an entity (direct handle, else ask it to focus itself). */
  focusPopout(id: string): void
  /** Close an entity's popout window and drop it from the map (caller re-views it in main). */
  bringBack(id: string): void
  /** True when THIS window is the live popout for `id`. */
  isSelfPopout(id: string): boolean
  /** From inside a popout: focus the opener and close; navigate to the main view when close is refused. */
  returnSelfToMain(): void
  /** Register THIS window as the live popout for `id` (responder role). Returns cleanup. */
  registerPopout(id: string): () => void
  /**
   * From inside a popout: forward a navigation intent to a main dashboard
   * window (claim handshake picks exactly one). When no main claims it within
   * `NAV_CLAIM_MS`, the destination opens in a new full browser tab instead.
   */
  forwardToMain(intent: NavIntent): void
  /**
   * Register THIS window as willing to perform forwarded navigation intents
   * (main dashboard role). Only windows with a registered handler answer
   * `nav-request`s — popout and embed windows never register. Returns cleanup.
   */
  setNavIntentHandler(fn: (intent: NavIntent) => void): () => void
  /** Test-only: swap the navigation sink (jsdom can't redefine window.location). */
  __setNavigateForTests(fn: (url: string) => void): void
  /** Test-only: swap the new-tab sink (asserts the no-main fallback). */
  __setWindowOpenForTests(fn: (url: string, target: string) => void): void
  /** Test-only: reset all instance state between cases. */
  __resetForTests(): void
}

/**
 * Create an independent popout controller bound to one BroadcastChannel. All
 * state lives in this closure, so separate features never share a map, heartbeat,
 * or window-handle registry.
 */
export function createPopoutController(opts: PopoutControllerOptions): PopoutController {
  const { channelName, logLabel, buildUrl, windowName, mainViewUrl } = opts

  let channel: BroadcastChannel | null = null
  let map: PopoutMap = {}
  let snapshot: ReadonlySet<string> = new Set<string>()
  const listeners = new Set<Listener>()
  let heartbeat: ReturnType<typeof setInterval> | null = null
  let mainSubscribers = 0
  /** Non-null when THIS window is itself a popout (drives the responder role). */
  let selfId: string | null = null
  /** Handles for popouts THIS window opened — lets us focus/close them directly. */
  const handles = new Map<string, Window | null>()
  /**
   * Main-role identity for the nav-intent claim handshake. Distinct from
   * `selfId` (which marks the POPOUT role) — a window becomes claim-eligible
   * only by registering a nav-intent handler, so `mainId` is minted lazily.
   */
  let mainId: string | null = null
  /** Registered by the main dashboard shell; popouts/embeds never register one. */
  let navIntentHandler: ((intent: NavIntent) => void) | null = null
  /** The popout side's in-flight forwarded navigation, if any. */
  let pendingNav: { nonce: string; intent: NavIntent; timer: ReturnType<typeof setTimeout> } | null = null

  /**
   * Guarded console output. The window-control paths (open / focus / close) can
   * be silently vetoed by the browser (popup blocker, no user activation); these
   * keep every veto diagnosable instead of swallowed by an empty catch.
   */
  function logDebug(msg: string, err?: unknown): void {
    // eslint-disable-next-line no-console
    console.debug(`[${logLabel}] ${msg}`, err ?? '')
  }
  function logWarn(msg: string): void {
    // eslint-disable-next-line no-console
    console.warn(`[${logLabel}] ${msg}`)
  }

  function recomputeSnapshot(): void {
    const keys = Object.keys(map)
    if (keys.length === snapshot.size && keys.every(k => snapshot.has(k))) return
    snapshot = new Set(keys)
    listeners.forEach(l => l())
  }

  function ensureChannel(): BroadcastChannel | null {
    if (channel || typeof BroadcastChannel === 'undefined') return channel
    channel = new BroadcastChannel(channelName)
    channel.onmessage = (e: MessageEvent<PopoutMsg>) => handleMessage(e.data)
    return channel
  }

  function post(msg: PopoutMsg): void {
    ensureChannel()?.postMessage(msg)
  }

  function handleMessage(msg: PopoutMsg): void {
    // Forwarder role: accept the first claim for an in-flight nav intent.
    // Keyed on pendingNav (not selfId) — only the window that posted the
    // matching nav-request can hold the nonce.
    if (msg.t === 'nav-offer' && pendingNav && msg.nonce === pendingNav.nonce) {
      // First offer wins: address the go at exactly that main so multiple
      // dashboard tabs don't all navigate. Later offers find pendingNav
      // cleared and are ignored.
      clearTimeout(pendingNav.timer)
      const { nonce, intent } = pendingNav
      pendingNav = null
      post({ t: 'nav-go', nonce, mainId: msg.mainId, intent })
      // Best-effort: raise a main window. The claimed main may not be the
      // opener, but same-origin child→opener focus is the one reliably
      // permitted path; the main also self-focuses on handling the intent.
      try { window.opener?.focus?.() } catch (e) { logDebug('opener focus vetoed', e) }
      return
    }
    // Popout responder role: answer liveness pings and honor control messages
    // addressed to this window's entity.
    if (selfId) {
      if (msg.t === 'ping') { post({ t: 'pong', id: selfId }); return }
      if (msg.t === 'focus' && msg.id === selfId) {
        // A channel-routed focus has no user activation, so browsers may veto it
        // (common after the opener refreshed and lost the direct handle).
        try { window.focus() } catch (e) { logDebug('self focus vetoed', e) }
        return
      }
      if (msg.t === 'bring-back' && msg.id === selfId) { returnSelfToMain(); return }
    }
    // Main dashboard role: claim + perform forwarded navigation intents. Only
    // windows that registered a handler participate (popouts/embeds don't).
    if (navIntentHandler) {
      if (msg.t === 'nav-request') {
        if (!mainId) mainId = randomId()
        post({ t: 'nav-offer', nonce: msg.nonce, mainId })
        return
      }
      if (msg.t === 'nav-go') {
        if (msg.mainId === mainId) navIntentHandler(msg.intent)
        return
      }
    }
    const now = Date.now()
    const next = pruneStale(applyMessage(map, msg, now), now)
    if (next !== map) { map = next; recomputeSnapshot() }
  }

  function heartbeatTick(): void {
    const now = Date.now()
    const pruned = pruneStale(map, now)
    if (pruned !== map) { map = pruned; recomputeSnapshot() }
    post({ t: 'ping' })
  }

  /**
   * Pause the interval while the tab is hidden, and on return to visible re-ping
   * BEFORE the next prune could run. Two reasons: a perpetual
   * 5s timer in every dashboard tab is wasted work for an opt-in feature, and a
   * backgrounded tab's throttled timers (~1/min) exceed STALE_MS, so without the
   * refresh a still-live popout would be pruned → its indicator flickers off/on.
   */
  function handleVisibilityChange(): void {
    if (document.hidden) {
      if (heartbeat) { clearInterval(heartbeat); heartbeat = null }
      return
    }
    if (mainSubscribers === 0 || heartbeat) return
    // Re-confirm liveness immediately: forgive the hidden gap (entries would
    // otherwise read as stale) and ping so live popouts re-add within a frame.
    const now = Date.now()
    let refreshed = false
    const next: PopoutMap = {}
    for (const [id, seen] of Object.entries(map)) {
      next[id] = Math.max(seen, now - HEARTBEAT_MS)
      refreshed = refreshed || next[id] !== seen
    }
    if (refreshed) { map = next; recomputeSnapshot() }
    post({ t: 'ping' })
    heartbeat = setInterval(heartbeatTick, HEARTBEAT_MS)
  }

  function startHeartbeat(): void {
    if (heartbeat || typeof BroadcastChannel === 'undefined') return
    document.addEventListener('visibilitychange', handleVisibilityChange)
    if (document.hidden) return // quiescent until visible; listener will start it
    post({ t: 'ping' })
    heartbeat = setInterval(heartbeatTick, HEARTBEAT_MS)
  }

  function stopHeartbeat(): void {
    document.removeEventListener('visibilitychange', handleVisibilityChange)
    if (heartbeat) { clearInterval(heartbeat); heartbeat = null }
  }

  function subscribe(listener: Listener): () => void {
    ensureChannel()
    listeners.add(listener)
    mainSubscribers += 1
    if (mainSubscribers === 1) startHeartbeat()
    return () => {
      listeners.delete(listener)
      mainSubscribers = Math.max(0, mainSubscribers - 1)
      if (mainSubscribers === 0) stopHeartbeat()
    }
  }

  function getSnapshot(): ReadonlySet<string> {
    return snapshot
  }

  function openPopout(id: string, title?: string): void {
    if (typeof window === 'undefined') return
    const existing = handles.get(id)
    if (existing && !existing.closed) {
      try { existing.focus() } catch (e) { logDebug(`focus of existing popout ${id} vetoed`, e) }
      return
    }
    const sc = window.screen
    const w = Math.min(880, Math.round((sc?.availWidth ?? 1280) * 0.55))
    const h = Math.min(900, Math.round((sc?.availHeight ?? 900) * 0.85))
    const availLeft = (sc as unknown as { availLeft?: number })?.availLeft ?? 0
    const availTop = (sc as unknown as { availTop?: number })?.availTop ?? 0
    const left = Math.round(availLeft + ((sc?.availWidth ?? 1280) - w) / 2)
    const top = Math.round(availTop + Math.max(0, ((sc?.availHeight ?? 900) - h) / 2))
    const features = `popup=yes,width=${w},height=${h},left=${left},top=${top},resizable=yes,scrollbars=yes`
    const win = window.open(buildUrl(id, title), windowName(id), features)
    if (!win) {
      // Popup blocker (or policy) vetoed the window. Don't optimistically mark
      // open — and don't fail silently: tell the user why nothing happened and
      // leave an operator-diagnosable trail.
      logWarn(`window.open blocked for ${id} — pop-up blocker or browser policy`)
      try {
        window.alert(i18nT('utils.popoutController.your_browser_blocked_the_pop_out_window_allow_po'))
      } catch { /* alert unavailable (e.g. sandboxed frame) — the warn above still records it */ }
      return
    }
    handles.set(id, win)
    try { win.focus() } catch (e) { logDebug(`focus of new popout ${id} vetoed`, e) }
    // Optimistically mark open; the window's own 'open' announce refreshes lastSeen.
    map = { ...map, [id]: Date.now() }
    recomputeSnapshot()
  }

  function focusPopout(id: string): void {
    const win = handles.get(id)
    if (win && !win.closed) {
      try { win.focus(); return } catch (e) { logDebug(`direct focus of ${id} vetoed — falling back to channel`, e) }
    }
    post({ t: 'focus', id }) // handle lost (main refreshed) — the window focuses itself
  }

  function bringBack(id: string): void {
    const win = handles.get(id)
    if (win && !win.closed) {
      try { win.close() } catch (e) { logDebug(`direct close of ${id} vetoed — falling back to channel`, e) }
    }
    post({ t: 'bring-back', id })
    handles.delete(id)
    if (id in map) {
      const next = { ...map }
      delete next[id]
      map = next
      recomputeSnapshot()
    }
  }

  /**
   * True when THIS window is the live popout for `id`. Surfaces rendered
   * inside a popout need this: a popout never holds its own id in the
   * coordination map (BroadcastChannel doesn't self-deliver), so
   * `isPoppedOut(ownId)` reads false in the popout window.
   */
  function isSelfPopout(id: string): boolean {
    return selfId !== null && selfId === id
  }

  /**
   * Navigation indirection: jsdom can't redefine `window.location`, so tests
   * swap this via `__setNavigateForTests` to assert the deep-link fallback.
   */
  let navigate = (url: string): void => window.location.assign(url)

  /**
   * Return THIS popout window's entity to the main dashboard: focus the opener
   * and close. `window.close()` is a spec-level no-op for windows the script
   * didn't open — a deep-linked / restored / refreshed popout has no opener — so
   * when the close doesn't take, navigate this window to the main view
   * instead. The control must always visibly do something.
   */
  function returnSelfToMain(): void {
    const id = selfId
    try { window.opener?.focus() } catch (e) { logDebug('opener focus vetoed', e) }
    try { window.close() } catch (e) { logDebug('self close vetoed', e) }
    // If we're still alive, the close was refused (no script opener). Fall back
    // to becoming the main view for this entity.
    if (!window.closed) {
      if (id) post({ t: 'close', id }) // tell other windows this popout is gone
      navigate(mainViewUrl(id))
    }
  }

  function registerPopout(id: string): () => void {
    ensureChannel()
    selfId = id
    post({ t: 'open', id })
    const announceClose = () => post({ t: 'close', id })
    window.addEventListener('beforeunload', announceClose)
    window.addEventListener('pagehide', announceClose)
    return () => {
      window.removeEventListener('beforeunload', announceClose)
      window.removeEventListener('pagehide', announceClose)
      announceClose()
      if (selfId === id) selfId = null
    }
  }

  /** Unguessable-enough id for the claim handshake (no security role — just collision avoidance). */
  function randomId(): string {
    try { return crypto.randomUUID() } catch { return `${Date.now()}-${Math.random().toString(36).slice(2)}` }
  }

  /**
   * New-tab indirection for the no-main fallback: swappable in tests (jsdom's
   * `window.open` is also spyable, but a dedicated sink keeps parity with the
   * `navigate` indirection above and avoids popup-blocker noise in assertions).
   */
  let windowOpen = (url: string, target: string): void => { window.open(url, target) }

  /**
   * Absolute destination URL for the no-main fallback. The new tab is a fresh
   * JS context with no Redux store to dispatch into, so the slot selection and
   * composer prefill ride as query params the main `/chat` route already (or
   * newly) understands: `?sid=` selects the slot, `?prefill=` seeds the
   * composer for it.
   */
  function navFallbackUrl(intent: NavIntent): string {
    const params = new URLSearchParams()
    if (intent.slotKey) params.set('sid', intent.slotKey)
    if (intent.prefill) params.set('prefill', intent.prefill.prompt)
    const qs = params.toString()
    return `${window.location.origin}${intent.path}${qs ? `?${qs}` : ''}`
  }

  function forwardToMain(intent: NavIntent): void {
    ensureChannel()
    // Supersede any still-pending forward — the newest user gesture wins.
    if (pendingNav) { clearTimeout(pendingNav.timer); pendingNav = null }
    const nonce = randomId()
    const timer = setTimeout(() => {
      // No main window claimed the intent — it either doesn't exist or is
      // gone. Open the destination as a full browser tab instead; the popout
      // stays pinned to its entity either way.
      pendingNav = null
      windowOpen(navFallbackUrl(intent), '_blank')
    }, NAV_CLAIM_MS)
    pendingNav = { nonce, intent, timer }
    post({ t: 'nav-request', nonce, intent })
  }

  function setNavIntentHandler(fn: (intent: NavIntent) => void): () => void {
    ensureChannel()
    navIntentHandler = fn
    return () => { if (navIntentHandler === fn) navIntentHandler = null }
  }

  function __setNavigateForTests(fn: (url: string) => void): void {
    navigate = fn
  }

  function __setWindowOpenForTests(fn: (url: string, target: string) => void): void {
    windowOpen = fn
  }

  function __resetForTests(): void {
    channel?.close()
    channel = null
    map = {}
    snapshot = new Set<string>()
    listeners.clear()
    stopHeartbeat()
    mainSubscribers = 0
    selfId = null
    handles.clear()
    navigate = (url: string) => window.location.assign(url)
    windowOpen = (url: string, target: string) => { window.open(url, target) }
    mainId = null
    navIntentHandler = null
    if (pendingNav) { clearTimeout(pendingNav.timer); pendingNav = null }
  }

  return {
    subscribe, getSnapshot, openPopout, focusPopout, bringBack,
    isSelfPopout, returnSelfToMain, registerPopout,
    forwardToMain, setNavIntentHandler,
    __setNavigateForTests, __setWindowOpenForTests, __resetForTests,
  }
}
