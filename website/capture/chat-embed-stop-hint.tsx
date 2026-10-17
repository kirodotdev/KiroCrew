/**
 * Isolated capture entry for ChatEmbed's stop hint strip beside the working
 * indicator.
 *
 * Mounts the REAL ChatEmbed inside the REAL AppApiProvider, the way an app
 * mounts it: no Redux Provider and no router. Fetch is stubbed so the slot
 * detail loads fixed fake data: a running turn whose stop press is pending,
 * so the scroller ends in the working indicator and the force-stop hint
 * shows above the composer.
 *
 * Query: ?theme=dark|light  ?width=<px> (the embed's width, default 640)
 * ?frameless=1 (the host draws the frame).
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { initI18n } from '../src/i18n/all'
import { AppApiProvider, ChatEmbed } from '../src/app-sdk/index'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const width = Number(params.get('width')) || 640
const frameless = params.get('frameless') === '1'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLOT = 'chat-embed-stop-hint-capture'
const transcript = {
  title: 'Spec: upload rate limit',
  running: true,
  stop_state: 'soft_pending',
  turn: 1,
  turn_gen: 1,
  // Enough rows to overflow the scroller, so the working indicator sits at
  // its bottom edge, right above the hint strip.
  messages: [
    { role: 'user', content: 'Draft the acceptance criteria for the upload rate limit.', cls: 'msg msg-u', ts: '2026-09-04T18:00:00Z' },
    {
      role: 'assistant',
      content: 'Three criteria: a 429 on the 11th upload within a minute, a `Retry-After` header on that 429, and the limit applied per API key rather than per IP.',
      cls: 'msg msg-a',
      ts: '2026-09-04T18:00:04Z',
    },
    { role: 'user', content: 'Also cap the per-key burst at 20, and say what the client sees when it hits the cap.', cls: 'msg msg-u', ts: '2026-09-04T18:01:00Z' },
    {
      role: 'assistant',
      content: 'A fourth criterion: a burst above 20 uploads in one second answers 429 with the same `Retry-After` header, and the response body names the limit that was hit so the client can show it.',
      cls: 'msg msg-a',
      ts: '2026-09-04T18:01:05Z',
    },
    { role: 'user', content: 'Now check the handler against all four and list what is missing.', cls: 'msg msg-u', ts: '2026-09-04T18:02:00Z' },
  ],
}

const realFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const path = new URL(url, location.origin)
  if (path.pathname === `/api/chat/slots/${SLOT}`) {
    return new Response(JSON.stringify(transcript), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  if (path.pathname.startsWith('/api/')) {
    return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } })
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
      <div data-capture-root style={{ width, height: 420, margin: '16px auto', background: 'var(--bg)', padding: 16, boxSizing: 'content-box' }}>
        <ChatEmbed slotKey={SLOT} agent="spec-builder" frameless={frameless} startAtBottom />
      </div>
    </AppApiProvider>
  </QueryClientProvider>,
)
