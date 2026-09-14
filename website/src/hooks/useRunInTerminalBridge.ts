import { useEffect, useRef } from 'react'
import { addTab as addDockTerminal } from './useBottomTerminal'
import {
  onTerminalReady, sendToTerminalSession, getTerminalShell, getTerminalFenceShells,
} from '../utils/terminalRegistry'
import { runInTerminalText } from '../utils/fenceShell'

/** How long a freshly minted dock terminal gets to connect its PTY before the
 *  request is answered `ok: false`. */
export const RUN_IN_TERMINAL_TIMEOUT_MS = 6000

/**
 * App-wide listener for the `mc:run-in-terminal` window event.
 *
 * The event is the one sanctioned way for page content to run a command in the
 * user's terminal: chat code blocks dispatch it via `RunInTerminalBtn`, and an
 * app panel can dispatch it too. The handler opens (or reuses, at the cap) a tab
 * in the app-wide dock panel, waits for its PTY to connect, writes the command,
 * and answers with `mc:run-in-terminal-result` carrying the same `reqId`.
 *
 * It lives at the shell level — not on the chat page — because the dock panel
 * itself is shell-level (`useBottomTerminal` is a module store rendered by App).
 * When the listener was mounted by ChatPage, a request dispatched from any other
 * route (`/apps/<id>`, `/projects`, …) had no listener at all and silently did
 * nothing, even though the dock was right there.
 *
 * @param cwd Working directory for a NEW dock tab — the selected session's
 *            project, or undefined for the server default. Read through a ref so
 *            the listener is registered once and never re-bound on slot changes.
 * @param hasDock False in windows that render no dock panel (popout, embed).
 *                The request is still answered — `ok: false`, at once — so the
 *                requester shows its failure state immediately instead of
 *                waiting out its own fallback timer; no tab is minted, because
 *                nothing there would render it.
 */
export function useRunInTerminalBridge(cwd: string | undefined, hasDock = true): void {
  const cwdRef = useRef(cwd)
  cwdRef.current = cwd
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail || {}
      const code: string = detail.code
      const reqId: string = detail.reqId
      const lang: string | undefined = typeof detail.lang === 'string' ? detail.lang : undefined
      if (typeof code !== 'string' || !code) return
      let settled = false
      const emit = (ok: boolean) => {
        if (settled) return
        settled = true
        window.dispatchEvent(new CustomEvent('mc:run-in-terminal-result', { detail: { reqId, ok } }))
      }
      if (!hasDock) { emit(false); return }
      const sessionId = addDockTerminal(cwdRef.current ?? undefined)
      if (!sessionId) { emit(false); return }
      // The shell is known only once `ready` has arrived, which is exactly when
      // this fires — so read it here, not at dispatch time.
      const unsub = onTerminalReady(sessionId, () => {
        const text = runInTerminalText(
          code, lang, getTerminalShell(sessionId), getTerminalFenceShells(sessionId),
        )
        emit(sendToTerminalSession(sessionId, text))
      })
      // Give the PTY time to connect; if it never does, report failure.
      setTimeout(() => { unsub(); emit(false) }, RUN_IN_TERMINAL_TIMEOUT_MS)
    }
    window.addEventListener('mc:run-in-terminal', handler)
    return () => window.removeEventListener('mc:run-in-terminal', handler)
  }, [hasDock])
}
