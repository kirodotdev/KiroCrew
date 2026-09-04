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
import { api } from '../api/client'
import { SettingsLink } from './SettingsLink'
import { useAppDispatch, useAppSelector } from '../store'
import { clearPaneReady, removeWarm, setActiveId, setPaneReady, setUnread, setWarm } from '../store/instancesSlice'
import InstanceTabBar, { visibleInstanceTabs, useCrewPins, toggleCrewPin, useCrewSwitcherStableOrder, setStableOrder } from './InstanceTabBar'
import { resolveTunnelOrigin } from '../lib/tunnelOrigin'
import { frameDocumentState, paneLog, safePaneUrl } from '../lib/paneLog'
import { LINUX_CAPTION_CONTROLS_WIDTH, TRAFFIC_LIGHT_INSET_PX, WIN_CAPTION_OVERLAY_WIDTH } from '../lib/electron'
import { isEmbeddedPane } from '../lib/embedded'
import AskAgentButton from './AskAgentButton'
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
  const refreshingRef = useRef<Set<string>>(new Set())
  const lastRefreshRef = useRef<Map<string, number>>(new Map())
  // Reactive (mc-auth-expired) re-mints answered per pane since its last Retry
  // — see MAX_REACTIVE_REMINTS.
  const reactiveMintsRef = useRef<Map<string, number>>(new Map())
  // Live iframe elements by id, so the parent can postMessage the switcher model
  // into each embedded pane. Set/cleared by the iframe ref cb.
  const iframeRefs = useRef<Map<string, HTMLIFrameElement>>(new Map())
  // Read-only mirrors for the long-lived message listener, kept current without
  // re-subscribing (mirrors the warmRef / portToIdRef pattern already used here).
  const postModelToRef = useRef<(id: string) => void>(() => {})
  const instancesRef = useRef<Array<{ id: string }>>([])

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
      if (!canRefreshNow(id)) return
      refreshingRef.current.add(id)
      try {
        const res = await api.refreshInstanceToken(id)
        const port = res.local_port || warmRef.current[id]?.port
        if (res.token && port) {
          dispatch(setWarm({ id, conn: { port, token: res.token } }))
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
    [dispatch, canRefreshNow],
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
          paneLog('warm', { id, port: st.local_port, via: 'auto' })
        } else {
          // No `else` used to exist here, and that is why a failed auto-warm was
          // untraceable: it leaves any PREVIOUS warm entry in place, so the tab
          // still renders a pane and the user sees only "loading" forever.
          paneLog('warm-declined', {
            id,
            via: 'auto',
            state: st.state,
            hasPort: !!st.local_port,
            hasToken: !!st.token,
            error: st.error || undefined,
            reason: st.diagnosis?.reason || undefined,
          })
        }
      } catch (err) {
        paneLog('warm-failed', { id, via: 'auto', error: (err as Error)?.message || 'unknown' })
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
  }, [dispatch, refreshToken, canRefreshNow])

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
        paneLog('warm', { id, port: st.local_port, via: 'connect' })
      } else {
        // Same silent shape as the auto-warm path: Retry can "succeed" as a
        // request while warming nothing, and the pane then reloads into the same
        // stuck state with no error anywhere.
        paneLog('warm-declined', {
          id,
          via: 'connect',
          state: st.state,
          hasPort: !!st.local_port,
          hasToken: !!st.token,
          error: st.error || undefined,
          reason: st.diagnosis?.reason || undefined,
        })
      }
      void queryClient.invalidateQueries({ queryKey: ['instances'] })
    },
    onError: (err, id) => {
      paneLog('warm-failed', { id, via: 'connect', error: (err as Error)?.message || 'unknown' })
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
  const activeWarmPort = activeWarmConn?.port
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
    if (!activeId || activeWarmPort === undefined || activeReady) return
    const id = activeId
    const port = activeWarmPort
    const t = window.setTimeout(() => {
      setTimedOut(prev => (prev[id] ? prev : { ...prev, [id]: true }))
      // `frame` is the verdict: `cross-origin` means the pane really loaded the
      // tunnel URL and then failed to announce readiness, while `about:blank`
      // means it never navigated at all and no crew bundle could ever have run.
      paneLog('load-timeout', {
        id,
        port,
        seq: activeSeq,
        afterMs: PANE_LOAD_TIMEOUT_MS,
        frame: frameDocumentState(iframeRefs.current.get(id)),
      })
    }, PANE_LOAD_TIMEOUT_MS)
    return () => window.clearTimeout(t)
  }, [activeId, activeWarmPort, activeSeq, activeReady])

  const retry = useCallback(
    (id: string) => {
      // Clear the stale verdict and force a reload even if the re-mint returns
      // an identical token (setWarm would be a no-op for the iframe src).
      paneLog('retry', {
        id,
        port: warmRef.current[id]?.port,
        frame: frameDocumentState(iframeRefs.current.get(id)),
      })
      setTimedOut(prev => ({ ...prev, [id]: false }))
      setReloadSeq(prev => ({ ...prev, [id]: (prev[id] || 0) + 1 }))
      // An explicit user press is a fresh start: re-open the reactive budget so
      // a pane that recovers on the next token can still self-heal afterwards.
      reactiveMintsRef.current.delete(id)
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
  }, [warm])

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
      return {
        type: 'mc-host-model', v: 1, tabs, activeId, self, macInset, focusMode,
        electron: isElectron,
        // Array, not the Set itself: structured clone rejects a Set across this
        // boundary in some engines and the receiver validates element-wise anyway.
        pinnedCrews,
        stableOrder,
      }
    },
    [instancesQuery.data, warm, unread, activeId, macInset, focusMode, pinnedCrews, stableOrder],
  )

  // Post the model into one embedded pane, addressed to its exact loopback
  // origin (never '*') so it can't leak to an unexpected frame.
  const postModelTo = useCallback(
    (id: string) => {
      const el = iframeRefs.current.get(id)
      const w = warm[id]
      if (!el?.contentWindow || !w) return
      const origin = `${window.location.protocol}//${window.location.hostname}:${w.port}`
      // A frame still on about:blank inherits THIS origin, so the post below is
      // rejected with a target-origin mismatch. Chromium reports that as a
      // console error rather than an exception, so the catch cannot see it —
      // journal the frame's state here instead. The post is still attempted:
      // this is instrumentation, and skipping on a mis-read would break a
      // delivery that would have worked.
      const frame = frameDocumentState(el)
      if (frame !== 'cross-origin') paneLog('post-model-undeliverable', { id, origin, frame })
      try {
        el.contentWindow.postMessage(buildModelFor(id), origin)
      } catch (err) {
        /* frame mid-navigation — the next broadcast / ready ping retries */
        paneLog('post-model-threw', { id, origin, frame, error: (err as Error)?.message || 'unknown' })
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
  }, [warm, activeId, unread, macInset, instancesQuery.data, postModelTo, pinnedCrews, stableOrder])

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
      stage: activeTimedOut ? 'pane_load' : 'connect',
      // The watchdog case has no backend error string at all — the tunnel claims
      // connected while the pane never loaded — so name that state explicitly
      // rather than journaling nothing for the one failure with no visible cause.
      fallbackMessage: showPanel && activeTimedOut
        ? i18nT('components.instancesViewport.pane_failed_to_load')
        : '',
    }))
  }, [showPanel, activeId, activeTimedOut, instancesQuery.data])
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
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- onLoad is a document-load lifecycle hook: it posts the model handshake once the pane's document exists. Not a user interaction, and nothing here needs a keyboard path — the pane's own SPA owns focus once loaded.
        <iframe
          // reloadSeq in the key forces a remount (= reload) on Retry even when
          // the re-minted src is byte-identical to the dead frame's.
          key={`${id}:${reloadSeq[id] || 0}`}
          ref={el => {
            if (el) {
              iframeRefs.current.set(id, el)
              // The mount is the moment the src is committed to a live frame.
              // An empty `src` here means srcFor found no warm entry, which is
              // the one way the pane can end up parked on about:blank forever.
              paneLog('iframe-mounted', {
                id,
                port: warmRef.current[id]?.port,
                seq: reloadSeq[id] || 0,
                src: safePaneUrl(el.getAttribute('src')),
              })
            } else {
              iframeRefs.current.delete(id)
              paneLog('iframe-unmounted', { id })
            }
          }}
          title={nameFor(id)}
          src={srcFor(id)}
          // The embedded pane is the SAME SPA on the tunnel's loopback port, so
          // it is a CROSS-ORIGIN iframe (same host, different port). Browsers
          // deny microphone and fullscreen in cross-origin frames unless the
          // parent delegates them via Permissions-Policy: without "microphone",
          // getUserMedia in the remote dashboard rejects with NotAllowedError;
          // without "fullscreen", document.fullscreenEnabled is false in the
          // pane and the native <video> controls render a disabled fullscreen
          // button. Local (top-level) use is unaffected. Loopback-only, and the
          // pane already runs our own token-authed SPA, so delegating these
          // grants nothing a same-origin top-level load wouldn't already.
          // allowFullScreen mirrors the legacy attribute some engines still
          // require alongside the Permissions-Policy delegation.
          allow="microphone; fullscreen"
          allowFullScreen
          onLoad={e => {
            // Fires for the initial about:blank too, which is why a load event is
            // NOT proof the pane loaded. `frame` says which one this was.
            paneLog('iframe-load', {
              id,
              port: warmRef.current[id]?.port,
              seq: reloadSeq[id] || 0,
              frame: frameDocumentState(e.currentTarget),
            })
            postModelTo(id)
          }}
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
              {/* Retry stays primary — a momentary drop is worth one press. This is
                  the other half: a first connect fails on SSH config, a remote
                  gateway that is not running, a wrong port or an SSM instance
                  profile, and none of those change between two presses. Nothing to
                  stash: the panel holds no input.

                  `onHandoff` returns to Local, and without it the hand-off is
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
              {!panelConnecting && panelReport && (
                <AskAgentButton
                  report={panelReport}
                  onHandoff={() => dispatch(setActiveId(null))}
                />
              )}
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
