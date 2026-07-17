/**
 * ChatEmbed — embeddable chat widget using KiroCrew's native rendering.
 *
 * Uses ChatMessageList (shared with ChatPage) for message rendering.
 * Manages its own state via useAppApi() + React Query. No Redux dependency.
 *
 * State management: polling via useQuery refetchInterval.
 * Poll faster during streaming (1s), slower when idle (5s).
 */
import { useState, useRef, useCallback, useEffect } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { ArrowUp, Loader2 } from 'lucide-react'
import ChatMessageList from './ChatMessageList'
import { useAppApi } from './index'
import type { ChatMessage } from '../types'

export interface ChatEmbedProps {
  slotKey: string
  agent?: string
  placeholder?: string
}

/** Minimal shape of the chat-slot payload consumed by this embed. */
interface ChatSlotData {
  messages?: ChatMessage[]
  running?: boolean
  title?: string
}

function ChatEmbed({ slotKey, agent, placeholder }: ChatEmbedProps) {
  const api = useAppApi()
  const [input, setInput] = useState('')
  const endRef = useRef<HTMLDivElement>(null)
  const lastHashRef = useRef('')

  const { data: slotData } = useQuery({
    queryKey: ['app-sdk-embed', slotKey],
    queryFn: () => api.get<ChatSlotData>('/api/chat/slots/' + encodeURIComponent(slotKey)),
    refetchInterval: (query) => {
      const running = query.state.data?.running ?? false
      return running ? 1000 : 5000
    },
  })

  const messages = slotData?.messages ?? []
  const running = slotData?.running ?? false
  const title = slotData?.title ?? ''

  // Auto-scroll when new messages arrive
  const msgHash = messages.length + ':' + (messages[messages.length - 1]?.content?.length || 0)
  useEffect(() => {
    if (msgHash !== lastHashRef.current) {
      lastHashRef.current = msgHash
      endRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [msgHash])

  const sendMutation = useMutation({
    mutationFn: (msg: string) =>
      api.post('/api/chat', { message: msg, slot: slotKey, agent: agent || '' })
        .catch((err) => {
          // POST /api/chat returns SSE — JSON parse fails, expected.
          if (err instanceof SyntaxError) return
          throw err
        }),
  })

  const send = useCallback(() => {
    const msg = input.trim()
    if (!msg) return
    setInput('')
    sendMutation.mutate(msg)
  }, [input, sendMutation])

  return (
    <div className="flex flex-col h-full min-h-0 border border-border rounded-lg overflow-hidden bg-bg">
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-card shrink-0">
        <span className={`w-2 h-2 rounded-full shrink-0 ${running ? 'bg-ok animate-pulse' : 'bg-accent'}`} />
        <span className="text-[13px] font-semibold text-text-strong truncate flex-1">{title || slotKey}</span>
        {agent && <span className="text-[10px] font-mono text-muted">{agent}</span>}
        {running && <span className="text-[10px] text-ok font-mono">streaming</span>}
      </div>

      <div className="flex-1 overflow-y-auto py-4 min-h-0">
        {messages.length === 0 && !running && (
          <div className="text-center text-muted text-[13px] py-10">Session ready. Type a message to start.</div>
        )}
        <ChatMessageList messages={messages} running={running} />
        <div ref={endRef} />
      </div>

      <div className="flex items-center gap-2 px-3 py-2 border-t border-border bg-bg-subtle shrink-0">
        <input
          type="text"
          aria-label="Chat message"
          className="flex-1 px-3 py-2 text-sm bg-bg-elevated border border-border rounded-md text-text outline-none focus:border-accent transition-colors"
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && input.trim()) { e.preventDefault(); send() } }}
          placeholder={running ? 'Agent is working...' : (placeholder || 'Message...')}
          disabled={sendMutation.isPending}
        />
        <button
          className="p-2 rounded-md bg-accent text-accent-fg disabled:opacity-40 disabled:cursor-not-allowed hover:opacity-80 transition-opacity"
          onClick={send}
          disabled={sendMutation.isPending || !input.trim()}
          title="Send"
          aria-label="Send message"
        >
          {sendMutation.isPending ? <Loader2 size={16} className="animate-spin" /> : <ArrowUp size={16} />}
        </button>
      </div>
    </div>
  )
}

export default ChatEmbed
