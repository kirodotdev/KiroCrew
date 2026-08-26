/**
 * ChatPanel — mounts the full KiroCrew ChatPage inside an app.
 *
 * ChatPage accepts `embedded` prop which skips URL sync effects.
 * ChatPanel dispatches switchSlot() to activate the workspace session.
 * The result is the complete native chat experience with no redirects.
 *
 * Usage:
 *   const { ChatPanel } = window.__kirocrew_modules['@kirocrew/app-sdk']
 *   <ChatPanel slotKey="coder-abc123" />
 */
import { useEffect, useRef } from 'react'
import { useAppDispatch } from '../store'
import { switchSlot } from '../store/chatSlice'
import ChatPage from '../pages/ChatPage'

export interface ChatPanelProps {
  slotKey: string
}

export default function ChatPanel({ slotKey }: ChatPanelProps) {
  const dispatch = useAppDispatch()
  const prevSlotRef = useRef<string | null>(null)

  useEffect(() => {
    if (slotKey && slotKey !== prevSlotRef.current) {
      prevSlotRef.current = slotKey
      dispatch(switchSlot(slotKey))
    }
  }, [slotKey, dispatch])

  return (
    <div className="flex flex-col h-full min-h-0 overflow-hidden">
      <ChatPage embedded />
    </div>
  )
}
