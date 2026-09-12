/**
 * Isolated capture entry for ChatEmbed's send-failure state (chat-core P2).
 *
 * WHY ISOLATED: a refused send needs a server that says no to POST /api/chat
 * (a 409 slot-agent mismatch, a 403 from an app that never granted the path)
 * -- none exists in a capture run. This mounts the REAL ChatEmbed inside the
 * REAL AppApiProvider (so the send goes through the permission-scoped api and
 * the app-sdk wire exactly as in production) against a fetch stub that serves
 * the slot detail and refuses the send with the same body shape the backend
 * answers with.
 *
 * What it documents: before this branch a refused send rendered NOTHING -- the
 * composer had cleared, the SSE/JSON mismatch was swallowed as success, and
 * the text was gone. The after-frame shows the `error` row with the server's
 * own reason and the text handed back to the composer.
 *
 * Query: ?theme=dark|light  ?refuse=409|403|late
 *   403  = the scoped api itself refusing an app that did not grant /api/chat
 *          (a refused receipt naming the missing grant).
 *   late = the POST never answers, so the transport deadline fires
 *          (a response-late receipt: text handed back under a notice).
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { AppApiProvider, ChatEmbed } from '../src/app-sdk/index'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const refuse = params.get('refuse') || '409'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLOT = 'chat-embed-capture'
const transcript = {
  title: 'Spec: upload rate limit',
  running: false,
  messages: [
    { role: 'user', content: 'Draft the acceptance criteria for the upload rate limit.', cls: 'msg msg-u', ts: '2026-09-04T18:00:00Z' },
    { role: 'assistant', content: 'Three criteria: a 429 on the 11th upload within a minute, a `Retry-After` header on that 429, and the limit applied per API key rather than per IP.', cls: 'msg msg-a', ts: '2026-09-04T18:00:04Z' },
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
    if (refuse === 'late') return new Promise<Response>(() => {})
    return new Response(
      JSON.stringify({ error: 'slot agent mismatch', code: 'slot_agent' }),
      { status: 409, headers: { 'Content-Type': 'application/json' } },
    )
  }
  return realFetch(input, init)
}

// A 403 scene grants the app NO chat path, so the scoped api refuses before
// any request leaves the document -- the transport-error receipt.
const allowedApiPaths = refuse === '403' ? ['/api/apps/capture'] : ['/api/chat']

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <AppApiProvider
          appName="capture"
          allowedApiPaths={allowedApiPaths}
          allowedEvents={[]}
          subscribeFn={() => () => {}}
          navigateFn={() => {}}
          notifyFn={() => {}}
        >
          <div data-capture-root style={{ width: 560, height: 480, margin: '16px auto', background: 'var(--bg)', padding: 16 }}>
            <ChatEmbed slotKey={SLOT} agent="spec-builder" />
          </div>
        </AppApiProvider>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
