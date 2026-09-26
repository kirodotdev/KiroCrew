import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { addTab } from './useBottomTerminal'
import { isTerminalEnabled, onTerminalReady, sendToTerminalSession, useTerminalEnabled } from '../utils/terminalRegistry'
import { bringBack } from '../utils/terminalPopout'
import { RUN_IN_TERMINAL_READY_DEADLINE_MS } from '../utils/fenceShell'
import { checkSensitiveCommand } from '../utils/sensitiveCommand'

type TerminalHost = 'unavailable' | 'detached' | 'docked'

/** Only a dashboard that mounts the terminal dock can accept command handoffs. */
export const TerminalHostContext = createContext<TerminalHost>('unavailable')

/** A separated bang is a command; Markdown images and shell history are ordinary chat. */
export function terminalCommand(value: string): string | null {
  const text = value.trimStart()
  return /^!(?:\s|$)/.test(text) ? text.slice(1).trimStart() : null
}

type Failure = 'unavailable' | 'host' | 'remote' | 'pending' | 'attachments' | 'empty' | 'limit' | 'failed'

interface Options {
  value: string
  slotId: string | null
  project?: string
  target?: 'local' | 'remote' | 'pending'
  hasAttachments: boolean
  expand: (text: string) => string
  clear: () => void
}

interface Confirmation extends Pick<Options, 'value' | 'slotId' | 'project' | 'target'> {
  raw: string
  code: string
  warnReason?: string
}

function commandRejection(
  options: Options, host: TerminalHost, raw: string | null, enabled = isTerminalEnabled(),
): Failure | null {
  if (raw === null || !options.target) return null
  if (options.target === 'pending') return 'pending'
  return options.target === 'remote' ? 'remote'
    : host === 'unavailable' ? 'host'
      : !enabled ? 'unavailable'
        : options.hasAttachments ? 'attachments'
          : !raw ? 'empty' : null
}

/** Owns a user-initiated terminal handoff, independently of the agent's turn. */
export function useTerminalCommand(options: Options) {
  const host = useContext(TerminalHostContext)
  const latest = useRef({ ...options, host })
  latest.current = { ...options, host }
  const [pending, setPending] = useState(false)
  const [failure, setFailure] = useState<Failure | null>(null)
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null)
  const confirmationRef = useRef<Confirmation | null>(null)
  const cancel = useRef<(() => void) | null>(null)
  const awaitingDock = useRef<(() => void) | null>(null)
  const command = options.target ? terminalCommand(options.value) : null
  const enabled = useTerminalEnabled()
  const blocked = commandRejection(options, host, command, enabled)
  const canAdvertise = options.target === 'local' && enabled && host === 'docked'
  const dismissFailure = useCallback(() => setFailure(null), [])
  const cancelConfirmation = useCallback(() => {
    confirmationRef.current = null
    setConfirmation(null)
  }, [])

  useEffect(() => {
    setFailure(null)
    setPending(false)
    cancelConfirmation()
    return () => {
      confirmationRef.current = null
      cancel.current?.()
      cancel.current = null
    }
  }, [options.slotId, options.project, options.target, cancelConfirmation])
  useEffect(() => { setFailure(null) }, [options.value, options.hasAttachments, host, enabled])
  useEffect(() => { if (host === 'docked') awaitingDock.current?.() }, [host])

  const run = useCallback(() => {
    if (cancel.current || confirmationRef.current) return
    const snapshot = latest.current
    const raw = terminalCommand(snapshot.value)
    if (raw === null || !snapshot.target) return
    const reject = commandRejection(snapshot, snapshot.host, raw)
    if (reject) { setFailure(reject); return }
    const code = snapshot.expand(raw)

    setFailure(null)
    // Drafts can contain agent-authored follow-ups, history, or optimized text.
    // Confirm every source, showing exactly the expanded text we will deliver.
    const request = {
      value: snapshot.value, slotId: snapshot.slotId, project: snapshot.project,
      target: snapshot.target, raw, code, warnReason: checkSensitiveCommand(code)?.reason,
    }
    confirmationRef.current = request
    setConfirmation(request)
  }, [])

  const confirm = useCallback(() => {
    // The dialog remains mounted during its exit animation. A cancelled or
    // already-consumed request must not run through that stale button.
    if (!confirmation || confirmationRef.current !== confirmation || cancel.current) return
    const snapshot = confirmation
    const { raw, code } = snapshot
    cancelConfirmation()
    const ownsContext = () => (
      latest.current.slotId === snapshot.slotId
      && latest.current.project === snapshot.project
      && latest.current.target === snapshot.target
    )
    if (!ownsContext()) return
    const reject = commandRejection(latest.current, latest.current.host, raw)
    if (reject) { setFailure(reject); return }

    setPending(true)
    let settled = false
    let unsubscribe = () => {}
    const stop = () => {
      settled = true
      clearTimeout(timer)
      unsubscribe()
      awaitingDock.current = null
      cancel.current = null
    }
    const finish = (ok: boolean, reason: Failure = 'failed') => {
      if (settled) return
      stop()
      setPending(false)
      if (!ok) { setFailure(reason); return }
      // A shell can take time to initialize. Never erase text typed meanwhile.
      if (
        ownsContext()
        && latest.current.value === snapshot.value
        && latest.current.expand(raw) === code
      ) {
        latest.current.clear()
      }
    }
    const timer = setTimeout(() => finish(false), RUN_IN_TERMINAL_READY_DEADLINE_MS)
    cancel.current = stop
    const ownsDock = () => (
      latest.current.host === 'docked'
      && ownsContext()
      && !commandRejection(latest.current, latest.current.host, raw)
    )
    const start = () => {
      awaitingDock.current = null
      if (settled) return
      if (!ownsDock()) { finish(false); return }
      const sessionId = addTab(snapshot.project || undefined)
      if (!sessionId) { finish(false, 'limit'); return }
      unsubscribe = onTerminalReady(
        sessionId,
        () => {
          if (!settled) finish(ownsDock() && sendToTerminalSession(sessionId, code, { preserveTrailingWhitespace: true }))
        },
        () => finish(false),
      )
    }
    if (latest.current.host === 'detached') {
      // The popout beacon can outlive bringBack(). Wait for App to mount the dock.
      awaitingDock.current = start
      bringBack()
    } else {
      start()
    }
  }, [confirmation, cancelConfirmation])

  return { active: command !== null, pending, blocked, failure, run, confirmation, confirm, cancelConfirmation, canAdvertise, dismissFailure }
}
