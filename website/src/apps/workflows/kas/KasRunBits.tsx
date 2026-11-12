/**
 * Small shared pieces of the IDE-style KAS run surfaces (in-chat card, side
 * panel list and run detail): status icons, step glyphs, a 1 Hz clock for
 * live elapsed times, and the icon-button run controls.
 *
 * The controls follow `row.controls` through `kasModel.ts` exactly: one
 * primary (Pause / Resume, or an inert "Pausing…" glyph), Stop in its own slot
 * and always confirmed, Delete only on a finished run.
 */
import { useEffect, useRef, useState, type MutableRefObject } from 'react'
import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  Circle,
  Loader2,
  Pause,
  PauseCircle,
  Play,
  PlugZap,
  SkipForward,
  Square,
  Trash2,
  XCircle,
} from 'lucide-react'

import type { KasRunRow } from '../../../api/client'
import { IconButton } from '../../../components/ui'
import { useConfirm } from '../../../components/ConfirmDialog'
import { i18nT } from '../../../i18n/t'
import { canDelete, canStop, primaryControl, runTitle, showReconnect } from './kasModel'
import { useKasRunActions } from './useKasWorkflows'

const SPIN = 'motion-safe:animate-spin'

/**
 * Gap 25: withdraws an open Stop confirm the moment the thing it would stop
 * can no longer be stopped (the run or step ended while the dialog was up), so
 * the person is never left confirming an act that would only earn a 409.
 *
 * Returns the ref the caller sets to true for the duration of ITS stop ask; a
 * Delete or Run-again confirm from the same `useConfirm` is never withdrawn,
 * because those stay valid on a finished run.
 */
export function useWithdrawStaleStop(stoppable: boolean, withdraw: () => void): MutableRefObject<boolean> {
  const asking = useRef(false)
  useEffect(() => {
    if (asking.current && !stoppable) withdraw()
  }, [stoppable, withdraw])
  return asking
}

/** The run's state icon. Attention overrides it with a warning and no spin. */
export function KasRunStatusIcon({ display, attention, size = 14 }: { display: string; attention?: boolean; size?: number }) {
  if (attention) return <AlertTriangle size={size} className="text-warn shrink-0" aria-hidden="true" data-icon="warning" />
  switch (display) {
    case 'running':
    case 'pausing':
      return <Loader2 size={size} className={`text-accent shrink-0 ${SPIN}`} aria-hidden="true" data-icon="running" />
    case 'paused':
      return <PauseCircle size={size} className="text-warn shrink-0" aria-hidden="true" data-icon="paused" />
    case 'completed':
      return <CheckCircle2 size={size} className="text-ok shrink-0" aria-hidden="true" data-icon="completed" />
    case 'failed':
      return <XCircle size={size} className="text-danger shrink-0" aria-hidden="true" data-icon="failed" />
    default:
      return <Ban size={size} className="text-muted shrink-0" aria-hidden="true" data-icon="stopped" />
  }
}

/** A step's glyph: ✔ done, spinner running, ○ pending, ✕ failed, ⤼ skipped, grey stopped. */
export function KasStepGlyph({ status, size = 13 }: { status: string; size?: number }) {
  switch (status) {
    case 'running':
      return <Loader2 size={size} className={`text-accent shrink-0 ${SPIN}`} aria-hidden="true" data-glyph="running" />
    case 'paused':
      return <PauseCircle size={size} className="text-warn shrink-0" aria-hidden="true" data-glyph="paused" />
    case 'completed':
      return <CheckCircle2 size={size} className="text-ok shrink-0" aria-hidden="true" data-glyph="completed" />
    case 'failed':
      return <XCircle size={size} className="text-danger shrink-0" aria-hidden="true" data-glyph="failed" />
    case 'aborted':
      return <Ban size={size} className="text-muted shrink-0" aria-hidden="true" data-glyph="stopped" />
    case 'skipped':
      return <SkipForward size={size} className="text-muted shrink-0" aria-hidden="true" data-glyph="skipped" />
    default:
      return <Circle size={size} className="text-muted shrink-0" aria-hidden="true" data-glyph="pending" />
  }
}

/** The step status a screen reader hears beside its glyph. */
export function stepStatusLabel(status: string): string {
  switch (status) {
    case 'running':
      return i18nT('apps.workflows.kas.state_running')
    case 'paused':
      return i18nT('apps.workflows.kas.state_paused')
    case 'completed':
      return i18nT('apps.workflows.kas.state_completed')
    case 'failed':
      return i18nT('apps.workflows.kas.state_failed')
    case 'aborted':
      return i18nT('apps.workflows.kas.state_stopped')
    case 'skipped':
      return i18nT('apps.workflows.kas.step_skipped')
    default:
      return i18nT('apps.workflows.kas.step_pending')
  }
}

/** `Date.now()`, re-read every second while `live`; frozen otherwise. */
export function useNow(live: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!live) return
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [live])
  return now
}

function errorText(error: unknown): string | null {
  if (!error) return null
  return error instanceof Error ? error.message : String(error)
}

/**
 * Pause / Resume, Stop (confirmed) and, with `withDelete`, Delete (confirmed)
 * as small icon buttons. Callers place them BESIDE their open button, never
 * inside it, so a control click cannot also open the run.
 */
export function KasRunIconControls({
  row,
  withDelete = false,
  stopOnly = false,
}: {
  row: KasRunRow
  withDelete?: boolean
  /** Read-only mode (workflows off, run still held): show only Stop. */
  stopOnly?: boolean
}) {
  const { control, reconnect } = useKasRunActions()
  const { confirm, confirmDialog, cancel: withdraw } = useConfirm()
  const stopAsk = useWithdrawStaleStop(canStop(row), withdraw)
  const primary = primaryControl(row)
  const title = runTitle(row)
  const busy = control.isPending && control.variables?.workflowId === row.workflowId
  const act = (action: 'pause' | 'resume' | 'cancel' | 'delete') => control.mutate({ workflowId: row.workflowId, action })

  const stop = async () => {
    stopAsk.current = true
    let ok = false
    try {
      ok = await confirm({
        title: i18nT('apps.workflows.kas.stop_title'),
        body: (
          <>
            <p className="m-0 mb-2 font-semibold text-text-strong">{title}</p>
            <p className="m-0">{i18nT('apps.workflows.kas.stop_body_warn')}</p>
          </>
        ),
        confirmLabel: i18nT('apps.workflows.kas.stop_run'),
      })
    } finally {
      stopAsk.current = false
    }
    // Gap 25: a run that ended while the dialog was open withdraws it (a "no"),
    // so a yes here was given to a run that could still be stopped.
    if (ok) act('cancel')
  }

  const remove = async () => {
    const ok = await confirm({
      title: i18nT('apps.workflows.kas.delete_title'),
      body: (
        <>
          <p className="m-0 mb-2 font-semibold text-text-strong">{title}</p>
          <p className="m-0">{i18nT('apps.workflows.kas.delete_body')}</p>
        </>
      ),
      confirmLabel: i18nT('apps.workflows.kas.delete_run'),
    })
    if (ok) act('delete')
  }

  const failure = errorText(control.error ?? reconnect.error)
  const showDelete = !stopOnly && withDelete && canDelete(row)
  const showPrimary =
    !stopOnly && (primary?.kind === 'pause' || primary?.kind === 'resume' || primary?.kind === 'pausing')
  const showReconnectBtn = !stopOnly && showReconnect(row)
  const anything = showPrimary || showReconnectBtn || canStop(row) || showDelete
  if (!anything && !failure) return confirmDialog

  return (
    <span className="inline-flex items-center gap-0.5 shrink-0" data-testid="kas-run-controls">
      {showPrimary && primary?.kind === 'pause' ? (
        <IconButton aria-label={i18nT('apps.workflows.kas.pause')} onClick={() => act('pause')} disabled={busy}>
          <Pause size={13} aria-hidden="true" />
        </IconButton>
      ) : showPrimary && primary?.kind === 'resume' ? (
        <IconButton variant="accent" aria-label={i18nT('apps.workflows.kas.resume')} onClick={() => act('resume')} disabled={busy}>
          <Play size={13} aria-hidden="true" />
        </IconButton>
      ) : showPrimary && primary?.kind === 'pausing' ? (
        // Inert, where the Pause just pressed sat, so it does not simply vanish.
        <span role="img" className="p-[4px] text-muted inline-flex" aria-label={i18nT('apps.workflows.kas.pausing')} title={i18nT('apps.workflows.kas.pausing')}>
          <Pause size={13} aria-hidden="true" />
        </span>
      ) : null}
      {showReconnectBtn ? (
        <IconButton
          aria-label={i18nT('apps.workflows.kas.reconnect')}
          title={i18nT('apps.workflows.kas.reconnect_hint')}
          onClick={() => row.slotKey && reconnect.mutate(row.slotKey)}
          disabled={reconnect.isPending}
        >
          <PlugZap size={13} aria-hidden="true" />
        </IconButton>
      ) : null}
      {canStop(row) ? (
        <IconButton variant="danger" aria-label={i18nT('apps.workflows.kas.stop')} onClick={() => void stop()} disabled={busy}>
          <Square size={12} aria-hidden="true" />
        </IconButton>
      ) : null}
      {showDelete ? (
        <IconButton variant="danger" aria-label={i18nT('apps.workflows.kas.delete')} onClick={() => void remove()} disabled={busy}>
          <Trash2 size={13} aria-hidden="true" />
        </IconButton>
      ) : null}
      {failure ? (
        // A control that silently does nothing reads as broken: say it failed.
        <span role="img" className="p-[4px] text-danger inline-flex" aria-label={failure} title={failure} data-testid="kas-control-error">
          <AlertTriangle size={13} aria-hidden="true" />
        </span>
      ) : null}
      {confirmDialog}
    </span>
  )
}

/** A step's agent, as a small chip. */
export function AgentTag({ agent }: { agent: string }) {
  return (
    <span className="shrink-0 rounded px-1.5 py-px text-[10.5px] font-mono bg-bg-hover text-muted border border-border" data-testid="kas-agent-tag">
      {agent}
    </span>
  )
}
