import { useState, useCallback, useRef, useEffect } from 'react'
import { SquareTerminal, Check, AlertCircle, ShieldAlert } from 'lucide-react'
import { checkSensitiveCommand } from '../utils/sensitiveCommand'

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
 */
export default function RunInTerminalBtn({ code }: { code: string }) {
  const [status, setStatus] = useState<'idle' | 'sent' | 'error' | 'warn'>('idle')
  const [warnReason, setWarnReason] = useState('')
  const warnTimerRef = useRef<ReturnType<typeof setTimeout>>()
  const flashTimerRef = useRef<ReturnType<typeof setTimeout>>()
  const resultTimerRef = useRef<ReturnType<typeof setTimeout>>()
  const resultUnsubRef = useRef<(() => void) | null>(null)

  useEffect(() => () => {
    clearTimeout(warnTimerRef.current)
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

  const run = useCallback(() => {
    const cleaned = stripPromptChars(code)
    if (!cleaned) return

    const match = checkSensitiveCommand(code)
    if (match) {
      setWarnReason(match.reason)
      setStatus('warn')
      clearTimeout(warnTimerRef.current)
      warnTimerRef.current = setTimeout(() => setStatus('idle'), 8000)
      return
    }

    execute(cleaned)
  }, [code, execute])

  const confirmRun = useCallback(() => {
    clearTimeout(warnTimerRef.current)
    const cleaned = stripPromptChars(code)
    if (!cleaned) return
    setStatus('idle')
    execute(cleaned)
  }, [code, execute])

  const cancelWarn = useCallback(() => {
    clearTimeout(warnTimerRef.current)
    setStatus('idle')
  }, [])

  if (status === 'warn') {
    return (
      <span className="inline-flex items-center gap-1">
        <span className="text-[11px] text-warn truncate max-w-[180px]" title={warnReason}>
          <ShieldAlert size={11} className="inline mr-0.5" />{warnReason}
        </span>
        <button
          className="px-1.5 py-0.5 rounded text-[11px] bg-warn/20 text-warn hover:bg-warn/30 cursor-pointer"
          onClick={confirmRun}
          aria-label="Confirm run sensitive command"
        >
          Run anyway
        </button>
        <button
          className="px-1.5 py-0.5 rounded text-[11px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
          onClick={cancelWarn}
          aria-label="Cancel"
        >
          Cancel
        </button>
      </span>
    )
  }

  if (status === 'sent') {
    return (
      <span className="p-1 rounded text-accent" title="Sent to terminal" aria-label="Sent to terminal">
        <Check size={13} />
      </span>
    )
  }

  if (status === 'error') {
    return (
      <span className="p-1 rounded text-danger" title="Couldn't run in terminal" aria-label="Couldn't run in terminal">
        <AlertCircle size={13} />
      </span>
    )
  }

  return (
    <button
      className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
      onClick={run}
      title="Run in terminal"
      aria-label="Run in terminal"
    >
      <SquareTerminal size={13} />
    </button>
  )
}
