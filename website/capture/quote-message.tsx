/**
 * Isolated capture entry for quoting a WHOLE message (not a selection).
 *
 * WHY ISOLATED: the sequence needs a transcript, a hover row, a right-click
 * menu, a staged quote in the composer and a sent row carrying `meta.quote`
 * in one frame set, without a gateway. This mounts the REAL `UserMessage`,
 * `AssistantMessage` and `ChatInput` over the literal ChatPage row chain, and
 * wires them through the REAL `useMessageQuote` hook exactly as ChatPage does:
 * a row's Quote stages it, the composer shows the card, Send consumes it into
 * `prependQuote(typed)` + `meta.quote`, and the new row draws the card.
 *
 * Query string: ?theme=dark|light&scene=hover|menu|composer|sent|narrow|unavailable[&host=crewmate]
 *   `unavailable` is `sent` plus the status row ChatPage shows when a quote
 *   card's jump finds no such message (the literal ChatPage status-row markup
 *   with `pages.chat.quoteCard.message_unavailable`).
 *   `composer` pre-stages Kiro's reply; `sent` also plays a send so the
 *   transcript holds a row with `meta.quote`.
 *   `host=crewmate` renders the transcript the way the crewmate DM does: the
 *   REAL `ChatMessageList` with `createTranscriptRenderers({ crewmate, threads,
 *   hideSteerBadge })` -- bubbles under the crewmate's avatar, Reply in thread
 *   in the row, so Quote is judged where the DM actually puts it (More + the
 *   bubble menu).
 */
import { useEffect, useState, type ReactNode } from 'react'
import { X } from 'lucide-react'
import { i18nT } from '../src/i18n/t'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import ChatInput from '../src/components/ChatInput'
import QueueStack from '../src/components/QueueStack'
import UserMessage from '../src/pages/chat/UserMessage'
import AssistantMessage from '../src/pages/chat/AssistantMessage'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import { useMessageQuote } from '../src/chat-core/composer/useMessageQuote'
import ChatMessageList from '../src/app-sdk/ChatMessageList'
import { createTranscriptRenderers } from '../src/pages/chat/transcriptRenderers'
import type { ChatMessage } from '../src/types'
import { prependQuote, type MessageQuote } from '../src/chat-core/composer/messageQuote'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const scene = params.get('scene') || 'hover'
const crewmateHost = params.get('host') === 'crewmate'
// `fork=window`: a loaded window, where ChatPage keeps Fork as a row button
// (a fork index, no server message id).
const forkWindow = params.get('fork') === 'window'
const CREWMATE = { name: 'kirocrew-worker', label: 'Worker' }
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
document.documentElement.setAttribute('data-mode', theme)

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/')) {
    const body = /commands|skills|agents|models|sessions|files|artifacts/.test(url) ? '[]' : '{}'
    return Promise.resolve(new Response(body, { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

interface Row { role: 'user' | 'assistant'; content: string; ts: string; mid: string; meta?: Record<string, unknown> }
const T0 = '2026-09-29T09:12:00Z'
const KIRO_1 = [
  'Three things landed in the composer:',
  '',
  '- Staged files now show as chips above the textarea, not tokens inside it.',
  '- Cancel on a queued message restores the typed text **and** re-stages the files.',
  '- The Steer/Queue split button remembers its last mode per slot.',
].join('\n')
const SEED: Row[] = [
  { role: 'user', content: 'What changed in the composer PR? Keep it short.', ts: T0, mid: 'm1' },
  { role: 'assistant', content: KIRO_1, ts: '2026-09-29T09:12:40Z', mid: 'm2' },
]
const TYPED = 'Can the chips get a fixed height instead? The re-measure jitters on my phone.'

const render = (c: string) => <MarkdownRenderer content={c} softBreaks />

/** Literal ChatPage per-row wrapper + row chain. */
function RowBox({ user, children }: { user?: boolean; children: ReactNode }) {
  return (
    <div className="px-4 mx-auto w-full py-1" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <div className={`group flex flex-col min-w-0 ${user ? 'items-end' : ''}`}>
        <div className={`flex flex-col gap-0.5 min-w-0 overflow-hidden max-w-full ${user ? 'items-end' : ''}`}>{children}</div>
      </div>
    </div>
  )
}

function Scene() {
  const [rows, setRows] = useState<Row[]>(SEED)
  const [draft, setDraft] = useState(scene === 'composer' || scene === 'narrow' ? TYPED : '')
  const [jumped, setJumped] = useState<string | null>(null)
  const quote = useMessageQuote({ slot: 'chat-1', assistantName: crewmateHost ? CREWMATE.label : undefined })
  // Pre-stage / pre-send for the later scenes, once, through the same hook path
  // ChatPage uses: stage -> consume -> row with meta.quote.
  useEffect(() => {
    if (scene === 'composer' || scene === 'narrow') quote.quoteMessage('assistant', KIRO_1, SEED[1].ts, SEED[1].mid)
    if (scene === 'selfquote') {
      // The user's own earlier message quoted: one sent row carrying it ("You"
      // on the card), and the same quote staged again in the composer.
      const q: MessageQuote = { role: 'user', text: SEED[0].content, ts: SEED[0].ts, mid: SEED[0].mid }
      setRows(r => [...r, { role: 'user', content: prependQuote('Still the short version, please.', q), ts: '2026-09-29T09:14:00Z', mid: 'm3', meta: { quote: q } }])
      quote.quoteMessage('user', SEED[0].content, SEED[0].ts, SEED[0].mid)
      setDraft('And one more thing about the chips:')
    }
    if (scene === 'sent' || scene === 'unavailable') {
      // What ChatPage's send() writes: the block-prefixed text + meta.quote.
      const q: MessageQuote = { role: 'assistant', text: KIRO_1, ts: SEED[1].ts, mid: SEED[1].mid, ...(crewmateHost ? { author: CREWMATE.label } : {}) }
      setRows(r => [...r, { role: 'user', content: prependQuote(TYPED, q), ts: '2026-09-29T09:14:00Z', mid: 'm3', meta: { quote: q } },
        { role: 'assistant', content: 'Because the chip type scale and padding can change independently; a constant drifted twice before. Measuring makes the reservation follow the CSS.', ts: '2026-09-29T09:14:30Z', mid: 'm4' }])
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount-only seed
  }, [])
  const send = () => {
    const { quote: q, text } = quote.consume(draft)
    if (!text.trim()) return
    const meta: Record<string, unknown> = {}
    if (q) meta.quote = q
    setRows(r => [...r, { role: 'user', content: text, ts: '2026-09-29T09:14:00Z', mid: `m${r.length + 1}`, meta }])
    setDraft('')
  }
  const onJump = (q: MessageQuote) => setJumped(q.ts ?? null)
  // ChatPage offers Regenerate on the newest reply only.
  const newestReply = [...rows].reverse().find(r => r.role === 'assistant')?.mid
  return (
    <div className="bg-bg text-text flex flex-col" style={{ height: '100vh' }} data-capture-root data-jumped={jumped ?? undefined}>
      {crewmateHost ? (
        <div className="flex-1 min-h-0 flex flex-col" data-testid="crewmate-host">
          <ChatMessageList
            messages={rows.map(m => ({ role: m.role, content: m.content, cls: m.role === 'user' ? 'msg msg-u' : 'msg', ts: m.ts, meta: { ...(m.meta ?? {}), mid: m.mid } }) as ChatMessage)}
            running={false}
            renderers={createTranscriptRenderers({ slot: 'chat-1', hideSteerBadge: true, crewmate: CREWMATE, crewmateTranscript: rows.map(m => ({ role: m.role, content: m.content, cls: '', ts: m.ts, meta: { ...(m.meta ?? {}), mid: m.mid } }) as ChatMessage) })}
            threads={{ summaryOf: () => undefined, onOpen: () => {}, crewmateName: CREWMATE.name }}
            onQuoteMessage={quote.quoteMessage}
          />
        </div>
      ) : (
      <div className="flex-1 overflow-y-auto py-4">
        {rows.map(m => m.role === 'user' ? (
          <RowBox key={m.mid} user>
            <UserMessage content={m.content} meta={m.meta} messageTs={m.ts} timestamp="09:12" timestampTitle="Today 09:12" slotKey="chat-1" slotTitle="Composer PR" onTogglePin={() => {}} canEdit onEditResend={() => {}} renderContent={render}
              onQuoteMessage={() => quote.quoteMessage('user', m.content, m.ts, m.mid)} onJumpToQuote={onJump} />
          </RowBox>
        ) : (
          <RowBox key={m.mid}>
            <AssistantMessage content={m.content} isStreaming={false} slotRunning={false} messageTs={m.ts} timestamp="09:12" timestampTitle="Today 09:12" slotKey="chat-1" slotTitle="Composer PR" onTogglePin={() => {}}
              onRegenerate={m.mid === newestReply ? () => {} : undefined}
              onFork={forkWindow ? () => {} : undefined} forkIndex={forkWindow ? rows.indexOf(m) : undefined}
              onQuoteMessage={() => quote.quoteMessage('assistant', m.content, m.ts, m.mid)} />
          </RowBox>
        ))}
      </div>
      )}
      {scene === 'unavailable' && (
        /* Literal ChatPage `pinStatus` row (ChatPage.tsx ~8043): the notice a
           failed quote jump raises through `jumpUnavailableNotice('quote')`. */
        <div role="status" className="mx-4 mt-2 mb-0 bg-bg-elevated border rounded-lg p-3 flex items-center gap-3" style={{ borderColor: 'color-mix(in srgb, var(--warn) 45%, transparent)' }} data-testid="quote-unavailable-notice">
          <span className="text-sm text-text flex-1">{i18nT('pages.chat.quoteCard.message_unavailable')}</span>
          <button aria-label={i18nT('app.dismiss')} className="text-muted hover:text-text leading-none p-0.5"><X className="w-4 h-4" /></button>
        </div>
      )}
      {scene === 'queued' && (
        /* A quoting send parked behind a busy turn: the queue card the drained
           row will come from (`QueueStack`), preview = the typed text alone. */
        <div className="mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }} data-testid="queued-host">
          <QueueStack
            messages={[{ role: 'queued', content: prependQuote(TYPED, { role: 'assistant', text: KIRO_1, ts: SEED[1].ts, mid: SEED[1].mid }), cls: 'msg msg-queued', ts: '2026-09-29T09:14:00Z', meta: { queueId: 'q1', quote: { role: 'assistant', text: KIRO_1, ts: SEED[1].ts, mid: SEED[1].mid } } } as ChatMessage]}
            onCancel={() => {}} onEdit={() => {}} />
        </div>
      )}
      <div className="mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
        <ChatInput value={draft} onChange={setDraft} onSend={send} connected pendingQuote={quote.pendingQuote} onRemoveQuote={quote.clearQuote} {...(crewmateHost ? { busyMode: 'steer-only' as const } : {})} />
      </div>
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Scene />
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
