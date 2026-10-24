import type { UseMutationResult } from '@tanstack/react-query'
import type { MutableRefObject, Dispatch, SetStateAction } from 'react'
import { Loader2, MessageSquare } from 'lucide-react'
import type { AppDispatch } from '../../../store'
import type { ChatSlot } from '../../../types'
import { useState } from 'react'
import { createSlot } from '../../../store/chatSlice'
import { selectSlotListPending } from '../../../store/dashboardSlice'
import { useAppSelector } from '../../../store'
import { EmptyState, Btn } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import { errMessage } from '../../../utils/thunkError'
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
}

/**
 * The pane with no session open: the empty state and its Start a new chat button (which retries a failed create).
 */
export default function ChatEmptyState({
  mode, dispatch, pendingAgent, pendingAgentKindRef, pendingModel, newSessionRef, newSlotFailed, newSlotMutation, setNewSlotFailed, setSidError,
}: ChatEmptyStateProps) {
  // A failed create from this button has no session pane to report into, so
  // the failure is shown here, beside the button that retries it.
  const [createError, setCreateError] = useState('')
  // Before the first slot list lands, "no session open" may only mean "not
  // arrived yet": saying "start a new chat" then tells a user with many chats
  // that they have none (#21785). Show a neutral placeholder until it lands.
  // A failed create still gets its retry button: that failure is known now.
  const slotListPending = useAppSelector(selectSlotListPending)
  if (slotListPending && !newSlotFailed) {
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
      <EmptyState icon={<MessageSquare className="lucide-inline" />} title={i18nT('pages.chatPage.what_can_i_do_for_you')} />
      <Btn
        primary
        disabled={newSlotMutation.isPending}
        {...uiLocation('chat.start-new-chat')}
        onClick={() => {
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
        }}
      >
        {i18nT('pages.chatPage.start_a_new_chat')}
      </Btn>
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
