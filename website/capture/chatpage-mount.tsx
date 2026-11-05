/**
 * Mounts the REAL ChatPage on a preloaded store, for capture entries that photograph
 * one fixture transcript. The capture script answers the page's API calls; its slot
 * response must carry the same messages, because the mount refetch replaces them.
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import chatReducer from '../src/store/chatSlice'
import dashboardReducer from '../src/store/dashboardSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import { ThemeProvider } from '../src/hooks/useTheme'
import type { RootState } from '../src/store'
import ChatPage from '../src/pages/ChatPage'

export interface ChatPageMount {
  messages: unknown[]
  title: string
  /** The active slot has a turn running, so the composer shows its mid-turn affordance. */
  busy?: boolean
  /** The earlier-messages bar and the older-messages spinner are both on screen. */
  paging?: boolean
}

export function mountChatPage({ messages, title, busy = false, paging = false }: ChatPageMount): void {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        slots: [{ key: 'slot-a', title, messages: messages.length, running: busy, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages,
        slotRunning: busy, slotStopping: false, slotState: busy ? 'streaming' : 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        // Only the loadOlderMessages thunk flips loadingOlder, so a preloaded true
        // survives the mount refetch; the paging chrome is also gated on the cursor
        // belonging to the active slot.
        slotHasMore: paging, slotOldestIndex: paging ? 4 : 0, loadingOlder: paging,
        slotCursorKey: paging ? 'slot-a' : null,
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
}
