/**
 * Isolated capture entry for the fork entries in an assistant message's More menu.
 *
 * Mounts the REAL `AssistantMessage` over the literal ChatPage row chain, with the props ChatPage
 * passes for a reply whose fork point resolves on the server (`forkIndex` + `forkMessageId`), so the
 * menu is the one a user sees. Nothing is mocked inside the component; `onFork` records its calls.
 *
 * Query string: ?theme=dark|light&choice=on|off[&inflight=1][&newest=1]
 *   choice=on    `forkDirectionChoice` true, as ChatPage passes it while Tail-only Fork is on.
 *   choice=off   `forkDirectionChoice` false, as ChatPage passes it while the setting is off.
 *   inflight=1   `onFork` never settles, so a fork stays in flight once an entry is chosen.
 *   newest=1     the reply is the newest message: no later user message is drawn and
 *                `forkNothingAfter` is true, as ChatPage passes it there. Without it a later user
 *                message follows the reply and `forkNothingAfter` is false.
 *   all=1        also pass `onSpeak` and `shareEnabled`, so Speak and Share join the menu.
 */
import type { ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import UserMessage from '../src/pages/chat/UserMessage'
import AssistantMessage from '../src/pages/chat/AssistantMessage'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const choice = params.get('choice') !== 'off'
const inflight = params.get('inflight') === '1'
const newest = params.get('newest') === '1'
const all = params.get('all') === '1'
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

const T0 = '2026-09-29T09:12:00Z'
const USER_TEXT = 'What changed in the composer PR? Keep it short.'
const T1 = '2026-09-29T09:13:05Z'
const LATER_TEXT = 'Which of those needs a test?'
const REPLY = [
  'Three things landed in the composer:',
  '',
  '- Staged files now show as chips above the textarea, not tokens inside it.',
  '- Cancel on a queued message restores the typed text **and** re-stages the files.',
  '- The Steer/Queue split button remembers its last mode per slot.',
].join('\n')

declare global { interface Window { __forkCalls: unknown[][] } }
window.__forkCalls = []
const onFork = (...args: unknown[]) => {
  window.__forkCalls.push(args)
  return inflight ? new Promise<void>(() => {}) : Promise.resolve()
}

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
  return (
    <div className="bg-bg text-text flex flex-col" style={{ height: '100vh' }} data-capture-root>
      <div className="flex-1 overflow-y-auto py-4">
        <RowBox user>
          <UserMessage content={USER_TEXT} messageTs={T0} timestamp="09:12" timestampTitle="Today 09:12" slotKey="chat-1" slotTitle="Composer PR" onTogglePin={() => {}} canEdit onEditResend={() => {}} renderContent={(c: string) => <MarkdownRenderer content={c} softBreaks />} />
        </RowBox>
        <RowBox>
          <AssistantMessage content={REPLY} isStreaming={false} slotRunning={false} messageTs="2026-09-29T09:12:40Z" timestamp="09:12" timestampTitle="Today 09:12" slotKey="chat-1" slotTitle="Composer PR" onTogglePin={() => {}}
            onRegenerate={() => {}} onQuoteMessage={() => {}}
            onFork={onFork} forkIndex={1} forkMessageId="m2" forkDirectionChoice={choice} forkNothingAfter={newest}
            onSpeak={all ? () => {} : undefined} shareEnabled={all} />
        </RowBox>
        {!newest && (
          <RowBox user>
            <UserMessage content={LATER_TEXT} messageTs={T1} timestamp="09:13" timestampTitle="Today 09:13" slotKey="chat-1" slotTitle="Composer PR" onTogglePin={() => {}} canEdit onEditResend={() => {}} renderContent={(c: string) => <MarkdownRenderer content={c} softBreaks />} />
          </RowBox>
        )}
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
