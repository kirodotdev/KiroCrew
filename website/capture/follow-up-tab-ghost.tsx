/**
 * Isolated capture entry for the composer's Tab-to-fill follow-up suggestion
 * (issue #28788). Mounts the REAL ChatPage with a preloaded store whose last
 * assistant turn ends in an `[OPTIONS:]` marker; the driver
 * (scripts/capture-follow-up-tab-ghost.mjs) answers its API calls.
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { initI18n } from '../src/i18n'
import chatReducer from '../src/store/chatSlice'
import dashboardReducer from '../src/store/dashboardSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import { ThemeProvider } from '../src/hooks/useTheme'
import type { RootState } from '../src/store'
import ChatPage from '../src/pages/ChatPage'
import '../src/index.css'

initI18n('en')

const theme = new URLSearchParams(location.search).get('theme') || 'dark'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme)

const style = document.createElement('style')
style.textContent = '*, *::before, *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; }'
document.head.appendChild(style)

const messages = [
  { role: 'user', content: 'Can the composer suggest the first option?', cls: '', ts: '2026-11-03T02:00:00Z' },
  { role: 'assistant', content: 'Yes. Pick how to go on.\n\n[OPTIONS: Open an issue and show screenshots | Open an issue only | Show screenshots first]', cls: '', ts: '2026-11-03T02:00:30Z' },
]
;(window as unknown as { __CAPTURE_MESSAGES__: typeof messages }).__CAPTURE_MESSAGES__ = messages

const store = configureStore({
  reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  preloadedState: {
    dashboard: {
      status: null,
      slots: [{ key: 'slot-a', title: 'tab ghost fixture', messages: messages.length, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
      slotsLoaded: true,
      unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: 'slot-a', messages,
      slotRunning: false, slotStopping: false, slotState: 'idle',
      history: [], historyHasMore: false, pendingInput: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
      slotHasMore: false, slotOldestIndex: 0, loadingOlder: false, slotCursorKey: null,
      slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
      historyOffset: 0, _wsChunkedDuringFetch: false,
      slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
    notifications: { items: [] } as unknown as RootState['notifications'],
  },
})

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <Provider store={store}>
      <ThemeProvider>
        <MemoryRouter>
          <div data-capture-root style={{ height: '100vh', display: 'flex', flexDirection: 'column' }}>
            <ChatPage />
          </div>
        </MemoryRouter>
      </ThemeProvider>
    </Provider>
  </QueryClientProvider>,
)
