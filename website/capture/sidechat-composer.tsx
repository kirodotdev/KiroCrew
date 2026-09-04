/**
 * Evidence for the SideChat composer upgrade (chat-core extraction, phase 1).
 *
 * THE CHANGE: the side panel's bare 2-row <textarea> + hand-rolled send button
 * is replaced by the REAL native composer (components/ChatInput) — the same
 * component the main chat and session-grid panes render — scoped by
 * capability omission (no uploads, no voice, no agent/model pickers).
 *
 * Scenes mount the REAL SideChat against the real store, stylesheet, theme
 * tokens and live i18n catalog, with only the api module untouched (no side
 * endpoint is hit: state is preloaded). ?scene=idle shows the resting
 * composer; ?scene=busy shows a mid-turn side session where the split
 * Steer/Queue button (previously a SideChat-local fork, now ChatInput's own)
 * is offered.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import SideChat from '../src/pages/chat/SideChat'
import dashboardReducer from '../src/store/dashboardSlice'
import { createTestStore } from '../src/test/helpers'
import reducer from '../src/store/chatSlice'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') === 'busy' ? 'busy' : 'idle'
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.dataset.mode = theme
document.documentElement.dataset.theme = theme === 'light' ? 'kiro-light' : 'kiro-dark'

initI18n()

const SLOT = 'capture-slot'
const initial = reducer(undefined, { type: '@@INIT' })

const store = createTestStore({
  // The composer blocks sends while the gateway reads as offline; these frames
  // show the connected resting state.
  dashboard: { ...dashboardReducer(undefined, { type: '@@INIT' }), connected: true },
  chat: {
    ...initial,
    activeSlot: SLOT,
    slotSide: {
      [SLOT]: {
        messages: [
          { role: 'user' as const, content: 'why is the schedule list empty?', ts: '2026-08-22T00:00:00Z', run_id: 'r1' },
          { role: 'assistant' as const, content: 'The cron store answered with zero jobs — checking whether the gateway filter dropped them.', ts: '2026-08-22T00:00:01Z', run_id: 'r1' },
        ],
        lastRunId: 'r1',
        pending: false,
        streaming: scene === 'busy',
        openedAtTurnCount: 0,
        createdAt: '2026-08-22T00:00:00Z',
      },
    },
  },
})

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

const root = createRoot(document.getElementById('root')!)
root.render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <div
        data-capture-root
        style={{ width: 420, height: 560, margin: '0 auto', background: 'var(--bg)', border: '1px solid var(--border)', display: 'flex', flexDirection: 'column' }}
      >
        <SideChat slot={SLOT} />
      </div>
    </QueryClientProvider>
  </Provider>,
)
