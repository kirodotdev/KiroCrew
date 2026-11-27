/**
 * The terminal-session helpers that callers need without xterm.
 *
 * `CliPanel.tsx` imports xterm and its addons (~300 KB) at the top, so any
 * module that imports it pulls xterm into the dashboard's first load. The
 * side panel, the dock, run-in-terminal and exit-close only need to close,
 * delete, search or prewarm a session, so those helpers live here and
 * `CliPanel` loads on demand (`LazyCliPanel`).
 */
import { useMutation } from '@tanstack/react-query'
import { apiTransport } from '../api/apiTransport'
import { disposeTerminalConnection } from '../utils/terminalRegistry'
import { setTerminalCloseFailed } from '../hooks/useBottomTerminal'

/** Each mounted terminal pane's "open the find bar" action, by session id. */
export const searchOpeners = new Map<string, () => void>()

/** Open the scrollback find bar on a mounted terminal pane (the overflow
 *  menu's Find item). Returns false when no pane for that id is mounted. */
export function openTerminalSearch(sessionId: string): boolean {
  const open = searchOpeners.get(sessionId)
  if (!open) return false
  open()
  return true
}

/** Disposes a session's cached xterm. Set by `CliPanel` when its chunk loads:
 *  until then no xterm exists, so there is nothing to dispose. */
let destroyTerm: ((id: string) => void) | null = null
export function registerDestroyTerm(fn: (id: string) => void): void { destroyTerm = fn }

type CliPanelModule = typeof import('./CliPanel')

/** The `CliPanel` module once its chunk has loaded, so a pane that mounts
 *  later renders it in the same commit instead of suspending. */
let loadedModule: CliPanelModule | null = null
export function loadedTerminalModule(): CliPanelModule | null { return loadedModule }
export function __forgetTerminalModuleForTests(): void { loadedModule = null }

/** Loads in flight, by kind: a pane waiting to draw, or a speculative load
 *  (hover, phone idle, prewarm) nobody is waiting on yet. */
const inFlight = { demanded: 0, speculative: 0 }

function load(kind: keyof typeof inFlight): Promise<CliPanelModule> {
  inFlight[kind]++
  return import('./CliPanel')
    .then(m => (loadedModule = m))
    .finally(() => { inFlight[kind]-- })
}

/** Load the `CliPanel` chunk for a pane that is waiting to draw it (one fetch
 *  however often it is called). */
export function loadTerminalModule(): Promise<CliPanelModule> {
  return load('demanded')
}

const TERMINAL_CHUNK_RE = /\/assets\/CliPanel-[^/\s]*\.(?:js|css)\b/

/**
 * Whether a `vite:preloadError` is the terminal chunk failing while only a
 * speculative load wanted it. main.tsx then skips its stale-chunk reload: a
 * hover must not reload the page over unsaved work. A pane that asks for the
 * chunk later fails on its own and takes the reload path every lazy page takes.
 */
export function isSpeculativeTerminalChunkError(error: unknown): boolean {
  if (inFlight.speculative === 0 || inFlight.demanded > 0) return false
  const message = error && typeof error === 'object' && 'message' in error ? (error as { message?: unknown }).message : undefined
  if (typeof message !== 'string') return false
  // WebKit names no URL ("Importing a module script failed."). In this window
  // the speculative load is the likely source, and a wrong guess only shows
  // that other page's own Try Again notice instead of reloading.
  if (!/\/assets\//.test(message)) return true
  return TERMINAL_CHUNK_RE.test(message)
}

/** Prewarms asked for while the `CliPanel` chunk is still loading. A dispose
 *  in that window drops the id, so the late prewarm builds no xterm for a
 *  closed tab. */
const pendingPrewarms = new Set<string>()

/**
 * Tear down a terminal tab's LOCAL state: close its persistent WS and dispose
 * the cached xterm instance. Killing the backend PTY is a separate server call
 * routed through useDeleteTerminalSession() (per the use-react-query guideline);
 * SidePanel.handleCloseTab fires both.
 */
export function disposeTerminalSession(sessionId: string): void {
  pendingPrewarms.delete(sessionId)
  disposeTerminalConnection(sessionId)
  destroyTerm?.(sessionId)
}

/** Load the `CliPanel` chunk, then build `id`'s xterm off screen (see
 *  `prewarmTerminal` in `CliPanel.tsx`). Best effort: a chunk that fails to
 *  load here shows its notice when the pane mounts. */
export function prewarmTerminal(id: string, width: number, height: number): void {
  pendingPrewarms.add(id)
  load('speculative').then(
    m => { if (pendingPrewarms.delete(id)) m.prewarmTerminal(id, width, height) },
    () => { pendingPrewarms.delete(id) },
  )
}

/** Start loading the `CliPanel` chunk (a no-op once it is loading or loaded). */
export function preloadTerminal(): void {
  load('speculative').catch(() => {})
}

/** Ref for a control that opens a terminal: pointing at, focusing or pressing
 *  it starts loading the chunk, so the open that follows rarely waits for it. */
export function preloadTerminalOnIntent(el: Element | null): void {
  if (!el) return
  for (const type of ['pointerenter', 'focus', 'pointerdown']) el.addEventListener(type, preloadTerminal, { once: true, passive: true })
}

/**
 * React Query mutation that kills a terminal's backend PTY
 * (DELETE /api/terminal/sessions/:id). Best-effort — a failed delete is
 * backstopped by the server-side orphan reaper — but routing it through a
 * mutation gives it the standard write lifecycle instead of a bare fetch.
 * Local teardown stays synchronous in disposeTerminalSession().
 *
 * `keepalive` lets the request outlive the document that issued it: the
 * terminal popout returns itself to the main window the moment its last tab
 * closes, and without it that final DELETE would be aborted with the window.
 * A rejection is recorded in the shared close-failed flag (every consumer
 * reports it the same way), which the always-mounted panel root renders.
 */
export function useDeleteTerminalSession() {
  return useMutation({
    mutationFn: async (sessionId: string) => {
      const res = await apiTransport.request(`/api/terminal/sessions/${sessionId}`, { method: 'DELETE', keepalive: true })
      // 404 means the server has no such session any more: the PTY was already
      // reaped (idle sweep, gateway restart, a second close racing this one).
      // The shell is stopped, which is the outcome the user asked for, so this
      // is success, not a failure to report.
      if (res.status === 404) return
      if (!res.ok) throw new Error(`Failed to delete terminal session (${res.status})`)
    },
    // Mutation-level (not per-`mutate`) so it still fires after the caller has
    // unmounted — closing the LAST tab hides the strip that would otherwise
    // render the failure.
    onError: () => setTerminalCloseFailed(true),
  })
}
