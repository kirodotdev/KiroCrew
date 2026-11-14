import { useCallback, useMemo, useState } from 'react'
import { goalSuggestionReplyFallback } from '../../app-sdk/protocol/options'
import { i18nT } from '../../i18n/t'
import type { AutomationRecord } from '../../monitoring/automation'
import { loadGoalDraft, saveGoalDraft } from '../../utils/goalDrafts'

/* The composer's suggested-goal card: which goal and replies the follow-up
   bar shows, whether the goal can be reviewed or is already running, and the
   Review & start handler that seeds the goal draft before opening the
   automation control. */
export function useSuggestedGoal({ followUpGoal, followUpOptions, automation, isRunning, sessionMode, onAutomationClick, slotId }: {
  followUpGoal?: string | null
  followUpOptions?: readonly string[]
  automation?: AutomationRecord | null
  isRunning: boolean
  sessionMode?: string
  onAutomationClick?: (open: boolean) => void
  slotId: string | null
}) {
  const [failedSuggestedGoal, setFailedSuggestedGoal] = useState<string | null>(null)
  const canReviewSuggestedGoal = !!onAutomationClick && !!slotId
  const displayedFollowUpGoal = followUpGoal ?? null
  const displayedFollowUpOptions = useMemo(
    () => canReviewSuggestedGoal
      ? (followUpOptions ?? [])
      : goalSuggestionReplyFallback(followUpOptions ?? [], followUpGoal ?? null),
    [followUpGoal, followUpOptions, canReviewSuggestedGoal],
  )
  const automationIsActive = !!automation?.active
    && !(automation.kind === 'structured_monitor' && automation.terminal)
  // The sidebar's "Waiting on you" signal: an armed loop, no turn running, and
  // a newest reply that asks the user to choose. The loop stays armed with its
  // next cycle scheduled, so the card calls it active, as the goal panel does.
  const automationWaitingOnReply = automationIsActive && !isRunning && displayedFollowUpOptions.length > 0
  // The reply that offered the goal stays on screen after Review & start, so
  // an active loop carrying this very objective is the user's own start, not
  // a competing automation to stop.
  const suggestedGoalStarted = automationIsActive
    && automation?.kind === 'legacy_goal_loop'
    && !!displayedFollowUpGoal
    && automation.message.trim() === displayedFollowUpGoal.trim()
  const sessionModeBlocksGoal = sessionMode === 'crew' || sessionMode === 'member'
  const suggestedGoalDisabledReason = suggestedGoalStarted
    ? undefined
    : automation
      ? i18nT(automationIsActive
        ? automation.kind === 'legacy_goal_loop'
          ? automationWaitingOnReply
            ? 'components.followUpBar.goal_paused'
            : 'components.followUpBar.goal_running'
          : automationWaitingOnReply
            ? 'components.followUpBar.monitor_paused'
            : 'components.followUpBar.monitor_running'
        : 'components.followUpBar.automation_stopped')
      : sessionModeBlocksGoal
        ? i18nT('components.sessionAutomationPopover.session_mode_unavailable')
        : undefined
  const reviewSuggestedGoal = useCallback((goal: string) => {
    if (!slotId || !canReviewSuggestedGoal || suggestedGoalStarted || suggestedGoalDisabledReason) return
    const remembered = loadGoalDraft(slotId)
    if (!saveGoalDraft(slotId, {
      message: goal,
      idleSecs: remembered?.idleSecs ?? 60,
      maxCycles: remembered?.maxCycles ?? 0,
    })) {
      setFailedSuggestedGoal(goal)
      return
    }
    setFailedSuggestedGoal(null)
    onAutomationClick?.(true)
  }, [slotId, canReviewSuggestedGoal, onAutomationClick, suggestedGoalStarted, suggestedGoalDisabledReason])
  const openGoalControl = useCallback(() => onAutomationClick?.(true), [onAutomationClick])
  // The goal half of the FollowUpBar's props, spread by the composer.
  const goalBarProps = {
    goal: canReviewSuggestedGoal ? displayedFollowUpGoal : null,
    onGoal: canReviewSuggestedGoal ? reviewSuggestedGoal : undefined,
    goalError: failedSuggestedGoal === displayedFollowUpGoal
      ? i18nT('components.followUpBar.goal_save_failed')
      : null,
    onDismissGoalError: () => setFailedSuggestedGoal(null),
    goalDisabledReason: suggestedGoalDisabledReason,
    goalStarted: suggestedGoalStarted,
    onOpenGoal: canReviewSuggestedGoal && !sessionModeBlocksGoal ? openGoalControl : undefined,
  }
  return {
    setFailedSuggestedGoal,
    canReviewSuggestedGoal,
    displayedFollowUpGoal,
    displayedFollowUpOptions,
    goalBarProps,
  }
}
