/** The search window's side preview: the chosen hit with the messages around it.
 *
 *  Read only. Rows come from `GET /api/chat/slots/{slot}/history/around`, so the
 *  chat's own loaded window is never touched: the hit is read here, with
 *  earlier and later rows loaded on request, and the chat itself never moves. */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, ChevronDown, ChevronUp } from 'lucide-react'

import { api } from '../../api/client'
import type { ChatHistoryHit } from '../../api/client'
import ChatMessageList from '../../app-sdk/ChatMessageList'
import JumpRevealContext, { type JumpReveal } from '../../hooks/JumpRevealContext'
import SearchHighlightContext from '../../hooks/SearchHighlightContext'
import { fmtDate } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import type { ChatMessage } from '../../types'
import ErrorNotice from '../ErrorNotice'
import { Btn } from '../ui'

/** Messages shown on each side of the hit at first, and added per "Show" press. */
export const AROUND_ROWS = 8
export const MORE_ROWS = 20

interface Range { start: number; end: number; total: number; hasEarlier: boolean; rows: ChatMessage[] }

const noop = () => {}

/** The row actions the preview's messages carry, beyond the ones every row has
 *  (copy). The host passes what its own chat offers: a session chat both, a bot
 *  chat quote only, as its transcript does. */
export interface PreviewActions {
  /** Put a message into this chat's composer as a quote. */
  onQuoteMessage?: (role: 'user' | 'assistant', content: string, ts?: string, mid?: string) => void
  /** Fork the chat from a message, by its id. */
  onForkMessage?: (mid: string) => void | Promise<unknown>
}

export default function ChatHistoryPreview({ slotKey, hit, query = '', onBack, onHandoff, onQuoteMessage, onForkMessage }: PreviewActions & {
  slotKey: string
  hit: ChatHistoryHit
  /** The text the hit matched, marked in the rows the way the chat's own find
   *  marks it; empty when the hit came from a day or a type alone. */
  query?: string
  /** Return to the results list. */
  onBack: () => void
  /** Close the panel when its error hands off to the agent. */
  onHandoff: () => void
}) {
  const [range, setRange] = useState<Range | null>(null)
  const [failed, setFailed] = useState(false)
  const [loading, setLoading] = useState<'earlier' | 'later' | 'first' | null>('first')
  const scrollerRef = useRef<HTMLDivElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  const read = useCallback(async (start: number, end: number, which: 'earlier' | 'later' | 'first') => {
    abortRef.current?.abort()
    const ctl = new AbortController()
    abortRef.current = ctl
    setLoading(which)
    setFailed(false)
    try {
      const got = await api.chatHistoryAround(slotKey, Math.max(0, start), Math.max(0, end), ctl.signal, which === 'first' ? hit.mid : undefined)
      if (ctl.signal.aborted) return
      setRange(prev => {
        if (which === 'first' || !prev) return { start: got.start, end: got.end, total: got.total, hasEarlier: got.has_earlier, rows: got.messages }
        if (which === 'earlier') return { ...prev, start: got.start, total: got.total, hasEarlier: got.has_earlier, rows: [...got.messages, ...prev.rows] }
        return { ...prev, end: got.end, total: got.total, rows: [...prev.rows, ...got.messages] }
      })
    } catch {
      if (!ctl.signal.aborted) setFailed(true)
    } finally {
      if (!ctl.signal.aborted) setLoading(null)
    }
  }, [slotKey, hit.mid])

  useEffect(() => {
    setRange(null)
    void read(hit.index - AROUND_ROWS, hit.index + AROUND_ROWS + 1, 'first')
    return () => abortRef.current?.abort()
  }, [hit.index, hit.ts, read])

  // Centre the hit once its rows are drawn (the first read only).
  const centred = useRef<string | null>(null)
  useEffect(() => {
    const id = `${hit.index}:${hit.mid ?? hit.ts ?? ''}`
    if (!range || centred.current === id) return
    centred.current = id
    // The preview's own scroller only: scrollIntoView would also scroll the
    // dialog and push its search field out of view.
    const scroller = scrollerRef.current
    const row = scroller?.querySelector('[data-jump-target]')
    if (!scroller || !(row instanceof HTMLElement)) return
    const offset = row.getBoundingClientRect().top - scroller.getBoundingClientRect().top
    scroller.scrollTop += offset - (scroller.clientHeight - row.offsetHeight) / 2
  }, [range, hit])

  const reveal = useMemo<JumpReveal>(() => ({
    ts: hit.ts ?? null,
    mid: hit.mid ?? null,
    seq: 1,
    claimScroll: noop,
    scrollerEl: () => scrollerRef.current,
  }), [hit.ts, hit.mid])

  // The chat find's own painting (utils/domHighlight): the rows are markdown, so
  // the result list's span wrapping cannot reach into them. No current match, so
  // nothing here moves a reader or takes the chat find's current range.
  // shortcut: the query is marked as one phrase, not word by word as the result
  // list marks it; split it if multi-word queries need each word marked.
  const highlight = useMemo(() => ({ term: query, caseSensitive: false, currentMessageIdx: -1, currentOccurrenceIdx: -1 }), [query])
  const day = hit.ts ? fmtDate(hit.ts) : ''

  return (
    <section aria-label={i18nT('components.chatHistorySearch.preview_label')} className="flex flex-1 min-h-0 min-w-0 flex-col">
      <header className="flex items-center gap-2 px-3 py-2 border-b border-border">
        <Btn onClick={onBack}>
          <ArrowLeft className="lucide-inline" aria-hidden />
          {i18nT('components.chatHistorySearch.back')}
        </Btn>
        {day && <h2 className="min-w-0 truncate text-[13px] font-medium text-text">{i18nT('components.chatHistorySearch.preview_day', { date: day })}</h2>}
      </header>
      {/* The hit's bubble wears a 2px accent ring on its own edge, drawn
          inside it (`-outline-offset-2`): the rows' bodies clip what sticks
          out, and the bubble's padding keeps the text off the line. A reply
          has no bubble of its own, so here every reply's text gets a user
          bubble's padding and radius, unfilled: ringing the hit then moves
          nothing. A row with no bubble (a sent file's card) is ringed itself.
          No fill, and the transcript's landing flash is switched off here
          (`animate-none`), so the ring stays while the preview shows the hit. */}
      <div ref={scrollerRef} className="flex-1 min-h-0 overflow-y-auto py-2 [&_[data-jump-target]]:animate-none [&_.message-bubble:not(.user-bubble)]:px-4 [&_.message-bubble:not(.user-bubble)]:py-2 [&_.message-bubble:not(.user-bubble)]:rounded-xl [&_[data-jump-target]_.message-bubble]:outline-2 [&_[data-jump-target]_.message-bubble]:outline-accent [&_[data-jump-target]_.message-bubble]:-outline-offset-2 [&_[data-jump-target]:not(:has(.message-bubble))]:rounded-xl [&_[data-jump-target]:not(:has(.message-bubble))]:outline-2 [&_[data-jump-target]:not(:has(.message-bubble))]:outline-accent [&_[data-jump-target]:not(:has(.message-bubble))]:-outline-offset-2" data-testid="chat-history-preview">
        {failed && (
          <div className="px-3 py-2">
            {/* The hand-off loses nothing: the panel holds only a search. */}
            <ErrorNotice variant="inline" message={i18nT('components.chatHistorySearch.preview_error')} askAgent onHandoff={onHandoff} testId="chat-history-preview-error" />
          </div>
        )}
        {range && range.hasEarlier && (
          <div className="flex justify-center py-1">
            <Btn disabled={loading !== null} onClick={() => read(range.start - MORE_ROWS, range.start, 'earlier')}>
              <ChevronUp className="lucide-inline" aria-hidden />
              {i18nT('components.chatHistorySearch.show_earlier')}
            </Btn>
          </div>
        )}
        {range && (
          <JumpRevealContext.Provider value={reveal}>
            <SearchHighlightContext.Provider value={highlight}>
              <ChatMessageList messages={range.rows} running={false} onQuoteMessage={onQuoteMessage} onForkMessage={onForkMessage} />
            </SearchHighlightContext.Provider>
          </JumpRevealContext.Provider>
        )}
        {range && range.end < range.total && (
          <div className="flex justify-center py-1">
            <Btn disabled={loading !== null} onClick={() => read(range.end, range.end + MORE_ROWS, 'later')}>
              <ChevronDown className="lucide-inline" aria-hidden />
              {i18nT('components.chatHistorySearch.show_later')}
            </Btn>
          </div>
        )}
        {loading === 'first' && !failed && (
          <p role="status" className="px-4 py-6 text-center text-[13px] text-muted">{i18nT('components.chatHistorySearch.loading')}</p>
        )}
      </div>
    </section>
  )
}
