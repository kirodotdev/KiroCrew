import { useTranslation } from 'react-i18next'
import { useCallback, useRef, type ReactNode } from 'react'
import { Check, Goal, Pause, Play, Radar, RotateCw, X } from 'lucide-react'
import { type LegacyGoalLoop } from '../monitoring/automation'
import { i18nT } from '../i18n/t'
import { fmtDuration, fmtNumber } from '../i18n/format'
import { Badge, Btn, PanelSectionHeader } from './ui'
import { PopoverContent } from './ui/popover'
import ErrorNotice from './ErrorNotice'
import { useScrollEdgesY } from '../hooks/useScrollEdges'

export function goalStatusLabel(loop: LegacyGoalLoop): string {
  if (loop.stoppedReason === 'goal_pause_unsaved') {
    return i18nT('components.goalProgress.pause_unsaved')
  }
  if (loop.stoppedReason === 'approval_stalled') {
    return i18nT('components.goalProgress.approval_timed_out')
  }
  if (loop.stoppedReason === 'cycle_cap') {
    return i18nT('components.goalProgress.cycle_limited')
  }
  if (loop.stoppedReason === 'runtime_budget') {
    return i18nT('components.goalProgress.runtime_limited')
  }
  const status = loop.goal?.status ?? 'paused'
  if (!loop.active && status === 'paused' && loop.goalGeneration === undefined) {
    return i18nT('components.goalProgress.status_needs_refresh')
  }
  const keys: Record<string, string> = {
    suggested: 'components.goalProgress.suggested',
    working: 'components.goalProgress.working',
    waiting: 'components.goalProgress.waiting',
    needs_input: 'components.goalProgress.needs_input',
    paused: 'components.goalProgress.paused',
    blocked: 'components.goalProgress.blocked',
    complete: 'components.goalProgress.complete',
    ended: 'components.goalProgress.ended',
  }
  return i18nT(keys[status] ?? keys.paused)
}

export default function GoalProgressContent({
  loop, statusLabel, changeFailure, changeFailureDetail, pauseUnconfirmed, pending, onAction, onDismiss, settings, onSetUpBoundedMonitor,
}: {
  loop: LegacyGoalLoop
  statusLabel: string
  changeFailure?: string
  /** The failed request's own error text, kept as the notice message for Ask Agent. */
  changeFailureDetail?: string
  pauseUnconfirmed: boolean
  pending: boolean
  onAction: () => void
  /** Removes an unstarted suggestion; offered only when its revision is known. */
  onDismiss?: () => void
  settings?: ReactNode
  onSetUpBoundedMonitor?: () => void
}) {
  const { t } = useTranslation()
  const contentRef = useRef<HTMLDivElement | null>(null)
  // The card scrolls inside its viewport bound, and an idle overlay scrollbar
  // leaves no sign that controls sit below the fold, so its edges get a fade.
  const [attachScroll, edges, , attachScrollContent] = useScrollEdgesY<HTMLDivElement>()
  const setContentRef = useCallback((node: HTMLDivElement | null) => {
    contentRef.current = node
    attachScroll(node)
  }, [attachScroll])
  const goal = loop.goal!
  const suggested = goal.status === 'suggested'
  const finished = goal.status === 'complete' || goal.status === 'ended'
  const limited = ['cycle_cap', 'runtime_budget'].includes(loop.stoppedReason)
  const pauseUnsaved = loop.stoppedReason === 'goal_pause_unsaved'
  const approvalStalled = loop.stoppedReason === 'approval_stalled'
  const needsInput = goal.status === 'needs_input' && !approvalStalled
  const needsRevision = !loop.active && !pauseUnsaved && loop.goalGeneration === undefined
  const refreshOnly = pauseUnconfirmed || needsRevision
  let limitDescription = ''
  if (loop.stoppedReason === 'cycle_cap') {
    limitDescription = Number.isFinite(loop.maxCycles) && loop.maxCycles > 0
      ? t('components.goalProgress.cycle_limit_with_count', {
        used: fmtNumber(loop.cycleCount), limit: fmtNumber(loop.maxCycles),
      })
      : t('components.goalProgress.cycle_limit')
  } else if (loop.stoppedReason === 'runtime_budget') {
    const seconds = loop.maxRuntimeSecs
    limitDescription = seconds !== undefined && Number.isFinite(seconds) && seconds > 0
      ? t('components.goalProgress.runtime_limit_with_duration', {
        duration: fmtDuration([
          [Math.floor(seconds / 3600), 'hour'],
          [Math.floor(seconds % 3600 / 60), 'minute'],
          [seconds % 60, 'second'],
        ], { dropZero: true, unitDisplay: 'long' }),
      })
      : t('components.goalProgress.runtime_limit')
  }
  const notice = [
    changeFailure,
    pauseUnsaved && !pauseUnconfirmed ? t('components.goalProgress.pause_unsaved_help') : undefined,
  ].filter(Boolean).join(' ')

  return (
    <PopoverContent
      ref={setContentRef}
      side="top" align="start"
      collisionPadding={8}
      aria-label={goal.objective}
      onOpenAutoFocus={event => {
        if (finished || limited || needsInput) {
          event.preventDefault()
          contentRef.current?.focus({ preventScroll: true })
        }
      }}
      className="w-[min(calc(100vw-1rem),26.25rem)] max-h-[min(80vh,42rem,var(--radix-popover-content-available-height))] overflow-y-auto"
    >
      {/* Sticky, with a negative margin cancelling their height, so the cues
          pin to the scrollport edges without moving the content. */}
      {edges.top && (
        <div aria-hidden="true" data-testid="goal-progress-scroll-cue-top" className="pointer-events-none sticky top-0 z-10 -mb-8 h-8 bg-gradient-to-b from-bg-elevated to-transparent" />
      )}
      <div ref={attachScrollContent} className="space-y-4">
        <div className="flex items-center gap-2">
          <Goal className="lucide-inline text-accent shrink-0" aria-hidden />
          <Badge variant={goal.status === 'complete' ? 'ok' : 'muted'} className="whitespace-normal">{statusLabel}</Badge>
        </div>
        {pauseUnconfirmed && (
          <p className="text-[12px] text-muted">{t('components.goalProgress.last_confirmed_status', { status: goalStatusLabel(loop) })}</p>
        )}
        {suggested && <PanelSectionHeader label={t('components.goalProgress.scope')} />}
        <p className="text-sm font-medium text-text break-words">{goal.objective}</p>
        {goal.progress && <p role="status" aria-live="polite" className="text-sm text-muted break-words">{goal.progress}</p>}
        {(suggested || goal.criteria.length > 0) && (
          <section className="space-y-2">
            <PanelSectionHeader label={t('components.goalProgress.done_when')} />
            <ul className="list-disc pl-4 space-y-1 text-sm text-text">
              {goal.criteria.map((criterion, index) => <li key={index} className="break-words">{criterion}</li>)}
            </ul>
            {goal.criteria.length === 0 && <p className="text-sm text-muted">{t('components.goalProgress.criteria_missing')}</p>}
          </section>
        )}
        {suggested && (
          <section className="space-y-2">
            <PanelSectionHeader label={t('components.goalProgress.limits')} />
            <p className="text-sm text-muted">{t('components.goalProgress.start_help')}</p>
            <dl className="space-y-1 text-sm">
              <div className="flex justify-between gap-2">
                <dt>{t('components.goalProgress.maximum_cycles')}</dt>
                <dd>{loop.maxCycles > 0 ? fmtNumber(loop.maxCycles) : t('components.goalProgress.no_cycle_limit')}</dd>
              </div>
              {loop.maxRuntimeSecs !== undefined && loop.maxRuntimeSecs > 0 && <div className="flex justify-between gap-2">
                <dt>{t('components.goalProgress.maximum_runtime')}</dt>
                <dd>{fmtDuration([
                    [Math.floor(loop.maxRuntimeSecs / 3600), 'hour'],
                    [Math.floor(loop.maxRuntimeSecs % 3600 / 60), 'minute'],
                    [loop.maxRuntimeSecs % 60, 'second'],
                  ], { dropZero: true, unitDisplay: 'long' })}</dd>
              </div>}
            </dl>
          </section>
        )}
        {goal.evidence.length > 0 && (
          <section className="space-y-2">
            <PanelSectionHeader label={t('components.goalProgress.evidence')} />
            <ul className="space-y-1 text-sm text-text">
              {goal.evidence.map((item, index) => (
                <li key={index} className="flex gap-2 break-words">
                  <Check className="lucide-inline text-accent shrink-0" aria-hidden />{item}
                </li>
              ))}
            </ul>
          </section>
        )}
        {!suggested && <p className="text-[12px] text-muted">
          {limited ? `${limitDescription} ${t('components.goalProgress.limit_help')}`
            : goal.status === 'ended' ? t('components.goalProgress.ended_help')
              : needsInput ? t('components.goalProgress.needs_input_help')
                : approvalStalled ? t('components.goalProgress.approval_stalled_help')
                  : goal.status === 'blocked' ? t('components.goalProgress.blocked_help')
                    : goal.status === 'waiting' ? t('components.goalProgress.waiting_help')
                      : t('components.goalProgress.steer_in_chat')}
        </p>}
        {suggested && <p className="text-[12px] text-muted">{t('components.goalProgress.manual_available')}</p>}
        {needsRevision && !finished && !limited && !needsInput && (
          <p className="text-[12px] text-muted">{t(suggested ? 'components.goalProgress.refresh_before_start' : 'components.goalProgress.refresh_before_resume')}</p>
        )}
        <ErrorNotice
          variant="inline" askAgent
          title={changeFailureDetail ? notice : undefined}
          message={changeFailureDetail || notice}
          // The request's own text reads as the cause under the one next step.
          messagePlacement="below"
        />
        {!finished && !limited && !needsInput && (
        <div className="flex flex-wrap gap-2">
          <Btn primary={suggested && !refreshOnly} disabled={pending} onClick={onAction}>
            {refreshOnly || pauseUnsaved ? <RotateCw className="lucide-inline" aria-hidden />
              : loop.active ? <Pause className="lucide-inline" aria-hidden />
                : <Play className="lucide-inline" aria-hidden />}
            {refreshOnly ? t('components.goalProgress.refresh_status')
              : pauseUnsaved ? t('components.goalProgress.retry_pause')
              : loop.active ? t('components.goalProgress.pause')
                : suggested ? t('components.goalProgress.start') : t('components.goalProgress.resume')}
          </Btn>
          {suggested && !refreshOnly && onDismiss && <Btn disabled={pending} onClick={onDismiss}>
            <X className="lucide-inline" aria-hidden />
            {t('components.goalProgress.dismiss')}
          </Btn>}
        </div>
        )}
        {/* A secondary path, so it sits apart from Start and Dismiss as a link. */}
        {onSetUpBoundedMonitor && !finished && !limited && !needsInput && (
          <p>
            <button
              type="button" disabled={pending} onClick={onSetUpBoundedMonitor}
              className="inline-flex items-center gap-1 bg-transparent border-none p-0 text-[12px] text-accent hover:underline cursor-pointer focus-ring disabled:cursor-not-allowed disabled:opacity-40"
            >
              <Radar className="lucide-inline" aria-hidden />
              {t('components.sessionAutomationPopover.set_up_bounded_monitor')}
            </button>
          </p>
        )}
        {settings}
      </div>
      {edges.bottom && (
        <div aria-hidden="true" data-testid="goal-progress-scroll-cue-bottom" className="pointer-events-none sticky bottom-0 z-10 -mt-8 h-8 bg-gradient-to-t from-bg-elevated to-transparent" />
      )}
    </PopoverContent>
  )
}
