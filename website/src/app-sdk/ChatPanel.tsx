/**
 * ChatPanel — mounts the full KiroCrew ChatPage inside an app.
 *
 * ChatPage accepts `embedded` prop which skips URL sync effects.
 * ChatPanel makes the workspace session the active slot (`useOwnSlotChat`) and
 * shows the chat once it is, so a session that is gone or fails to open never
 * puts another conversation in its place.
 * The result is the complete native chat experience with no redirects.
 *
 * An app usually creates the session a moment before mounting the panel, and
 * the dashboard's slot list only learns of it from the next live push. ChatPage
 * clears an active slot that is missing from that list and falls back to the
 * last-used session, so the panel adds a placeholder row first, the same way
 * the artifact companion does, and refreshes the list to fill the row in.
 *
 * `expandSteps` opens the transcript's reasoning and tool rows for this panel
 * only, through ExpandStepsContext, without writing the user's chat settings.
 *
 * Usage:
 *   const { ChatPanel } = window.__kirocrew_modules['@kirocrew/app-sdk']
 *   <ChatPanel slotKey="coder-abc123" />
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useAppDispatch, useAppStore } from '../store'
import { addSlotOptimistic, fetchSlots } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'
import ChatPage from '../pages/ChatPage'
import ErrorNotice from '../components/ErrorNotice'
import { Btn } from '../components/ui'
import { useOwnSlotChat } from '../hooks/useOwnSlotChat'
import { ExpandStepsContext } from '../hooks/useToolCallsStartExpanded'

import { i18nT } from '../i18n/t'

export interface ChatPanelProps {
  slotKey: string
  conversationOnly?: boolean
  expandSteps?: boolean
}

export default function ChatPanel({ slotKey, conversationOnly = false, expandSteps = false }: ChatPanelProps) {
  const dispatch = useAppDispatch()
  const store = useAppStore()
  const prevSlotRef = useRef<string | null>(null)
  // A memory-mode pick replaces the session while the app still names the old
  // key, so the panel follows the replacement itself.
  const [replaced, setReplaced] = useState<{ from: string; to: string } | null>(null)
  const ownKey = replaced?.from === slotKey ? replaced.to : slotKey
  const [goneKey, setGoneKey] = useState<string | null>(null)

  useEffect(() => {
    if (slotKey && slotKey !== prevSlotRef.current) {
      prevSlotRef.current = slotKey
      const { slots, closingSlots } = store.getState().dashboard
      // A key the user is closing stays out: re-adding it would drop its close tombstone.
      if (!slots.some(s => s.key === slotKey) && !closingSlots?.[slotKey]) {
        dispatch(addSlotOptimistic({ key: slotKey, messages: 0, running: false } as ChatSlot))
        dispatch(fetchSlots())
      }
    }
  }, [slotKey, dispatch, store])

  const followReplacement = useCallback(
    (_old: string, next: string) => setReplaced({ from: slotKey, to: next }),
    [slotKey],
  )
  const { showChat, failure, retry } = useOwnSlotChat({
    slotKey: ownKey,
    onSessionGone: setGoneKey,
    onSessionReplaced: followReplacement,
    followsNavigation: !conversationOnly,
  })

  return (
    <div className="flex flex-col h-full min-h-0 overflow-hidden">
      {goneKey === ownKey ? (
        // No hand-off: it navigates to the chat page and unmounts the host app, discarding a draft the app holds (an editor buffer, a half-filled form).
        <ErrorNotice className="m-2" message={i18nT('appSdk.chatPanel.session_gone')} />
      ) : failure ? (
        // No hand-off: it navigates to the chat page and unmounts the host app, discarding a draft the app holds (an editor buffer, a half-filled form).
        <ErrorNotice
          className="m-2"
          message={failure}
          actionPlacement="below"
          footer={<Btn type="button" onClick={retry}>{i18nT('components.webPreviewPanel.try_again')}</Btn>}
        />
      ) : showChat ? (
        <ExpandStepsContext.Provider value={expandSteps}>
          <ChatPage
            embedded
            embedMode={conversationOnly ? 'chat' : undefined}
            noUrlSync={conversationOnly || undefined}
          />
        </ExpandStepsContext.Provider>
      ) : null}
    </div>
  )
}
