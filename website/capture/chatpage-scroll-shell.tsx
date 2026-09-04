/**
 * Isolated capture entry for ChatPage's transcript SCROLL SHELL golden frames.
 *
 * Mounts the REAL ChatPage (not a mock of it) with a preloaded store and a
 * deterministic fixture transcript; the capture script answers its API calls
 * via page.route. Golden frames are the visual half of the characterization
 * net: the migration re-captures the same scenes and compares pixels, so
 * everything nondeterministic is pinned here — animations off, fixed
 * timestamps, fixed viewport, DSF 1.
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

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') || 'long' // long | short | paging
// ThemeProvider derives the root theme from its OWN preference store
// (localStorage 'mc-theme', falling back to 'system') and rewrites the
// data-theme attribute on mount — so setting the attribute alone is
// overwritten and every frame silently renders in the system theme.
// Seed the preference the provider actually reads; the attribute below
// only covers the pre-mount paint.
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme)

// Deterministic pixels: no entrance eases, no pulsing dots, no smooth scroll.
const style = document.createElement('style')
style.textContent = '*, *::before, *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; }'
document.head.appendChild(style)

type Msg = { role: string; content: string; cls: string; ts: string }
const pad = (n: number) => String(n).padStart(2, '0')
const mkLong = (): Msg[] => Array.from({ length: 16 }, (_, i) => ([
  { role: 'user', content: `Status check #${i + 1}: anything new in the queue?`, cls: '', ts: `2026-08-27T01:${pad(10 + i)}:00Z` },
  { role: 'assistant', content: `Sweep ${i + 1} done.\n\n- two issues triaged as duplicates\n- one PR moved to review-ready\n- CI green on the retry`, cls: '', ts: `2026-08-27T01:${pad(10 + i)}:30Z` },
])).flat()
const mkShort = (): Msg[] => mkLong().slice(0, 2)

const messages = scene === 'short' ? mkShort() : mkLong()
;(window as unknown as { __CAPTURE_MESSAGES__: Msg[] }).__CAPTURE_MESSAGES__ = messages

const store = configureStore({
  reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  preloadedState: {
    dashboard: {
      status: null,
      slots: [{ key: 'slot-a', title: 'scroll-shell fixture', messages: messages.length, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
      slotsLoaded: true,
      unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: 'slot-a', messages,
      slotRunning: false, slotStopping: false, slotState: 'idle',
      history: [], historyHasMore: false, pendingInput: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
      // The paging scene photographs the two states the shell extraction moved
      // across the file boundary: the earlier-messages bar (aboveRows slot,
      // gated on slotHasMore) and the sticky older-messages spinner
      // (loadingOlder). Only the loadOlderMessages thunk flips loadingOlder,
      // so a preloaded true survives the mount refetch.
      slotHasMore: scene === 'paging', slotOldestIndex: scene === 'paging' ? 4 : 0, loadingOlder: scene === 'paging',
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
