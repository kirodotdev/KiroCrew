import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Send, MessageSquare, Copy, Check, RotateCcw } from 'lucide-react'
import { useMutation } from '@tanstack/react-query'
import { api } from '../../api/client'
import { useAppSelector, useAppDispatch } from '../../store'
import { sideClose, sideOptimisticAppend, sideOptimisticRollback } from '../../store/chatSlice'
import { copyToClipboard } from '../../utils/clipboard'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import { useKiroSessionReady } from '../../providers/KiroReadinessContext'
import type { SideMessage } from '../../store/chatSlice'

const MAX_QUESTION_BYTES = 32_768
// Max auto-grow height (px) for the side-question input before it scrolls.
const MAX_INPUT_H = 240

function SideMessageBubble({ msg, isStreaming }: { msg: SideMessage; isStreaming: boolean }) {
  const [copied, setCopied] = useState(false)

  if (msg.role === 'user') {
    return (
      <div className="rounded-md bg-accent/10 px-2.5 py-1.5 text-[13px] text-text whitespace-pre-wrap">
        {msg.content}
      </div>
    )
  }

  if (msg.is_error) {
    return (
      <div className="rounded-md bg-danger/10 px-2.5 py-1.5 text-[13px] text-danger whitespace-pre-wrap">
        {msg.content}
      </div>
    )
  }

  return (
    <div className="group/side-msg rounded-md bg-bg-hover px-2.5 py-1.5 text-sm leading-relaxed text-text overflow-hidden" style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}>
      <MarkdownRenderer content={msg.content} streaming={isStreaming} />
      {!isStreaming && msg.content.length > 0 && (
        <div className="flex items-center gap-1 mt-0.5 opacity-0 transition-opacity group-hover/side-msg:opacity-100">
          <button
            className="text-muted hover:text-text p-0.5 rounded transition-colors"
            title="Copy"
            aria-label={copied ? 'Copied!' : 'Copy'}
            onClick={() => {
              copyToClipboard(msg.content).then(() => {
                setCopied(true)
                setTimeout(() => setCopied(false), 1500)
              }).catch(() => {})
            }}
          >
            {copied ? <Check size={13} className="text-ok" /> : <Copy size={13} />}
          </button>
        </div>
      )}
    </div>
  )
}

function relativeTime(iso: string): string | null {
  const diff = Date.now() - new Date(iso).getTime()
  if (diff < 30 * 60_000) return null
  if (diff < 60 * 60_000) return `${Math.floor(diff / 60_000)}m`
  if (diff < 24 * 3600_000) return `${Math.floor(diff / 3600_000)}h`
  return `${Math.floor(diff / (24 * 3600_000))}d`
}

export default function SideChat({ slot }: { slot: string }) {
  const kiroSessionReady = useKiroSessionReady()
  const dispatch = useAppDispatch()
  const reduxSide = useAppSelector(s => s.chat.slotSide[slot])
  const parentTurnCount = useAppSelector(s =>
    s.chat.messages.filter(m => m.role === 'user' || m.role === 'assistant').length
  )
  const [draft, setDraft] = useState('')
  const [localError, setLocalError] = useState<string | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const isNearBottomRef = useRef(true)

  const messages = reduxSide?.messages ?? []
  const isPending = reduxSide?.pending ?? false

  const sendMutation = useMutation({
    mutationFn: async (q: string) => {
      await api.sideOpen(slot)
      return api.sideTurn(slot, q)
    },
    onMutate: (q: string) => {
      setLocalError(null)
      const optimistic: SideMessage = { role: 'user', content: q, ts: new Date().toISOString() }
      dispatch(sideOptimisticAppend({ slot, message: optimistic }))
      setDraft('')
    },
    onError: () => {
      dispatch(sideOptimisticRollback(slot))
    },
  })

  const refreshMutation = useMutation({
    // local close is the source of truth — backend close errors are
    // intentionally not surfaced (the side state is gone locally either way).
    mutationFn: () => api.sideClose(slot),
    onMutate: () => {
      dispatch(sideClose(slot))
    },
  })

  const handleScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    isNearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
  }, [])

  const lastMessageContent = messages[messages.length - 1]?.content
  useEffect(() => {
    const el = scrollRef.current
    if (el && isNearBottomRef.current) {
      requestAnimationFrame(() => { el.scrollTop = el.scrollHeight })
    }
  }, [messages.length, lastMessageContent])

  // Select-to-Ask seed: when the user clicks "Ask" in the selection toolbar,
  // ChatPage opens this panel and fires a `side-seed` CustomEvent carrying the
  // selected text. Prefill the draft with the selection as a grounding
  // blockquote and focus the input so the user types their actual question
  // (which then fires sideOpen → sideTurn as usual). Isolated from main context.
  useEffect(() => {
    const onSeed = (e: Event) => {
      const detail = (e as CustomEvent<{ text?: string }>).detail
      const sel = detail?.text?.trim()
      if (!sel) return
      const quoted = sel.split('\n').map(line => `> ${line}`).join('\n')
      setDraft(prev => (prev.trim() ? `${prev.trimEnd()}\n\n${quoted}\n\n` : `${quoted}\n\n`))
      // Focus + place caret at the end so the user immediately types the question.
      requestAnimationFrame(() => {
        const el = textareaRef.current
        if (el) {
          el.focus()
          const len = el.value.length
          el.setSelectionRange(len, len)
          // Scroll to the top so the START of a long quote is visible (focusing
          // + caret-at-end scrolls to the bottom otherwise, hiding the quote).
          el.scrollTop = 0
        }
      })
    }
    window.addEventListener('side-seed', onSeed)
    return () => window.removeEventListener('side-seed', onSeed)
  }, [])

  // Auto-grow the input so a seeded multi-line quote (or a long typed question)
  // is fully visible instead of being clipped to the 2-row default. Grows with
  // content up to MAX_INPUT_H, then scrolls. The `min-h-[52px]` class floors it
  // at ~2 rows so an empty box keeps its original size.
  useLayoutEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, MAX_INPUT_H)}px`
  }, [draft])

  const send = useCallback(() => {
    const q = draft.trim()
    if (!kiroSessionReady || !q || sendMutation.isPending || !slot) return
    if (new Blob([q]).size > MAX_QUESTION_BYTES) {
      setLocalError(`Question too long (max ${MAX_QUESTION_BYTES.toLocaleString()} bytes)`)
      return
    }
    sendMutation.mutate(q)
  }, [draft, slot, sendMutation, kiroSessionReady])

  const onKeyDown = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      void send()
    }
  }, [send])

  const lastIdx = messages.length - 1
  const lastMsg = messages[lastIdx]
  const isStreaming = reduxSide?.streaming ?? false
  const isStreamingLast = lastMsg?.role === 'assistant' && isStreaming

  const sendErr = sendMutation.error
  const displayError = sendErr
    ? (sendErr instanceof Error ? sendErr.message : String(sendErr))
    : localError

  const turnsBehind = reduxSide ? parentTurnCount - reduxSide.openedAtTurnCount : 0
  const age = reduxSide?.createdAt ? relativeTime(reduxSide.createdAt) : null
  const showBanner = !!reduxSide && messages.length > 0
  const isStale = turnsBehind >= 10 || (reduxSide?.createdAt && Date.now() - new Date(reduxSide.createdAt).getTime() >= 4 * 3600_000)

  const handleRefresh = useCallback(() => {
    refreshMutation.mutate()
  }, [refreshMutation])

  return (
    <div className="flex-1 flex flex-col min-h-0">
      {showBanner && (
        <div className={`flex items-center justify-between px-3 py-1.5 text-[12px] border-b border-border shrink-0 ${isStale ? 'bg-warning/10 text-warning' : 'bg-bg-hover/50 text-muted'}`}>
          <span className="italic">
            Context from {turnsBehind} turn{turnsBehind !== 1 ? 's' : ''} ago{age ? ` · ${age}` : ''}
          </span>
          <button
            onClick={() => void handleRefresh()}
            disabled={refreshMutation.isPending}
            className="flex items-center gap-1 text-[11px] font-medium text-accent hover:text-accent-hover disabled:opacity-50 bg-transparent border-none cursor-pointer disabled:cursor-not-allowed"
          >
            <RotateCcw size={11} className={refreshMutation.isPending ? 'animate-spin' : ''} />
            Refresh context
          </button>
        </div>
      )}
      <div ref={scrollRef} onScroll={handleScroll} className="flex-1 overflow-y-auto px-3 py-2 space-y-2">
        {messages.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-muted/30 gap-2 py-8">
            <span className="text-[24px]"><MessageSquare className="lucide-inline" /></span>
            <span className="text-[13px]">Ask a side question — main agent keeps working</span>
          </div>
        ) : (
          messages.map((m, i) => (
            <SideMessageBubble
              key={i}
              msg={m}
              isStreaming={i === lastIdx && isStreamingLast}
            />
          ))
        )}
        {isPending && lastMsg?.role === 'user' && (
          <div className="flex items-center gap-1.5 px-2.5 py-2 text-muted">
            <span className="flex gap-0.5">
              <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse" style={{ animationDelay: '0ms' }} />
              <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse" style={{ animationDelay: '150ms' }} />
              <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse" style={{ animationDelay: '300ms' }} />
            </span>
            <span className="text-[12px] streaming-indicator">Thinking…</span>
          </div>
        )}
      </div>
      {displayError && (
        <div className="px-3 py-1 text-[12px] text-danger border-t border-border">{displayError}</div>
      )}
      <div className="border-t border-border p-2 flex items-end gap-2 shrink-0">
        <textarea
          ref={textareaRef}
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          aria-label="Ask a side question"
          placeholder={kiroSessionReady ? 'Ask a side question…' : 'Finish Kiro CLI setup first'}
          rows={2}
          disabled={sendMutation.isPending || !kiroSessionReady}
          style={{ maxHeight: MAX_INPUT_H }}
          className="flex-1 resize-none overflow-y-auto min-h-[52px] rounded-md border border-border bg-bg px-2 py-1.5 text-[13px] text-text focus:outline-none focus:border-accent disabled:opacity-60"
        />
        <button
          onClick={() => void send()}
          disabled={sendMutation.isPending || !kiroSessionReady || !draft.trim()}
          className="shrink-0 px-2.5 py-1.5 rounded-md bg-accent text-accent-fg text-[12px] font-medium cursor-pointer hover:bg-accent-hover disabled:opacity-40 disabled:cursor-not-allowed border-none"
          title="Send"
          aria-label="Send"
        >
          <Send size={13} />
        </button>
      </div>
    </div>
  )
}
