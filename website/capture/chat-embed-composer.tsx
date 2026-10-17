/**
 * Isolated capture entry for ChatEmbed's composer (chat-core P3-f).
 *
 * Mounts the REAL ChatEmbed inside the REAL AppApiProvider, the way an app
 * mounts it: no Redux Provider and no router. Fetch is stubbed so the slot
 * detail loads with fixed fake data and POST /api/chat answers as the scene
 * asks.
 *
 * Query: ?theme=dark|light  ?running=1 (a turn is running)  ?refuse=1 (the
 * send is refused with a 409, so the error row and the restored draft show).
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { initI18n } from '../src/i18n/all'
import { AppApiProvider, ChatEmbed } from '../src/app-sdk/index'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const running = params.get('running') === '1'
const refuse = params.get('refuse') === '1'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLOT = 'chat-embed-composer-capture'
const transcript = {
  title: 'Spec: upload rate limit',
  running,
  messages: [
    { role: 'user', content: 'Draft the acceptance criteria for the upload rate limit.', cls: 'msg msg-u', ts: '2026-09-04T18:00:00Z' },
    {
      role: running ? 'streaming' : 'assistant',
      content: 'Three criteria: a 429 on the 11th upload within a minute, a `Retry-After` header on that 429, and the limit applied per API key rather than per IP.',
      cls: 'msg msg-a',
      ts: '2026-09-04T18:00:04Z',
    },
  ],
}

const realFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const path = new URL(url, location.origin)
  if (path.pathname === `/api/chat/slots/${SLOT}`) {
    return new Response(JSON.stringify(transcript), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  if (path.pathname === '/api/chat' && init?.method === 'POST') {
    if (refuse) {
      return new Response(JSON.stringify({ error: 'slot agent mismatch', code: 'slot_agent' }), { status: 409, headers: { 'Content-Type': 'application/json' } })
    }
    return new Response(JSON.stringify({ ok: true, queued: running, dispatched: !running }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
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
      <div data-capture-root style={{ width: 560, height: 420, margin: '16px auto', background: 'var(--bg)', padding: 16 }}>
        <ChatEmbed slotKey={SLOT} agent="spec-builder" />
      </div>
    </AppApiProvider>
  </QueryClientProvider>,
)
