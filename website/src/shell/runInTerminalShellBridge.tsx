import { useCallback, useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'

import ErrorNotice from '../components/ErrorNotice'
import { useRunInTerminalBridge, useRunInTerminalRefs } from '../hooks/useRunInTerminalBridge'
import { i18nT } from '../i18n/t'

/**
 * The shell's "Run in terminal" receiver. A code block outside every chat (an
 * app page, a knowledge doc) sends its request with no scope, and before this
 * only the chat page answered those, so off /chat the button timed out. The
 * dock terminal is app-wide, so the shell answers them on every route, in the
 * selected session's project. It stands down while the chat page is mounted,
 * which still answers them itself (`unscopedFallback`).
 *
 * A hook, called from App's own body after useAppShell, so its config read
 * joins the shell's ['kirocrewConfig'] read at the same place in boot order.
 * `hasDock` is false in popout and embed windows, which render no dock panel:
 * there the shell answers nothing rather than open a tab no one can see.
 * Returns the failure to show and its dismiss.
 */
export function useRunInTerminalShellBridge(cwd: string | undefined, hasDock: boolean): { error: string; dismiss: () => void } {
  const queryClient = useQueryClient()
  const [error, setError] = useState('')
  const showActionError = useCallback((message: string) => setError(message), [])
  const dismiss = useCallback(() => setError(''), [])
  // A run that succeeds after a failure proves the old notice wrong: clear it,
  // so "Couldn't run in terminal" never sits over a terminal that just ran.
  // Except the config-read failure: that run still succeeds (in a fresh tab),
  // and the notice is about the ignored reuse setting, which success does not fix.
  useEffect(() => {
    const configReadFailed = i18nT('pages.chatPage.run_in_terminal_config_read_failed_error')
    const onResult = (e: Event) => {
      if ((e as CustomEvent).detail?.ok !== true) return
      setError(prev => (prev === configReadFailed ? prev : ''))
    }
    window.addEventListener('mc:run-in-terminal-result', onResult)
    return () => window.removeEventListener('mc:run-in-terminal-result', onResult)
  }, [])
  const cwdRef = useRef(cwd)
  cwdRef.current = cwd
  const refs = useRunInTerminalRefs()
  useRunInTerminalBridge({
    queryClient,
    showActionError,
    cwdRef,
    unscopedFallback: hasDock,
    ...refs,
  })
  return { error, dismiss }
}

/**
 * The shell receiver's failures, fixed over the page, because the page under
 * it may be any route. Dismiss-only: an Ask Agent hand-off navigates to chat,
 * and that would drop an unsaved edit on the page the user is on.
 */
export function RunInTerminalShellNotice({ error, onDismiss }: { error: string; onDismiss: () => void }) {
  if (!error) return null
  return (
    <div className="fixed left-1/2 top-safe-offset-3 z-[80] w-[min(640px,calc(100vw-1.5rem))] -translate-x-1/2 rounded-lg bg-card shadow-xl">
      {/* No hand-off: Ask Agent navigates to /chat, and this notice can sit
          over /artifacts/<slug> while its document editor holds unsaved edits
          (ArtifactDetailPage's `dirty` edit buffer, which navigating away
          destroys). The hand-off would discard that draft. */}
      <ErrorNotice
        title={i18nT('components.runInTerminalBtn.couldn_t_run_in_terminal')}
        message={error}
        onDismiss={onDismiss}
        askAgent={false}
        testId="run-in-terminal-error"
      />
    </div>
  )
}
