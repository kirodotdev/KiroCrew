/** The one live dashboard socket: open, close, reconnect with backoff, and
 *  best-effort sends. Frame routing, the open-time catch-up and the silence
 *  watchdog compose this from `useWebSocket`. */
import { useMemo, useRef, type MutableRefObject } from 'react'
import type { AppDispatch } from '../../store'
import { sseDisconnected } from '../../store/dashboardSlice'

export type LogCallback = ((data: { level: string; msg: string }) => void) | null

/** First reconnect delay; each consecutive failure doubles it. */
const RECONNECT_INITIAL_MS = 1000
/** Ceiling for the doubled reconnect delay. */
const RECONNECT_MAX_MS = 10000

export interface SocketConnection {
  wsRef: MutableRefObject<WebSocket | null>
  /** Set by the first successful open for the life of the hook: every later
   *  open is a reconnect and runs the catch-up. */
  wasConnectedRef: MutableRefObject<boolean>
  /** True from a reconnect's open until its slot list refetch settles: the
   *  replayed backlog is history, not news (no unread marks, approval,
   *  question or turn-done chimes, live banners or read relays; a feed
   *  `notification` frame's own sound and the theme's `message-received`
   *  sound still play). */
  reconnectingRef: MutableRefObject<boolean>
  logCbRef: MutableRefObject<LogCallback>
  /** Construct and register a new socket, or null when one is already OPEN or
   *  CONNECTING, or the hook is unmounting. */
  open(): WebSocket | null
  resetBackoff(): void
  /** `onclose` for `ws`: a stale socket is ignored entirely; the live one is
   *  marked disconnected and, unless the hook is unmounting, releases its
   *  voice stream and schedules `reconnect` after the current backoff. */
  handleClose(ws: WebSocket, dispatch: AppDispatch, releaseVoice: () => void, reconnect: () => void): void
  /** Replace the socket now, resetting the backoff. */
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
      const ws = new WebSocket(`${proto}//${location.host}/api/ws?caps=slot_patch`)
      wsRef.current = ws
      return ws
    },
    resetBackoff() {
      reconnectRef.current = RECONNECT_INITIAL_MS
    },
    handleClose(ws, dispatch, releaseVoice, reconnect) {
      // Stale WS (e.g. from StrictMode cleanup) — ignore entirely.
      if (wsRef.current !== ws) return

      dispatch(sseDisconnected())
      wsRef.current = null

      if (closingRef.current) return
      releaseVoice()
      const delay = reconnectRef.current
      reconnectRef.current = Math.min(delay * 2, RECONNECT_MAX_MS)
      reconnectTimerRef.current = setTimeout(reconnect, delay)
    },
    /**
     * Force an immediate reconnect: cancels any pending backoff timer, closes
     * the existing WS (if any), resets the backoff window, and reconnects.
     *
     * Used by `useDashboardHealthProbe` when its periodic /api/status poll
     * succeeds while the dashboard is in `connected: false` state — that's the
     * signal that the gateway came back up. Without this, the next reconnect
     * attempt could be up to 10s away (capped exponential backoff in onclose).
     */
    forceReconnect(releaseVoice, reconnect) {
      if (closingRef.current) return
      clearTimeout(reconnectTimerRef.current)
      reconnectRef.current = RECONNECT_INITIAL_MS  // reset backoff window
      const ws = wsRef.current
      if (ws && ws.readyState !== WebSocket.CLOSED) {
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
