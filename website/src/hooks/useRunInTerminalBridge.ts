import { useEffect, useId, useRef, type MutableRefObject } from 'react'
import { type QueryClient, useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import { disposeTerminalSession, useDeleteTerminalSession } from '../components/CliPanel'
import { i18nT } from '../i18n/t'
import { copyToClipboard } from '../utils/clipboard'
import { runInTerminalText, RUN_IN_TERMINAL_READY_DEADLINE_MS, RUN_IN_TERMINAL_OPENING_GRACE_MS } from '../utils/fenceShell'
import { isPopoutOpen as isTerminalPopoutOpen, focusPopout as focusTerminalPopout } from '../utils/terminalPopout'
import { onTerminalReady, sendToTerminalSession, getTerminalShell, getTerminalFenceShells, getTerminalInputWs } from '../utils/terminalRegistry'
import { errMessage } from '../utils/thunkError'
import { addTab as addDockTerminal, removeTab as removeDockTerminal, hasTab as hasDockTerminal, reuseCurrentTab as reuseDockTerminal } from './useBottomTerminal'

/** The refs the Run-in-terminal handler reads at event time. They mirror the
 *  host page's state so the listener installs once. */
export interface RunInTerminalRefs {
  /** Ends a Run-in-terminal tab's backend PTY when the dispatch rolls back. */
  deleteTerminalSessionRef: MutableRefObject<ReturnType<typeof useDeleteTerminalSession>>
  /** dashboard.terminal.reuse_current, and whether its read is still pending or failed. */
  terminalReuseRef: MutableRefObject<boolean>
  terminalCfgUnsettledRef: MutableRefObject<boolean>
  terminalCfgReadFailedRef: MutableRefObject<boolean>
}

export interface RunInTerminalBridgeOptions extends RunInTerminalRefs {
  queryClient: QueryClient
  showActionError: (message: string, title?: string) => void
  /** The owning chat's working directory; the dock terminal starts there. */
  cwdRef: MutableRefObject<string | undefined>
  /** False while the owning chat's cwd is not known yet: a run then answers
   *  `ok: false` at once rather than opening a shell in the wrong directory.
   *  Omitted means always ready (the chat page's behavior). */
  readyRef?: MutableRefObject<boolean>
  /** Also answer requests that carry no scope (an emitter outside every
   *  RunInTerminalScope). Only the chat page does: it is where every request
   *  went before scopes existed. */
  acceptUnscoped?: boolean
}

/**
 * The reuse-current setting and the PTY-delete mutation, mirrored onto refs for
 * useRunInTerminalBridge, for a host other than the chat page (which keeps its
 * own copies; its comments say why each exists). Shares the ['kirocrewConfig']
 * query key, so no extra fetch is made.
 */
export function useRunInTerminalRefs(): RunInTerminalRefs {
  const deleteTerminalSession = useDeleteTerminalSession()
  const deleteTerminalSessionRef = useRef(deleteTerminalSession)
  deleteTerminalSessionRef.current = deleteTerminalSession
  const { data, isError, isPending } = useQuery<{ dashboard?: { terminal?: { reuse_current?: boolean } } }>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    staleTime: 30_000,
  })
  const terminalReuseRef = useRef(false)
  terminalReuseRef.current = data?.dashboard?.terminal?.reuse_current === true
  const terminalCfgUnsettledRef = useRef(false)
  terminalCfgUnsettledRef.current = isPending === true
  const terminalCfgReadFailedRef = useRef(false)
  terminalCfgReadFailedRef.current = isError === true
  return { deleteTerminalSessionRef, terminalReuseRef, terminalCfgUnsettledRef, terminalCfgReadFailedRef }
}

/**
 * "Run in terminal" (from chat code blocks): open a terminal tab in the
 * app-wide dock panel and run the command in it, starting in the owning chat's
 * working dir. The dock panel persists across routes (unlike chat-scoped
 * terminal tabs) so the running shell survives navigation.
 *
 * Each host page owns the chats it renders: it wraps them in
 * `RunInTerminalScope` with the scope this hook returns, and the handler only
 * answers requests carrying that scope. When two hosts are mounted at once (a
 * chat page embedded in another page's panel), exactly one of them runs a
 * given click instead of both opening a terminal.
 *
 * Event names, detail shapes, `reqId` echoes and result ordering are the
 * contract RunInTerminalBtn depends on.
 */
export function useRunInTerminalBridge({
  queryClient,
  showActionError,
  cwdRef,
  readyRef,
  acceptUnscoped = false,
  deleteTerminalSessionRef,
  terminalReuseRef,
  terminalCfgUnsettledRef,
  terminalCfgReadFailedRef,
}: RunInTerminalBridgeOptions): string {
  const scope = useId()
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail || {}
      const code: string = detail.code
      const reqId: string = detail.reqId
      const lang: string | undefined = typeof detail.lang === 'string' ? detail.lang : undefined
      // Another host's chat (or, without acceptUnscoped, an emitter outside
      // every scope): not ours to answer.
      if (typeof detail.scope === 'string' ? detail.scope !== scope : !acceptUnscoped) return
      if (typeof code !== 'string' || !code) return
      // Opt-in reuse focuses the terminal tab the user selected and copies the
      // command for a manual paste. Sending raw bytes into a live terminal is
      // unsafe: a partially typed shell command or a foreground program owns
      // stdin, so appending `text + "\\n"` could merge and execute unrelated
      // input. The dashboard has no shell-prompt protocol that can prove an
      // empty, idle prompt, so manual paste is the safe boundary. With no
      // existing terminal to focus, fall through and mint a fresh tab, where
      // this dispatch owns the entire input stream and can safely send.
      if (terminalReuseRef.current || terminalCfgUnsettledRef.current) {
        // Reuse-on ALWAYS copies — never runs. The confirm dialog it shows
        // promises a copy for manual paste (RunInTerminalConfirm willCopy), and
        // the action must match that promise unconditionally: a fall-through to
        // minting a fresh tab and sending the bytes would EXECUTE a command the
        // dialog said would only be copied. The same copy-never-run path also
        // covers the UNSETTLED initial-read window: until the config query
        // settles we cannot know whether reuse was saved on, and copying on an
        // unknown is the non-destructive choice (a reuse-off user merely pastes
        // it themselves), whereas executing on an unknown is the harm removed.
        //
        // Focusing an existing tab is a best-effort nicety, so try to reuse one
        // and, if it lives in a popped-out window, raise that window — but the
        // copy proceeds whether or not a reusable tab exists. `reuseCurrentTab`
        // returns null when there is no settled tab to focus; that is not a
        // reason to run, only a reason to skip the focus step.
        const reusedId = reuseDockTerminal()
        // Raise the popped-out window whenever a tab was reused: the main-window
        // BroadcastChannel map (isTerminalPopoutOpen) is not synchronously
        // correct across a main-window reload — in the beacon-only window it
        // reads false though the popout is live — so gating the focus on it
        // leaves the reused window behind. focusTerminalPopout is a no-op when
        // no popout exists, so an unconditional call on a real reuse is safe.
        if (reusedId) focusTerminalPopout()
        // With a focused tab, fence-transform for that shell; with none, copy
        // the command verbatim (no target shell to transform for).
        const text = reusedId
          ? runInTerminalText(code, lang, getTerminalShell(reusedId), getTerminalFenceShells(reusedId))
          : code
        void copyToClipboard(text).then(copied => {
          // A refused copy is a user-facing failure, not a transient icon:
          // route it through ErrorNotice (errors-use-error-notice) so the
          // command the user asked to run is not silently lost.
          if (!copied) {
            showActionError(i18nT('pages.chatPage.run_in_terminal_copy_failed_error'))
          }
          window.dispatchEvent(new CustomEvent('mc:run-in-terminal-result', {
            detail: { reqId, ok: copied, copied },
          }))
        })
        return
      }
      // The owning chat's cwd is not known yet (a crewmate thread whose slot
      // record has not arrived): a shell opened now would start in the wrong
      // directory, so refuse at once, through ErrorNotice like every other
      // failed run, instead of leaving the button to time out.
      if (readyRef && !readyRef.current) {
        showActionError(i18nT('pages.chatPage.run_in_terminal_cwd_pending_error'))
        window.dispatchEvent(new CustomEvent('mc:run-in-terminal-result', { detail: { reqId, ok: false } }))
        return
      }
      // When reuse is off — whether the user set it off, or its config query is
      // still loading so the saved value is not yet known — the command opens a
      // fresh terminal. That fresh tab IS the shipped default, so no notice
      // fires for the off/pending case: a reuse-downgrade notice there would
      // assert a reuse preference the off-majority never set (a pending query is
      // indistinguishable from a genuinely-off setting).
      //
      // A config-read FAILURE is different, and is the errors-use-error-notice
      // case: the read errored, so a saved reuse-on is being silently ignored
      // with nothing on screen. Surface that read failure through ErrorNotice
      // (the query's isError reaching a user-facing notice), then still run in a
      // fresh tab so the command the user asked for is not dropped.
      if (terminalCfgReadFailedRef.current) {
        showActionError(i18nT('pages.chatPage.run_in_terminal_config_read_failed_error'))
      }
      const sessionId = addDockTerminal(cwdRef.current ?? undefined)
      let settled = false
      const emit = (ok: boolean) => {
        if (settled) return
        settled = true
        window.dispatchEvent(new CustomEvent('mc:run-in-terminal-result', { detail: { reqId, ok } }))
      }
      if (!sessionId) {
        // F3: no fresh tab could be minted (the terminal cap is full). The
        // command is neither copied nor run, so say so through ErrorNotice
        // rather than leaving only the button's error glyph
        // (errors-use-error-notice).
        showActionError(i18nT('pages.chatPage.run_in_terminal_no_tab_error'))
        emit(false); return
      }
      // The shell is known only once `ready` has arrived, which is exactly when
      // this fires — so read it here, not at dispatch time.
      const unsub = onTerminalReady(sessionId, () => {
        const text = runInTerminalText(
          code, lang, getTerminalShell(sessionId), getTerminalFenceShells(sessionId),
        )
        emit(sendToTerminalSession(sessionId, text))
      }, () => {
        if (settled) return
        // Local disposal removes the socket before notifying failure, but the
        // tab-close caller removes the tab afterwards. Socket ownership here
        // distinguishes that release from an error on an upgraded connection;
        // it says nothing about whether the shell is alive.
        const ownsSocket = Boolean(getTerminalInputWs(sessionId))
        emit(false)
        if (!ownsSocket) return
        queueMicrotask(() => {
          // Let synchronous tab close / popout transfer finish before reporting.
          if (!hasDockTerminal(sessionId) || isTerminalPopoutOpen()) return
          showActionError(i18nT('pages.chatPage.run_in_terminal_liveness_probe_failed_error'))
        })
      })
      // Give the PTY time to connect. A missing `ready` frame is not enough to
      // prove the dispatch died because a shell profile can replace the
      // readiness hook while the child process stays live. At the deadline,
      // report failure for the button hint, then ask the existing terminal
      // sessions route whether this dispatch's shell is still running.
      // `settled` distinguishes the normal ready path: once ready has fired,
      // the result is already emitted and the deadline does nothing.
      setTimeout(() => {
        if (settled) return
        unsub()
        emit(false)

        // Only probe while this dispatch still owns the tab it minted. Closing
        // the tab or popping the panel out transfers teardown ownership.
        if (!hasDockTerminal(sessionId) || isTerminalPopoutOpen()) return

        void (async () => {
          // One look at the sessions route. `reuseMs` is the cache window: the
          // first probe shares a request with any concurrent deadline, the
          // confirm probe must see the present.
          const probe = async (reuseMs: number) => {
            const payload: unknown = await queryClient.fetchQuery({
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
            // Concurrent deadlines are what this reuse window dedupes, so it is
            // far shorter than the deadline itself: a session young enough to be
            // missing from a reused snapshot cannot have reached its own
            // deadline yet, so no probe can read a snapshot older than itself.
            session = await probe(1_000)
            if (!session) {
              // Absent is not gone. A shell still opening holds a placeholder
              // the sessions route skips, so it reads exactly like a session
              // that never existed -- and rolling that back would remove the tab
              // from under a shell about to come up. Confirm once, uncached,
              // after a bounded grace.
              await new Promise(resolve => setTimeout(resolve, RUN_IN_TERMINAL_OPENING_GRACE_MS))
              if (!hasDockTerminal(sessionId) || isTerminalPopoutOpen()) return
              session = await probe(0)
            }
          } catch (error) {
            // Keep on probe failure: removing a possibly-live shell and its
            // scrollback is irreversible. The tab is user-closable, and the
            // backend orphan reaper backstops the PTY. The kept tab is
            // otherwise unexplained, so say so through the required surface --
            // and keep the probe's own transport error out of that copy, since
            // the user asked to run a command, not to list terminal sessions.
            // The console keeps it for whoever debugs the probe.
            // eslint-disable-next-line no-console -- a failed liveness probe is invisible in dev otherwise
            console.warn('run-in-terminal: liveness probe failed:', errMessage(error))
            showActionError(
              i18nT('pages.chatPage.run_in_terminal_liveness_probe_failed_error'),
            )
            return
          }

          // The user may close the tab or pop the panel out while the probe is
          // in flight. In either case this dispatch no longer owns it.
          if (!hasDockTerminal(sessionId) || isTerminalPopoutOpen()) return
          if (session?.alive === true) {
            // A profile that replaces the readiness hook (#7657) lands here on
            // EVERY click, so this is the routine outcome rather than an edge:
            // the terminal opens, the command never runs, and a 2s button flash
            // is too small to carry that. The shell is confirmed live, so the
            // tab is worth keeping and the silence is worth breaking.
            showActionError(i18nT('pages.chatPage.run_in_terminal_shell_alive_error'))
            return
          }

          // Same teardown, same order, as the tab-close paths: end the backend
          // PTY, drop the local WS + cached xterm, then remove the store entry.
          // A session the probe did not list is already gone from the backend
          // registry, so skip the DELETE -- it would 404 and surface a spurious
          // close failure for a session that needs no closing.
          if (session) deleteTerminalSessionRef.current.mutate(sessionId)
          disposeTerminalSession(sessionId)
          removeDockTerminal(sessionId)
          // Closing a tab the user watched open is the ROUTINE outcome here, so
          // it cannot be the quiet one: say what happened to the command.
          showActionError(i18nT('pages.chatPage.run_in_terminal_dispatch_rolled_back_error'))
        })()
      }, RUN_IN_TERMINAL_READY_DEADLINE_MS)
    }
    window.addEventListener('mc:run-in-terminal', handler)
    return () => window.removeEventListener('mc:run-in-terminal', handler)
    // All are stable for the host's lifetime (a context client, a []-dep
    // useCallback, refs and a useId), so the listener still installs once.
  }, [scope, acceptUnscoped, queryClient, showActionError, cwdRef, readyRef, deleteTerminalSessionRef, terminalReuseRef, terminalCfgUnsettledRef, terminalCfgReadFailedRef])
  return scope
}
