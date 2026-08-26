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

interface Conn {
  term: Terminal
  fit: FitAddon
  cwd?: string | null
  ws: WebSocket | null
  disposed: boolean
  retries: number
  reconnectTimer?: ReturnType<typeof setTimeout>
}
const conns = new Map<string, Conn>()

function connect(sessionId: string, c: Conn) {
  if (c.disposed || c.retries >= MAX_RETRIES) return
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const qs = c.cwd ? `?cwd=${encodeURIComponent(c.cwd)}` : ''
  const ws = new WebSocket(`${proto}//${location.host}/api/ws/terminal/${sessionId}${qs}`)
  ws.binaryType = 'arraybuffer'
  c.ws = ws

  ws.onopen = () => {
    c.retries = 0
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
        if (m && m.type === 'ready') registerTerminalWs(sessionId, ws)
        if (m && m.type === 'title' && typeof m.text === 'string') setSessionTitle(sessionId, m.text)
        if (m && m.type === 'cwd' && typeof m.path === 'string') cwds.set(sessionId, m.path)
      } catch { /* ignore non-JSON control frames */ }
    }
  }

  ws.onclose = () => {
    unregisterTerminalWs(sessionId)
    if (c.disposed) return
    const attempt = c.retries++
    if (attempt >= MAX_RETRIES) return
    const delay = Math.min(BASE_DELAY_MS * 2 ** attempt, MAX_DELAY_MS)
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
  const c: Conn = { term, fit, cwd, ws: null, disposed: false, retries: 0 }
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
  // Drop any pending onTerminalReady callbacks. They're normally drained by
  // registerTerminalWs when the socket opens; if the tab is closed before the
  // WS ever connects, they'd otherwise leak in readyListeners indefinitely.
  readyListeners.delete(sessionId)
}
