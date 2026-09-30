/**
 * ChatPanel — mounts the full KiroCrew ChatPage inside an app.
 *
 * ChatPage accepts `embedded` prop which skips URL sync effects.
 * ChatPanel dispatches switchSlot() to activate the workspace session.
 * The result is the complete native chat experience with no redirects.
 *
 * An app usually creates the session a moment before mounting the panel, and
 * the dashboard's slot list only learns of it from the next live push. ChatPage
 * clears an active slot that is missing from that list and falls back to the
 * last-used session, so the panel adds a placeholder row first, the same way
 * the artifact companion does, and refreshes the list to fill the row in.
 *
 * Usage:
 *   const { ChatPanel } = window.__kirocrew_modules['@kirocrew/app-sdk']
 *   <ChatPanel slotKey="coder-abc123" />
 */
import { useEffect, useRef } from 'react'
import { useAppDispatch, useAppStore } from '../store'
import { switchSlot } from '../store/chatSlice'
import { addSlotOptimistic, fetchSlots } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'
import ChatPage from '../pages/ChatPage'

export interface ChatPanelProps {
  slotKey: string
  conversationOnly?: boolean
}

export default function ChatPanel({ slotKey, conversationOnly = false }: ChatPanelProps) {
  const dispatch = useAppDispatch()
  const store = useAppStore()
  const prevSlotRef = useRef<string | null>(null)

  useEffect(() => {
    if (slotKey && slotKey !== prevSlotRef.current) {
      prevSlotRef.current = slotKey
      const { slots, closingSlots } = store.getState().dashboard
      // A key the user is closing stays out: re-adding it would drop its close tombstone.
      if (!slots.some(s => s.key === slotKey) && !closingSlots?.[slotKey]) {
        dispatch(addSlotOptimistic({ key: slotKey, messages: 0, running: false } as ChatSlot))
        dispatch(fetchSlots())
      }
      dispatch(switchSlot(slotKey))
    }
  }, [slotKey, dispatch, store])

  return (
    <div className="flex flex-col h-full min-h-0 overflow-hidden">
      <ChatPage
        embedded
        embedMode={conversationOnly ? 'chat' : undefined}
        noUrlSync={conversationOnly || undefined}
      />
    </div>
  )
}
