import { useEffect, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { addTab as addDockTerminal, hasTab as hasDockTerminal, removeTab as removeDockTerminal } from './useBottomTerminal'
import {
  onTerminalReady, sendToTerminalSession, getTerminalShell, getTerminalFenceShells,
} from '../utils/terminalRegistry'
import {
  RUN_IN_TERMINAL_OPENING_GRACE_MS,
  RUN_IN_TERMINAL_READY_DEADLINE_MS,
  runInTerminalText,
} from '../utils/fenceShell'
import { isPopoutOpen as isTerminalPopoutOpen } from '../utils/terminalPopout'
import { disposeTerminalSession, useDeleteTerminalSession } from '../components/CliPanel'
import { errMessage } from '../utils/thunkError'
import { i18nT } from '../i18n/t'

/** Kept as the bridge-local name used by its focused tests. */
export const RUN_IN_TERMINAL_TIMEOUT_MS = RUN_IN_TERMINAL_READY_DEADLINE_MS

export type RunInTerminalErrorHandler = (message: string) => void

/**
 * App-wide listener for the post-confirmation `mc:run-in-terminal` event.
 *
 * `RunInTerminalBtn` owns the confirmation dialog and the sensitive-command
 * check. This event is the transport after that decision, not a permission
 * boundary: every new first-party dispatcher must reuse the same confirmation
 * flow before dispatching it. The bridge deliberately does not execute raw
 * app-panel requests on behalf of an unconfirmed caller.
 *
 * The handler lives at the shell level because the dock panel is shell-level.
 * When it was mounted by ChatPage, a request from another route had no receiver.
 *
 * @param cwd Working directory for a new dock tab — the selected session's
 *            project, or undefined for the server default.
 * @param hasDock False in popout/embed windows that render no dock panel.
 * @param onError Receives user-visible liveness/rollback errors. The shell
 *                supplies this so failures remain visible outside ChatPage.
 */
export function useRunInTerminalBridge(
  cwd: string | undefined,
  hasDock = true,
  onError?: RunInTerminalErrorHandler,
): void {
  const cwdRef = useRef(cwd)
  cwdRef.current = cwd
  const queryClient = useQueryClient()
  const queryClientRef = useRef(queryClient)
  queryClientRef.current = queryClient
  const deleteTerminalSession = useDeleteTerminalSession()
  const deleteTerminalSessionRef = useRef(deleteTerminalSession)
  deleteTerminalSessionRef.current = deleteTerminalSession
  const onErrorRef = useRef(onError)
  onErrorRef.current = onError

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

      // The shell is known only once `ready` arrives, so read its shell at that
      // point rather than when the request is dispatched.
      const unsub = onTerminalReady(sessionId, () => {
        const text = runInTerminalText(
          code, lang, getTerminalShell(sessionId), getTerminalFenceShells(sessionId),
        )
        emit(sendToTerminalSession(sessionId, text))
      })

      // A missing ready frame is not enough to prove the dispatch died: a shell
      // profile can replace the readiness hook while the child stays live.
      setTimeout(() => {
        if (settled) return
        unsub()
        emit(false)

        // Closing the tab or popping the panel out transfers teardown ownership.
        if (!hasDockTerminal(sessionId) || isTerminalPopoutOpen()) return

        void (async () => {
          const probe = async (reuseMs: number) => {
            const payload: unknown = await queryClientRef.current.fetchQuery({
              queryKey: ['terminal-sessions'],
              queryFn: async () => {
                const response = await fetch('/api/terminal/sessions')
                if (!response.ok) {
                  throw new Error(`Failed to list terminal sessions (${response.status})`)
                }
                return response.json()
              },
              staleTime: reuseMs,
            })
            if (
              !payload
              || typeof payload !== 'object'
              || !('sessions' in payload)
              || !Array.isArray(payload.sessions)
            ) {
              throw new Error('Invalid terminal sessions response')
            }
            const found: Record<string, unknown> | undefined = payload.sessions.find(
              (entry: unknown): entry is Record<string, unknown> => (
                !!entry
                && typeof entry === 'object'
                && 'session_id' in entry
                && entry.session_id === sessionId
              ),
            )
            if (found && typeof found.alive !== 'boolean') {
              throw new Error('Invalid terminal session liveness response')
            }
            return found
          }

          let session: Record<string, unknown> | undefined
          try {
            session = await probe(1_000)
            if (!session) {
              // The backend skips a placeholder while a shell is still opening.
              await new Promise(resolve => setTimeout(resolve, RUN_IN_TERMINAL_OPENING_GRACE_MS))
              if (!hasDockTerminal(sessionId) || isTerminalPopoutOpen()) return
              session = await probe(0)
            }
          } catch (error) {
            // Keeping an uncertain tab is safer than deleting a live shell.
            // eslint-disable-next-line no-console -- probe failures need a dev breadcrumb
            console.warn('run-in-terminal: liveness probe failed:', errMessage(error))
            onErrorRef.current?.(i18nT('pages.chatPage.run_in_terminal_liveness_probe_failed_error'))
            return
          }

          if (!hasDockTerminal(sessionId) || isTerminalPopoutOpen()) return
          if (session?.alive === true) {
            onErrorRef.current?.(i18nT('pages.chatPage.run_in_terminal_shell_alive_error'))
            return
          }

          // The backend session is known dead or absent. Dispose the local
          // connection before removing the store entry, matching tab-close order.
          if (session) deleteTerminalSessionRef.current.mutate(sessionId)
          disposeTerminalSession(sessionId)
          removeDockTerminal(sessionId)
          onErrorRef.current?.(i18nT('pages.chatPage.run_in_terminal_dispatch_rolled_back_error'))
        })()
      }, RUN_IN_TERMINAL_READY_DEADLINE_MS)
    }

    window.addEventListener('mc:run-in-terminal', handler)
    return () => window.removeEventListener('mc:run-in-terminal', handler)
  }, [hasDock])
}
