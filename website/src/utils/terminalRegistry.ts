import { useSyncExternalStore } from 'react'
import type { Terminal } from '@xterm/xterm'
import type { FitAddon } from '@xterm/addon-fit'

/** sessionId → shell-ready WebSocket. Each terminal tab owns one entry. */
const registry = new Map<string, WebSocket>()
/** Per-session one-shot shell-ready listeners. */
const readyListeners = new Map<string, Set<() => void>>()

let _enabled = false
const enabledListeners = new Set<() => void>()

export function setTerminalEnabledFlag(v: boolean) {
  _enabled = v
  for (const cb of enabledListeners) cb()
}
export function isTerminalEnabled(): boolean { return _enabled }

function subscribeEnabled(cb: () => void): () => void {
  enabledListeners.add(cb)
  return () => { enabledListeners.delete(cb) }
}
function getEnabledSnapshot(): boolean { return _enabled }

export function useTerminalEnabled(): boolean {
  return useSyncExternalStore(subscribeEnabled, getEnabledSnapshot)
}

/* ── Per-session terminal title (live "what's running" / cwd basename) ── */
const titles = new Map<string, string>()
const titleListeners = new Map<string, Set<() => void>>()

/* ── Per-session live cwd (full path, pushed by the backend title poller).
 * Read imperatively at hand-off time (Send to chat), so a plain map with no
 * subscription machinery is enough. */
const cwds = new Map<string, string>()

/** Live current working directory of a session's shell, if the backend has
 *  reported one. Falls back to undefined (callers use the spawn cwd). */
export function getTerminalCwd(sessionId: string): string | undefined {
  return cwds.get(sessionId)
}

/* ── Per-session launched shell (absolute path, reported by the backend in the
 * `ready` frame). The client mints session ids and opens the socket without
 * asking what will be spawned, so this is the only place it learns which shell
 * will interpret the bytes it writes. Read imperatively at hand-off time, same
 * as `cwds`. */
const shells = new Map<string, string>()
/* ── Per-session map of fence-nameable shell name -> ABSOLUTE path, as the
 * backend resolved it on this host. A snippet handed to another shell must name
 * an absolute path: a bare name would be resolved again in the terminal's
 * project cwd, where a relative PATH entry could supply a planted binary. */
const fenceShells = new Map<string, Record<string, string>>()

/** Absolute path of the shell a session actually launched, if the backend has
 *  reported one. Undefined until the `ready` frame arrives, and on a gateway
 *  that does not report it -- callers must treat undefined as "unknown" and
 *  not guess. */
export function getTerminalShell(sessionId: string): string | undefined {
  return shells.get(sessionId)
}

/** Fence-nameable shells the backend resolved on this host, name -> absolute
 *  path. Empty until the `ready` frame arrives, and on a gateway that does not
 *  report them -- a shell missing from this map is one the caller must not try
 *  to invoke. */
export function getTerminalFenceShells(sessionId: string): Record<string, string> {
  return fenceShells.get(sessionId) ?? {}
}

function setSessionTitle(sessionId: string, title: string) {
  if (titles.get(sessionId) === title) return
  titles.set(sessionId, title)
  const ls = titleListeners.get(sessionId)
  if (ls) for (const cb of ls) cb()
}

/** React hook: a session's live terminal title (undefined until the first
 *  title frame arrives — callers fall back to the tab's default cwd title). */
export function useTerminalTitle(sessionId: string): string | undefined {
  return useSyncExternalStore(
    (cb) => {
      let s = titleListeners.get(sessionId)
      if (!s) { s = new Set(); titleListeners.set(sessionId, s) }
      s.add(cb)
      return () => { s?.delete(cb) }
    },
    () => titles.get(sessionId),
    () => undefined,
  )
}

export function registerTerminalWs(sessionId: string, ws: WebSocket) {
  registry.set(sessionId, ws)
  const ls = readyListeners.get(sessionId)
  if (ls) {
    readyListeners.delete(sessionId)
    for (const cb of ls) cb()
  }
}

export function unregisterTerminalWs(sessionId: string) {
  registry.delete(sessionId)
}

export function getTerminalWs(sessionId: string): WebSocket | null {
  const ws = registry.get(sessionId)
  return ws && ws.readyState === WebSocket.OPEN ? ws : null
}

/**
 * Run `cb` once the given session's shell is ready for input — immediately if
 * it already is. Returns an unsubscribe fn (no-op once it fires). Used by
 * "Run in terminal" to keep command batches behind shell initialization.
 */
export function onTerminalReady(sessionId: string, cb: () => void): () => void {
  if (getTerminalWs(sessionId)) { cb(); return () => {} }
  let set = readyListeners.get(sessionId)
  if (!set) { set = new Set(); readyListeners.set(sessionId, set) }
  set.add(cb)
  return () => { set?.delete(cb) }
}

/**
 * Send a line of code to a specific terminal session. Returns false if that
 * session has no open socket.
 */
export function sendToTerminalSession(sessionId: string, code: string): boolean {
  const ws = getTerminalWs(sessionId)
  if (!ws) return false
  try {
    ws.send(new TextEncoder().encode(code.trimEnd() + '\n'))
    return true
  } catch {
    return false
  }
}

/**
 * Send raw bytes to a terminal session WITHOUT appending a newline — used by
 * inline path completion to type an accepted suggestion into the shell's line
 * editor. Returns false if that session has no open socket.
 */
export function sendRawToTerminalSession(sessionId: string, data: string): boolean {
  const ws = getTerminalWs(sessionId)
  if (!ws) return false
  try {
    ws.send(new TextEncoder().encode(data))
    return true
  } catch {
    return false
  }
}

/* ── Persistent per-session connection manager ──
 * The WebSocket lives here (module scope), NOT in the TerminalView component,
 * so unmounting the terminal tab (activity-bar close, tab switch, chat switch,
 * route change) does NOT close the socket. The connection is created once per
 * session and torn down only on explicit tab close (disposeTerminalConnection,
 * called from CliPanel's disposeTerminalSession). Genuine socket drops
 * (reload/network/server) reconnect with backoff; the backend keeps the PTY
 * alive for the orphan-reaper window so a reconnect re-attaches. */

const MAX_RETRIES = 10
const BASE_DELAY_MS = 1000
const MAX_DELAY_MS = 30_000

/** Coarse connection state a session's terminal view can render. */
export type TerminalConnStatus = 'connected' | 'reconnecting' | 'disconnected'

interface Conn {
  term: Terminal
  fit: FitAddon
  cwd?: string | null
  ws: WebSocket | null
  disposed: boolean
  retries: number
  reconnectTimer?: ReturnType<typeof setTimeout>
  status: TerminalConnStatus
  /**
   * Set when the user clicked "Reconnect" and cleared the moment the socket
   * next resolves (connected) or the redial chain gives up (disconnected). It
   * scopes the "Reconnecting…" banner to a user-initiated attempt: ordinary
   * automatic redials (transient tab-hide/visibility blips) leave it false and
   * stay bannerless, preserving the deliberate anti-flicker behaviour.
   */
  manualRetry: boolean
}
const conns = new Map<string, Conn>()

/* ── Per-session connection status, published to the terminal view ──
 * The status lives on the Conn but is surfaced through its own listener set so
 * a view can subscribe with useSyncExternalStore without reaching into the
 * connection manager. 'disconnected' is the terminal state after the retry
 * ceiling is hit; 'reconnecting' covers both the automatic backoff chain and a
 * socket that has not opened yet. */
const statusListeners = new Map<string, Set<() => void>>()

function notifyStatus(sessionId: string) {
  const ls = statusListeners.get(sessionId)
  if (ls) for (const cb of ls) cb()
}

function setConnStatus(sessionId: string, c: Conn, status: TerminalConnStatus) {
  // A resolved outcome ends any user-initiated attempt: 'connected' means the
  // redial succeeded, 'disconnected' means the chain gave up — both return the
  // banner to its non-manual presentation.
  const clearManual = (status === 'connected' || status === 'disconnected') && c.manualRetry
  if (clearManual) c.manualRetry = false
  if (c.status === status) {
    // Status unchanged but the manual flag flipped off: still publish so the
    // "Reconnecting…" banner can retire.
    if (clearManual) notifyStatus(sessionId)
    return
  }
  c.status = status
  notifyStatus(sessionId)
}

/**
 * Mark a session as being under a user-initiated reconnect and publish it, so
 * the view can distinguish a manual retry (banner stays, as "Reconnecting…")
 * from an automatic redial (bannerless). Idempotent notify-wise.
 */
function setManualRetry(sessionId: string, c: Conn) {
  if (c.manualRetry) return
  c.manualRetry = true
  notifyStatus(sessionId)
}

/** Imperative read of whether a session's reconnect was user-initiated. */
function manualRetryActive(sessionId: string): boolean {
  return conns.get(sessionId)?.manualRetry ?? false
}

/**
 * React hook: whether this session's current reconnect attempt was started by
 * the user clicking "Reconnect". Drives the "Reconnecting…" banner, which is
 * deliberately NOT shown for ordinary automatic redials.
 */
export function useTerminalManualRetry(sessionId: string): boolean {
  return useSyncExternalStore(
    (cb) => {
      let s = statusListeners.get(sessionId)
      if (!s) { s = new Set(); statusListeners.set(sessionId, s) }
      s.add(cb)
      return () => { s?.delete(cb) }
    },
    () => manualRetryActive(sessionId),
    () => false,
  )
}

/** React hook: a session's live connection status. Undefined until a
 *  connection is managed for the session. */
export function useTerminalConnStatus(sessionId: string): TerminalConnStatus | undefined {
  return useSyncExternalStore(
    (cb) => {
      let s = statusListeners.get(sessionId)
      if (!s) { s = new Set(); statusListeners.set(sessionId, s) }
      s.add(cb)
      return () => { s?.delete(cb) }
    },
    () => conns.get(sessionId)?.status,
    () => undefined,
  )
}

/**
 * Re-arm and immediately redial a session's connection: clears the retry
 * ceiling and any pending backoff timer, then dials at once. Used by the
 * manual "Reconnect" button and by the network/visibility revive listeners.
 * No-op for a disposed, unmanaged, or already-live (OPEN/CONNECTING) socket.
 *
 * `manual` marks a user-initiated retry so the view keeps the banner visible
 * as "Reconnecting…"; the automatic revive listeners pass false so transient
 * redials stay bannerless.
 */
export function retryTerminalConnection(sessionId: string, manual = true): void {
  const c = conns.get(sessionId)
  if (!c || c.disposed) return
  if (manual) setManualRetry(sessionId, c)
  const rs = c.ws?.readyState
  if (rs === WebSocket.OPEN || rs === WebSocket.CONNECTING) return
  if (rs === WebSocket.CLOSING) {
    // The socket's own onclose is imminent and will schedule a redial. Dialing
    // here too would race it into two competing PTY attachments (each reset()
    // + scrollback replay corrupting the display). Reset the budget so that
    // onclose redials promptly even if the chain was exhausted, and let it own
    // the dial.
    c.retries = 0
    return
  }
  c.retries = 0
  clearTimeout(c.reconnectTimer)
  c.reconnectTimer = undefined
  connect(sessionId, c)
}

/**
 * Revive every managed session whose socket is not currently OPEN/CONNECTING.
 * Registered once at module load against the events that signal a stalled
 * backoff chain can make progress again: the tab regaining network ('online')
 * and a backgrounded tab returning to the foreground ('visibilitychange' →
 * visible), where mobile browsers freeze setTimeout so the backoff chain can
 * silently stall or exhaust while hidden.
 */
function reviveAllConnections() {
  for (const [sessionId] of conns) retryTerminalConnection(sessionId, false)
}

if (typeof window !== 'undefined') {
  window.addEventListener('online', reviveAllConnections)
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') reviveAllConnections()
  })
}

function connect(sessionId: string, c: Conn) {
  if (c.disposed) return
  if (c.retries >= MAX_RETRIES) {
    // Backoff exhausted: no further dial will happen until a revive event
    // (online / tab foreground) or a manual Reconnect re-arms it. This is the
    // one place dialing truly stops, so it owns the terminal 'disconnected'
    // state — an earlier onclose still has a redial pending.
    setConnStatus(sessionId, c, 'disconnected')
    return
  }
  setConnStatus(sessionId, c, 'reconnecting')
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const qs = c.cwd ? `?cwd=${encodeURIComponent(c.cwd)}` : ''
  const ws = new WebSocket(`${proto}//${location.host}/api/ws/terminal/${sessionId}${qs}`)
  ws.binaryType = 'arraybuffer'
  c.ws = ws

  ws.onopen = () => {
    c.retries = 0
    setConnStatus(sessionId, c, 'connected')
    // Server replays this session's scrollback on (re)connect; reset first so
    // the cached term's retained screen doesn't stack under the replay.
    c.term.reset()
    // The server upgrades the socket before it spawns a fresh login shell.
    // Registration waits for its explicit ready frame so automated command
    // batches cannot land while shell startup still owns the terminal.
    // Only fit when the terminal is actually laid out. A reconnect can fire
    // while this tab is hidden (display:none), where fit() measures 0×0 and
    // would ship bogus cols/rows to the PTY. When the tab becomes visible,
    // doRefit -> term.onResize sends the correct dimensions.
    if (c.term.element?.offsetParent) {
      c.fit.fit()
      ws.send(JSON.stringify({ type: 'resize', cols: c.term.cols, rows: c.term.rows }))
    }
  }

  ws.onmessage = (ev) => {
    if (ev.data instanceof ArrayBuffer) { c.term.write(new Uint8Array(ev.data)); return }
    if (typeof ev.data === 'string') {
      try {
        const m = JSON.parse(ev.data)
        if (m && m.type === 'ready') {
          // Record the shell BEFORE registering: registerTerminalWs drains the
          // ready listeners synchronously, and Run-in-terminal's listener reads
          // the shell to decide how to hand over the snippet.
          if (typeof m.shell === 'string' && m.shell) shells.set(sessionId, m.shell)
          if (m.fence_shells && typeof m.fence_shells === 'object') {
            fenceShells.set(sessionId, m.fence_shells as Record<string, string>)
          }
          registerTerminalWs(sessionId, ws)
        }
        if (m && m.type === 'title' && typeof m.text === 'string') setSessionTitle(sessionId, m.text)
        if (m && m.type === 'cwd' && typeof m.path === 'string') cwds.set(sessionId, m.path)
      } catch { /* ignore non-JSON control frames */ }
    }
  }

  ws.onclose = () => {
    unregisterTerminalWs(sessionId)
    if (c.disposed) return
    const attempt = c.retries++
    if (attempt >= MAX_RETRIES) {
      // Already past the ceiling (a straggler close after dialing stopped):
      // reflect the terminal state without scheduling another dial.
      setConnStatus(sessionId, c, 'disconnected')
      return
    }
    setConnStatus(sessionId, c, 'reconnecting')
    const delay = Math.min(BASE_DELAY_MS * 2 ** attempt, MAX_DELAY_MS)
    // The scheduled connect() flips to 'disconnected' itself once retries has
    // reached the ceiling, so exhaustion is surfaced even on the last redial.
    c.reconnectTimer = setTimeout(() => connect(sessionId, c), delay + delay * 0.2 * Math.random())
  }

  ws.onerror = () => ws.close()
}

/**
 * Ensure a persistent WebSocket exists for `sessionId`, wired to the given
 * (cached) xterm instance. Idempotent — safe to call on every TerminalView
 * mount; the socket is created only once and survives subsequent unmounts.
 */
export function ensureTerminalConnection(
  sessionId: string, term: Terminal, fit: FitAddon, cwd?: string | null,
): void {
  if (conns.has(sessionId)) return
  const c: Conn = { term, fit, cwd, ws: null, disposed: false, retries: 0, status: 'reconnecting', manualRetry: false }
  conns.set(sessionId, c)
  // Wire terminal I/O once (the term is cached for the session's lifetime;
  // its listeners are cleaned up by term.dispose() in destroyTerm).
  term.onData((data) => {
    if (c.ws?.readyState === WebSocket.OPEN) c.ws.send(new TextEncoder().encode(data))
  })
  term.onResize(({ cols, rows }) => {
    if (c.ws?.readyState === WebSocket.OPEN) c.ws.send(JSON.stringify({ type: 'resize', cols, rows }))
  })
  connect(sessionId, c)
}

/** Tear down a session's persistent connection (explicit tab close only). */
export function disposeTerminalConnection(sessionId: string): void {
  const c = conns.get(sessionId)
  if (!c) return
  c.disposed = true
  clearTimeout(c.reconnectTimer)
  if (c.ws) { c.ws.onclose = null; c.ws.close() }
  conns.delete(sessionId)
  unregisterTerminalWs(sessionId)
  titles.delete(sessionId)
  titleListeners.delete(sessionId)
  cwds.delete(sessionId)
  shells.delete(sessionId)
  fenceShells.delete(sessionId)
  statusListeners.delete(sessionId)
  // Drop any pending onTerminalReady callbacks. They're normally drained by
  // registerTerminalWs when the socket opens; if the tab is closed before the
  // WS ever connects, they'd otherwise leak in readyListeners indefinitely.
  readyListeners.delete(sessionId)
}
