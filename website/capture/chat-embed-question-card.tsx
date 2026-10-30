/**
 * Isolated capture entry for ChatEmbed's ask_question card (#25336).
 *
 * Mounts the REAL ChatEmbed inside the REAL AppApiProvider, the way an app
 * mounts it: no Redux Provider and no router. Fetch is stubbed: the slot
 * detail loads a running turn, and `GET /api/chat/slots/{slot}/question`
 * returns one blocking card. `?answered=1` makes the question route answer
 * an empty list (the card after it was answered).
 *
 * Query: ?theme=dark|light  ?width=<px> (default 640)  ?answered=1
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { initI18n } from '../src/i18n/all'
import { AppApiProvider, ChatEmbed } from '../src/app-sdk/index'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const width = Number(params.get('width')) || 640
const answered = params.get('answered') === '1'
// ?fail=1: the question read answers 500 (the load-failure notice).
const fail = params.get('fail') === '1'
// ?tall=1: six long options; ?height=<px>: the embed's height (default 560).
const tall = params.get('tall') === '1'
const height = Number(params.get('height')) || 560
// The capture script sets this to retire the card mid-draft (the restored notice).
declare global { interface Window { __retireQuestion?: boolean } }

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLOT = 'chat-embed-question-capture'
const transcript = {
  title: 'Spec: upload rate limit',
  running: true,
  turn: 1,
  turn_gen: 1,
  messages: [
    { role: 'user', content: 'Add a rate limit to the upload endpoint.', cls: 'msg msg-u', ts: '2026-10-28T18:00:00Z' },
    { role: 'assistant', content: 'Before I write it, one choice needs your call.', cls: 'msg msg-a', ts: '2026-10-28T18:00:04Z' },
  ],
}
const card = {
  ask_id: '0123456789abcdef0123456789abcdef',
  slot: SLOT,
  ts: 1,
  questions: [{
    question: 'Which key should the limit count against?',
    header: 'SCOPE',
    options: tall
      ? Array.from({ length: 6 }, (_, i) => ({
        label: `Option ${i + 1}`,
        description: 'A long description that wraps onto a second line in a narrow embed, so the card grows taller than the space it has.',
      }))
      : [
        { label: 'Per API key', description: 'One budget per key, wherever it calls from' },
        { label: 'Per IP address', description: 'One budget per client address' },
      ],
  }],
}

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
const realFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const path = new URL(url, location.origin)
  if (path.pathname === `/api/chat/slots/${SLOT}`) return json(transcript)
  if (path.pathname === `/api/chat/slots/${SLOT}/question`) {
    if (fail) return new Response('{"error":"down"}', { status: 500, headers: { 'Content-Type': 'application/json' } })
    return json({ pending: answered || window.__retireQuestion ? [] : [card] })
  }
  if (path.pathname.startsWith('/api/')) return json({})
  return realFetch(input, init)
}

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <AppApiProvider
      appName="capture"
      allowedApiPaths={['/api/chat']}
      allowedEvents={[]}
      subscribeFn={() => () => {}}
      navigateFn={() => {}}
      notifyFn={() => {}}
    >
      <div data-capture-root style={{ width, height, margin: '16px auto', background: 'var(--bg)', padding: 16, boxSizing: 'content-box' }}>
        <ChatEmbed slotKey={SLOT} agent="spec-builder" startAtBottom />
      </div>
    </AppApiProvider>
  </QueryClientProvider>,
)
