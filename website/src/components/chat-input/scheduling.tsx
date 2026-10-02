import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Clock, PenLine } from 'lucide-react'
import { normalizeAutomationRecord, type AutomationRecord, type LegacyGoalLoop } from '../../monitoring/automation'
import { fmtDateTime } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { mergeIntoDraft } from '../../utils/chatDrafts'
import { expandAll, type PasteBlock } from '../../utils/pasteTokens'
import Clickable from '../Clickable'
import ErrorNotice from '../ErrorNotice'
import ScheduleLaterPopover from '../ScheduleLaterPopover'
import { Btn } from '../ui'

function requestError(status: number, payload: Record<string, unknown>): string {
  if (payload.code === 'scheduled_message_in_flight') {
    return i18nT('components.chatInput.schedule_message_sending')
  }
  if (payload.code === 'autonudge_not_armed' && status === 409) {
    return i18nT('components.chatInput.schedule_message_conflict')
  }
  return i18nT('components.chatInput.schedule_message_failed')
}

function errorMessage(error: unknown): string {
  return error instanceof Error
    ? error.message
    : i18nT('components.chatInput.schedule_message_failed')
}


export function SendLaterMenuContent({
  pending,
  disabledReason,
}: {
  pending: boolean
  disabledReason: string
}) {
  return (
    <>
      <Clock className="h-3.5 w-4 shrink-0 text-muted lucide-inline" aria-hidden />
      <div className="min-w-0">
        <div className="text-[12px] font-medium text-text">
          {i18nT('components.chatInput.send_later')}
        </div>
        <div className="text-[11px] text-muted leading-snug">
          {pending
            ? i18nT('components.jobForm.saving')
            : disabledReason || i18nT('components.chatInput.send_later_subtitle')}
        </div>
      </div>
    </>
  )
}
interface SchedulingParams {
  slotId: string | null
  value: string
  onChange: (value: string) => void
  pasteBlocks: PasteBlock[]
  onPasteBlocksChange?: (next: PasteBlock[]) => void
  /** True while the draft stages anything the scheduled POST cannot carry:
   *  uploaded files, `@rel/` folder tokens, or session references. The
   *  ordinary send path serializes each of those alongside the text; the
   *  one-shot record is `{slot_key, message, at}` and nothing else, so
   *  scheduling such a draft would deliver its prose and leave the rest
   *  behind. Paste tokens are NOT in this set: they expand into the text. */
  hasAttachments?: boolean
  automation?: AutomationRecord | null
  onAutomationChange?: (automation: AutomationRecord | null) => void
  onAutomationClick?: (open: boolean) => void
  creationReady?: boolean
  connected: boolean
  disabled: boolean
  sessionMode?: string
  memoryMode?: string
  composerCollapsed: boolean
  expandComposer: () => void
  closeAttachMenu: () => void
}

interface MutationIdentity {
  slotKey: string
  captured: AutomationRecord | null
}

export function useScheduledComposer({
  slotId,
  value,
  onChange,
  pasteBlocks,
  onPasteBlocksChange,
  hasAttachments = false,
  automation = null,
  onAutomationChange,
  onAutomationClick,
  creationReady = true,
  connected,
  disabled,
  sessionMode = '',
  memoryMode = 'persistent',
  composerCollapsed,
  expandComposer,
  closeAttachMenu,
}: SchedulingParams) {
  const queryClient = useQueryClient()
  const [scheduleOpen, setScheduleOpen] = useState(false)
  const [scheduleAnchor, setScheduleAnchor] = useState<DOMRect | null>(null)
  const [scheduleError, setScheduleError] = useState('')
  const slotRef = useRef(slotId)
  const automationRef = useRef<AutomationRecord | null>(automation)
  const valueRef = useRef(value)
  const onChangeRef = useRef(onChange)
  const pasteBlocksRef = useRef(pasteBlocks)
  const onPasteBlocksChangeRef = useRef(onPasteBlocksChange)
  const hasAttachmentsRef = useRef(hasAttachments)
  const onAutomationChangeRef = useRef(onAutomationChange)
  slotRef.current = slotId
  automationRef.current = automation
  valueRef.current = value
  onChangeRef.current = onChange
  pasteBlocksRef.current = pasteBlocks
  onPasteBlocksChangeRef.current = onPasteBlocksChange
  hasAttachmentsRef.current = hasAttachments
  onAutomationChangeRef.current = onAutomationChange

  // A scheduled message keeps its identity through dispatch: the server refuses
  // a cancel or an edit once `cycle_count` leaves 0 (`scheduled_message_in_flight`)
  // while the record stays active until delivery settles.
  const liveScheduledMessage = automation?.kind === 'legacy_goal_loop'
    && automation.scheduledMessage === true
    && automation.active
    && (automation.scheduledAt ?? 0) > 0
    ? automation
    : null
  // Pending: still cancellable/editable, so the banner offers both.
  const scheduledAutomation = liveScheduledMessage && liveScheduledMessage.cycleCount === 0
    ? liveScheduledMessage
    : null
  // Sending: dispatch has started. Not a goal loop cycling, and not something
  // the banner can still unschedule, so it must never read as "Goal active".
  const sendingScheduledMessage = liveScheduledMessage && liveScheduledMessage.cycleCount > 0

  const requestSlotIsCurrent = useCallback((request: MutationIdentity) => (
    slotRef.current === request.slotKey
  ), [])

  const requestAutomationIsCurrent = useCallback((request: MutationIdentity) => (
    automationRef.current === request.captured
  ), [])

  useEffect(() => {
    setScheduleOpen(false)
    setScheduleError('')
  }, [slotId])

  const restoreScheduledDraft = useCallback((message: string) => {
    if (valueRef.current !== message) {
      onChangeRef.current(mergeIntoDraft(valueRef.current, message))
    }
  }, [])

  const scheduleSend = useMutation({
    mutationFn: async (request: MutationIdentity & {
      text: string
      draft: string
      pasteBlocks: PasteBlock[]
      atSecs: number
    }) => {
      const response = await fetch('/api/autonudge', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          slot_key: request.slotKey,
          message: request.text,
          at: request.atSecs,
        }),
      })
      const payload = await response.json().catch(() => ({})) as Record<string, unknown>
      if (!response.ok) throw new Error(requestError(response.status, payload))
      const next = normalizeAutomationRecord(payload.loop)
      if (next?.kind !== 'legacy_goal_loop' || next.scheduledMessage !== true
        || !(next.scheduledAt && next.scheduledAt > 0)) {
        throw new Error(i18nT('components.sessionAutomationPopover.request_failed'))
      }
      return { next: { ...next, message: request.text } as LegacyGoalLoop, request }
    },
    onSuccess: ({ next, request }) => {
      void queryClient.invalidateQueries({ queryKey: ['session-automation', request.slotKey] })
      if (!requestSlotIsCurrent(request)) return
      setScheduleError('')
      const pasteBlocksAreCurrent = request.pasteBlocks.length === pasteBlocksRef.current.length
        && request.pasteBlocks.every((block, index) => pasteBlocksRef.current[index] === block)
      if (valueRef.current === request.draft && pasteBlocksAreCurrent) {
        onChangeRef.current('')
        onPasteBlocksChangeRef.current?.([])
      }
      if (requestAutomationIsCurrent(request)) onAutomationChangeRef.current?.(next)
    },
    onError: (error, request) => {
      if (!requestSlotIsCurrent(request)) return
      setScheduleError(errorMessage(error))
    },
  })

  const cancelScheduled = useMutation({
    mutationFn: async (request: MutationIdentity & { id: string; message: string }) => {
      const response = await fetch(`/api/autonudge/${encodeURIComponent(request.id)}?intent=stop`, {
        method: 'DELETE',
      })
      const payload = await response.json().catch(() => ({})) as Record<string, unknown>
      if (!response.ok) throw new Error(requestError(response.status, payload))
      return {
        request,
        message: typeof payload.message === 'string' ? payload.message : request.message,
      }
    },
    onSuccess: ({ request, message }) => {
      void queryClient.invalidateQueries({ queryKey: ['session-automation', request.slotKey] })
      if (!requestSlotIsCurrent(request)) return
      setScheduleError('')
      restoreScheduledDraft(message)
      if (requestAutomationIsCurrent(request)) onAutomationChangeRef.current?.(null)
    },
    onError: (error, request) => {
      if (!requestSlotIsCurrent(request)) return
      setScheduleError(errorMessage(error))
    },
  })

  // Attachments lead the chain: they are the one blocker typing cannot clear,
  // so a draft with a staged file and no text learns the real obstacle before
  // it learns to type. A disabled composer (the slot is stopping) comes next
  // and for the same reason: the editor itself is disabled, so "type a message
  // first" would send the user to a control that will not take input. It is
  // the same gate `scheduleComposer` enforces -- a reason the chain omitted
  // left every Send later control enabled, the picker opening, and Schedule
  // returning without a request or an error.
  const sendLaterDisabledReason = hasAttachments
    ? i18nT('components.chatInput.schedule_text_only')
    : disabled
      ? i18nT('components.chatInput.stopping')
      : !value.trim()
        ? i18nT('components.chatInput.type_a_message_first')
        : !connected
          ? i18nT('components.chatInput.gateway_offline_message_will_not_send')
          : automation
            ? scheduledAutomation
              ? i18nT('components.chatInput.message_already_scheduled')
              : sendingScheduledMessage
                ? i18nT('components.chatInput.schedule_message_sending')
                : automation.kind === 'legacy_goal_loop'
                  ? i18nT('components.autoNudgePopover.goal_active_cycle', { cycle: automation.cycleCount })
                  : i18nT('components.chatInput.stop_monitor_to_schedule')
            : sessionMode === 'crew' || sessionMode === 'member'
              ? i18nT('components.sessionAutomationPopover.session_mode_unavailable')
              : memoryMode === 'incognito'
                ? i18nT('components.welcomeView.incognito_active_switch_to_persistent')
                : memoryMode === 'temporary'
                  ? i18nT('components.welcomeView.temporary_active_switch_to_persistent')
                  : !slotId || !onAutomationChange || !creationReady
                    ? i18nT('components.chatInput.schedule_not_ready')
                    : ''

  // An attachment staged while the picker is open (drag-drop, a sidebar
  // session drop) would otherwise leave a Schedule button that does nothing,
  // and so would a composer disabled mid-pick: `scheduleComposer` refuses
  // both. Closing the picker returns the user to the disabled control, whose
  // title carries the reason.
  useEffect(() => {
    if (hasAttachments || disabled) setScheduleOpen(false)
  }, [hasAttachments, disabled])

  const openScheduleLater = useCallback((anchor: DOMRect | null) => {
    if (!anchor) return
    closeAttachMenu()
    setScheduleError('')
    setScheduleAnchor(anchor)
    setScheduleOpen(true)
  }, [closeAttachMenu])

  const scheduleComposer = useCallback((atSecs: number) => {
    const draft = valueRef.current
    const capturedPasteBlocks = pasteBlocksRef.current
    const currentSlot = slotRef.current
    const captured = automationRef.current
    if (!draft.trim() || hasAttachmentsRef.current || disabled || !currentSlot || !onAutomationChangeRef.current) return
    setScheduleOpen(false)
    scheduleSend.mutate({
      slotKey: currentSlot,
      captured,
      text: expandAll(draft, capturedPasteBlocks),
      draft,
      pasteBlocks: capturedPasteBlocks,
      atSecs,
    })
  }, [disabled, scheduleSend])

  const openScheduledEditor = useCallback(() => {
    if (composerCollapsed) {
      expandComposer()
      requestAnimationFrame(() => onAutomationClick?.(true))
      return
    }
    onAutomationClick?.(true)
  }, [composerCollapsed, expandComposer, onAutomationClick])

  const banner: ReactNode = scheduledAutomation ? (
    <div className="px-4 mb-1">
      <div
        role="status"
        data-testid="scheduled-message-banner"
        className="flex items-center gap-2 rounded-lg border border-accent-subtle bg-accent-subtle/40 px-2.5 py-1.5 text-[12px] text-muted"
      >
        <Clickable
          onClick={openScheduledEditor}
          className="flex cursor-pointer items-center gap-1.5 p-1 -m-1 min-w-0 flex-1 justify-start rounded-md text-left transition-all active:scale-[0.97] active:duration-75 hover:bg-bg-hover focus-ring"
          data-testid="scheduled-message-edit"
        >
          <Clock className="h-3.5 w-3.5 shrink-0 lucide-inline" aria-hidden />
          <span className="min-w-0 flex-1 text-left">
            <span className="block font-medium text-text">
              {i18nT('components.chatInput.scheduled_message')}
            </span>
            <span className="block">{fmtDateTime(scheduledAutomation.scheduledAt as number)}</span>
            <span className="block break-words text-text line-clamp-2">
              {scheduledAutomation.message}
            </span>
          </span>
          <span
            data-testid="scheduled-message-edit-affordance"
            className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border px-1.5 py-0.5 text-[11px] font-medium text-text"
          >
            <PenLine className="h-3 w-3 lucide-inline" aria-hidden />
            <span className="max-[389px]:sr-only">
              {i18nT('components.chatInput.edit_scheduled_message')}
            </span>
          </span>
        </Clickable>
        <Btn
          onClick={() => cancelScheduled.mutate({
            slotKey: slotId as string,
            captured: scheduledAutomation,
            id: scheduledAutomation.id,
            message: scheduledAutomation.message,
          })}
          disabled={cancelScheduled.isPending}
          data-testid="scheduled-message-cancel"
        >
          {i18nT('components.chatInput.unschedule')}
        </Btn>
      </div>
    </div>
  ) : null

  const popover: ReactNode = scheduleOpen && scheduleAnchor ? (
    <ScheduleLaterPopover
      anchorRect={scheduleAnchor}
      onSchedule={scheduleComposer}
      onClose={() => setScheduleOpen(false)}
      scheduling={scheduleSend.isPending}
    />
  ) : null

  const error: ReactNode = scheduleError ? (
    <div className="px-4 mb-1">
      {/* No hand-off: the unsent composer draft remains available for retry. */}
      <ErrorNotice
        variant="inline"
        testId="schedule-error"
        message={scheduleError}
        onDismiss={() => setScheduleError('')}
      />
    </div>
  ) : null

  return {
    banner,
    popover,
    error,
    restoreScheduledDraft,
    sendLaterDisabledReason,
    schedulePending: scheduleSend.isPending,
    openScheduleLater,
  }
}
