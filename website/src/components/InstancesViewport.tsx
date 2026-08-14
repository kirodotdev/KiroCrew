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
import { api } from '../api/client'
import { useAppDispatch, useAppSelector } from '../store'
import { removeWarm, setActiveId, setPaneReady, setUnread, setWarm } from '../store/instancesSlice'
import InstanceTabBar, { visibleInstanceTabs } from './InstanceTabBar'
import { resolveTunnelOrigin } from '../lib/tunnelOrigin'
import { TRAFFIC_LIGHT_INSET_PX, WIN_CAPTION_OVERLAY_WIDTH } from '../lib/electron'
import { isEmbeddedPane } from '../lib/embedded'
import { isElectron, isWinElectron } from '../lib/electron'
import type { DragGap } from '../lib/dragGaps'

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
const PANE_LOAD_TIMEOUT_MS = 15_000

/** Parse a ``<int>[hm]`` TTL (e.g. "20h", "30m") to seconds; 0 if unparseable. */
function ttlToSeconds(ttl: string): number {
  const m = /^(\d+)([hm])$/.exec(ttl || '')
  if (!m) return 0
  const n = Number(m[1])
  return m[2] === 'h' ? n * 3600 : n * 60
}

export default function InstancesViewport({ macInset = false }: { macInset?: boolean } = {}) {
  const dispatch = useAppDispatch()
  const queryClient = useQueryClient()
  const warm = useAppSelector(s => s.instances.warm)
  const activeId = useAppSelector(s => s.instances.activeId)
  const mru = useAppSelector(s => s.instances.mru)
  const unread = useAppSelector(s => s.instances.unread)
  // Panes whose embedded SPA has announced readiness for their CURRENT src.
  // Tests preload partial slices, so tolerate a missing map.
  const ready = useAppSelector(s => s.instances.ready) ?? {}

  // Per-instance header drag gaps relayed up by each embedded pane
  // (mc-drag-gaps). Only the ACTIVE pane's gaps are rendered, but they are
  // keyed by id so a background pane's report is retained for an instant switch.
  const [dragGaps, setDragGaps] = useState<Record<string, DragGap[]>>({})

  // Embedded instance panes never host nested panes (single-level by design),
  // so skip the poll and render nothing — see isEmbeddedPane / InstanceTabBar.
  const embedded = isEmbeddedPane()

  // Poll so token_ttl_remaining (and connection dots) stay current; this also
  // drives the proactive token-refresh effect below.
  const instancesQuery = useQuery({
    queryKey: ['instances'],
    queryFn: () => api.listInstances(),
    refetchInterval: 60_000,
    enabled: !embedded,
  })
  const warmCap = instancesQuery.data?.warm_set_cap || 5

  // Current warm map in a ref so the refresh callback (used by the long-lived
  // postMessage listener) always sees the latest ports without re-subscribing.
  const warmRef = useRef(warm)
  warmRef.current = warm
  const refreshingRef = useRef<Set<string>>(new Set())
  const lastRefreshRef = useRef<Map<string, number>>(new Map())
  // Live iframe elements by id, so the parent can postMessage the switcher model
  // into each embedded pane. Set/cleared by the iframe ref cb.
  const iframeRefs = useRef<Map<string, HTMLIFrameElement>>(new Map())
  // Read-only mirrors for the long-lived message listener, kept current without
  // re-subscribing (mirrors the warmRef / portToIdRef pattern already used here).
  const postModelToRef = useRef<(id: string) => void>(() => {})
  const instancesRef = useRef<Array<{ id: string }>>([])

  // Force a fresh token mint for one instance and reload its iframe by updating
  // warm[id].token (srcFor re-derives the ?token= URL, so changing the token
  // reloads the iframe). Mirrors the gateway's mint-and-load. Concurrency- and
  // rate-guarded so the reactive path can't loop.
  const refreshToken = useCallback(
    async (id: string) => {
      if (refreshingRef.current.has(id)) return
      const last = lastRefreshRef.current.get(id) || 0
      if (Date.now() - last < REFRESH_MIN_INTERVAL_MS) return
      refreshingRef.current.add(id)
      try {
        const res = await api.refreshInstanceToken(id)
        const port = res.local_port || warmRef.current[id]?.port
        if (res.token && port) {
          dispatch(setWarm({ id, conn: { port, token: res.token } }))
        }
      } catch {
        /* transient — the next poll / auth-expired signal retries */
      } finally {
        refreshingRef.current.delete(id)
        lastRefreshRef.current.set(id, Date.now())
      }
    },
    [dispatch],
  )

  // Pre-mint + warm one connected instance without surfacing it. Cheap when the
  // backend already auto-reconnected the tunnel (connect() returns the cached
  // token without re-minting). Failures are swallowed: the sticky tab + in-pane
  // error/Retry panel handle an instance that can't be warmed.
  const autoWarm = useCallback(
    async (id: string) => {
      try {
        const st = await api.connectInstance(id)
        if (st.state === 'connected' && st.local_port && st.token) {
          dispatch(setWarm({ id, conn: { port: st.local_port, token: st.token } }))
        }
      } catch {
        /* leave it — clicking the tab will surface the error panel */
      }
    },
    [dispatch],
  )

  // Origin→id map for the relay listener, kept current without re-subscribing.
  const portToIdRef = useRef<Map<number, string>>(new Map())
  useEffect(() => {
    const m = new Map<number, string>()
    for (const [id, w] of Object.entries(warm)) m.set(w.port, id)
    portToIdRef.current = m
  }, [warm])

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
  }, [warm])

  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      const id = resolveTunnelOrigin(e.origin, portToIdRef.current)
      if (!id) return
      const data = e.data
      if (!data || typeof data !== 'object') return
      if (data.type === 'mc-unread-slots') {
        const count = Number(data.count)
        if (!Number.isFinite(count) || count < 0) return
        dispatch(setUnread({ id, count }))
      } else if (data.type === 'mc-auth-expired') {
        // Reactive recovery: the embedded dashboard reported an expired session.
        // Force a fresh mint and reload its iframe rather than letting it show
        // the in-pane paste-token banner. No foreground guard here — the active
        // pane is exactly the one the user wants restored.
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
      } else if (data.type === 'mc-embedded-ready') {
        // The pane just (re)mounted and asked for the current model — send it now
        // rather than waiting for the next input-driven broadcast. Also record
        // readiness: this is the parent's only proof the pane actually loaded
        // (drives the loading overlay + load watchdog below).
        dispatch(setPaneReady(id))
        postModelToRef.current(id)
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
  }, [dispatch, refreshToken])

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
      const total = ttlToSeconds(inst.ttl)
      if (typeof remaining !== 'number' || total <= 0) continue
      if (remaining > total * (1 - REFRESH_AT_ELAPSED_FRAC)) continue
      void refreshToken(id)
    }
  }, [instancesQuery.data, warm, activeId, refreshToken])

  // Retry connect from the in-pane error panel: re-mint a token and warm the
  // iframe. Idempotent on the backend (the tunnel is often already live after a
  // startup auto-reconnect), so this mainly restores the browser-side token.
  const connectMutation = useMutation({
    mutationFn: (id: string) => api.connectInstance(id),
    onSuccess: (st, id) => {
      if (st.state === 'connected' && st.local_port && st.token) {
        dispatch(setWarm({ id, conn: { port: st.local_port, token: st.token } }))
      }
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
  const [timedOut, setTimedOut] = useState<Record<string, boolean>>({})
  const [reloadSeq, setReloadSeq] = useState<Record<string, number>>({})
  const activeWarmConn = activeId ? warm[activeId] : undefined
  // Primitive deps for the watchdog effect (a fresh conn object identity on
  // every setWarm would defeat the dep comparison; the src only depends on these).
  const activeWarmPort = activeWarmConn?.port
  const activeWarmToken = activeWarmConn?.token
  const activeReady = activeId ? !!ready[activeId] : true
  const activeSeq = activeId ? reloadSeq[activeId] || 0 : 0
  useEffect(() => {
    if (!activeId || activeWarmPort === undefined || activeReady) return
    const id = activeId
    const t = window.setTimeout(() => {
      setTimedOut(prev => (prev[id] ? prev : { ...prev, [id]: true }))
    }, PANE_LOAD_TIMEOUT_MS)
    return () => window.clearTimeout(t)
    // Restart the countdown whenever the pane's src (port/token) or forced
    // reload sequence changes — each of those reloads the iframe.
  }, [activeId, activeWarmPort, activeWarmToken, activeSeq, activeReady])

  const retry = useCallback(
    (id: string) => {
      // Clear the stale verdict and force a reload even if the re-mint returns
      // an identical token (setWarm would be a no-op for the iframe src).
      setTimedOut(prev => ({ ...prev, [id]: false }))
      setReloadSeq(prev => ({ ...prev, [id]: (prev[id] || 0) + 1 }))
      connectMutation.mutate(id)
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
    if (victim) dispatch(removeWarm(victim))
  }, [warm, warmCap, mru, activeId, dispatch])

  // Auto-warm on load: after the first instances poll, pre-mount every
  // currently-connected instance's iframe (up to the warm cap) so panes are
  // instantly usable after a gateway restart + page reload — the user never has
  // to click to re-establish a connection. We deliberately do NOT change
  // activeId: the dashboard always lands on the Local tab and the warmed iframes
  // sit hidden and ready. Down instances are skipped (they stay sticky error
  // tabs); this runs once per mount. warmRef avoids re-firing on warm changes.
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
    for (const inst of candidates) void autoWarm(inst.id)
  }, [instancesQuery.data, warmCap, autoWarm])

  const warmIds = useMemo(() => Object.keys(warm), [warm])
  const srcFor = useCallback(
    (id: string) => {
      const w = warm[id]
      // Use the parent dashboard's OWN hostname (not a hardcoded 127.0.0.1) so the iframe
      // is ALWAYS same-site with the parent. Otherwise SameSite=Lax auth cookies are
      // withheld on the iframe's subrequests (e.g. parent on localhost + iframe on
      // 127.0.0.1 = cross-site -> 403 storm). The hostname resolves to the same loopback
      // the SSH forward binds (127.0.0.1), since the dashboard itself is reached via it.
      return w ? `http://${window.location.hostname}:${w.port}/?token=${encodeURIComponent(w.token)}` : ''
    },
    [warm],
  )

  // Build the switcher model relayed to the embedded pane `id`: the full tab
  // list (same rule as the local inline bar), which tab is active, this pane's
  // OWN tunnel status (for its readout capsule), and the macOS inset.
  const buildModelFor = useCallback(
    (id: string) => {
      const insts = instancesQuery.data?.instances ?? []
      const tabs = visibleInstanceTabs(insts, warm).map(i => ({
        id: i.id,
        name: i.name,
        sshHost: i.ssh_host,
        state: i.status?.state,
        unread: unread[i.id] || 0,
      }))
      const selfInst = insts.find(i => i.id === id)
      const self = selfInst
        ? {
            state: selfInst.status?.state,
            ttlRemaining: selfInst.status?.token_ttl_remaining,
            ttlTotal: ttlToSeconds(selfInst.ttl),
          }
        : null
      return { type: 'mc-host-model', v: 1, tabs, activeId, self, macInset, electron: isElectron }
    },
    [instancesQuery.data, warm, unread, activeId, macInset],
  )

  // Post the model into one embedded pane, addressed to its exact loopback
  // origin (never '*') so it can't leak to an unexpected frame.
  const postModelTo = useCallback(
    (id: string) => {
      const el = iframeRefs.current.get(id)
      const w = warm[id]
      if (!el?.contentWindow || !w) return
      const origin = `${window.location.protocol}//${window.location.hostname}:${w.port}`
      try {
        el.contentWindow.postMessage(buildModelFor(id), origin)
      } catch {
        /* frame mid-navigation — the next broadcast / ready ping retries */
      }
    },
    [warm, buildModelFor],
  )
  postModelToRef.current = postModelTo
  instancesRef.current = instancesQuery.data?.instances ?? []

  // Broadcast the model to every warm pane whenever any input changes (active
  // tab, tunnel status, unread, inset). Cheap: each post is a structured clone
  // to a loopback frame.
  useEffect(() => {
    for (const id of Object.keys(warm)) postModelTo(id)
  }, [warm, activeId, unread, macInset, instancesQuery.data, postModelTo])

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
  const showPanel = activeId !== null && (!warm[activeId] || !activeLive || activeTimedOut)
  // Loading overlay: the active pane is warm and the backend says connected,
  // but the embedded SPA hasn't announced readiness yet. Without this the
  // window between Retry succeeding (setWarm) and the remote SPA rendering its
  // embedded switcher is a black pane with NO tabs — the local header is
  // display:none while a remote tab is active, so the user would be stranded.
  const showLoading = !showPanel && activeId !== null && !!warm[activeId] && !activeReady
  if (embedded || (warmIds.length === 0 && !showPanel)) return null

  const nameFor = (id: string) =>
    instancesQuery.data?.instances.find(i => i.id === id)?.name || id

  const panelState = activeInst?.status?.state
  const panelConnecting =
    (connectMutation.isPending && connectMutation.variables === activeId) ||
    panelState === 'connecting'
  const panelError = activeInst?.status?.error || activeInst?.status?.diagnosis?.reason || ''

  return (
    <div
      className="absolute inset-0 bg-bg"
      style={{ display: activeId === null ? 'none' : 'block', zIndex: 1 }}
    >
      {warmIds.map(id => (
        <iframe
          // reloadSeq in the key forces a remount (= reload) on Retry even when
          // the re-minted src is byte-identical to the dead frame's.
          key={`${id}:${reloadSeq[id] || 0}`}
          ref={el => {
            if (el) iframeRefs.current.set(id, el)
            else iframeRefs.current.delete(id)
          }}
          title={nameFor(id)}
          src={srcFor(id)}
          // The embedded pane is the SAME SPA on the tunnel's loopback port, so
          // it is a CROSS-ORIGIN iframe (same host, different port). Browsers
          // disable the microphone in cross-origin frames unless the parent
          // delegates it via Permissions-Policy, so getUserMedia in the remote
          // dashboard rejects with NotAllowedError ("permission denied") without
          // this. Local (top-level) use is unaffected. Loopback-only, and the
          // pane already runs our own token-authed SPA, so delegating the mic
          // here grants nothing a same-origin top-level load wouldn't already.
          allow="microphone"
          onLoad={() => postModelTo(id)}
          className="absolute inset-0 w-full h-full border-0"
          style={{ display: id === activeId ? 'block' : 'none' }}
        />
      ))}
      {/* Draggable title-bar strips for the active remote pane. Rendered AFTER
          the iframe so they follow it in DOM order — Electron collects
          draggable regions in document order, so a `drag` strip here re-adds
          drag over the gap that the blanket `iframe` no-drag rule subtracted.
          Only while the pane's own header is actually on screen (not the
          loading/error overlays, which carry their own interactive tab strip).
          Each strip sits in a control-free gap the pane measured, so it never
          swallows a header button's clicks. */}
      {isElectron && activeId && !showPanel && !showLoading && !!warm[activeId] && activeReady &&
        (dragGaps[activeId] ?? []).map((g, i) => {
          // On Windows, stay clear of the native titleBarOverlay caption buttons
          // (right edge); the pane can't know the host is Windows, so clip here.
          const rightBound = isWinElectron
            ? Math.max(0, window.innerWidth - WIN_CAPTION_OVERLAY_WIDTH)
            : Number.POSITIVE_INFINITY
          const left = g.x
          const width = Math.min(g.x + g.w, rightBound) - left
          if (width < 1) return null
          return <div key={`drag-${i}`} aria-hidden className="host-drag-strip" style={{ left, width }} />
        })}
      {showLoading && activeId && (
        <div className="absolute inset-0 flex flex-col bg-bg">
          {/* Same escape hatch as the error panel: while this overlay is up the
              only other switcher lives inside the still-loading iframe, so the
              strip is the user's sole way to reach Local or another instance. */}
          <InstanceTabBar
            variant="strip"
            style={macInset ? { paddingLeft: TRAFFIC_LIGHT_INSET_PX } : undefined}
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
          {/* Escape hatch. While a remote
              tab is active the local header — and with it the only top-level
              InstanceTabBar — is display:none, and the embedded switcher lives
              INSIDE the (now dead/absent) iframe. Without this strip the panel
              is a dead end: no way to reach Local or any other instance. The
              non-embedded InstanceTabBar renders the full switcher; inset it
              clear of the macOS traffic lights when this strip is topmost. */}
          <InstanceTabBar
            variant="strip"
            style={macInset ? { paddingLeft: TRAFFIC_LIGHT_INSET_PX } : undefined}
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
                  : activeTimedOut
                    ? i18nT('components.instancesViewport.pane_failed_to_load')
                    : panelState === 'error'
                      ? i18nT('components.instancesViewport.connection_error')
                      : i18nT('components.instancesViewport.disconnected')}
              </div>
              {!panelConnecting && activeTimedOut && !panelError && (
                <div className="text-xs text-muted">
                  {i18nT('components.instancesViewport.the_tunnel_looks_connected_but_the_remote_dashbo')}
                </div>
              )}
              {!panelConnecting && panelError && (
                <div className="w-full max-h-32 overflow-auto rounded-md border border-border bg-bg-hover px-3 py-2 text-left text-xs text-muted whitespace-pre-wrap break-words">
                  {panelError}
                </div>
              )}
              <button
                type="button"
                disabled={panelConnecting}
                onClick={() => retry(activeId)}
                className="mt-1 inline-flex items-center gap-1.5 text-xs py-1.5 px-3.5 rounded-md bg-accent text-accent-fg disabled:opacity-60"
              >
                <RefreshCw size={13} className={panelConnecting ? 'animate-spin' : ''} /> {i18nT('components.instancesViewport.retry')}
              </button>
              <div className="text-[11px] text-muted">
                {i18nT('components.instancesViewport.this_tab_stays_until_you_disconnect_the_instance')}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
