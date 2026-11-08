/**
 * Isolated capture entry for a sub-agent's approval card in its chat (#28691).
 *
 * WHY ISOLATED: a live sub-agent approval needs a gateway, a parent session and a
 * sub-agent that calls a tool needing approval. This mounts the REAL ChatInput
 * against the REAL chat reducer, then feeds the store the same actions the socket
 * does: the parent's transcript, the coordinator `approval` frame's permission row
 * (`useWebSocket`), and the parent's next user message. The composer then draws
 * whatever approval bar the shipped reducers and selector leave pending.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import ChatInput from '../src/components/ChatInput'
import chatReducer, { sseChatMessage } from '../src/store/chatSlice'
import dashboardReducer from '../src/store/dashboardSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import instancesReducer from '../src/store/instancesSlice'
import type { RootState } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLOT = 'slot-parent'
const ts = (s: number) => new Date(Date.UTC(2026, 10, 2, 22, 0, s)).toISOString()

const preloadedState = {
  chat: {
    activeSlot: SLOT,
    messages: [
      { role: 'user', content: 'run the two backtests in parallel', ts: ts(0), meta: { mid: 'u-1' } },
      { role: 'assistant', content: 'Started two sub-agents.', ts: ts(1), meta: { mid: 'a-1' } },
    ],
    toolLog: [],
    slotStatusDetail: {},
  } as unknown as RootState['chat'],
  dashboard: {
    slots: [{ key: SLOT, messages: 2, running: false, subagents_running: 2, pending_approval: true, waiting_for_input: false, last_activity_ts: undefined }],
    approvalMode: 'normal',
    connected: true,
    channelTrusted: false,
    refreshTrigger: 0,
    unreadSlots: [],
    updateProgress: null,
  } as unknown as RootState['dashboard'],
}

const store = configureStore({
  reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer, instances: instancesReducer },
  preloadedState,
})

// The coordinator `approval` frame's row, as `useWebSocket` writes it.
store.dispatch(sseChatMessage({
  slot: SLOT, role: 'permission', content: '[subagent] Write File', ts: ts(5),
  meta: { tool_input: '{"path":"report.md"}', approval_id: 'sub-approval-1', source: 'subagent', registry: 'coordinator' },
}))
// The person asks the parent something while the sub-agent waits.
store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'how far along are they?', ts: ts(6), meta: { mid: 'u-2' } }))

await initI18n()

const qc = new QueryClient({
  defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false } },
})

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        {/* Width mirrors the chat content column so the row wraps as it does in production. */}
        <div className="bg-bg text-text min-h-[260px] p-8" data-capture-root>
          <div className="max-w-[860px] mx-auto"><ChatInput value="" onChange={() => {}} onSend={() => {}} /></div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
