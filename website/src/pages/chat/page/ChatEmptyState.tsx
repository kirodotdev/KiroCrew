import type { UseMutationResult } from '@tanstack/react-query'
import type { MutableRefObject, Dispatch, SetStateAction } from 'react'
import { Loader2, MessageSquare, MessageSquareOff } from 'lucide-react'
import type { AppDispatch } from '../../../store'
import type { ChatSlot } from '../../../types'
import { useEffect, useId, useRef, useState } from 'react'
import { createSlot } from '../../../store/chatSlice'
import { selectSlotListPending } from '../../../store/dashboardSlice'
import { useAppSelector } from '../../../store'
import { EmptyState, Btn } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import { errMessage } from '../../../utils/thunkError'
import { closeSelf } from '../../../utils/chatPopout'
import { i18nT } from '../../../i18n/t'
import { uiLocation } from '../../../uiLocations/uiLocation'

interface ChatEmptyStateProps {
  mode?: string
  dispatch: AppDispatch
  pendingAgent: string
  pendingAgentKindRef: MutableRefObject<'member' | 'template' | undefined>
  pendingModel: string
  newSessionRef: MutableRefObject<boolean>
  newSlotFailed: boolean
  newSlotMutation: UseMutationResult<ChatSlot, Error, void, unknown>
  setNewSlotFailed: Dispatch<SetStateAction<boolean>>
  setSidError: Dispatch<SetStateAction<string>>
  /** A popout whose own session the server confirmed closed outside this window. */
  closedElsewhere?: boolean
}

/**
 * The pane with no session open: the empty state and its Start a new chat button (which retries a failed create).
 * In a popout whose session was closed outside it, it says so instead and
 * offers Close window first.
 */
export default function ChatEmptyState({
  mode, dispatch, pendingAgent, pendingAgentKindRef, pendingModel, newSessionRef, newSlotFailed, newSlotMutation, setNewSlotFailed, setSidError, closedElsewhere,
}: ChatEmptyStateProps) {
  // A failed create from this button has no session pane to report into, so
  // the failure is shown here, beside the button that retries it.
  const [createError, setCreateError] = useState('')
  const closeRef = useRef<HTMLButtonElement>(null)
  const noticeId = useId()
  // The composer that held focus unmounted with the session, which leaves
  // focus on <body>. Put a keyboard user on the notice's first action, but
  // never take focus from a control they moved to themselves.
  useEffect(() => {
    if (!closedElsewhere) return
    const active = document.activeElement
    if (!active || active === document.body) closeRef.current?.focus()
  }, [closedElsewhere])
  const startNewChat = () => {
    if (newSlotFailed) {
      // Re-arm before state updates can let auto-selection run.
      newSessionRef.current = true
      setNewSlotFailed(false)
      setSidError('')
      newSlotMutation.mutate()
      return
    }
    setCreateError('')
    dispatch(createSlot({ agent: pendingAgent || undefined, agent_kind: pendingAgent ? pendingAgentKindRef.current : undefined, model: pendingModel || undefined, mode, defaultFolder: true }))
      .unwrap()
      .catch((error: unknown) => setCreateError(errMessage(error) || i18nT('pages.chatPage.unknown_error')))
  }
  // Before the first slot list lands, "no session open" may only mean "not
  // arrived yet": saying "start a new chat" then tells a user with many chats
  // that they have none (#21785). Show a neutral placeholder until it lands.
  // A failed create still gets its retry button: that failure is known now.
  const slotListPending = useAppSelector(selectSlotListPending)
  if (slotListPending && !newSlotFailed && !closedElsewhere) {
    return (
      <div
        className="flex-1 flex items-center justify-center gap-2 px-8 text-[13px] text-muted"
        role="status"
        aria-busy="true"
        data-testid="chat-pane-loading"
      >
        <Loader2 size={16} className="lucide-inline animate-spin" aria-hidden="true" />
        {i18nT('pages.chatPage.loading')}
      </div>
    )
  }
  return (
    <div className="flex-1 flex flex-col items-center justify-center gap-4 px-8">
      {closedElsewhere ? (
        // A live region inserted already filled is often not announced, and
        // focus lands on Close window at once, so that button names the
        // notice as its description too.
        <div role="status" id={noticeId} data-testid="popout-session-closed">
          <EmptyState icon={<MessageSquareOff className="lucide-inline" />} title={i18nT('pages.chatPage.session_closed_in_another_window')} testId="popout-session-closed-notice" />
        </div>
      ) : (
        <EmptyState icon={<MessageSquare className="lucide-inline" />} title={i18nT('pages.chatPage.what_can_i_do_for_you')} />
      )}
      <div className="flex flex-wrap items-center justify-center gap-2">
        {closedElsewhere && (
          <Btn ref={closeRef} primary onClick={closeSelf} aria-describedby={noticeId} data-testid="popout-session-closed-close">
            {i18nT('pages.chatPage.close_window')}
          </Btn>
        )}
        {/* One render site for both labels: the ui-location index allows one per marker. */}
        <Btn
          primary={!closedElsewhere}
          disabled={newSlotMutation.isPending}
          {...uiLocation('chat.start-new-chat')}
          onClick={startNewChat}
          data-testid={closedElsewhere ? 'popout-session-closed-new' : undefined}
        >
          {closedElsewhere ? i18nT('pages.chatPage.start_a_new_chat_here') : i18nT('pages.chatPage.start_a_new_chat')}
        </Btn>
      </div>
      {/* The hand-off loses nothing: no session exists yet, so nothing is unsaved. */}
      <ErrorNotice
        title={i18nT('pages.chatPage.could_not_start_a_new_session')}
        message={createError || null}
        askAgent
        onDismiss={() => setCreateError('')}
        testId="empty-state-create-error"
      />
    </div>
  )
}
