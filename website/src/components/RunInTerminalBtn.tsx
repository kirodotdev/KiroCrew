import { useState, useCallback, useRef, useEffect } from 'react'
import { SquareTerminal, Check, AlertCircle } from 'lucide-react'
import { checkSensitiveCommand } from '../utils/sensitiveCommand'
import RunInTerminalConfirm from './RunInTerminalConfirm'

import { i18nT } from '../i18n/t'
export const SHELL_LANGS = new Set(['bash', 'sh', 'shell', 'zsh', 'console', 'terminal', 'fish'])

function stripPromptChars(code: string): string {
  return code.replace(/^[\$>]\s+/gm, '')
}

/**
 * "Run in terminal" button on shell code blocks. Dispatches a `mc:run-in-terminal`
 * request; ChatPage opens a fresh terminal tab in the current chat (starting in
 * that chat's working directory) and runs the command there, then echoes back a
 * `mc:run-in-terminal-result` so we can flash sent/failed. Correlated by reqId
 * so overlapping runs don't cross wires.
 *
 * Every click opens a confirmation dialog first. The code block clips long lines
 * (the <pre> scrolls horizontally), so the visible text is not necessarily the
 * whole command — the dialog shows the exact string that will run, wrapped.
 */
export default function RunInTerminalBtn({ code }: { code: string }) {
  const [status, setStatus] = useState<'idle' | 'sent' | 'error'>('idle')
  const [pending, setPending] = useState<{ command: string; warnReason: string } | null>(null)
  const flashTimerRef = useRef<ReturnType<typeof setTimeout>>()
  const resultTimerRef = useRef<ReturnType<typeof setTimeout>>()
  const resultUnsubRef = useRef<(() => void) | null>(null)

  useEffect(() => () => {
    clearTimeout(flashTimerRef.current)
    clearTimeout(resultTimerRef.current)
    resultUnsubRef.current?.()
  }, [])

  const flash = useCallback((s: 'sent' | 'error') => {
    clearTimeout(flashTimerRef.current)
    setStatus(s)
    flashTimerRef.current = setTimeout(() => setStatus('idle'), s === 'sent' ? 1200 : 2000)
  }, [])

  const execute = useCallback((cleaned: string) => {
    const reqId = Math.random().toString(36).slice(2)
    const onResult = (e: Event) => {
      if ((e as CustomEvent).detail?.reqId !== reqId) return
      resultUnsubRef.current?.()
      flash((e as CustomEvent).detail.ok ? 'sent' : 'error')
    }
    // Single unsub clears both the listener and the fallback timer.
    resultUnsubRef.current?.()
    window.addEventListener('mc:run-in-terminal-result', onResult)
    resultTimerRef.current = setTimeout(() => { resultUnsubRef.current?.(); flash('error') }, 8000)
    resultUnsubRef.current = () => {
      window.removeEventListener('mc:run-in-terminal-result', onResult)
      clearTimeout(resultTimerRef.current)
      resultUnsubRef.current = null
    }
    window.dispatchEvent(new CustomEvent('mc:run-in-terminal', { detail: { code: cleaned, reqId } }))
  }, [flash])

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
        onConfirm={confirmRun}
        onCancel={cancelRun}
      />
    </>
  )
}
