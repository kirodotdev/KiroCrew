/**
 * Isolated capture entry for ChatPane's working indicator (the ghost-pose
 * carousel from ChatFooter, newly hosted by the pane).
 *
 * Mounts the REAL ChatPane against the real stylesheet, theme tokens and live
 * i18n catalog. API responses come from the capture script's route
 * interception (gateway-free); this entry seeds what the pane reads from the
 * store: the active slot, a short transcript, and the per-slot stream state
 * that drives the loader (`tool_running` = a turn is running with no text
 * arriving — exactly the gap the indicator exists to cover).
 *
 * Scenes via query string: ?theme=dark|light and ?state=tool_running|idle.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ChatPane from '../src/components/ChatPane'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { sseSlots } from '../src/store/dashboardSlice'
import { hydrateSlotMessages, sseChatMessage } from '../src/store/chatSlice'
import '../src/index.css'

/** Mirrors chatSlice's (unexported) SlotState — the capture drives two of them. */
type PaneState = 'idle' | 'tool_running'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const state = (params.get('state') || 'tool_running') as PaneState
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLOT = 'chat-1-loader'

store.dispatch(
  sseSlots([
    {
      key: SLOT,
      title: 'radar',
      messages: 2,
      running: state !== 'idle',
      mode: 'member',
      agent: 'radar',
    },
  ] as never),
)
// The pane is a WARM (non-active) slot — the shape a member DM thread or a
// split pane actually has — so messages come from the per-slot cache and the
// stream state from slotRun, both fed by the same reducers the WS drives.
store.dispatch(
  hydrateSlotMessages({
    slot: SLOT,
    messages: [
      { role: 'user', content: '帮我看一下今晚新增的 issue', ts: '2026-09-01T00:00:00Z' },
      { role: 'assistant', content: '收到，我扫一遍 issue 队列和关联的 PR，稍等。', ts: '2026-09-01T00:00:05Z' },
    ],
    hasMore: false,
    total: 2,
    running: state !== 'idle',
  } as never),
)
if (state === 'tool_running') {
  // The same WS frame a real running turn delivers: sseChatMessage on a
  // non-active slot inserts the tool row AND flips slotRun to tool_running —
  // the exact signal the pane's loader reads in production.
  store.dispatch(
    sseChatMessage({
      slot: SLOT,
      role: 'tool',
      content: '🔧 gh issue list --state open',
      ts: '2026-09-01T00:00:06Z',
      meta: { kind: 'shell' },
    }),
  )
}

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

async function main() {
  await initI18n()
  createRoot(document.getElementById('root')!).render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <div className="h-screen bg-bg text-text" data-capture-root>
            <ChatPane slotKey={SLOT} frameless />
          </div>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

main()
