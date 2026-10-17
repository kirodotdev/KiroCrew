import { useState, useCallback, useRef, useEffect, useContext } from 'react'
import { useQuery, QueryClientContext } from '@tanstack/react-query'
import { SquareTerminal, Check, ClipboardCheck, AlertCircle } from 'lucide-react'
import { checkSensitiveCommand } from '../utils/sensitiveCommand'
import { RUN_IN_TERMINAL_RESULT_FALLBACK_MS } from '../utils/fenceShell'
import RunInTerminalConfirm from './RunInTerminalConfirm'
import { RunInTerminalScope } from './runInTerminalScope'
import { api } from '../api/client'

import { i18nT } from '../i18n/t'
export const SHELL_LANGS = new Set(['bash', 'sh', 'shell', 'zsh', 'console', 'terminal', 'fish'])

function stripPromptChars(code: string): string {
  return code.replace(/^[\$>]\s+/gm, '')
}

/**
 * "Run in terminal" button on shell code blocks. Dispatches a `mc:run-in-terminal`
 * request; the host page that owns this chat (the `RunInTerminalScope` it
 * provides) opens a fresh dock terminal tab starting in that chat's working
 * directory and runs the command there, then echoes back a
 * `mc:run-in-terminal-result` so we can flash sent/failed. Correlated by reqId
 * so overlapping runs don't cross wires.
 *
 * Every click opens a confirmation dialog first. The code block clips long lines
 * (the <pre> scrolls horizontally), so the visible text is not necessarily the
 * whole command — the dialog shows the exact snippet that will be run, wrapped.
 *
 * `lang` is the fence language the mount site already matched against
 * SHELL_LANGS to decide this button renders at all. It travels with the
 * request because the tag names the shell the snippet was written for, and a
 * consumer that only receives `code` cannot tell fish from bash.
 *
 * The dialog shows the SNIPPET, which is every character that will execute. On
 * the one path where the fence names a shell incompatible with the terminal's,
 * the handler delegates it to that shell (`'/usr/bin/fish' -c '<snippet>'`), so
 * the line in the scrollback carries that prefix. The delegation cannot be
 * decided here: which shell is running is only known once the terminal has
 * opened, which happens after this dialog is confirmed.
 */
export default function RunInTerminalBtn({ code, lang }: { code: string; lang?: string }) {
  // Read the reuse-current setting only when a QueryClient is in scope. Every
  // real mount sits under the app-level provider, but this button is embedded
  // in code blocks that many tests render in isolation without one, and a bare
  // useQuery throws there. Reading the context directly (undefined, not a throw,
  // when absent) lets the button default to the Run path off-provider instead
  // of requiring every embedder to supply a client.
  const hasClient = useContext(QueryClientContext) != null
  return hasClient
    ? <RunInTerminalBtnWithConfig code={code} lang={lang} />
    : <RunInTerminalBtnInner code={code} lang={lang} willCopy={false} />
}

/** Subscribes to the reuse-current setting; only mounted under a QueryClient. */
function RunInTerminalBtnWithConfig({ code, lang }: { code: string; lang?: string }) {
  // The reuse-current path COPIES the command instead of running it, so the
  // confirm dialog must say Copy, not Run. Only a literal `true` counts — the
  // same "one literal decides it" rule ChatPage's handler uses; a pending or
  // errored read reads as off here (the dialog defaults to Run), and ChatPage
  // owns the actual copy-vs-run decision and its read-failure notice at event
  // time. Shares the ['kirocrewConfig'] query key so no extra fetch is made.
  const { data: terminalCfg } = useQuery<{ dashboard?: { terminal?: { reuse_current?: boolean } } }>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    staleTime: 30_000,
  })
  const willCopy = terminalCfg?.dashboard?.terminal?.reuse_current === true
  return <RunInTerminalBtnInner code={code} lang={lang} willCopy={willCopy} />
}

function RunInTerminalBtnInner({ code, lang, willCopy }: { code: string; lang?: string; willCopy: boolean }) {
  // The host page that renders this chat; only it answers the request.
  const scope = useContext(RunInTerminalScope)
  const [status, setStatus] = useState<'idle' | 'sent' | 'copied' | 'error'>('idle')
  const [pending, setPending] = useState<{ command: string; warnReason: string } | null>(null)
  const flashTimerRef = useRef<ReturnType<typeof setTimeout>>()
  const resultTimerRef = useRef<ReturnType<typeof setTimeout>>()
  const resultUnsubRef = useRef<(() => void) | null>(null)

  useEffect(() => () => {
    clearTimeout(flashTimerRef.current)
    clearTimeout(resultTimerRef.current)
    resultUnsubRef.current?.()
  }, [])

  const flash = useCallback((s: 'sent' | 'copied' | 'error') => {
    clearTimeout(flashTimerRef.current)
    setStatus(s)
    flashTimerRef.current = setTimeout(() => setStatus('idle'), s === 'error' ? 2000 : 1200)
  }, [])

  const execute = useCallback((cleaned: string) => {
    const reqId = Math.random().toString(36).slice(2)
    const onResult = (e: Event) => {
      if ((e as CustomEvent).detail?.reqId !== reqId) return
      resultUnsubRef.current?.()
      const detail = (e as CustomEvent).detail
      flash(detail.ok ? (detail.copied ? 'copied' : 'sent') : 'error')
    }
    // Single unsub clears both the listener and the fallback timer.
    resultUnsubRef.current?.()
    window.addEventListener('mc:run-in-terminal-result', onResult)
    resultTimerRef.current = setTimeout(() => { resultUnsubRef.current?.(); flash('error') }, RUN_IN_TERMINAL_RESULT_FALLBACK_MS)
    resultUnsubRef.current = () => {
      window.removeEventListener('mc:run-in-terminal-result', onResult)
      clearTimeout(resultTimerRef.current)
      resultUnsubRef.current = null
    }
    const detail = scope == null ? { code: cleaned, reqId, lang } : { code: cleaned, reqId, lang, scope }
    window.dispatchEvent(new CustomEvent('mc:run-in-terminal', { detail }))
  }, [flash, lang, scope])

  const askToRun = useCallback(() => {
    const cleaned = stripPromptChars(code)
    if (!cleaned) return
    // Check both forms: a prompt-prefixed line only matches the patterns after stripping.
    const match = checkSensitiveCommand(code) ?? checkSensitiveCommand(cleaned)
    setPending({ command: cleaned, warnReason: match?.reason ?? '' })
  }, [code])

  const confirmRun = useCallback(() => {
    if (!pending) return
    setPending(null)
    execute(pending.command)
  }, [pending, execute])

  const cancelRun = useCallback(() => setPending(null), [])

  const trigger = (() => {
    if (status === 'copied') {
      return (
        <span className="p-1 rounded text-accent" title={i18nT('components.runInTerminalBtn.copied_paste_into_terminal')} aria-label={i18nT('components.runInTerminalBtn.copied_paste_into_terminal')}>
          <ClipboardCheck size={13} />
        </span>
      )
    }
    if (status === 'sent') {
      return (
        <span className="p-1 rounded text-accent" title={i18nT('components.runInTerminalBtn.sent_to_terminal')} aria-label={i18nT('components.runInTerminalBtn.sent_to_terminal')}>
          <Check size={13} />
        </span>
      )
    }
    if (status === 'error') {
      return (
        <span className="p-1 rounded text-danger" title={i18nT('components.runInTerminalBtn.couldn_t_run_in_terminal')} aria-label={i18nT('components.runInTerminalBtn.couldn_t_run_in_terminal')}>
          <AlertCircle size={13} />
        </span>
      )
    }
    return (
      <button
        className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
        onClick={askToRun}
        title={i18nT('components.runInTerminalBtn.run_in_terminal')}
        aria-label={i18nT('components.runInTerminalBtn.run_in_terminal')}
      >
        <SquareTerminal size={13} />
      </button>
    )
  })()

  return (
    <>
      {trigger}
      <RunInTerminalConfirm
        open={!!pending}
        command={pending?.command ?? ''}
        warnReason={pending?.warnReason || undefined}
        willCopy={willCopy}
        onConfirm={confirmRun}
        onCancel={cancelRun}
      />
    </>
  )
}
