/**
 * Isolated capture entry for the session-trust tier on a card whose COMMAND
 * scope the gateway could not prove.
 *
 * WHY ISOLATED: reaching this state through the full SPA needs a gateway, a
 * live session, and a tool call whose command the ACP transport redacted or
 * could not canonicalize. This mounts the REAL ChatInput against the real
 * stylesheet with the permission meta the gateway sends, so the row is the
 * shipped row.
 *
 * The change under test is which meta the gateway emits, so the two scenes are
 * two payloads rendered by the same build rather than two builds:
 *   before - no grant proof at all, which is what the runner emitted for an
 *            underivable command. Allow once and Reject are the only controls.
 *   after  - `trust_grantable: '1'` with no command-scoped bits, which is what
 *            the runner emits now. Trust is back, and its menu holds exactly
 *            the one tier that names no command.
 *   folded - a fully-proven READ-ONLY command: the row that would otherwise
 *            grow to four controls (Allow once, Trust reads, Trust, Reject).
 *            Every standing grant is a tier in the one dropdown, so the row
 *            stays at three.
 *
 * Scene + theme come from the query string: ?scene=after&theme=dark
 */
import { createRoot } from 'react-dom/client'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import ChatInput from '../src/components/ChatInput'
import chatReducer from '../src/store/chatSlice'
import dashboardReducer from '../src/store/dashboardSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import instancesReducer from '../src/store/instancesSlice'
import type { RootState } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') || 'after'

document.documentElement.dataset.theme = theme
document.documentElement.classList.toggle('dark', theme === 'dark')

/** The command from the report: canonicalization failed, so no command-scoped
 *  tier is offered on the before/after scenes. The folded scene uses a proven
 *  read-only command instead, since that is the row the cap applies to. */
const CARD_TITLE = scene === 'folded'
  ? 'Running: ls -la ~/.kiro/crew/workspace/docs'
  : 'Running: cd ~/.kiro/crew/workspace/docs/structured-payments'

const meta: Record<string, string> = {
  approval_id: 'ap-capture',
  request_id: 'req-capture',
  tool_title: CARD_TITLE,
  is_shell: '1',
}
if (scene === 'after' || scene === 'folded') meta.trust_grantable = '1'
if (scene === 'folded') {
  meta.is_read_only = '1'
  meta.full_command = 'ls -la ~/.kiro/crew/workspace/docs'
  meta.base_command = 'ls'
  meta.trust_command_grantable = '1'
  meta.trust_base_grantable = '1'
}

const preloadedState = {
  chat: {
    activeSlot: 'slot-1',
    messages: [
      { role: 'user', content: 'open the payments docs' },
      { role: 'permission', content: CARD_TITLE, meta },
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

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        {/* Width mirrors the chat content column so the row wraps as it does in production. */}
        <div className="bg-bg text-text min-h-screen p-8" data-capture-root>
          <div className="max-w-[860px] mx-auto">
            <ChatInput value="" onChange={() => {}} onSend={() => {}} />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
