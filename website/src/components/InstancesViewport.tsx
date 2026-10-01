/**
 * InstancesViewport — renders the remote instance panes inside the pane stack
 * below the top instance tab bar (see InstanceTabBar / App.tsx). Each connected
 * instance's dashboard is an absolutely-positioned, full-bleed <iframe>; the
 * active instance is shown and the rest stay warm (mounted, hidden). The whole
 * stack is hidden when the Local tab is active so the native dashboard (a
 * sibling pane) shows through — nothing is unmounted, so switching is instant.
 *
 * Load-bearing rules:
 * - **Hide-not-unmount**: every warm instance's <iframe> stays mounted; only
 *   `display` toggles. Unmounting would reload the remote + re-run the token
 *   handshake and lose scroll/session state. This holds across Local<->remote
 *   switches too (the stack is display:none on Local, not unmounted).
 * - **Warm-set cap** (instances.warm_set_cap): keep at most K warm iframes;
 *   exceeding the cap evicts (unmounts) the least-recently-used non-active
 *   iframe. Eviction does NOT disconnect the tunnel — the tab persists and
 *   re-warms on next click. Tabs are removed only by an explicit disconnect.
 * - **Origin-validated unread relay**: trust postMessage counts only
 *   from a known loopback tunnel origin.
 *
 * For an active instance with no warm iframe (down / reconnecting after a
 * restart) it renders an in-pane error/reconnect panel; otherwise it renders
 * nothing only when nothing is warm.
 *
 * - **Pane readiness**: a warm iframe is only trusted once its
 *   embedded SPA posts `mc-embedded-ready` for the current src. Until then the
 *   active pane shows a loading overlay that carries the tab strip (the local
 *   header is hidden while a remote tab is active, so without it a slow or
 *   dead load would strand the user on a black pane with no tabs). If readiness
 *   never arrives within PANE_LOAD_TIMEOUT_MS the error panel surfaces with
 *   Retry, which force-reloads the iframe even for an identical re-minted src.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Loader2, RefreshCw } from 'lucide-react'
import { Trans } from 'react-i18next'
import { api, type InstanceView } from '../api/client'
import {
  CHAINED_CREW_MESSAGE,
  CHAINED_CREW_REFUSED_MESSAGE,
  chainAdoptionPlan,
  chainRefusalCode,
  readChainedCrewNotice,
} from '../lib/chainAnnounce'
import { tokenTtlTotalSeconds } from '../lib/tokenTtl'
import { WARM_SET_CAP_AUTO_CEILING } from '../utils/remoteCrew'
import { SettingsLink } from './SettingsLink'
import { useAppDispatch, useAppSelector, useAppStore } from '../store'
import { clearPaneReady, removeWarm, setActiveId, setPaneReady, setUnread, setWarm } from '../store/instancesSlice'
import InstanceTabBar, { visibleInstanceTabs, chainRows, useCrewPins, toggleCrewPin, useCrewSwitcherStableOrder, setStableOrder } from './InstanceTabBar'
import { parseLoopbackOriginPort } from '../lib/tunnelOrigin'
import {
  paneEndpointSrc,
  paneMessageEnvelope,
  parsePaneEndpoint,
  relayRenewDelayMs,
  RELAY_LEASE_RENEW_RETRY_MS,
  resolvePaneBootstrapRequest,
  resolvePaneMessage,
  resolvePaneMode,
  type PaneEndpoint,
  type PaneMessageContext,
} from '../lib/paneChannel'
import { RelayStorageBank, memoryStorage, parseRelayStorageMutation } from '../lib/relayStorage'
import {
  buildBootstrapEnvelope,
  PANE_CHANNEL_FIELD,
  RELAY_BOOTSTRAP_REPLY,
  RELAY_BOOTSTRAP_REQUEST,
  RELAY_ENVELOPE_VERSION,
  RELAY_STORAGE_MESSAGE,
} from '../lib/relayPaneBootstrap'
import {
  CURSOR_AWAY_CANCEL_TYPE,
  CURSOR_AWAY_RESULT_TYPE,
  CURSOR_AWAY_VERSION,
  CURSOR_AWAY_WATCH_TYPE,
  watchCursorAwayNative,
} from '../lib/cursorAway'
import { NATIVE_NOTIFY_TYPE, parseNativeNotifyEnvelope, postRelayedNativeNotification } from '../lib/nativeNotify'
import { frameDocumentState, paneLog, safePaneUrl } from '../lib/paneLog'
import { clearPaneHttpCache, paneOriginFor } from '../lib/paneCache'
import { connectInstanceInto } from '../lib/connectInstance'
import { LINUX_CAPTION_CONTROLS_WIDTH, TRAFFIC_LIGHT_INSET_PX, WIN_CAPTION_OVERLAY_WIDTH, WIN_CAPTION_RESERVE_PX } from '../lib/electron'
import { isEmbeddedPane } from '../lib/embedded'
import ErrorNotice from './ErrorNotice'
import { errMessage } from '../utils/thunkError'
import { reportInstanceFailure } from '../utils/instanceFailureReport'
import type { ErrorReport } from '../utils/errorReport'
import { isElectron, isLinuxFramelessElectron, isWinElectron } from '../lib/electron'
import type { DragGap } from '../lib/dragGaps'
import { useFocusMode, useFocusChromeVisible, setFocusModeEnabled, setFocusChromeVisible } from '../hooks/useFocusMode'

import { i18nT } from '../i18n/t'
// Refresh the embedded token once elapsed reaches this fraction of its TTL
// (mirrors the gateway's default 80% threshold). Proactive refresh reloads the
// out-of-view iframe with a fresh token well before the gateway's TTL cap.
const REFRESH_AT_ELAPSED_FRAC = 0.8
// Don't re-mint the same instance more than once per this window — bounds the
// reactive (auth-expired) path so a persistently-rejecting remote can't spin
// a reconnect/reload storm.
const REFRESH_MIN_INTERVAL_MS = 10_000
// If the ACTIVE pane's embedded SPA hasn't announced `mc-embedded-ready`
// within this long of its iframe (re)loading, treat the load as failed and
// surface the error panel (with the tab strip) instead of a silent black pane.
// Iframes report no load errors to the parent, and the backend can say
// "connected" while the browser-side load is dead (tunnel half-up, token
// rejected, remote gateway mid-restart) — this watchdog is the only signal.
// NOTE: this is deliberately LONGER than REFRESH_MIN_INTERVAL_MS, so the two
// cannot be compared to decide whether the watchdog can fire. The countdown is
// per LOAD, not per iframe src: the token is absent from the watchdog effect's
// deps below precisely so that a re-mint arriving inside the window cannot
// postpone it.
const PANE_LOAD_TIMEOUT_MS = 15_000
// Gap between successive auto-warm iframe mounts on first load (see the
// auto-warm effect). Long enough for a tunnel's shell + entry bundle to land
// before the next pane starts pulling its own; short enough that four panes
// are all warm within the time the user spends reading the Local tab.
const AUTO_WARM_STAGGER_MS = 1_500
// How many reactive re-mints one pane may ask for before the parent stops
// answering. The child posts `mc-auth-expired` on EVERY 403 it sees
// (api/client.ts hands recovery to the hub before it latches its own banner),
// so a pane whose session cannot be repaired by a fresh token asks forever —
// one SSH mint per REFRESH_MIN_INTERVAL_MS, for as long as the window stays
// open. Past this count the reactive path goes quiet and the pane is failed to
// the error panel, so the user gets Retry (which re-mints on demand) instead of
// an invisible mint storm.
//
// Only Retry resets the count. `mc-embedded-ready` deliberately does NOT:
// EmbeddedHostBridge posts it from a mount effect, BEFORE the pane's first
// authenticated request, so it proves the SPA mounted — not that the new token
// worked. A pane whose shell mounts fine and whose API then 403s posts it on
// every reload, so resetting there would clear the count once per re-mint and
// leave the very loop this cap exists to bound running unbounded.
const MAX_REACTIVE_REMINTS = 3


// The relay pane iframe's `sandbox`. It MUST equal the keyword set the backend
// stamps as the relay response CSP `sandbox` directive
// (instance_pane_relay._DOCUMENT_CSP), so the iframe attribute and the served CSP
// agree. Crucially it OMITS `allow-same-origin` — the document is therefore an
// opaque origin with no parent DOM/cookie/storage/hub-session access — and omits
// top-navigation, so the pane cannot navigate the parent. Direct-loopback panes
// are NOT sandboxed (they are a cross-origin loopback SPA, unchanged).
const RELAY_PANE_SANDBOX = 'allow-scripts allow-forms allow-popups allow-modals allow-downloads'

/** The loopback port of a direct-loopback warm endpoint, else undefined. A relay
 *  endpoint carries no port (its address is a same-origin capability path), so
 *  every port-shaped journal field and desktop cache key reads undefined there. */
function warmPort(conn: PaneEndpoint | undefined): number | undefined {
  return conn?.kind === 'direct-loopback' ? conn.port : undefined
}

/** A stable identity for one pane LOAD, for the watchdog + journals: the port in
 *  direct mode, the capability channel in relay mode. A new value is a new load. */
function warmLoadKey(conn: PaneEndpoint | undefined): string | undefined {
  if (!conn) return undefined
  return conn.kind === 'direct-loopback' ? `p:${conn.port}` : `c:${conn.channel}`
}

export default function InstancesViewport({ macInset = false }: { macInset?: boolean } = {}) {
  // Windows counterpart of `macInset`: the caption overlay is a shell property,
  // not a window state, so it derives straight from the platform flag rather
  // than arriving as a prop.
  const winInset = isWinElectron
  // Inset for the HOST-rendered InstanceTabBar strips atop the loading/error
  // overlays: clear of the macOS traffic lights on the left, and of the
  // Windows titleBarOverlay caption buttons on the right — those strips are
  // the topmost header while an overlay is up, exactly like the pane header.
  const stripInsetStyle = macInset || winInset
    ? {
        ...(macInset ? { paddingLeft: TRAFFIC_LIGHT_INSET_PX } : null),
        ...(winInset ? { paddingRight: WIN_CAPTION_RESERVE_PX } : null),
      }
    : undefined
  const dispatch = useAppDispatch()
  const queryClient = useQueryClient()
  const warm = useAppSelector(s => s.instances.warm)
  const activeId = useAppSelector(s => s.instances.activeId)
  const mru = useAppSelector(s => s.instances.mru)
  const unread = useAppSelector(s => s.instances.unread)
  // Panes whose embedded SPA has announced readiness for their CURRENT src.
  // Tests preload partial slices, so tolerate a missing map.
  const ready = useAppSelector(s => s.instances.ready) ?? {}
  // The crew-switcher pin preference, relayed into every embedded pane so a
  // remote pane's bar matches the local bar. Reactive: a change re-broadcasts
  // the model (see buildModelFor deps + the broadcast effect) so all panes flip
  // together, and an embedded pin toggle routes back here via `mc-set-crew-pin`.
  const [pinnedCrewSet] = useCrewPins()
  // Stable array identity per pin change, so the model memo below does not
  // re-broadcast on every render.
  const pinnedCrews = useMemo(() => [...pinnedCrewSet], [pinnedCrewSet])
  // The crew-switcher "keep tab order fixed" preference, relayed into every
  // embedded pane so a remote pane's bar orders its chips the same way the local
  // bar does. Reactive like the pins: a change re-broadcasts the model, and an
  // embedded toggle routes back here via `mc-set-stable-order`.
  const [stableOrder] = useCrewSwitcherStableOrder()
  // Focus mode is a property of the WINDOW, not of one pane: a remote crew shown
  // inside a focused window must hide its chrome too. Relayed down the host model
  // below, and it also gates the host drag strips (see their render site).
  const { enabled: focusMode } = useFocusMode()
  const focusChromeVisible = useFocusChromeVisible()

  // Per-instance header drag gaps relayed up by each embedded pane
  // (mc-drag-gaps). Only the ACTIVE pane's gaps are rendered, but they are
  // keyed by id so a background pane's report is retained for an instant switch.
  const [dragGaps, setDragGaps] = useState<Record<string, DragGap[]>>({})

  // Embedded instance panes never host nested panes (single-level by design),
  // so skip the poll and render nothing — see isEmbeddedPane / InstanceTabBar.
  const embedded = isEmbeddedPane()

  // How this parent dashboard addresses its remote-crew panes, resolved once
  // from the connection it arrived on (window.location does not change over the
  // page's life). Two transports (see lib/paneChannel):
  //  - `direct-loopback` — desktop / localhost: a pane is an http loopback
  //    `host:port` iframe validated by exact origin. Unchanged, byte for byte.
  //  - `same-origin-relay` — a published HTTPS parent whose only exposed origin
  //    is the hub's: a pane is a SANDBOXED, opaque-origin iframe served by the
  //    hub at a capability `documentPath`, its messages bound to the exact frame
  //    + a per-pane channel (event.origin is the opaque `'null'`, never trusted).
  const paneMode = useMemo(() => resolvePaneMode(window.location), [])
  const relayMode = paneMode.kind === 'same-origin-relay'

  // The parent-side durable banks for relay-pane Web Storage. A relay pane is an
  // opaque origin that can persist nothing itself, so the parent keeps each
  // connected crew's local/session storage under its OWN origin, namespaced per
  // instance and bounded (see lib/relayStorage). Created lazily and only in relay
  // mode; direct mode never touches them. sessionStorage may be unavailable
  // (privacy modes) — fall back so a missing area never breaks the viewport.
  const relayBanksRef = useRef<{ local: RelayStorageBank; session: RelayStorageBank } | null>(null)
  const relayBanks = useCallback(() => {
    if (!relayBanksRef.current) {
      // Accessing window.localStorage/sessionStorage can itself THROW
      // (SecurityError when storage is disabled, blocked cookies, private modes,
      // or an opaque parent), so probe each behind a guard and fall back to an
      // in-memory Storage — persistence is lost for the page's lifetime, but the
      // viewport never breaks on an unavailable area. The bank guards every
      // later access too, so a working area that throws on a full write is safe.
      const safeArea = (pick: () => Storage): Storage => {
        try {
          const s = pick()
          void s.length // some engines only throw on first use, not on the getter
          return s
        } catch {
          return memoryStorage()
        }
      }
      relayBanksRef.current = {
        local: new RelayStorageBank(safeArea(() => window.localStorage)),
        session: new RelayStorageBank(safeArea(() => window.sessionStorage)),
      }
    }
    return relayBanksRef.current
  }, [])

  // Poll so token_ttl_remaining (and connection dots) stay current; this also
  // drives the proactive token-refresh effect below.
  const instancesQuery = useQuery({
    queryKey: ['instances'],
    queryFn: () => api.listInstances(),
    refetchInterval: 60_000,
    enabled: !embedded,
  })
  const warmCap = instancesQuery.data?.warm_set_cap || WARM_SET_CAP_AUTO_CEILING

  // Current warm map in a ref so the refresh callback (used by the long-lived
  // postMessage listener) always sees the latest ports without re-subscribing.
  const warmRef = useRef(warm)
  warmRef.current = warm
  // Read inside the message listener rather than closed over: the listener is
  // registered once, and only the ACTIVE pane may speak for the window's chrome.
  const activeIdRef = useRef(activeId)
  activeIdRef.current = activeId
  // Each pane's last-reported chrome visibility (mc-focus-chrome), so a pane
  // SWITCH can apply the incoming pane's state immediately. Without this the
  // window keeps the OUTGOING pane's value — switching necessarily happens from
  // a peeked header (the tab bar lives on it), so the traffic lights stayed
  // visible over the new pane until its own next hover cycle re-posted.
  const paneChromeRef = useRef<Record<string, boolean>>({})
  // The off-window cursor watch this frame is running FOR a pane (see the
  // mc-cursor-away-watch handler), or null. At most one: the main process polls
  // once per window, and only the active pane may hold it.
  const paneCursorWatchRef = useRef<{ paneId: string; watchId: string; stop: () => void } | null>(null)
  const stopPaneCursorWatch = useCallback(() => {
    const live = paneCursorWatchRef.current
    if (!live) return
    paneCursorWatchRef.current = null
    live.stop()
  }, [])
  // A pane switch (or unmount) orphans the outgoing pane's watch: its reveal is
  // no longer on screen, and nothing should keep polling for it.
  useEffect(() => stopPaneCursorWatch, [activeId, stopPaneCursorWatch])
  const refreshingRef = useRef<Set<string>>(new Set())
  const lastRefreshRef = useRef<Map<string, number>>(new Map())
  // Reactive (mc-auth-expired) re-mints answered per pane since its last Retry
  // — see MAX_REACTIVE_REMINTS.
  const reactiveMintsRef = useRef<Map<string, number>>(new Map())
  // Load watchdog verdict + forced-reload sequence, documented at their consumer
  // (the watchdog effect below); declared here because the relay listener also
  // bumps `reloadSeq` for the one-shot script-error heal.
  const [timedOut, setTimedOut] = useState<Record<string, boolean>>({})
  const [reloadSeq, setReloadSeq] = useState<Record<string, number>>({})
  // Terminal relay-lease failure, modelled INDEPENDENTLY of load readiness and
  // keyed by the channel that failed. The load watchdog only fires while a pane
  // is NOT ready, so it cannot represent the other terminal outcome: a pane that
  // booted and went ready, then had its short-lived capability lease exhaust its
  // renewal budget (see `renewRelayLease`). Such a pane keeps `ready[id]` true —
  // its shell is still on screen — while every future relayed request 404s once
  // the lease expires, so it must surface the recovery panel WITHOUT clearing
  // readiness (clearing it would repopulate the iframe `name` seed and re-arm the
  // watchdog). The value is the channel the failure was recorded against, so a
  // later reissue (a new channel via renewal or Retry) no longer matches and the
  // verdict clears itself; a stale in-flight renewal that resolves against an
  // already-replaced channel likewise cannot mark the newer lease failed.
  const [relayLeaseFailed, setRelayLeaseFailed] = useState<Record<string, string>>({})
  // The document-bootstrap watchdog, keyed by instance id: the nonce + channel
  // of the CURRENT relay document generation and the timer that fails it if it
  // authenticates its port but never announces readiness. Modelled INDEPENDENTLY
  // of the initial load watchdog, which cannot cover a navigation inside an
  // already-ready pane — that pane keeps `ready[id]` true, so the readiness-gated
  // watchdog never arms — see the bootstrap-request handler for the full note.
  const bootstrapWatchRef = useRef<
    Map<string, { nonce: string; channel: string; timer: ReturnType<typeof setTimeout> }>
  >(new Map())
  // Terminal document-bootstrap failure, keyed by the channel it failed against
  // (like `relayLeaseFailed`) so a reissue/Retry — which mints a new channel —
  // self-clears it, and surfaced WITHOUT gating on readiness so an already-ready
  // pane whose newly-navigated document hangs still gets the recovery panel.
  const [bootstrapFailed, setBootstrapFailed] = useState<Record<string, string>>({})
  // Panes already granted their one automatic cache-evict-and-reload after a
  // `script-error` (see the relay listener). Cleared by Retry.
  const scriptErrorHealsRef = useRef<Set<string>>(new Set())
  // Live iframe elements by id, so the parent can postMessage the switcher model
  // into each embedded pane. Set/cleared by the iframe ref cb.
  const iframeRefs = useRef<Map<string, HTMLIFrameElement>>(new Map())
  // One STABLE ref callback per pane id. An inline `ref={el => ...}` gets a new
  // function identity on every render, and React 18 then calls the OLD callback
  // with null and the NEW one with the same, never-detached element on each
  // re-render. With the journal lines inside that callback, every 10s poll and
  // every host-model broadcast printed an `iframe-unmounted` + `iframe-mounted`
  // pair for every warm pane (600+ per pane per hour in one capture) and read
  // as a remount storm that never happened, burying the one real question --
  // did THIS pane's document ever load -- under fake churn. Caching the callback
  // per id means React only calls it when the element genuinely attaches or
  // detaches, which is what the two journal lines claim to mean.
  const iframeRefCallbacks = useRef<Map<string, (el: HTMLIFrameElement | null) => void>>(new Map())
  // Read-only mirrors for the long-lived message listener, kept current without
  // re-subscribing (mirrors the warmRef pattern already used here).
  const postModelToRef = useRef<(id: string) => void>(() => {})
  // Distinct readiness ack, sent ONLY from the mc-embedded-ready handler (never
  // from the input-driven broadcast). It is the pane's proof that THIS parent
  // recorded its readiness, so the pane can stop re-announcing without mistaking
  // an ordinary model broadcast for an ack — see EmbeddedHostBridge.
  const postAckToRef = useRef<(id: string) => void>(() => {})
  const instancesRef = useRef<InstanceView[]>([])

  // The AUTHENTICATED downward MessagePort per relay pane, and the channel it was
  // bound under. A relay document hands the parent one port of a MessageChannel
  // in its bootstrap request; the parent replies AND sends every later downward
  // message (host model, readiness ack, cursor replies) over that port, NEVER via
  // a wildcard `window.postMessage` to the frame. The port is entangled with the
  // exact document that authenticated, so a successor document in the same iframe
  // — whose `WindowProxy` survives navigation but which never authenticated —
  // cannot receive the channel or any host state. The channel is recorded beside
  // it so a port left over from a superseded endpoint (a lease rotation / Retry
  // mints a new channel) is closed rather than reused for the new generation.
  // Relay-only; direct-loopback panes never populate it (they keep the exact
  // loopback-origin `window.postMessage`, unchanged).
  const relayPortsRef = useRef<Map<string, { port: MessagePort; channel: string }>>(new Map())
  // Send one downward message to a relay pane over its authenticated document
  // port, or drop it. Returns false (and sends nothing) when there is no bound
  // port yet, or when the bound port belongs to a superseded endpoint — a
  // downward post is bound to the authenticated document, never broadcast to
  // whatever occupies the iframe. The channel is stamped so the child's existing
  // channel checks pass; the port is the actual binding.
  const sendDownRelay = useCallback((id: string, message: Record<string, unknown>): boolean => {
    const entry = relayPortsRef.current.get(id)
    if (!entry) return false
    const conn = warmRef.current[id]
    if (!conn || conn.kind !== 'same-origin-relay' || conn.channel !== entry.channel) {
      // The endpoint rotated (renewal / Retry) and this port is bound to the dead
      // channel: close it and drop. The replacement document re-binds a fresh one.
      try { entry.port.close() } catch { /* already closed */ }
      relayPortsRef.current.delete(id)
      return false
    }
    try {
      entry.port.postMessage({ ...message, [PANE_CHANNEL_FIELD]: entry.channel })
    } catch {
      /* port closed (document replaced mid-post) — the successor cannot reach it */
    }
    return true
  }, [])
  // Close every relay port and cancel every document-bootstrap timer on unmount
  // so no entangled peer is left dangling and no timer fires after teardown.
  useEffect(
    () => () => {
      for (const { port } of relayPortsRef.current.values()) {
        try { port.close() } catch { /* already closed */ }
      }
      relayPortsRef.current.clear()
      for (const { timer } of bootstrapWatchRef.current.values()) clearTimeout(timer)
      bootstrapWatchRef.current.clear()
    },
    [],
  )

  // Whether `refreshToken` would actually mint for this id right now: no mint
  // already in flight, and outside the rate window. Split out of refreshToken so
  // the reactive path can tell a declined call from an answered one BEFORE it
  // charges the pane's budget — a call the guards drop mints nothing, so
  // charging for it would fail the pane early (see MAX_REACTIVE_REMINTS).
  const canRefreshNow = useCallback((id: string) => {
    if (refreshingRef.current.has(id)) return false
    return Date.now() - (lastRefreshRef.current.get(id) || 0) >= REFRESH_MIN_INTERVAL_MS
  }, [])

  // Force a fresh token mint for one instance and reload its iframe by updating
  // warm[id].token (srcFor re-derives the ?token= URL, so changing the token
  // reloads the iframe). Mirrors the gateway's mint-and-load. Concurrency- and
  // rate-guarded so the reactive path can't loop.
  const refreshToken = useCallback(
    async (id: string) => {
      // Direct-loopback only: a browser token exists solely on that transport. A
      // relay pane holds no token (the capability lease + the manager's own
      // upstream credential authenticate it), so it is re-issued through
      // openInstancePane, never re-minted here.
      if (relayMode) return
      if (!canRefreshNow(id)) return
      refreshingRef.current.add(id)
      try {
        const res = await api.refreshInstanceToken(id)
        const port = res.local_port || warmPort(warmRef.current[id])
        if (res.token && port) {
          dispatch(setWarm({ id, conn: { kind: 'direct-loopback', port, token: res.token } }))
          paneLog('remint', { id, port })
        } else {
          // A mint that returns nothing usable leaves the OLD warm entry standing,
          // so the pane looks unchanged. Journal it: on screen this is
          // indistinguishable from success, which is how it stayed invisible.
          paneLog('remint-empty', { id, port, hasToken: !!res.token })
        }
      } catch (err) {
        paneLog('remint-failed', { id, error: (err as Error)?.message || 'unknown' })
      } finally {
        refreshingRef.current.delete(id)
        lastRefreshRef.current.set(id, Date.now())
      }
    },
    [dispatch, canRefreshNow, relayMode],
  )

  // Reissue one relay pane's capability lease and rotate the whole endpoint onto
  // the fresh grant. A same-origin-relay lease expires on the hub
  // (DEFAULT_LEASE_TTL_SECONDS); after it, every request under the capability —
  // API, asset, reload, or a socket reconnect — gets the relay's uniform 404. So
  // reissue BEFORE the deadline: `openInstancePane` mints a fresh capability
  // (new documentPath + channel + lease), and `setWarm` sees a changed endpoint
  // (`sameEndpoint` is false on a new documentPath/channel), which clears the
  // pane's readiness; the iframe key embeds the channel, so the frame remounts
  // with a freshly-seeded window.name — endpoint, iframe, channel, readiness,
  // and storage seed rotate together onto the new lease, with the OLD lease
  // still live until its own deadline so there is no gap. Concurrency-guarded so
  // a slow reissue cannot overlap itself.
  const relayRenewingRef = useRef<Set<string>>(new Set())
  // Pending renewal timers (first attempt and bounded retries), keyed by id, so
  // an endpoint/warm change or unmount can cancel a retry chain the `warm` effect
  // did not itself schedule (a retry is armed inside the callback, between effect
  // runs). The `warm` effect clears every entry on each run — cancel-on-change.
  const relayRenewTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())
  // A ref to the latest callback so both the `warm` effect and a self-armed retry
  // call the current identity WITHOUT taking it as an effect dependency (which
  // would reschedule every renewal whenever the callback's closure changed).
  const renewRelayLeaseRef = useRef<
    (id: string, expectedChannel: string, deadlineMs: number) => void
  >(() => {})
  const renewRelayLease = useCallback(
    async (id: string, expectedChannel: string, deadlineMs: number) => {
      if (!relayMode) return
      const cur = warmRef.current[id]
      // Cancel-on-change: the lease we were scheduled against is gone (disconnect
      // / eviction) or was replaced by a DIFFERENT lease (a new channel is a new
      // capability), so this attempt is stale. Do nothing.
      if (!cur || cur.kind !== 'same-origin-relay' || cur.channel !== expectedChannel) return
      if (relayRenewingRef.current.has(id)) return
      relayRenewingRef.current.add(id)
      let renewed = false
      try {
        // Connected-only: renewal must never reconnect a tunnel the user
        // disconnected, and must never surface a remote token. The gateway
        // decides under its manager lock; a forward that is down declines (no
        // endpoint) and this reissue simply retries or, at the deadline, surfaces
        // the timed-out pane.
        const raw = await api.openInstancePane(id, 'same-origin-relay', { onlyIfConnected: true })
        const endpoint = parsePaneEndpoint(raw)
        if (endpoint && endpoint.kind === 'same-origin-relay') {
          dispatch(setWarm({ id, conn: endpoint }))
          paneLog('relay-renew', { id })
          renewed = true
        } else {
          // A 200 whose body is not a valid relay endpoint (a connected-only
          // decline, or an unusable shape): leave the current endpoint standing
          // and fall through to the bounded retry below.
          paneLog('relay-renew-declined', { id, reason: 'no_relay_endpoint' })
        }
      } catch (err) {
        paneLog('relay-renew-failed', { id, error: (err as Error)?.message || 'unknown' })
      } finally {
        relayRenewingRef.current.delete(id)
      }
      // On success, `setWarm` writes a later deadline and the `warm` effect
      // reschedules against the fresh lease — nothing more to do here.
      if (renewed) return
      // Re-read after the await: a disconnect, eviction, or endpoint change during
      // the request cancels any retry.
      const after = warmRef.current[id]
      if (!after || after.kind !== 'same-origin-relay' || after.channel !== expectedChannel) return
      // Bounded retry WITHIN the remaining lease budget: arm another attempt only
      // if it can still complete before the deadline. Otherwise the lease can no
      // longer be renewed in time, so surface the pane's timed-out state (the same
      // signal the load watchdog uses) instead of letting it silently 404 when the
      // capability expires.
      if (Date.now() + RELAY_LEASE_RENEW_RETRY_MS < deadlineMs) {
        paneLog('relay-renew-retry', { id })
        relayRenewTimersRef.current.set(
          id,
          setTimeout(
            () => void renewRelayLeaseRef.current(id, expectedChannel, deadlineMs),
            RELAY_LEASE_RENEW_RETRY_MS,
          ),
        )
      } else {
        // Terminal: no renewal attempt fits before the deadline. Modelled
        // independently of the load watchdog because a renewal-exhausted pane is
        // usually READY (its shell rendered before the lease began expiring), and
        // `timedOut && !ready` can never surface a ready pane. Keyed on the
        // channel that failed so a later reissue (renewal or Retry mints a new
        // channel) stops matching and the verdict clears itself — and a stale
        // in-flight renewal resolving here cannot mark an already-replaced lease
        // failed (the `expectedChannel` guard above already returned for that).
        paneLog('relay-renew-timeout', { id })
        setRelayLeaseFailed(prev =>
          prev[id] === expectedChannel ? prev : { ...prev, [id]: expectedChannel },
        )
      }
    },
    [dispatch, relayMode],
  )
  renewRelayLeaseRef.current = renewRelayLease

  // Adopt a crew a pane just connected, as a top-level tab of ours.
  //
  // `parentId` is the pane's own instance id here, established by its ORIGIN
  // resolving to a warm tunnel port — not by anything in the payload. Everything
  // in `raw` came from frame code, so it is shape-checked before it is used and
  // then handed to the gateway, which owns the two decisions that matter: whether
  // the chain is too deep, and whether it closes a loop back onto us.
  //
  // Idempotent by (parent, the parent's id for the crew): a pane re-announces on
  // every reconnect, and its loopback port changes each time. An existing row is
  // re-pointed at the new port rather than duplicated, which is also what repairs
  // a chain after the pane's own gateway restarts.
  const adoptChainedCrew = useCallback(
    async (parentId: string, raw: unknown) => {
      // Every field came from frame code. The SENDER is trusted (its origin
      // resolved to a warm pane above); the PAYLOAD is not, and the rules live in
      // `readChainedCrewNotice` so they can be tested without a host.
      const notice = readChainedCrewNotice(raw)
      if (!notice) return
      const { id: remoteId, name, sshHost: host, remotePort, port } = notice
      // Keyed on the PARENT's id for the crew, not on its host string: one machine
      // answers to many spellings, so a host key both misses a re-announce that
      // spells it differently and collides across two crews on one machine.
      const existing = instancesRef.current.find(
        i => i.via_instance_id === parentId && i.via_remote_id === remoteId,
      )
      try {
        if (existing) {
          const plan = chainAdoptionPlan(existing.via_remote_port, port)
          if (plan.repoint) {
            // Drop the warm entry BEFORE the PATCH, not after. It names the OLD hop
            // port and a token minted for it; the PATCH tears the tunnel down and
            // the reconnect below allocates a FRESH port, so an entry left in place
            // is a dead port paired with a live credential. Nothing repairs it on
            // its own either -- auto-warm skips any id that is already warm -- so
            // the pane would keep dialling the old port until the user pressed
            // Retry. Removing it first means no reader can observe the stale pair.
            dispatch(removeWarm(existing.id))
            await api.updateInstance(existing.id, { via_remote_port: port })
            paneLog('chain-repointed', { id: existing.id, parentId, port })
          }
          // Through `connectInstanceInto`, never bare `api.connectInstance`: this is
          // the canonical warm-writer, and it is what rewrites warm[id] from the
          // authenticated response, so the new port and the token minted for it
          // arrive as one pair. Calling the api directly here discarded the status,
          // which is how the stale pair survived a repoint in the first place.
          if (plan.connect) await connectInstanceInto(dispatch, existing.id, 'auto-connect')
        } else {
          const added = await api.addInstance({
            name,
            ssh_host: host,
            // A crew's own gateway port is a record, not a dial target from here.
            // An out-of-range value is dropped rather than refused: the row is
            // still usable without it, and the hop port is what we forward to.
            ...(Number.isInteger(remotePort) && remotePort >= 1 && remotePort <= 65535
              ? { remote_port: remotePort }
              : {}),
            via_instance_id: parentId,
            via_remote_port: port,
            via_remote_id: remoteId,
          })
          paneLog('chain-added', { id: added.id, parentId, port })
          // Connect it. A row alone does NOT become a tab: `visibleInstanceTabs`
          // admits a crew only once it is connected, warm, or carries the sticky
          // connect intent a connect sets — so adopting without this leaves the
          // promised top-level tab missing and the crew reachable only from the
          // Remote Crew list. The announcing pane's user connected it there; this
          // is the same intent arriving here.
          await api.connectInstance(added.id)
        }
        void queryClient.invalidateQueries({ queryKey: ['instances'] })
      } catch (err) {
        const reason = (err as Error)?.message || ''
        // One refusal is not a failure: `chain_duplicate` says this crew is
        // ALREADY on the record here, which is what a stale read above produces
        // -- the list is refreshed only after the add and the connect it
        // triggers, so a second announcement inside that window asks for a crew
        // it cannot yet see. Refresh and say nothing: the crew is present, and
        // relaying a refusal would report a problem the user does not have.
        if (chainRefusalCode(err) === 'chain_duplicate') {
          paneLog('chain-already-present', { parentId, port })
          void queryClient.invalidateQueries({ queryKey: ['instances'] })
          return
        }
        // Every other refusal IS the user's business, and the pane is where they
        // acted: its Remote Crew panel already shows the gateway's reason for the
        // connect it made, and this is the other half of that sentence. Only we
        // hold the reason -- the depth cap and the cycle guard are OUR gateway's
        // decisions, taken against a registry the pane never sees -- so a refusal
        // we keep to ourselves reads to the user as a crew that connected and
        // then silently failed to appear.
        paneLog('chain-refused', { parentId, port, error: reason || 'unknown' })
        const refusal = { type: CHAINED_CREW_REFUSED_MESSAGE, v: 1, id: remoteId, reason }
        if (relayMode) {
          // Relay: over the pane's AUTHENTICATED document port, like every other
          // downward message. This refusal arrives asynchronously, after awaited
          // owner-side calls, and the announcing document may have navigated away
          // by then: a wildcard post would hand the live channel to whatever
          // replacement now occupies the frame, while the port only reaches the
          // document that authenticated. A rotated or missing port drops it; the
          // pane's Remote Crew panel already showed the gateway's own reason.
          sendDownRelay(parentId, refusal)
          return
        }
        const el = iframeRefs.current.get(parentId)
        const w = warmRef.current[parentId]
        // Direct mode: the pane's exact loopback origin. We only reach here from a
        // message a pane sent, which resolvePaneMessage already attributed, so the
        // pane is warm and addressable.
        const env = w ? paneMessageEnvelope(paneMode, w, refusal) : null
        if (el?.contentWindow && env) {
          try {
            el.contentWindow.postMessage(env.message, env.targetOrigin)
          } catch {
            /* frame mid-navigation: the panel keeps the connect it already reported */
          }
        }
      }
    },
    [queryClient, dispatch, paneMode, relayMode, sendDownRelay],
  )

  // Pre-mint + warm one connected instance without surfacing it. Cheap when the
  // backend already auto-reconnected the tunnel (connect() returns the cached
  // token without re-minting). Failures are swallowed: the sticky tab + in-pane
  // error/Retry panel handle an instance that can't be warmed.
  //
  // Connected-only, on purpose: auto-warm pre-mounts panes for tunnels that are
  // ALREADY up; bringing one up is the fan-out's and the click's job. The
  // gateway enforces that under its manager lock, so a warm that fires after
  // the user disconnected the crew (the stagger below makes that window real)
  // is declined server-side instead of re-opening the tunnel and re-persisting
  // the intent the disconnect just cleared.
  const autoWarm = useCallback(
    async (id: string) => {
      try {
        // The shared connect step journals its own outcome (`warm` /
        // `warm-declined` / `warm-failed`, tagged via=auto-warm), so a failed
        // auto-warm is no longer untraceable: it used to leave any PREVIOUS warm
        // entry in place with nothing in the log, and the user saw only
        // "loading" forever.
        await connectInstanceInto(dispatch, id, 'auto-warm', { onlyIfConnected: true })
      } catch {
        // Already journaled by connectInstanceInto; the sticky tab + in-pane
        // panel handle an instance that cannot be warmed.
      }
    },
    [dispatch],
  )

  // Origin→id map for the relay listener, read from the store at message time.
  // The store is current the instant `setWarm` dispatches; a render and its
  // passive effects come later. A fast loopback iframe can post its first
  // `mc-embedded-boot` inside that gap, so a map refreshed by an effect on
  // `warm` would still lack the new port and drop the boot as unattributed.
  const store = useAppStore()
  // The attribution context read at message time (see lib/paneChannel). Direct
  // mode maps a loopback port → id; relay mode maps id → endpoint (for the
  // channel match) and id → live contentWindow (for the exact-frame check). Read
  // from the store, not a render-time snapshot: a fast pane can post its first
  // message before an effect that watches `warm` would refresh a memoized map.
  const paneMessageCtx = useCallback((): PaneMessageContext => {
    const warmNow = store.getState().instances.warm
    if (!relayMode) {
      const portToId = new Map<number, string>()
      for (const [id, w] of Object.entries(warmNow)) {
        if (w.kind === 'direct-loopback') portToId.set(w.port, id)
      }
      return { portToId }
    }
    const endpoints = new Map<string, PaneEndpoint>(Object.entries(warmNow))
    const frames = new Map<string, Window>()
    for (const [id, el] of iframeRefs.current) {
      const cw = el.contentWindow
      if (cw) frames.set(id, cw)
    }
    return { endpoints, frames }
  }, [store, relayMode])

  // Drop relayed drag gaps for panes that are no longer warm, so the map cannot
  // grow without bound and a re-warmed pane starts from its own fresh report.
  useEffect(() => {
    setDragGaps(prev => {
      const next: Record<string, DragGap[]> = {}
      let changed = false
      for (const id of Object.keys(prev)) {
        if (warm[id]) next[id] = prev[id]
        else changed = true
      }
      return changed ? next : prev
    })
    for (const id of iframeRefCallbacks.current.keys()) {
      if (!warm[id]) iframeRefCallbacks.current.delete(id)
    }
    // The one-shot script-error heal is per LOAD: an evicted pane that re-warms
    // is a new load and gets its budget back, otherwise its next poisoned-cache
    // failure could only be healed by a manual Retry.
    for (const id of scriptErrorHealsRef.current) {
      if (!warm[id]) scriptErrorHealsRef.current.delete(id)
    }
    // Retire the authenticated downward port of any pane that is no longer warm
    // (eviction / disconnect / delete) OR whose endpoint rotated to a new channel
    // (lease renewal / Retry). Closing it here — not only lazily on the next send
    // — keeps the map bounded and guarantees a superseded document's port can
    // never be reused for the new generation. The replacement document re-binds a
    // fresh port through its own handshake.
    for (const [id, entry] of relayPortsRef.current) {
      const conn = warm[id]
      if (!conn || conn.kind !== 'same-origin-relay' || conn.channel !== entry.channel) {
        try { entry.port.close() } catch { /* already closed */ }
        relayPortsRef.current.delete(id)
      }
    }
    // Cancel the document-bootstrap watchdog of any pane no longer warm or whose
    // channel rotated (lease renewal / Retry): its generation is gone, and the
    // replacement document arms its own on its next fresh-port request. A stale
    // timer left running could otherwise fail a pane whose lease just rotated.
    for (const [id, w] of bootstrapWatchRef.current) {
      const conn = warm[id]
      if (!conn || conn.kind !== 'same-origin-relay' || conn.channel !== w.channel) {
        clearTimeout(w.timer)
        bootstrapWatchRef.current.delete(id)
      }
    }
  }, [warm])

  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      const data = e.data
      if (!data || typeof data !== 'object') return
      const ctx = paneMessageCtx()
      if (relayMode && (data as { type?: unknown }).type === RELAY_BOOTSTRAP_REQUEST) {
        // Every relay document — the first load and every subsequent navigation —
        // hands the parent one port of a fresh MessageChannel here. This message
        // carries NO channel (the document does not have it yet), so it is bound
        // by the EXACT sender frame + the capability `documentPath` the parent
        // issued (`resolvePaneBootstrapRequest`); `event.origin` (opaque `'null'`)
        // is never trusted. The parent adopts the transferred port, closes any
        // prior one for the pane, and replies OVER the port with the channel + a
        // bounded bank snapshot — and every later downward message rides that same
        // port. Because the port is entangled with the requesting document, a
        // successor document in the same iframe (its `WindowProxy` survives
        // navigation) receives neither the channel nor any host state, even though
        // it still matches `ctx.frames`.
        const grant = resolvePaneBootstrapRequest(
          {
            source: e.source,
            documentPath: (data as { documentPath?: unknown }).documentPath,
            nonce: (data as { nonce?: unknown }).nonce,
          },
          ctx,
        )
        const port = e.ports && e.ports[0]
        if (!grant) {
          // A well-formed transfer we could NOT authenticate (wrong frame, wrong
          // or unknown capability): close the port so nothing is ever delivered
          // over it. An off-capability replacement document lands here and gets
          // no channel and no host model.
          if (port) {
            try { port.close() } catch { /* already closed */ }
            paneLog('relay-bootstrap-refused', {})
          }
          return
        }
        // The child transfers its port on the FIRST ask and re-asks WITHOUT one on
        // its bounded retries (a port cannot be transferred twice). A fresh
        // transfer (re)binds — closing any prior port for the pane; a port-less
        // retry re-replies on the port already bound for this exact channel, so a
        // parent slow to reply the first time still answers on the one inbox the
        // child is listening on. A retry with no bound port yet (the first ask was
        // dropped before this listener wired) simply waits for the next ask.
        let entry = relayPortsRef.current.get(grant.id)
        if (port) {
          if (entry && entry.port !== port) {
            try { entry.port.close() } catch { /* already closed */ }
          }
          entry = { port, channel: grant.channel }
          relayPortsRef.current.set(grant.id, entry)
          // (Re)arm the document-bootstrap watchdog for THIS generation. A fresh
          // transferred port is a NEW document, and the initial load watchdog
          // cannot cover a navigation inside an already-ready pane: that pane
          // keeps `ready[id]` true, so the readiness-gated watchdog never arms,
          // and a new document that authenticated its port here but then went
          // silent before announcing readiness would hang invisibly — the same
          // exhaustion the initial watchdog exists to catch. Track it here,
          // independent of readiness and bound to the document's nonce + channel:
          //  - cancel any prior generation's timer first, so an OLD document's
          //    timer can never fire against and fail this NEW one;
          //  - reset any stale verdict, so the fresh document gets a clean window;
          //  - arm one that, on expiry, surfaces the existing Retry panel WITHOUT
          //    clearing `ready[id]` (which would repopulate the iframe `name` seed
          //    and re-arm the initial watchdog).
          // The clear rides the ordinary `mc-embedded-ready` path below and needs
          // no per-message nonce: the prior document was destroyed by the
          // navigation and had already been acked, so every message it ever posted
          // was enqueued before this fresh-port request and delivered before this
          // timer was armed — a readiness that arrives AFTER the arm is necessarily
          // this generation's.
          const prevWatch = bootstrapWatchRef.current.get(grant.id)
          if (prevWatch) clearTimeout(prevWatch.timer)
          const bootId = grant.id
          const bootNonce = grant.nonce
          const bootChannel = grant.channel
          setBootstrapFailed(prev => {
            if (!(bootId in prev)) return prev
            const next = { ...prev }
            delete next[bootId]
            return next
          })
          const timer = setTimeout(() => {
            // Only the CURRENT generation may fail the pane. A superseded timer
            // was cleared when its replacement armed; the nonce guard is the belt
            // to that suspenders, so a stale fire can never mark a newer document
            // failed.
            const cur = bootstrapWatchRef.current.get(bootId)
            if (!cur || cur.nonce !== bootNonce) return
            bootstrapWatchRef.current.delete(bootId)
            setBootstrapFailed(prev =>
              prev[bootId] === bootChannel ? prev : { ...prev, [bootId]: bootChannel },
            )
            paneLog('relay-bootstrap-timeout', { id: bootId })
          }, PANE_LOAD_TIMEOUT_MS)
          bootstrapWatchRef.current.set(bootId, { nonce: bootNonce, channel: bootChannel, timer })
        } else if (!entry || entry.channel !== grant.channel) {
          return
        }
        const banks = relayBanks()
        try {
          entry.port.postMessage({
            type: RELAY_BOOTSTRAP_REPLY,
            nonce: grant.nonce,
            channel: grant.channel,
            parentOrigin: window.location.origin,
            protocol: grant.protocol,
            storage: {
              local: banks.local.snapshot(grant.id),
              session: banks.session.snapshot(grant.id),
            },
          })
          paneLog('relay-bootstrap-reply', { id: grant.id })
        } catch {
          /* port closed before the reply — the child's document was replaced */
        }
        return
      }
      // One attribution authority for both transports (see lib/paneChannel):
      // direct-loopback validates the exact loopback origin + a port we own;
      // same-origin-relay binds on the per-pane channel AND the exact
      // contentWindow, never on event.origin (the opaque `'null'`).
      const id = resolvePaneMessage({ source: e.source, origin: e.origin, data }, paneMode, ctx)
      if (!id) {
        // Unattributed. In DIRECT mode a readiness/boot announce from a loopback
        // origin this parent no longer maps to a warm pane is the handshake being
        // dropped on the floor (the warm entry moved to another port or was
        // evicted): journal only those two types -- the child sends ready at most
        // six times and boot exactly three per load, so the lines cannot flood --
        // which keeps "no `boot` line" a trustworthy reading of "no JavaScript of
        // ours ran in that frame". In RELAY mode event.origin is the opaque
        // `'null'` and carries no port, and a message is unattributed only when
        // its channel is stale/absent or its frame foreign, so nothing is logged.
        if (!relayMode && parseLoopbackOriginPort(e.origin) !== null) {
          const knownPorts = [...(ctx.portToId ?? new Map<number, string>()).keys()].join(',')
          if (data.type === 'mc-embedded-ready') {
            paneLog('ready-unattributed', { origin: e.origin, knownPorts })
          } else if (data.type === 'mc-embedded-boot') {
            paneLog('boot-unattributed', {
              origin: e.origin,
              stage: typeof data.stage === 'string' ? data.stage : 'unknown',
              knownPorts,
            })
          }
        }
        return
      }
      if (data.type === RELAY_STORAGE_MESSAGE) {
        // A relay pane reported a Web Storage mutation (the opaque origin can
        // persist nothing itself). Apply it to the parent's per-instance bank,
        // which enforces the caps and keeps one crew's keys isolated from
        // another's. Relay-only; a direct pane never sends it. A bad area or
        // mutation shape is dropped, never thrown — a child message must not
        // raise in the parent's message loop.
        if (!relayMode) return
        const area = (data as { area?: unknown }).area
        if (area !== 'local' && area !== 'session') return
        // Parse the UNTRUSTED payload into the exact mutation union before the
        // bank sees it: a missing field, a wrong type, or an unknown op is
        // dropped here rather than cast, and the bank's key encoding is
        // exception-safe, so neither a malformed shape nor an unmatched-surrogate
        // key can raise in this message listener. A later valid mutation still
        // applies.
        const mutation = parseRelayStorageMutation((data as { mutation?: unknown }).mutation)
        if (mutation === null) return
        relayBanks()[area].apply(id, mutation)
        return
      }
      if (data.type === 'mc-unread-slots') {
        const count = Number(data.count)
        if (!Number.isFinite(count) || count < 0) return
        dispatch(setUnread({ id, count }))
      } else if (data.type === NATIVE_NOTIFY_TYPE) {
        // A pane wants an OS banner it cannot post itself: `notifications` is a
        // main-frame-only permission (permission-handler.js) and a browser tab
        // denies it to a cross-origin iframe too. This frame holds the grant, so
        // it posts on the pane's behalf. The SENDER is already trusted -- its
        // origin resolved to a currently-warm tunnel port above -- and the pane
        // has already applied its own mute / hidden / silent rules, so the only
        // checks here are shape (every field the exact expected type, bounded)
        // and this frame's own permission. The title is prefixed with the
        // instance's name and the tag namespaced per instance id so several
        // crews' notes stay distinguishable and never collapse onto one.
        const note = parseNativeNotifyEnvelope(data)
        if (!note) return
        const name = instancesRef.current.find(i => i.id === id)?.name || id
        // Clicking the banner brings the named crew forward, not whichever tab
        // happened to be active; the id is a warm instance, so the switch is
        // the same one the inline switcher would honour.
        postRelayedNativeNotification(name, id, note, () => {
          window.focus()
          dispatch(setActiveId(id))
        })
      } else if (data.type === 'mc-auth-expired') {
        // Reactive recovery: the embedded dashboard reported an expired session.
        // Force a fresh mint and reload its iframe rather than letting it show
        // the in-pane paste-token banner. No foreground guard here — the active
        // pane is exactly the one the user wants restored.
        //
        // Bounded, though: a session a fresh token cannot repair re-asks on
        // every 403 forever (the child hands off before latching its own
        // banner), which is one SSH mint every REFRESH_MIN_INTERVAL_MS with
        // nothing to show for it. Count the mints actually issued and go quiet
        // once a pane has burned MAX_REACTIVE_REMINTS of them.
        const spent = reactiveMintsRef.current.get(id) || 0
        if (spent >= MAX_REACTIVE_REMINTS) {
          // Going quiet is not enough on its own: this ask can arrive while the
          // pane is READY (its shell mounted, only its API is 403ing), and both
          // affordances are off in that state — the load watchdog below skips a
          // ready pane, and the child has already latched its own hand-off so it
          // shows no banner either. Dropping the ask silently would leave a
          // live-looking pane serving stale content with no way out, the same
          // dead end this fix is about. Retract readiness and record the verdict
          // so the error panel carrying Retry surfaces instead.
          dispatch(clearPaneReady(id))
          setTimedOut(prev => (prev[id] ? prev : { ...prev, [id]: true }))
          paneLog('remint-budget-exhausted', { id, spent })
          return
        }
        // Charge the budget only for asks that are actually answered with a mint.
        // A pane can post several asks inside one rate window — a 200 landing
        // mid-reload re-arms the child's hand-off latch, so a 403 from a poll
        // that started before the reload posts again seconds later — and
        // refreshToken drops those. Counting them anyway would spend the budget
        // on mints that never happened and show the panel after one real retry
        // instead of MAX_REACTIVE_REMINTS.
        if (!canRefreshNow(id)) return
        reactiveMintsRef.current.set(id, spent + 1)
        paneLog('auth-expired', { id, spent: spent + 1 })
        void refreshToken(id)
      } else if (data.type === 'mc-switch-instance') {
        // The embedded pane's inline switcher asks the parent to flip
        // the active tab. The SENDER is already trusted (its origin resolved to a
        // warm tunnel above); validate the TARGET is Local (null) or a known
        // instance before honoring it.
        const target = (data as { id?: unknown }).id
        if (target === null) {
          dispatch(setActiveId(null))
        } else if (
          typeof target === 'string' &&
          (instancesRef.current.some(i => i.id === target) || !!warmRef.current[target])
        ) {
          dispatch(setActiveId(target))
        }
      } else if (data.type === CHAINED_CREW_MESSAGE) {
        // A pane connected a crew of its own. Its gateway is a crew of OURS, so
        // that further crew is reachable from here only by riding the hop we
        // already hold to the pane — and we never see the pane's registry, which
        // is why it has to tell us.
        //
        // The SENDER is already trusted: its origin resolved to a currently-warm
        // tunnel port above, and `id` is that pane's instance id here, which is
        // the parent of the chain. The PAYLOAD is not trusted — shape-checked
        // here, and the gateway then applies the depth cap and the cycle guard,
        // which are the decisions no frame may make.
        void adoptChainedCrew(id, data)
      } else if (data.type === 'mc-set-crew-pin') {
        // A pin was toggled inside an embedded pane. It has no access to the
        // parent's preference store from its own iframe realm, so it relays the
        // crew id here; applying it broadcasts to every bar (local header + all
        // panes) via the module store, keeping the set one shared value.
        const id = (data as { id?: unknown }).id
        if (typeof id === 'string' && id) toggleCrewPin(id)
      } else if (data.type === 'mc-set-stable-order') {
        // The "keep tab order fixed" toggle was flipped inside an embedded pane.
        // Like the pin, it has no access to the parent's preference store from
        // its own iframe realm, so it relays the desired value here; applying it
        // broadcasts to every bar (local header + all panes) via the module
        // store, keeping the preference one shared value. Idempotent, so the
        // model re-broadcast's return trip to the sending pane is a no-op.
        const on = (data as { on?: unknown }).on
        if (typeof on === 'boolean') setStableOrder(on)
      } else if (data.type === 'mc-set-focus-mode') {
        // Focus mode was toggled inside an embedded pane. It belongs to the WINDOW,
        // not to one pane, so applying it here is what makes the state one shared
        // value: the module store re-renders the local header's own toggle, and the
        // model re-broadcast below carries it to every OTHER pane. The pane that
        // sent it already adopted it locally, and the setter is idempotent, so the
        // return trip is a no-op rather than a loop.
        const on = (data as { on?: unknown }).on
        if (typeof on === 'boolean') setFocusModeEnabled(on)
      } else if (data.type === 'mc-focus-chrome') {
        // The pane reports whether ITS chrome is on screen. Only the pane the user
        // is actually looking at may speak for the window: a background pane's peek
        // must not summon the host's traffic lights over a different pane. The host
        // is the only side that can act on this at all — the lights are AppKit
        // views on this window and the drag bar lives in this document.
        // Every pane's report is REMEMBERED (not just the active one's): the
        // switch-time effect below needs the incoming pane's last-known state,
        // because a pane whose chrome state did not change re-posts nothing.
        const on = (data as { on?: unknown }).on
        if (typeof on === 'boolean') {
          paneChromeRef.current[id] = on
          if (id === activeIdRef.current) setFocusChromeVisible(on)
        }
      } else if (data.type === CURSOR_AWAY_WATCH_TYPE) {
        // A pane's focus-mode reveal wants to know how far the cursor travels
        // off-window. It has no preload to ask the main process itself, so this
        // frame watches on its behalf. Same rule as mc-focus-chrome: only the
        // pane the user is looking at may arm it — a background pane gets no
        // answer. The frame check pins the requester to that pane's own iframe,
        // which is also where the answer goes. A host with no bridge (a browser)
        // stays silent the same way.
        const watchId = (data as { id?: unknown }).id
        if (typeof watchId !== 'string' || !watchId || watchId.length > 64) return
        if (data.v !== CURSOR_AWAY_VERSION || id !== activeIdRef.current) return
        const frame = iframeRefs.current.get(id)?.contentWindow
        if (!frame || e.source !== frame) return
        // One watch per window: the main process polls once per window, so a
        // newer request supersedes whatever was pending.
        stopPaneCursorWatch()
        const requestOrigin = e.origin
        const reply = (msg: Record<string, unknown>) => {
          const payload = { v: CURSOR_AWAY_VERSION, id: watchId, ...msg }
          try {
            if (relayMode) {
              // Relay: answer over the pane's AUTHENTICATED document port, never a
              // wildcard post to the frame — the reply must reach only the
              // document that armed the watch, not a successor in the same iframe.
              sendDownRelay(id, payload)
            } else {
              // Direct: answer the EXACT origin the watch arrived on, unchanged
              // from before the relay — the frame's own loopback origin.
              frame.postMessage(payload, requestOrigin)
            }
          } catch {
            /* frame mid-navigation — nothing left to answer */
          }
        }
        const stop = watchCursorAwayNative(away => {
          if (paneCursorWatchRef.current?.watchId === watchId) paneCursorWatchRef.current = null
          reply({ type: CURSOR_AWAY_RESULT_TYPE, away })
        })
        if (!stop) return
        paneCursorWatchRef.current = { paneId: id, watchId, stop }
      } else if (data.type === CURSOR_AWAY_CANCEL_TYPE) {
        const watchId = (data as { id?: unknown }).id
        const live = paneCursorWatchRef.current
        if (live && live.paneId === id && live.watchId === watchId) stopPaneCursorWatch()
      } else if (data.type === 'mc-embedded-boot') {
        // The pane's bundle EXECUTED (posted from main.tsx before React renders,
        // see EmbeddedHostBridge for the ready half). This line splits the one
        // failure the journal could not: a pane whose shell loaded (200,
        // cross-origin) but never announced readiness was either a bundle that
        // never ran (no `boot`) or an App that mounted and got stuck before the
        // bridge (a `boot` with no `ready`). Journal only, no state change --
        // except `script-error`, below.
        const stage = typeof data.stage === 'string' ? data.stage : 'unknown'
        const src = typeof data.src === 'string' ? safePaneUrl(data.src) : undefined
        paneLog('boot', { id, stage, src })
        if (stage === 'script-error') {
          // Posted by the pane's index.html shell (not its bundle, which never
          // ran): the entry <script type=module> fired `error`, i.e. some chunk
          // in its graph failed to FETCH. Whatever happens next, this pane is
          // NOT ready: it may have been (a ready pane reloads itself after a
          // bundle change and can land on a mid-swap 404), and a stale `ready`
          // keeps the load watchdog off and the error panel unreachable, so a
          // failed heal would leave a blank pane with no Retry. Retract first.
          // The one cause a reload cannot fix by itself is a 404 the desktop's
          // HTTP cache is replaying under this origin, so evict that origin's
          // cache and reload once. ONCE: a 404 the gateway is really serving
          // right now (dist/ mid-swap) would otherwise loop clear/reload
          // forever; after the one attempt the (now armed) watchdog surfaces
          // the error panel, and Retry re-opens the budget.
          dispatch(clearPaneReady(id))
          if (!scriptErrorHealsRef.current.has(id)) {
            scriptErrorHealsRef.current.add(id)
            paneLog('script-error-heal', { id, origin: e.origin })
            const cleared = clearPaneHttpCache(e.origin)
            const reload = () => setReloadSeq(prev => ({ ...prev, [id]: (prev[id] || 0) + 1 }))
            if (cleared) void cleared.then(reload, reload)
            else reload()
          }
        }
      } else if (data.type === 'mc-embedded-ready') {
        // The pane just (re)mounted and asked for the current model — send it now
        // rather than waiting for the next input-driven broadcast. Also record
        // readiness: this is the parent's only proof the pane actually loaded
        // (drives the loading overlay + load watchdog below).
        // NOTE: deliberately does NOT clear reactiveMintsRef — this fires on
        // mount, before the pane's first authenticated request, so it is no
        // evidence the token works. See MAX_REACTIVE_REMINTS.
        dispatch(setPaneReady(id))
        paneLog('ready', { id })
        // This document reached app-readiness: clear its bootstrap watchdog and
        // any failed verdict for it. The readiness is necessarily the CURRENT
        // document's (see the bootstrap-request handler's arming note), so no
        // per-message nonce is needed to attribute it.
        const bw = bootstrapWatchRef.current.get(id)
        if (bw) {
          clearTimeout(bw.timer)
          bootstrapWatchRef.current.delete(id)
        }
        setBootstrapFailed(prev => {
          if (!(id in prev)) return prev
          const next = { ...prev }
          delete next[id]
          return next
        })
        postModelToRef.current(id)
        // Distinct ack so the pane can stop re-announcing. Sent only here (after
        // readiness is recorded), never from the broadcast, so a late announce
        // can't revive readiness once this pane has been given up on.
        postAckToRef.current(id)
      } else if (data.type === 'mc-drag-gaps') {
        // The embedded pane relays the control-free spans of its header so the
        // host can re-add `-webkit-app-region: drag` there (the blanket marks
        // the whole iframe no-drag). Sanitize: finite, positive-width spans
        // only, capped so a malformed/hostile pane can't flood the render.
        const raw = Array.isArray((data as { gaps?: unknown }).gaps)
          ? ((data as { gaps: unknown[] }).gaps)
          : []
        const gaps: DragGap[] = []
        for (const g of raw) {
          if (!g || typeof g !== 'object') continue
          const x = Number((g as { x?: unknown }).x)
          const w = Number((g as { w?: unknown }).w)
          if (!Number.isFinite(x) || !Number.isFinite(w) || x < 0 || w <= 0) continue
          gaps.push({ x, w })
          if (gaps.length >= 32) break
        }
        setDragGaps(prev => ({ ...prev, [id]: gaps }))
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [dispatch, refreshToken, canRefreshNow, paneMessageCtx, stopPaneCursorWatch, adoptChainedCrew, paneMode, relayMode, relayBanks, sendDownRelay])

  // Proactive refresh: when an embedded token passes REFRESH_AT_ELAPSED_FRAC of
  // its TTL, re-mint and reload that iframe ahead of the cap. Skips the active
  // tab so a reload never interrupts the pane in use (the reactive path above
  // covers the active tab). Driven by the 60s instances poll.
  useEffect(() => {
    const data = instancesQuery.data
    if (!data) return
    for (const inst of data.instances) {
      const id = inst.id
      if (!warm[id] || id === activeId) continue
      if (inst.status?.state !== 'connected') continue
      const remaining = inst.status?.token_ttl_remaining
      const total = tokenTtlTotalSeconds(inst.status, inst.ttl)
      if (typeof remaining !== 'number' || total <= 0) continue
      if (remaining > total * (1 - REFRESH_AT_ELAPSED_FRAC)) continue
      void refreshToken(id)
    }
  }, [instancesQuery.data, warm, activeId, refreshToken])

  // Proactive relay-lease renewal: schedule one timer per warm same-origin-relay
  // pane to reissue before its lease deadline (see renewRelayLease). Re-runs
  // whenever `warm` changes — a completed renewal writes a new endpoint with a
  // later deadline, which reschedules the timer against the fresh lease; a
  // disconnect/eviction drops the entry and its timer. Direct-loopback panes
  // carry no lease and are skipped, so direct mode schedules nothing.
  useEffect(() => {
    if (!relayMode) return
    const timers = relayRenewTimersRef.current
    // Reschedule from scratch on every `warm` change: the cleanup below cleared
    // any pending first attempt OR bounded retry (cancel-on-change), and here we
    // arm one fresh first attempt per current relay pane against its live lease.
    // A completed renewal writes a new endpoint, which re-runs this effect and
    // reschedules against the later deadline; a disconnect/eviction drops the
    // entry (and its timer) here. Direct-loopback panes carry no lease and are
    // skipped. The callback is reached through a ref so this effect does not
    // depend on its identity (which would churn the schedule needlessly).
    const now = Date.now()
    for (const [id, conn] of Object.entries(warm)) {
      if (conn.kind !== 'same-origin-relay') continue
      const delay = relayRenewDelayMs(now, conn.leaseExpiresAtEpochMs)
      const channel = conn.channel
      const deadline = conn.leaseExpiresAtEpochMs
      timers.set(
        id,
        setTimeout(() => void renewRelayLeaseRef.current(id, channel, deadline), delay),
      )
    }
    return () => {
      for (const [, t] of timers) clearTimeout(t)
      timers.clear()
    }
  }, [warm, relayMode])

  // Retry connect from the in-pane error panel: re-mint a token and warm the
  // iframe. Idempotent on the backend (the tunnel is often already live after a
  // startup auto-reconnect), so this mainly restores the browser-side token.
  const connectMutation = useMutation({
    // The shared connect step writes the warm entry and journals the outcome
    // (via=retry). Retry can "succeed" as a request while warming nothing, and
    // the pane then reloads into the same stuck state — that case is the
    // `warm-declined` line the step emits.
    mutationFn: ({ id, rebuild }: { id: string; rebuild: boolean }) =>
      connectInstanceInto(dispatch, id, 'retry', { rebuild }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['instances'] })
    },
  })

  // Load watchdog + forced reload. `timedOut[id]` flips true when the active
  // pane's iframe has been loading for PANE_LOAD_TIMEOUT_MS without the
  // embedded SPA announcing readiness; the render below then swaps the black
  // pane for the error panel (which carries the tab strip, so the user is
  // never stranded). `reloadSeq[id]` is bumped by Retry to force an iframe
  // remount even when the backend returns the SAME cached port+token (identical
  // src would otherwise not reload a dead frame).
  // (`timedOut`, `reloadSeq` and `scriptErrorHealsRef` are declared up by
  // reactiveMintsRef: the relay listener above reaches them too.)
  // Live mirror of `timedOut` for `retry`, which must read the verdict at press
  // time without re-creating itself on every watchdog flip.
  const timedOutRef = useRef(timedOut)
  timedOutRef.current = timedOut
  const activeWarmConn = activeId ? warm[activeId] : undefined
  // Apply the INCOMING pane's chrome state at switch time. The store otherwise
  // keeps whatever the outgoing surface last reported — and a switch necessarily
  // happens from a peeked header (the tab bar lives on it), so it is `true` —
  // while the incoming pane, whose own state did not change, re-posts nothing.
  // Symptom fixed: traffic lights stranded visible over the new pane until its
  // next hover cycle. A pane with NO recorded report defaults to VISIBLE: remote
  // crews are independently versioned installs, so a pane that has never posted
  // mc-focus-chrome is most likely a pre-focus-mode version that renders its full
  // header unconditionally — defaulting it to hidden would strip the traffic
  // lights and drag strips out from under a header the user can see. A
  // focus-mode-aware pane's first report corrects the brief lights-flash; a
  // non-conforming pane keeps working chrome forever. Switching to LOCAL is
  // covered by App.tsx's own writer (gated on activeInstanceId === null).
  useEffect(() => {
    // Only while focus mode is ON: off, chrome is unconditionally visible and
    // owned by the surfaces themselves (and the local writer in App.tsx).
    if (activeId === null || !focusMode) return
    setFocusChromeVisible(paneChromeRef.current[activeId] ?? true)
  }, [activeId, focusMode])
  // Primitive deps for the watchdog effect (a fresh conn object identity on
  // every setWarm would defeat the dep comparison).
  // Load identity for the watchdog: the port in direct mode, the capability
  // channel in relay mode. A new value is a new load and legitimately restarts
  // the clock; a token re-mint (direct) keeps the same port, so it is NOT part
  // of it. BOTH are primitives (never the conn object) so a fresh conn identity
  // on every setWarm does not defeat the effect's dep comparison and re-arm the
  // countdown-from-scratch — the invariant the watchdog depends on.
  const activeLoadKey = warmLoadKey(activeWarmConn)
  const activeWarmPort = warmPort(activeWarmConn)
  const activeReady = activeId ? !!ready[activeId] : true
  const activeSeq = activeId ? reloadSeq[activeId] || 0 : 0
  // The watchdog's countdown is anchored to the identity of the LOAD — (id, port,
  // reloadSeq) — and NOT to the iframe src. A token re-mint also changes the src,
  // but the token is deliberately ABSENT from the deps below, so a re-mint neither
  // clears nor restarts the pending timer: it keeps ticking against the deadline
  // the load began with. That absence is the fix. Refreshes are rate-limited to
  // REFRESH_MIN_INTERVAL_MS, which is SHORTER than PANE_LOAD_TIMEOUT_MS, so while
  // the token WAS a dep a pane stuck in an `mc-auth-expired` -> re-mint loop
  // restarted a countdown-from-scratch every 10s and could never reach 15s.
  // Symptom: the loading overlay spun forever and the error panel — the only
  // affordance carrying Retry and the tab strip — could never surface, stranding
  // the user on a pane that looked merely slow. An explicit Retry or a real port
  // change is what legitimately restarts the clock. A re-mint that DOES load still
  // clears the verdict, because `activeTimedOut` additionally requires
  // `!activeReady`.
  useEffect(() => {
    // Arm whenever the active pane is warm but not yet ready, in either mode. A
    // relay pane mounts a real same-origin src and announces readiness through
    // its channel exactly like a direct pane, so the watchdog is the same signal
    // for both — there is no longer an unembeddable case to surface separately.
    if (!activeId || activeLoadKey === undefined || activeReady) return
    const id = activeId
    const port = activeWarmPort
    // The countdown STARTING is journaled too, not only its expiry. A pane that
    // shows "loading" forever without ever producing `load-timeout` is a pane
    // whose watchdog never armed — because this effect saw `activeReady` as
    // true while the overlay used a different verdict, or because it re-armed
    // in a loop — and the absence of an arm line is what says so.
    paneLog('watchdog-armed', { id, port, loadKey: activeLoadKey, seq: activeSeq })
    const t = window.setTimeout(() => {
      setTimedOut(prev => (prev[id] ? prev : { ...prev, [id]: true }))
      // `frame` is the verdict: `cross-origin` (direct) / a live document (relay)
      // means the pane really loaded and then failed to announce readiness, while
      // `about:blank` means it never navigated at all and no bundle could run.
      paneLog('load-timeout', {
        id,
        port,
        loadKey: activeLoadKey,
        seq: activeSeq,
        afterMs: PANE_LOAD_TIMEOUT_MS,
        frame: frameDocumentState(iframeRefs.current.get(id)),
      })
    }, PANE_LOAD_TIMEOUT_MS)
    return () => window.clearTimeout(t)
  }, [activeId, activeLoadKey, activeWarmPort, activeSeq, activeReady])

  // See iframeRefCallbacks: the callback is created once per id and reused
  // across renders, so React invokes it only on a real attach/detach. It reads
  // the reload seq through a ref because a closure over `reloadSeq` state would
  // force a new identity per change -- exactly the churn this avoids; Retry
  // remounts via the element KEY, which detaches and re-attaches for real, so
  // the seq journaled at attach time is already the new one.
  const reloadSeqRef = useRef(reloadSeq)
  reloadSeqRef.current = reloadSeq
  const iframeRefFor = useCallback((id: string) => {
    let cb = iframeRefCallbacks.current.get(id)
    if (!cb) {
      cb = (el: HTMLIFrameElement | null) => {
        if (el) {
          iframeRefs.current.set(id, el)
          // The mount is the moment the src is committed to a live frame.
          // An empty `src` here means srcFor found no warm entry, which is
          // the one way the pane can end up parked on about:blank forever.
          paneLog('iframe-mounted', {
            id,
            port: warmPort(warmRef.current[id]),
            seq: reloadSeqRef.current[id] || 0,
            src: safePaneUrl(el.getAttribute('src')),
          })
        } else {
          iframeRefs.current.delete(id)
          paneLog('iframe-unmounted', { id })
        }
      }
      iframeRefCallbacks.current.set(id, cb)
    }
    return cb
  }, [])

  const retry = useCallback(
    (id: string) => {
      const frame = frameDocumentState(iframeRefs.current.get(id))
      // A watchdog verdict on a document that DID navigate is the one case a
      // plain reconnect cannot fix: the tunnel answers every probe, so the
      // idempotent connect returns the same forwarder, and the pane reloads
      // into the same stalled module graph (one hashed-chunk stream that never
      // finishes over that TCP path). Ask the gateway to rebuild the tunnel —
      // a new forwarder on a different local port (the freed one is excluded
      // from the allocation) — so the reload rides neither the stalled stream
      // nor whatever may be wrong with the old port. A pane
      // that never navigated (`about:blank`) or whose connect itself failed
      // keeps the cheap path: there is no stalled stream to escape.
      const rebuild = !!timedOutRef.current[id] && frame === 'cross-origin'
      const port = warmPort(warmRef.current[id])
      // Clear the stale verdict and force a reload even if the re-mint returns
      // an identical token (setWarm would be a no-op for the iframe src).
      paneLog('retry', {
        id,
        port,
        frame,
        rebuild: rebuild || undefined,
      })
      const proceed = () => {
        setTimedOut(prev => ({ ...prev, [id]: false }))
        // Clear any terminal lease-failure verdict too: Retry reissues the lease
        // (a fresh channel), so the old verdict no longer applies. The reissue's
        // new channel would stop matching anyway, but clearing here avoids a
        // flash of the panel while the new endpoint is still in flight.
        setRelayLeaseFailed(prev => {
          if (!(id in prev)) return prev
          const next = { ...prev }
          delete next[id]
          return next
        })
        // Clear any document-bootstrap failure + its watchdog too: Retry reissues
        // the lease (a fresh channel), so the verdict no longer applies. The new
        // channel would stop matching anyway, but clearing here avoids a flash of
        // the panel while the new endpoint is still in flight.
        const bw = bootstrapWatchRef.current.get(id)
        if (bw) {
          clearTimeout(bw.timer)
          bootstrapWatchRef.current.delete(id)
        }
        setBootstrapFailed(prev => {
          if (!(id in prev)) return prev
          const next = { ...prev }
          delete next[id]
          return next
        })
        setReloadSeq(prev => ({ ...prev, [id]: (prev[id] || 0) + 1 }))
        // An explicit user press is a fresh start: re-open the reactive budget so
        // a pane that recovers on the next token can still self-heal afterwards,
        // and the one-shot script-error heal likewise.
        reactiveMintsRef.current.delete(id)
        scriptErrorHealsRef.current.delete(id)
        connectMutation.mutate({ id, rebuild })
      }
      // Desktop only: evict this origin's HTTP cache BEFORE the reload, so a
      // chunk 404 the cache is replaying (the failure a tunnel rebuild cannot
      // reach) is re-fetched rather than replayed again. Resolves fast (one IPC
      // round-trip) and the reload proceeds whatever it answers. A pane that
      // never navigated has no port to name and nothing cached to evict.
      const cleared = typeof port === 'number' ? clearPaneHttpCache(paneOriginFor(port)) : null
      if (cleared) void cleared.then(proceed, proceed)
      else proceed()
    },
    [connectMutation],
  )

  // K-cap eviction drops only the least-recently-used non-active *warm iframe*
  // to free memory — it does NOT disconnect the tunnel or clear was_connected,
  // so the tab persists and re-warms instantly on next click. Tabs are removed
  // only by an explicit disconnect (InstancesPanel), never by eviction.
  useEffect(() => {
    const ids = Object.keys(warm)
    if (ids.length <= warmCap) return
    const victim = [...mru].reverse().find(id => id !== activeId && warm[id])
    if (victim) {
      // Journaled because eviction is the one teardown the user never asked
      // for: the tab stays, the tunnel stays, only the iframe goes — and the
      // next click re-warms it as a brand-new load. Without this line a pane
      // that was evicted and then failed to re-warm reads, in the log, like a
      // pane that never had a problem until it suddenly did.
      paneLog('evict', { id: victim, port: warmPort(warm[victim]), warmCount: ids.length, cap: warmCap })
      dispatch(removeWarm(victim))
    }
  }, [warm, warmCap, mru, activeId, dispatch])

  // Drop the per-pane load facts of a pane that is no longer warm. Both the
  // reactive budget and the timed-out verdict describe ONE load of ONE
  // connection; the connection they describe is gone (K-cap eviction above, an
  // explicit disconnect from InstancesPanel, a crew deleted), and the next
  // warm is a new load rather than a continuation of the dead one. Retry was
  // the only thing clearing either, and a re-warm is precisely the path that
  // does not go through Retry — so without this an exhausted-then-evicted pane
  // comes back with its budget already spent and its verdict already latched,
  // and renders "Pane failed to load" before its fresh iframe has had a chance
  // to load at all, having minted nothing. Keyed on `warm` because that is the
  // one signal every teardown path shares, whoever dispatched it.
  useEffect(() => {
    for (const id of reactiveMintsRef.current.keys()) {
      if (!warm[id]) reactiveMintsRef.current.delete(id)
    }
    setTimedOut(prev => {
      const stale = Object.keys(prev).filter(id => prev[id] && !warm[id])
      if (stale.length === 0) return prev
      const next = { ...prev }
      for (const id of stale) delete next[id]
      return next
    })
    // Same for the terminal lease-failure verdict: an evicted / disconnected /
    // deleted pane's failed lease is gone, and its next warm is a fresh load.
    // (A still-warm pane whose lease was replaced clears via the channel compare
    // in `activeRelayLeaseFailed`; this only drops verdicts for panes no longer
    // warm at all, so the map cannot grow without bound.)
    setRelayLeaseFailed(prev => {
      const stale = Object.keys(prev).filter(id => !warm[id])
      if (stale.length === 0) return prev
      const next = { ...prev }
      for (const id of stale) delete next[id]
      return next
    })
    // Same for the terminal document-bootstrap verdict: an evicted / disconnected
    // / deleted pane's failed generation is gone, and its next warm is a fresh
    // load. (A still-warm pane whose channel rotated clears via the channel
    // compare in `activeBootstrapFailed`; this only drops verdicts for panes no
    // longer warm at all, so the map cannot grow without bound.)
    setBootstrapFailed(prev => {
      const stale = Object.keys(prev).filter(id => !warm[id])
      if (stale.length === 0) return prev
      const next = { ...prev }
      for (const id of stale) delete next[id]
      return next
    })
  }, [warm])

  // Auto-warm on load: after the first instances poll, pre-mount every
  // currently-connected instance's iframe (up to the warm cap) so panes are
  // instantly usable after a gateway restart + page reload — the user never has
  // to click to re-establish a connection. We deliberately do NOT change
  // activeId: the dashboard always lands on the Local tab and the warmed iframes
  // sit hidden and ready. Down instances are skipped (they stay sticky error
  // tabs); this runs once per mount. warmRef avoids re-firing on warm changes.
  //
  // Staggered, not simultaneous. Each warm mounts an iframe that immediately
  // pulls a ~4 MB module graph (~240 hashed chunks) over a just-opened SSH
  // tunnel, and the tunnels themselves were raised seconds earlier by the
  // auto-connect fan-out. Four panes cold-loading in the same second is the
  // exact condition under which one stream stalled and its pane never
  // finished loading (see pane-asset-journal in the desktop shell). One warm
  // per AUTO_WARM_STAGGER_MS keeps the loads sequential enough that a single
  // tunnel's first bytes are not competing with three others' bulk transfer.
  // The user's active pane is never delayed by this: it is warmed by the
  // select path, not here.
  const autoWarmTimersRef = useRef<number[]>([])
  useEffect(() => () => { for (const t of autoWarmTimersRef.current) window.clearTimeout(t) }, [])
  const didAutoWarmRef = useRef(false)
  useEffect(() => {
    const data = instancesQuery.data
    if (!data || didAutoWarmRef.current) return
    didAutoWarmRef.current = true
    const room = Math.max(0, warmCap - Object.keys(warmRef.current).length)
    if (room <= 0) return
    const candidates = data.instances
      .filter(i => i.status?.state === 'connected' && !warmRef.current[i.id])
      .slice(0, room)
    // Timers live in a ref and are cleared only on unmount: this effect re-runs
    // on every instances poll (its deps include the query data), and a cleanup
    // returned from it would cancel the pending warms after the first poll
    // while the once-only guard above stops them from ever being re-armed.
    //
    // The delay opens a window the immediate fan-out never had: the user can
    // disconnect a crew before its timer fires. That race is closed on the
    // gateway, not here: autoWarm asks for a connected-only connect, which the
    // manager evaluates under the same lock disconnect holds, so a late warm
    // is declined (`warm-declined`) rather than re-opening the tunnel. The one
    // thing worth checking client-side is whether something else (a click)
    // already warmed the pane meanwhile — then the warm is simply redundant.
    autoWarmTimersRef.current = candidates.map((inst, i) =>
      window.setTimeout(() => {
        if (warmRef.current[inst.id]) {
          paneLog('auto-warm-skipped', { id: inst.id, index: i, alreadyWarm: true })
          return
        }
        if (i > 0) paneLog('auto-warm-staggered', { id: inst.id, index: i, delayMs: i * AUTO_WARM_STAGGER_MS })
        void autoWarm(inst.id)
      }, i * AUTO_WARM_STAGGER_MS),
    )
  }, [instancesQuery.data, warmCap, autoWarm])

  const warmIds = useMemo(() => Object.keys(warm), [warm])
  const srcFor = useCallback(
    (id: string) => {
      // The single pane-address authority (see lib/paneChannel). In
      // direct-loopback mode it builds the exact same URL as before — the parent
      // dashboard's OWN hostname (not a hardcoded 127.0.0.1) so the iframe is
      // ALWAYS same-site with the parent, or SameSite=Lax auth cookies are
      // withheld on its subrequests (parent on localhost + iframe on 127.0.0.1 =
      // cross-site -> 403 storm); that hostname resolves to the same loopback the
      // SSH forward binds. In same-origin-relay mode it is the capability
      // `documentPath` (a same-origin, root-relative path the hub serves) — never
      // a host:port URL and never the token. An empty src (no endpoint yet) fails
      // closed into the panel below rather than a frame that cannot load.
      return paneEndpointSrc(paneMode, warm[id])?.src ?? ''
    },
    [warm, paneMode],
  )

  // Build the switcher model relayed to the embedded pane `id`: the full tab
  // list (same rule as the local inline bar), which tab is active, this pane's
  // OWN tunnel status (for its readout capsule), and the platform insets.
  const buildModelFor = useCallback(
    (id: string) => {
      const insts = instancesQuery.data?.instances ?? []
      // Ordered as a tree, exactly as the local bar orders it, so a pane's own
      // switcher shows the same shape the window's does. A pane cannot derive
      // this: it never sees the host's registry.
      const tabs = chainRows(visibleInstanceTabs(insts, warm)).map(
        ({ inst: i, depth, parentName, reachable, brokenAt }) => ({
          id: i.id,
          name: i.name,
          sshHost: i.ssh_host,
          state: i.status?.state,
          unread: unread[i.id] || 0,
          depth,
          reachable,
          pathName: parentName
            ? i18nT('components.instanceTabBar.chain_via', { via: parentName, name: i.name })
            : '',
          pathParent: parentName,
          brokenAt,
        }),
      )
      const selfInst = insts.find(i => i.id === id)
      const self = selfInst
        ? {
            state: selfInst.status?.state,
            ttlRemaining: selfInst.status?.token_ttl_remaining,
            ttlTotal: tokenTtlTotalSeconds(selfInst.status, selfInst.ttl),
          }
        : null
      return {
        type: 'mc-host-model', v: 1, tabs, activeId, self, macInset, winInset, focusMode,
        electron: isElectron,
        // Array, not the Set itself: structured clone rejects a Set across this
        // boundary in some engines and the receiver validates element-wise anyway.
        pinnedCrews,
        stableOrder,
      }
    },
    [instancesQuery.data, warm, unread, activeId, macInset, winInset, focusMode, pinnedCrews, stableOrder],
  )

  // Post the model into one embedded pane through the one envelope authority:
  // the pane's exact loopback origin in direct mode (never '*'), or `'*'` + the
  // per-pane channel in relay mode (the exact-frame send plus the channel bind
  // it — an opaque frame's origin never matches a concrete string).
  const postModelTo = useCallback(
    (id: string) => {
      const w = warm[id]
      if (!w) return
      if (relayMode) {
        // Relay: deliver over the pane's AUTHENTICATED document port. Drops
        // silently until the pane's bootstrap handshake has bound one (the model
        // is (re)sent on `mc-embedded-ready`, which the app posts only after boot,
        // by which point the port is bound), and never falls back to a wildcard
        // post that a replacement document could receive.
        sendDownRelay(id, buildModelFor(id))
        return
      }
      const el = iframeRefs.current.get(id)
      if (!el?.contentWindow) return
      const env = paneMessageEnvelope(paneMode, w, buildModelFor(id))
      if (!env) return
      // In direct mode a frame still on about:blank inherits THIS origin, so a
      // post to the exact loopback origin is rejected with a target-origin
      // mismatch that Chromium logs as a console error the catch cannot see —
      // journal the frame's state. The post is still attempted (instrumentation only).
      const frame = frameDocumentState(el)
      if (frame !== 'cross-origin') paneLog('post-model-undeliverable', { id, origin: env.targetOrigin, frame })
      try {
        el.contentWindow.postMessage(env.message, env.targetOrigin)
      } catch (err) {
        /* frame mid-navigation — the next broadcast / ready ping retries */
        paneLog('post-model-threw', { id, origin: env.targetOrigin, frame, error: (err as Error)?.message || 'unknown' })
      }
    },
    [warm, buildModelFor, paneMode, relayMode, sendDownRelay],
  )
  postModelToRef.current = postModelTo

  // Acknowledge a pane's readiness announce, addressed to its exact loopback
  // origin like the model post. A dedicated message (not a model) so the pane
  // can distinguish "the parent recorded my readiness" from an ordinary model
  // broadcast — the pane stops re-announcing only on this. A dropped ack (frame
  // mid-navigation) is harmless: the pane's next re-announce re-triggers it.
  const postAckTo = useCallback(
    (id: string) => {
      const w = warm[id]
      if (!w) return
      if (relayMode) {
        // Relay: acknowledge over the authenticated document port, like the model.
        sendDownRelay(id, { type: 'mc-embedded-ack', v: 1 })
        return
      }
      const el = iframeRefs.current.get(id)
      if (!el?.contentWindow) return
      const env = paneMessageEnvelope(paneMode, w, { type: 'mc-embedded-ack', v: 1 })
      if (!env) return
      try {
        el.contentWindow.postMessage(env.message, env.targetOrigin)
      } catch {
        /* frame mid-navigation — the pane's next re-announce re-triggers this */
      }
    },
    [warm, paneMode, relayMode, sendDownRelay],
  )
  postAckToRef.current = postAckTo

  // The versioned `window.name` envelope seeded into a relay pane BEFORE it
  // navigates (via the iframe `name` attribute). It carries the per-pane channel
  // the parent authenticates the frame's messages by, the parent's own origin
  // (the opaque child cannot read it otherwise), the pane-relay protocol, and the
  // instance's current storage snapshot from the parent bank. `window.name` is
  // read synchronously at the first line of the injected pre-module bootstrap and
  // then cleared, so the channel/snapshot never linger in browser-visible state.
  // Direct panes get `''` — `window.name` stays untouched and native storage is
  // used, so direct mode is byte-identical.
  const relaySeed = useCallback(
    (id: string): string => {
      const w = warm[id]
      if (!relayMode || !w || w.kind !== 'same-origin-relay') return ''
      const banks = relayBanks()
      return buildBootstrapEnvelope({
        v: RELAY_ENVELOPE_VERSION,
        channel: w.channel,
        parentOrigin: window.location.origin,
        protocol: w.protocol,
        storage: { local: banks.local.snapshot(id), session: banks.session.snapshot(id) },
      })
    },
    [warm, relayMode, relayBanks],
  )

  instancesRef.current = instancesQuery.data?.instances ?? []

  // Broadcast the model to every warm pane whenever any input changes (active
  // tab, tunnel status, unread, inset). Cheap: each post is a structured clone
  // to a loopback frame.
  useEffect(() => {
    for (const id of Object.keys(warm)) postModelTo(id)
  }, [warm, activeId, unread, macInset, winInset, instancesQuery.data, postModelTo, pinnedCrews, stableOrder])

  // Keep warm iframes mounted across Local<->remote switches (hide-not-unmount).
  // Also render when the active tab is a remote instance with no warm iframe
  // ((re)connecting or down) so we can show the in-pane panel instead of a blank
  // pane. Bail only when there is nothing to show, or when embedded.
  const activeInst = activeId ? instancesQuery.data?.instances.find(i => i.id === activeId) : undefined
  // Surface the in-pane panel when the active tab has no warm iframe (down /
  // reconnecting) OR when it has a stale warm entry whose live tunnel is no
  // longer connected. Without the status check a mid-session drop would leave a
  // dead iframe on screen with no error/Retry affordance.
  // A MISSING activeInst (instances query still loading / refetching, or not yet
  // in the results) is treated as "no evidence of disconnection" so we never
  // flash the panel over a perfectly healthy warm iframe.
  const activeLive = !activeInst || activeInst.status?.state === 'connected'
  // Watchdog verdict for the active pane: only meaningful while it has still
  // not announced readiness (a late `mc-embedded-ready` clears the alarm).
  const activeTimedOut = activeId !== null && !!timedOut[activeId] && !activeReady
  // Terminal relay-lease failure for the active pane. Deliberately NOT gated on
  // `!activeReady` — a renewal-exhausted pane is usually ready — and matched
  // against the CURRENT channel so a reissue (renewal/Retry rotates the channel)
  // clears the verdict on its own. Direct panes have no lease and never set it.
  const activeRelayLeaseFailed =
    activeId !== null &&
    (() => {
      const c = warm[activeId]
      return c?.kind === 'same-origin-relay' && relayLeaseFailed[activeId] === c.channel
    })()
  // Terminal document-bootstrap failure for the active pane: a new document
  // authenticated its port but never announced readiness. Like the lease failure
  // it is deliberately NOT gated on `!activeReady` — the pane it strands is
  // usually one that WAS ready before it navigated to the new document — and it
  // is matched against the CURRENT channel so a reissue/Retry (which mints a new
  // channel) clears it on its own. Direct panes carry no channel and never set it.
  const activeBootstrapFailed =
    activeId !== null &&
    (() => {
      const c = warm[activeId]
      return c?.kind === 'same-origin-relay' && bootstrapFailed[activeId] === c.channel
    })()
  // The terminal "this pane cannot be served — offer Retry" verdicts. All render
  // the same recovery panel; a load that never announced readiness
  // (`activeTimedOut`), a lease that could no longer be renewed
  // (`activeRelayLeaseFailed`), and a navigated-to document that authenticated
  // but never went ready (`activeBootstrapFailed`) differ only in cause.
  const activePaneUnserviceable = activeTimedOut || activeRelayLeaseFailed || activeBootstrapFailed
  // The published-HTTPS "unembeddable" fail-closed is GONE: a relay endpoint is a
  // real, same-origin address, so a warm relay pane embeds and announces
  // readiness like any other. A published parent that CANNOT get an endpoint
  // (remote too old, forward down) surfaces through the connect/open-pane error
  // on the panel below (see connectFailure), not a separate unembeddable branch.
  const showPanel =
    activeId !== null && (!warm[activeId] || !activeLive || activePaneUnserviceable)
  // Loading overlay: the active pane is warm and the backend says connected,
  // but the embedded SPA hasn't announced readiness yet. Without this the
  // window between Retry succeeding (setWarm) and the remote SPA rendering its
  // embedded switcher is a black pane with NO tabs — the local header is
  // display:none while a remote tab is active, so the user would be stranded.
  const showLoading = !showPanel && activeId !== null && !!warm[activeId] && !activeReady
  // Journal the failure this panel is about to show, so its hand-off carries the
  // diagnosis ladder instead of the one sentence on screen. Recorded here rather
  // than by the API client because the panel's evidence arrives on a SUCCESSFUL
  // poll: `status.error` and the ladder verdict ride a 200, and the auto-warm
  // connect that failed earlier swallowed its own rejection. Placed above the
  // early return below so the hook order cannot depend on what is warm.
  // The report the panel's hand-off carries. Held in state because producing it
  // journals, and passed as an object so the prompt is bound to THIS crew rather
  // than to whichever crew last journaled the same sentence.
  const [panelReport, setPanelReport] = useState<ErrorReport | null>(null)
  useEffect(() => {
    if (!activeId) return
    const inst = instancesQuery.data?.instances.find(i => i.id === activeId)
    // A connect in flight is not a failure. Without this the transient
    // `connecting` state would journal as its own distinct signature, so every
    // Retry would leave a phantom entry between the real ones.
    if (inst?.status?.state === 'connecting') return
    setPanelReport(reportInstanceFailure({
      id: activeId,
      name: inst?.name || activeId,
      transport: inst?.connection_method === 'ssm' ? 'ssm' : 'ssh',
      // With the panel down the pane is healthy, so pass no status: the recorder's
      // no-failure path is what clears its de-dup signature, and gating this call
      // on `showPanel` would make that branch unreachable — leaving the signature
      // standing after recovery so a later identical failure is suppressed.
      status: showPanel ? inst?.status : undefined,
      stage: activePaneUnserviceable ? 'pane_load' : 'connect',
      // The watchdog / lease-failure cases have no backend error string at all —
      // the tunnel can still claim connected — so name that state explicitly
      // rather than journaling nothing for the one failure with no visible cause.
      fallbackMessage: showPanel && activePaneUnserviceable
        ? i18nT('components.instancesViewport.pane_failed_to_load')
        : '',
    }))
  }, [showPanel, activeId, activePaneUnserviceable, instancesQuery.data])
  if (embedded || (warmIds.length === 0 && !showPanel)) return null

  const nameFor = (id: string) =>
    instancesQuery.data?.instances.find(i => i.id === id)?.name || id

  const panelState = activeInst?.status?.state
  const panelConnecting =
    (connectMutation.isPending && connectMutation.variables?.id === activeId) ||
    panelState === 'connecting'
  // The Retry's own rejection used to reach only `paneLog`: the panel kept
  // showing the LIST's last status.error (or nothing) while the connect that
  // just failed said something newer. The mutation's error for THIS crew wins
  // while it is the latest thing that happened.
  const connectFailure = connectMutation.isError && connectMutation.variables?.id === activeId
    ? (errMessage(connectMutation.error) || i18nT('components.instancesViewport.connection_error'))
    : ''
  const panelError = connectFailure || activeInst?.status?.error || activeInst?.status?.diagnosis?.reason || ''

  // Draggable title-bar strip for the loading/error overlays. On frameless
  // macOS the window is dragged SOLELY by `-webkit-app-region: drag`
  // host-drag-strips (the per-pane strips above are gated off once an overlay
  // is up), and each overlay's opaque `bg-bg` cover plus the still-mounted
  // iframe otherwise leave the top band with no draggable region — so the
  // window can't be moved while a pane is connecting or shows a connection
  // error. Mirror the per-pane strips: lay one across the top of each overlay,
  // clipped clear of the Windows/Linux caption controls at the right edge (a
  // drag strip over Close would drag the window instead of clicking it). The
  // injected `button/a/[role=button]/[tabindex] { -webkit-app-region: no-drag }`
  // rule keeps the InstanceTabBar switcher, the Retry button, the ErrorNotice
  // and the SettingsLink clickable under the strip. Computed once and reused in
  // both overlays below. Precedent: App.tsx's `focus-mac-drag-strip`.
  const overlayDragStrip = isElectron
    ? (() => {
        const rightBound = isWinElectron
          ? Math.max(0, window.innerWidth - WIN_CAPTION_OVERLAY_WIDTH)
          : isLinuxFramelessElectron
            ? Math.max(0, window.innerWidth - LINUX_CAPTION_CONTROLS_WIDTH)
            : Number.POSITIVE_INFINITY
        const width = Math.min(window.innerWidth, rightBound)
        if (width < 1) return null
        return <div aria-hidden data-testid="overlay-drag-strip" className="host-drag-strip" style={{ left: 0, width }} />
      })()
    : null

  return (
    <div
      className="absolute inset-0 bg-bg"
      style={{ display: activeId === null ? 'none' : 'block', zIndex: 1 }}
    >
      {warmIds.map(id => {
        const conn = warm[id]
        const relayed = relayMode && conn?.kind === 'same-origin-relay'
        // The relay pane's browsing context reads `window.name` once and clears
        // it, so a NEW capability/channel needs a fresh context to re-seed. The
        // channel in the key remounts the iframe on re-issue (Retry / lease
        // rotation); direct panes keep the exact `${id}:${seq}` key, unchanged.
        const key = relayed ? `${id}:${reloadSeq[id] || 0}:${conn.channel}` : `${id}:${reloadSeq[id] || 0}`
        return (
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- onLoad is a document-load lifecycle hook: it posts the model handshake once the pane's document exists. Not a user interaction, and nothing here needs a keyboard path — the pane's own SPA owns focus once loaded.
        <iframe
          // reloadSeq in the key forces a remount (= reload) on Retry even when
          // the re-issued src is byte-identical to the dead frame's; the relay
          // channel is appended so a re-issued capability also remounts.
          key={key}
          ref={iframeRefFor(id)}
          title={nameFor(id)}
          src={srcFor(id)}
          // Relay panes only: seed the bootstrap envelope into the child's
          // `window.name` (set before navigation, so the pre-module bootstrap
          // reads it on its first line) and sandbox the frame WITHOUT
          // allow-same-origin (opaque origin) or top-navigation. Direct panes get
          // neither — `name=''` leaves window.name untouched and no sandbox keeps
          // the loopback SPA behaviour exactly as before.
          //
          // Seed ONCE per endpoint generation, then clear it after readiness: the
          // envelope carries the channel and a storage snapshot, so once the
          // child has consumed and cleared its own `window.name` (on `mc-embedded
          // -ready`) the parent element's `name` attribute is emptied too, so the
          // secret does not linger in a browser-visible DOM attribute or
          // repopulate the browsing-context name on a later same-element reload.
          // This does not fight lease renewal: a re-issue mints a new channel,
          // which changes the iframe `key` and REMOUNTS the frame with `ready`
          // cleared, so the fresh element re-seeds the new envelope before it
          // loads — the clear only ever applies to a frame that has already
          // booted on the current generation.
          {...(relayed
            ? { name: ready[id] ? '' : relaySeed(id), sandbox: RELAY_PANE_SANDBOX }
            : {})}
          // The embedded pane is the SAME SPA on the tunnel's loopback port, so
          // it is a CROSS-ORIGIN iframe (same host, different port). Browsers
          // deny microphone, fullscreen and clipboard-write in cross-origin
          // frames unless the parent delegates them via Permissions-Policy:
          // without "microphone", getUserMedia in the remote dashboard rejects
          // with NotAllowedError; without "fullscreen",
          // document.fullscreenEnabled is false in the pane and the native
          // <video> controls render a disabled fullscreen button; without
          // "clipboard-write", navigator.clipboard.writeText() rejects in the
          // pane, so every copy affordance fails (CliPanel's selection copy
          // surfaces "Copy failed"; TerminalKeyBar and WebAppArtifactCard hit
          // the same rejection). Local (top-level) use is unaffected.
          // Loopback-only, and the pane already runs our own token-authed SPA,
          // so delegating these grants nothing a same-origin top-level load
          // wouldn't already. display-capture follows the same rule: without
          // it, getDisplayMedia() rejects in the pane while the snip
          // affordances still RENDER, because the presence gate
          // (isScreenSnipSupported) only checks the function exists -- true
          // inside iframes -- so ChatPage's snip flow, WebPreviewPanel's
          // crop-to-chat and MochiSnipHost all die on click with
          // NotAllowedError. Delegation only lets the pane ASK. In a browser
          // the engine's own source picker decides. In the packaged app the main
          // process decides, and capture-trust.js authorizes by identity -- a
          // registered capture surface, its own main frame, still on its
          // registered origin -- so a pane is refused there rather than answered
          // with the whole desktop from one gesture. That is deliberately not a
          // frame-position test: a page inside this iframe could navigate the top
          // frame and inherit its position. Making pane capture WORK under
          // Electron needs an in-app picker naming the requesting frame, which is
          // a separate change.
          // clipboard-read is by contrast still NOT delegated:
          // read is the more sensitive grant class and exceeds this fix's
          // clipboard-write scope. The pane's Paste key (TerminalKeyBar's
          // readText) therefore still fails inside embedded panes, visibly,
          // with its named paste_permission_needed status; delegating read is
          // left as a maintainer decision.
          // allowFullScreen mirrors the legacy attribute some engines still
          // require alongside the Permissions-Policy delegation.
          allow="microphone; fullscreen; clipboard-write; display-capture"
          allowFullScreen
          onLoad={e => {
            // Fires for the initial about:blank too, which is why a load event is
            // NOT proof the pane loaded. `frame` says which one this was.
            paneLog('iframe-load', {
              id,
              port: warmPort(warmRef.current[id]),
              seq: reloadSeq[id] || 0,
              frame: frameDocumentState(e.currentTarget),
            })
            postModelTo(id)
          }}
          className="absolute inset-0 w-full h-full border-0"
          style={{ display: id === activeId ? 'block' : 'none' }}
        />
        )
      })}
      {/* Draggable title-bar strips for the active remote pane. Rendered AFTER
          the iframe so they follow it in DOM order — Electron collects
          draggable regions in document order, so a `drag` strip here re-adds
          drag over the gap that the blanket `iframe` no-drag rule subtracted.
          Only while the pane's own header is actually on screen (not the
          loading/error overlays, which carry their own interactive tab strip).
          Each strip sits in a control-free gap the pane measured, so it never
          swallows a header button's clicks. */}
      {/* Host-rendered drag strips over the pane's own header gaps. In focus
          mode they follow the PANE's chrome: while its header is hidden they are
          suppressed — the strips are `-webkit-app-region: drag`, which the
          compositor resolves BEFORE hit-testing, so leaving them up would make
          the pane's top band answer neither hover nor clicks and its own chrome
          could never be peeked back. While the pane's header IS peeked they must
          render: the pane's own app-region CSS is inert (draggable regions are
          only collected from the host document, never from a cross-origin
          iframe), so these strips are the ONLY thing that makes the peeked
          header move the window. */}
      {isElectron && (!focusMode || focusChromeVisible) && activeId && !showPanel && !showLoading && !!warm[activeId] && activeReady &&
        (dragGaps[activeId] ?? []).map((g, i) => {
          // Stay clear of the caption controls at the right edge: Windows'
          // native titleBarOverlay buttons, or frameless Linux's injected
          // cluster (#electron-linux-controls). The pane can't know the host
          // platform, so clip here — otherwise a drag strip overlays Close and
          // a click there drags the window instead.
          const rightBound = isWinElectron
            ? Math.max(0, window.innerWidth - WIN_CAPTION_OVERLAY_WIDTH)
            : isLinuxFramelessElectron
              ? Math.max(0, window.innerWidth - LINUX_CAPTION_CONTROLS_WIDTH)
              : Number.POSITIVE_INFINITY
          const left = g.x
          const width = Math.min(g.x + g.w, rightBound) - left
          if (width < 1) return null
          return <div key={`drag-${i}`} aria-hidden className="host-drag-strip" style={{ left, width }} />
        })}
      {showLoading && activeId && (
        <div className="absolute inset-0 flex flex-col bg-bg">
          {overlayDragStrip}
          {/* Same escape hatch as the error panel: while this overlay is up the
              only other switcher lives inside the still-loading iframe, so the
              strip is the user's sole way to reach Local or another instance. */}
          <InstanceTabBar
            variant="strip"
            style={stripInsetStyle}
          />
          <div className="flex-1 flex items-center justify-center p-6">
            <div className="flex flex-col items-center gap-3 text-center">
              <Loader2 size={28} className="animate-spin text-muted" />
              <div className="text-sm font-medium text-text">{nameFor(activeId)}</div>
              <div className="text-xs text-muted">{i18nT('components.instancesViewport.loading_pane')}</div>
            </div>
          </div>
        </div>
      )}
      {showPanel && activeId && (
        <div className="absolute inset-0 flex flex-col bg-bg">
          {overlayDragStrip}
          {/* Escape hatch. While a remote
              tab is active the local header — and with it the only top-level
              InstanceTabBar — is display:none, and the embedded switcher lives
              INSIDE the (now dead/absent) iframe. Without this strip the panel
              is a dead end: no way to reach Local or any other instance. The
              non-embedded InstanceTabBar renders the full switcher; inset it
              clear of the macOS traffic lights when this strip is topmost. */}
          <InstanceTabBar
            variant="strip"
            style={stripInsetStyle}
          />
          <div className="flex-1 flex items-center justify-center p-6">
            <div className="max-w-md w-full flex flex-col items-center gap-3 text-center">
              {panelConnecting ? (
                <Loader2 size={28} className="animate-spin text-muted" />
              ) : (
                <AlertTriangle size={28} className="text-[var(--danger)]" />
              )}
              <div className="text-sm font-medium text-text">{nameFor(activeId)}</div>
              <div className="text-xs text-muted">
                {panelConnecting
                  ? i18nT('components.instancesViewport.connecting')
                  : activePaneUnserviceable
                    ? i18nT('components.instancesViewport.pane_failed_to_load')
                    : panelState === 'error'
                      ? i18nT('components.instancesViewport.connection_error')
                      : i18nT('components.instancesViewport.disconnected')}
              </div>
              {!panelConnecting && activePaneUnserviceable && !panelError && (
                // The watchdog case has no backend error string: the tunnel
                // claims connected while the pane never loaded. Still a
                // failure, so it carries the same hand-off as the one below.
                <ErrorNotice
                  className="w-full text-left text-xs"
                  message={i18nT('components.instancesViewport.the_tunnel_looks_connected_but_the_remote_dashbo')}
                  report={panelReport ?? undefined}
                  askAgent={!!panelReport}
                  onHandoff={() => dispatch(setActiveId(null))}
                  testId="instances-viewport-timeout-error"
                />
              )}
              {!panelConnecting && panelError && (
                // askAgent on: the panel holds no input (see the hand-off note
                // below). `report` binds the prompt to THIS crew's journal entry;
                // `onHandoff` returns to Local because this overlay sits over the
                // chat the hand-off navigates to.
                <ErrorNotice
                  className="w-full max-h-32 overflow-auto text-left text-xs"
                  message={panelError}
                  report={panelReport ?? undefined}
                  askAgent={!!panelReport}
                  onHandoff={() => dispatch(setActiveId(null))}
                  testId="instances-viewport-panel-error"
                />
              )}
              <button
                type="button"
                disabled={panelConnecting}
                onClick={() => retry(activeId)}
                className="mt-1 inline-flex items-center gap-1.5 text-xs py-1.5 px-3.5 rounded-md bg-accent text-accent-fg disabled:opacity-60"
              >
                <RefreshCw size={13} className={panelConnecting ? 'animate-spin' : ''} /> {i18nT('components.instancesViewport.retry')}
              </button>
              {/* Retry stays primary — a momentary drop is worth one press. The
                  agent hand-off (inside the ErrorNotice above) is the other half:
                  a first connect fails on SSH config, a remote gateway that is
                  not running, a wrong port or an SSM instance profile, and none
                  of those change between two presses. Nothing to stash: the
                  panel holds no input, so askAgent is on.

                  Its `onHandoff` returns to Local, and without it the hand-off is
                  INVISIBLE: this panel renders inside the viewport's root overlay
                  (`absolute inset-0 bg-bg`, opaque, over the local pane whenever a
                  remote tab is active), and the hand-off only soft-navigates the
                  local SPA to /chat — underneath. The user would keep staring at
                  the same error panel and read the button as dead, while each
                  further click stacked another copy of the prompt onto the
                  hand-off QUEUE. The only other `setActiveId(null)` comes from the
                  embedded pane's own switcher, and that iframe is exactly what
                  failed here. Timed AFTER on purpose: leaving the panel on a
                  FAILED staging would clear the error with no chat to show for it. */}
              {/* Same overlay rule as the hand-off above: the link soft-navigates
                  the LOCAL SPA, which is underneath this panel while a remote tab
                  is active, so a click that is going to navigate returns to Local
                  first or the navigation is invisible. SettingsLink only fires this
                  for an unmodified click the page's leave guard allowed -- a
                  modified click (new tab) and a vetoed one leave the panel alone. */}
              <div className="text-[11px] text-muted">
                <Trans
                  i18nKey="components.instancesViewport.this_tab_stays_until_you_disconnect_the_instance"
                  components={[
                    <SettingsLink key="l" tab="instances" onPlainClick={() => dispatch(setActiveId(null))} />,
                  ]}
                />
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
