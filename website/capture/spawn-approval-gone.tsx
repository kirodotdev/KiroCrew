/**
 * Isolated capture entry for a spawn approval that is already gone, seen from
 * BOTH surfaces that resolve it: the composer's spawn banner and the activity
 * panel's sub-agent card (#11180 follow-up).
 *
 * WHY ISOLATED: a gone approval needs a pending spawn whose future the gateway
 * no longer holds (decided or expired across a reconnect), which a live stack
 * cannot stage on demand. This mounts the REAL ChatInput and the REAL
 * ActivityViewer on one store seeded with a pending spawn, against the real
 * stylesheet. The panel reads its sub-agents from that store, as ChatPage
 * does. Every approval POST answers the gateway's own refusal for a missing
 * future (`api_approval_resolve`: 404 `not found or expired`).
 *
 * Theme comes from the query string: ?theme=dark
 */
import { createRoot } from 'react-dom/client'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import ChatInput from '../src/components/ChatInput'
import ActivityViewer from '../src/pages/chat/ActivityViewer'
import chatReducer from '../src/store/chatSlice'
import dashboardReducer from '../src/store/dashboardSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import instancesReducer from '../src/store/instancesSlice'
import { useAppSelector, type RootState } from '../src/store'
import type { SubagentActivity } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/approvals/')) {
    return Promise.resolve(new Response('{"error": "not found or expired"}', {
      status: 404,
      headers: { 'Content-Type': 'application/json' },
    }))
  }
  if (url.includes('/api/')) {
    // The composer's slash-command menu reads a list; everything else an object.
    const body = url.includes('/api/slash-commands') ? '[]' : '{}'
    return Promise.resolve(new Response(body, { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  return realFetch(input, init)
}) as typeof fetch

const SLOT = 'slot-1'
const pending: SubagentActivity = {
  id: 'a1',
  task: 'Audit the release notes for the insider build',
  agent: 'kirocrew',
  status: 'pending',
  streaming: '',
  lastTool: '',
  startedAt: Date.now() - 42_000,
  elapsed: 42,
  approval_id: 'spawn:a1',
}

const chatInit = chatReducer(undefined, { type: '@@capture/init' })
const dashboardInit = dashboardReducer(undefined, { type: '@@capture/init' })
const store = configureStore({
  reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer, instances: instancesReducer },
  preloadedState: {
    chat: { ...chatInit, activeSlot: SLOT, messages: [{ role: 'user', content: 'Audit the release notes' }], subagents: { a1: pending } } as RootState['chat'],
    dashboard: { ...dashboardInit, connected: true } as RootState['dashboard'],
  },
})

await initI18n()

const qc = new QueryClient({
  defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false } },
})

/** The panel is fed from the store, exactly as ChatPage feeds it. */
function Panel() {
  const subagents = useAppSelector(s => s.chat.subagents)
  return <ActivityViewer subagents={subagents} toolLog={[]} open onToggle={() => {}} slot={SLOT} view="subagents" />
}

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <div data-capture-root style={{ display: 'flex', height: '100vh', background: 'var(--bg)' }}>
          <div data-capture-composer style={{ flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'flex-end', padding: 16 }}>
            <ChatInput value="" onChange={() => {}} onSend={() => {}} />
          </div>
          <div data-capture-panel style={{ width: 400, display: 'flex', flexDirection: 'column', borderLeft: '1px solid var(--border)' }}>
            <Panel />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
