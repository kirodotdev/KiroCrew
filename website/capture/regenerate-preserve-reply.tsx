/**
 * Isolated capture entry for #14632: Regenerate keeps the previous reply until
 * the new turn ends.
 *
 * Mounts the REAL ChatPage with a preloaded store. The capture script
 * (scripts/capture-14632-regenerate-preserve-reply.mjs) answers the API with
 * page.route and plays the server's side of the turn through the same store
 * paths the live socket drives: `chunk` frames for the streaming reply,
 * `setSlotRunning` for the turn's start and end, and `refreshSlot` for the
 * `chat_variant_switch` refetch that lands the swap.
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { initI18n } from '../src/i18n'
import chatReducer, { refreshSlot, sseChatMessage, setSlotRunning } from '../src/store/chatSlice'
import dashboardReducer from '../src/store/dashboardSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import { ThemeProvider } from '../src/hooks/useTheme'
import type { RootState } from '../src/store'
import ChatPage from '../src/pages/ChatPage'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme)

const SLOT = 'slot-a'
// Must match the script's fixture: the mount refetch REPLACES chat.messages.
const messages = [
  { role: 'user', content: 'Summarize what changed in the release branch this week.', cls: '', ts: '2026-10-24T14:00:00Z', meta: { mid: 'm-user-1' } },
  { role: 'assistant', content: 'Three changes landed this week:\n\n- the retry budget moved to config\n- the health check now times out after 5s\n- two flaky tests were quarantined', cls: '', ts: '2026-10-24T14:00:20Z', meta: { mid: 'm-reply-1' } },
]
;(window as unknown as { __CAPTURE_MESSAGES__: unknown }).__CAPTURE_MESSAGES__ = messages

const store = configureStore({
  reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  preloadedState: {
    dashboard: {
      status: null,
      slots: [{ key: SLOT, title: 'release notes', messages: messages.length, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
      slotsLoaded: true,
      unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: SLOT, messages,
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

// The server's side of the turn, driven by the capture script.
;(window as unknown as { __capture: unknown }).__capture = {
  startTurn: () => store.dispatch(setSlotRunning(true)),
  chunk: (content: string, seq: number) => store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content, seq } as never)),
  endTurn: () => store.dispatch(setSlotRunning(false)),
  refetch: () => store.dispatch(refreshSlot(SLOT)),
}

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
