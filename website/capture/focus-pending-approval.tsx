/**
 * Isolated capture entry for the Alt+Shift+Enter approval-focus chord (issue #7379).
 *
 * WHY ISOLATED: reaching a live pending approval through the full SPA needs a
 * gateway, a session and an agent that happens to call a tool needing approval.
 * This mounts the REAL ChatInput with a seeded pending-approval message AND the
 * REAL useKeyboardShortcuts handler, against the real stylesheet, so a keypress
 * in a real browser drives the same chain the shipped app does -- including the
 * real api client, which is how the shot also shows that focusing resolves
 * nothing (no request is made).
 *
 * Scene + theme come from the query string: ?scene=bar&theme=dark
 * Scenes:
 *   bar          - the approval row as it renders today
 *   bar-focused  - after the capture script presses the chord
 *
 * `bar-focused` is not a separate render: the script presses the chord against
 * the same page, so the frame shows a real focus ring produced by real focus.
 */
import { createRoot } from 'react-dom/client'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import ChatInput from '../src/components/ChatInput'
import { useKeyboardShortcuts } from '../src/hooks/useKeyboardShortcuts'
import chatReducer from '../src/store/chatSlice'
import dashboardReducer from '../src/store/dashboardSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import instancesReducer from '../src/store/instancesSlice'
import type { RootState } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'

document.documentElement.dataset.theme = theme
document.documentElement.classList.toggle('dark', theme === 'dark')

const preloadedState = {
  chat: {
    activeSlot: 'slot-1',
    messages: [
      { role: 'user', content: 'list the files in /tmp' },
      {
        role: 'permission',
        content: 'Running: ls /tmp',
        meta: {
          approval_id: 'ap-capture',
          request_id: 'req-capture',
          tool_input: '{"command":"ls /tmp"}',
          is_read_only: '1',
          tool_title: 'Running: ls /tmp',
          is_shell: '1',
          full_command: 'ls /tmp',
          base_command: 'ls',
          trust_command_grantable: '1',
          trust_base_grantable: '1',
          tool_call_id: 'tc-capture',
        },
      },
    ],
    toolLog: [],
    slotStatusDetail: {},
  } as unknown as RootState['chat'],
  dashboard: {
    slots: [{ key: 'slot-1', messages: 2, running: true, pending_approval: true, waiting_for_input: false, last_activity_ts: undefined }],
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

await initI18n()

const qc = new QueryClient({
  defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false } },
})

/** The composer plus the root chord handler, which lives at the app root. */
function Scene() {
  useKeyboardShortcuts({ onToggleShortcutsModal: () => {}, onNewChat: () => {} })
  return <ChatInput value="" onChange={() => {}} onSend={() => {}} />
}

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        {/* Width mirrors the chat content column so the row wraps as it does in production. */}
        <div className="bg-bg text-text min-h-screen p-8" data-capture-root>
          <div className="max-w-[860px] mx-auto"><Scene /></div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
