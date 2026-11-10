/** The one live dashboard socket: open, close, reconnect with backoff, and
 *  best-effort sends. Frame routing, the open-time catch-up and the silence
 *  watchdog compose this from `useWebSocket`. */
import { useMemo, useRef, type MutableRefObject } from 'react'
import type { AppDispatch } from '../../store'
import { markGatewayDisconnected } from './gatewayConnectivity'
import { jittered, provedHealthy } from '../../utils/reconnectBackoff'

/** The fold-watch capability this bundle declares on `/api/ws` (see
 *  `foldWatch.ts`). A wire token, not copy. */
export const foldDeltaCapability = 'fold_delta.1'

/** Declared beside `fold_delta.1`: this bundle applies `slot_fold` delta frames
 *  for the work board (see `workDelta.ts`). A separate token, because a bundle
 *  that declared only `fold_delta.1` reads whole values alone. */
export const foldApplyCapability = 'fold_delta.2'

export type LogCallback = ((data: { level: string; msg: string }) => void) | null

/** First reconnect delay; each consecutive failure doubles it. */
const RECONNECT_INITIAL_MS = 1000
/** Ceiling for the doubled reconnect delay. */
const RECONNECT_MAX_MS = 10000
/** The live socket is going away: start the backoff over when it proved healthy.
 *  True when it opened and is going away before that. */
function settleBackoff(openedAtRef: MutableRefObject<number>, reconnectRef: MutableRefObject<number>): boolean {
  const openedAt = openedAtRef.current
  openedAtRef.current = 0
  if (!openedAt) return false
  if (!provedHealthy(openedAt, Date.now())) return true
  reconnectRef.current = RECONNECT_INITIAL_MS
  return false
}

export interface SocketConnection {
  wsRef: MutableRefObject<WebSocket | null>
  /** Set by the first successful open for the life of the hook: every later
   *  open is a reconnect and runs the catch-up. */
  wasConnectedRef: MutableRefObject<boolean>
  /** True from a reconnect's open until the socket's slot list is applied
   *  (`createReconnectSnapshotWait`): the replayed backlog is history, not
   *  news (no unread marks, approval, question or turn-done chimes, live
   *  banners or read relays; a feed `notification` frame's own sound and the
   *  theme's `message-received` sound still play). */
  reconnectingRef: MutableRefObject<boolean>
  logCbRef: MutableRefObject<LogCallback>
  /** Construct and register a new socket, or null when one is already OPEN or
   *  CONNECTING, or the hook is unmounting. */
  open(): WebSocket | null
  /** The live socket opened: it proves the gateway healthy once it has stayed
   *  open for `RECONNECT_HEALTHY_MS`. */
  noteOpen(): void
  /** `onclose` for `ws`: a stale socket is ignored entirely; the live one is
   *  marked disconnected and, unless the hook is unmounting, releases its
   *  voice stream and schedules `reconnect` after the current backoff less a
   *  random part of up to half of it. The backoff starts over only when `ws`
   *  had proved healthy. */
  handleClose(ws: WebSocket, dispatch: AppDispatch, releaseVoice: () => void, reconnect: () => void): void
  /** Replace the live socket now. With none, bring the backed-off reconnect
   *  forward, unless the last socket opened and dropped before it proved
   *  healthy. */
  forceReconnect(releaseVoice: () => void, reconnect: () => void): void
  /** Send `payload` on the live socket; dropped while it is not OPEN. */
  sendIfOpen(payload: object): void
  isClosing(): boolean
  /** Mount: allow connecting again (a StrictMode re-mount follows an unmount). */
  resume(): void
  /** Unmount, first step: refuse reconnects and cancel a pending one. */
  beginClosing(): void
  /** Unmount, last socket step: close and forget the live socket. */
  closeForUnmount(): void
  /** Subscribe to log events — call with callback on mount, null on unmount. */
  subscribeLogs(cb: LogCallback): void
  subscribeSubagents(subscribe: boolean): void
}

export function useSocketConnection(): SocketConnection {
  const wsRef = useRef<WebSocket | null>(null)
  const closingRef = useRef(false)  // true when cleanup intentionally closes WS
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout>>()  // pending reconnect timer
  const reconnectRef = useRef(RECONNECT_INITIAL_MS)
  const openedAtRef = useRef(0)  // when the live socket opened; 0 while it has not
  const droppedYoungRef = useRef(false)  // the last socket opened, then closed before it proved healthy
  const wasConnectedRef = useRef(false)
  const reconnectingRef = useRef(false)  // suppress markSlotUnread during reconnect catch-up
  const logCbRef = useRef<LogCallback>(null)

  return useMemo<SocketConnection>(() => ({
    wsRef,
    wasConnectedRef,
    reconnectingRef,
    logCbRef,
    open() {
      // Guard against double-connect in StrictMode (dev) — if we already
      // have a WS that's open OR still connecting, reuse it.
      const existing = wsRef.current
      if (existing && (existing.readyState === WebSocket.OPEN || existing.readyState === WebSocket.CONNECTING)) return null
      if (closingRef.current) return null  // component unmounted, don't reconnect
      // closingRef invariant: reset by the mount effect before connecting
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
      // `caps=slot_patch`: this bundle applies one-row `slot_patch` frames, so
      // the gateway sends those instead of the full slot list after a pin,
      // rename, folder move or close. A gateway that predates the frame ignores
      // the parameter and keeps sending full lists, which the router still
      // applies.
      // `fold_delta.1`: this bundle sends `fold_watch` once the gateway says
      // `fold_hello` (see `foldWatch.ts`). An older gateway ignores it and
      // keeps sending every board's slot-fold frames. `fold_delta.2`: send the
      // work board as `slot_fold` deltas; a gateway without it sends whole values.
      const ws = new WebSocket(`${proto}//${location.host}/api/ws?caps=slot_patch,${foldDeltaCapability},${foldApplyCapability}`)
      wsRef.current = ws
      openedAtRef.current = 0
      return ws
    },
    noteOpen() {
      openedAtRef.current = Date.now()
    },
    handleClose(ws, dispatch, releaseVoice, reconnect) {
      // Stale WS (e.g. from StrictMode cleanup) — ignore entirely.
      if (wsRef.current !== ws) return
      // Settled on every path below: `forceReconnect` reads what this socket's close left.
      droppedYoungRef.current = settleBackoff(openedAtRef, reconnectRef)

      markGatewayDisconnected(dispatch)
      wsRef.current = null

      if (closingRef.current) return
      releaseVoice()
      const delay = reconnectRef.current
      reconnectRef.current = Math.min(delay * 2, RECONNECT_MAX_MS)
      reconnectTimerRef.current = setTimeout(reconnect, jittered(delay))
    },
    /**
     * Replace the live socket now (the silence and row-delivery watchdogs): cancel
     * any pending backoff timer, close the socket and reconnect at once. The
     * backoff starts over only when the replaced socket had proved healthy.
     *
     * With no live socket the backoff already has a reconnect scheduled, and the
     * caller is `useDashboardHealthProbe`, whose /api/status poll just succeeded
     * while the dashboard is `connected: false`. When the last socket was refused
     * at its handshake that says the gateway came back, so the reconnect is
     * brought forward to at most `RECONNECT_INITIAL_MS`, spread like a backoff
     * delay because every tab's probe answers in the same tick. When the last
     * socket opened and dropped before it proved healthy, the gateway was already
     * reachable: the poll adds nothing and the backoff stands, or a gateway that
     * accepts and drops sockets would be retried every probe tick.
     */
    forceReconnect(releaseVoice, reconnect) {
      if (closingRef.current) return
      const ws = wsRef.current
      if (!ws) {
        if (droppedYoungRef.current) return
        clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = setTimeout(reconnect, jittered(RECONNECT_INITIAL_MS))
        return
      }
      clearTimeout(reconnectTimerRef.current)
      settleBackoff(openedAtRef, reconnectRef)
      droppedYoungRef.current = false
      if (ws.readyState !== WebSocket.CLOSED) {
        releaseVoice()
        // Detach handlers BEFORE close() so the onclose handler doesn't fire
        // asynchronously and schedule a redundant reconnect on top of our 0ms
        // timer below — that race would briefly create two parallel WebSocket
        // connections. The existing onclose guard (wsRef.current !== ws) also
        // catches this, but explicit detach is cleaner and removes the
        // dispatch(sseDisconnected()) we don't want during a force-reconnect
        // (we're already in connected:false state and forcing a reconnect
        // because the probe just confirmed the gateway is back).
        ws.onclose = null
        ws.onerror = null
        try { ws.close() } catch { /* ignore */ }
      }
      wsRef.current = null
      reconnectTimerRef.current = setTimeout(reconnect, 0)
    },
    sendIfOpen(payload) {
      const ws = wsRef.current
      if (!ws || ws.readyState !== WebSocket.OPEN) return
      ws.send(JSON.stringify(payload))
    },
    isClosing() {
      return closingRef.current
    },
    resume() {
      closingRef.current = false  // reset for StrictMode re-mount
    },
    beginClosing() {
      closingRef.current = true
      clearTimeout(reconnectTimerRef.current)
    },
    closeForUnmount() {
      wsRef.current?.close()
      wsRef.current = null
    },
    subscribeLogs(cb) {
      logCbRef.current = cb
      const ws = wsRef.current
      if (!ws || ws.readyState !== WebSocket.OPEN) return
      if (cb) {
        ws.send(JSON.stringify({ type: 'subscribe_logs' }))
      } else {
        ws.send(JSON.stringify({ type: 'unsubscribe_logs' }))
      }
    },
    subscribeSubagents(subscribe) {
      const ws = wsRef.current
      if (!ws || ws.readyState !== WebSocket.OPEN) return
      ws.send(JSON.stringify({ type: subscribe ? 'subscribe_subagents' : 'unsubscribe_subagents' }))
    },
  }), [])
}
