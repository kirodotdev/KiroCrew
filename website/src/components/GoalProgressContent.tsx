import { useTranslation } from 'react-i18next'
import { Check, Goal, Pause, Play, RotateCw } from 'lucide-react'
import { type LegacyGoalLoop } from '../monitoring/automation'
import { i18nT } from '../i18n/t'
import { fmtDuration, fmtNumber } from '../i18n/format'
import { Badge, Btn, PanelSectionHeader } from './ui'
import { PopoverContent } from './ui/popover'
import ErrorNotice from './ErrorNotice'

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
  const keys: Record<string, string> = {
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
  loop, statusLabel, changeFailure, pauseUnconfirmed, pending, onAction,
}: {
  loop: LegacyGoalLoop
  statusLabel: string
  changeFailure?: string
  pauseUnconfirmed: boolean
  pending: boolean
  onAction: () => void
}) {
  const { t } = useTranslation()
  const goal = loop.goal!
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
      side="top" align="start"
      aria-label={goal.objective}
      className="w-[min(calc(100vw-1rem),26.25rem)] max-h-[min(80vh,42rem)] overflow-y-auto"
    >
      <div className="space-y-4">
        <div className="flex items-center gap-2">
          <Goal className="lucide-inline text-accent shrink-0" aria-hidden />
          <Badge variant={goal.status === 'complete' ? 'ok' : 'muted'} className="whitespace-normal">{statusLabel}</Badge>
        </div>
        {pauseUnconfirmed && (
          <p className="text-[12px] text-muted">{t('components.goalProgress.last_confirmed_status', { status: goalStatusLabel(loop) })}</p>
        )}
        <p className="text-sm font-medium text-text break-words">{goal.objective}</p>
        {goal.progress && <p role="status" aria-live="polite" className="text-sm text-muted break-words">{goal.progress}</p>}
        {goal.criteria.length > 0 && (
          <section className="space-y-2">
            <PanelSectionHeader label={t('components.goalProgress.done_when')} />
            <ul className="list-disc pl-4 space-y-1 text-sm text-text">
              {goal.criteria.map((criterion, index) => <li key={index} className="break-words">{criterion}</li>)}
            </ul>
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
        <p className="text-[12px] text-muted">
          {limited ? `${limitDescription} ${t('components.goalProgress.limit_help')}`
            : goal.status === 'ended' ? t('components.goalProgress.ended_help')
              : needsInput ? t('components.goalProgress.needs_input_help')
                : approvalStalled ? t('components.goalProgress.approval_stalled_help')
                  : goal.status === 'blocked' ? t('components.goalProgress.blocked_help')
                    : t('components.goalProgress.steer_in_chat')}
        </p>
        {needsRevision && !finished && !limited && !needsInput && (
          <p className="text-[12px] text-muted">{t('components.goalProgress.refresh_before_resume')}</p>
        )}
        <ErrorNotice variant="inline" askAgent message={notice} />
        {!finished && !limited && !needsInput && (
          <Btn disabled={pending} onClick={onAction}>
            {refreshOnly ? <RotateCw className="lucide-inline" aria-hidden />
              : loop.active || pauseUnsaved ? <Pause className="lucide-inline" aria-hidden />
                : <Play className="lucide-inline" aria-hidden />}
            {refreshOnly ? t('components.goalProgress.refresh_status')
              : pauseUnsaved ? t('components.goalProgress.retry_pause')
              : loop.active ? t('components.goalProgress.pause')
                : t('components.goalProgress.resume')}
          </Btn>
        )}
      </div>
    </PopoverContent>
  )
}
