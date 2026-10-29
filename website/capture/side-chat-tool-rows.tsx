/**
 * Evidence for the side transcript drawing the side turn's tool calls.
 *
 * THE CHANGE: a side turn's tool attempts arrive as `tool` rows on the side
 * wire and the shared transcript draws them where they happened — the 🔧 pill
 * between the text rows, a refused one marked blocked by its hidden 🚫 sibling,
 * a finished turn folding them under the same "N tool calls" header the main
 * chat uses. Before, the panel showed the thinking dots and then one text block.
 *
 * Scenes mount the REAL SideChat against the real store, stylesheet, theme
 * tokens and live i18n catalog, with only the api module untouched (no side
 * endpoint is hit: state is preloaded). ?scene=before preloads the transcript
 * the old wire could carry (text only); ?scene=after preloads the same turn
 * with its two tool rows, one of them refused.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import SideChat from '../src/pages/chat/SideChat'
import dashboardReducer from '../src/store/dashboardSlice'
import { createTestStore } from '../src/test/helpers'
import reducer from '../src/store/chatSlice'
import type { SideMessage } from '../src/store/chatSlice'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') === 'before' ? 'before' : 'after'
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.dataset.mode = theme
document.documentElement.dataset.theme = theme === 'light' ? 'kiro-light' : 'kiro-dark'

initI18n()

const SLOT = 'capture-slot'
const initial = reducer(undefined, { type: '@@INIT' })

const question: SideMessage = { role: 'user', content: 'what does the cron store return for this slot?', ts: '2026-10-12T00:00:00Z', run_id: 'r1' }
const opening: SideMessage = { role: 'assistant', content: 'Checking the store file first.', ts: '2026-10-12T00:00:01Z', run_id: 'r1' }
const answer: SideMessage = {
  role: 'assistant',
  content: 'The store holds **2 jobs** for this slot, both paused:\n\n| Job | Schedule |\n|---|---|\n| `nightly-digest` | `0 9 * * *` |\n| `pr-watch` | every 300 s |\n\nI could not rewrite the file from here; use the main chat to resume them.',
  ts: '2026-10-12T00:00:04Z',
  run_id: 'r1',
}
const toolRows: SideMessage[] = [
  {
    role: 'tool',
    content: '🔧 Running: cat ~/.kiro/crew/crons.json\ncat ~/.kiro/crew/crons.json',
    ts: '2026-10-12T00:00:02Z',
    run_id: 'r1',
    meta: { tool_call_id: 'call-1', kind: 'execute', purpose: 'read the cron store', input_preview: 'cat ~/.kiro/crew/crons.json' },
  },
  {
    role: 'tool',
    content: '🔧 Running: sed -i s/paused/active/ ~/.kiro/crew/crons.json\nsed -i s/paused/active/ ~/.kiro/crew/crons.json',
    ts: '2026-10-12T00:00:03Z',
    run_id: 'r1',
    meta: { tool_call_id: 'call-2', kind: 'execute', purpose: 'resume the paused jobs', input_preview: 'sed -i s/paused/active/ ~/.kiro/crew/crons.json' },
  },
  { role: 'tool', content: '🚫 Running: sed -i s/paused/active/ ~/.kiro/crew/crons.json', ts: '2026-10-12T00:00:03Z', run_id: 'r1', meta: { tool_call_id: 'call-2' } },
]

const messages: SideMessage[] = scene === 'before'
  ? [question, { ...opening, content: `${opening.content}\n\n${answer.content}` }]
  : [question, opening, ...toolRows, answer]

const store = createTestStore({
  dashboard: { ...dashboardReducer(undefined, { type: '@@INIT' }), connected: true },
  chat: {
    ...initial,
    activeSlot: SLOT,
    slotSide: {
      [SLOT]: {
        messages,
        lastRunId: 'r1',
        pending: false,
        streaming: false,
        openedAtTurnCount: 0,
        createdAt: '2026-10-12T00:00:00Z',
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
        style={{ width: 460, height: 760, margin: '0 auto', background: 'var(--bg)', border: '1px solid var(--border)', display: 'flex', flexDirection: 'column' }}
      >
        <SideChat slot={SLOT} />
      </div>
    </QueryClientProvider>
  </Provider>,
)
