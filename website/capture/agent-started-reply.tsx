/**
 * Isolated capture entry for a turn the agent started on its own
 * (#17566, docs/request-for-change/rfc-agent-started-turns.md).
 *
 * Renders the shared ChatPane transcript against the real stylesheet, theme tokens
 * and i18n catalog, for the repro: a background command, then the turn the agent
 * starts when it finishes, then the person's next message.
 *   - after (default): the agent's turn is its own group, headed by its label.
 *   - before: the same rows as main marks them, so the agent's tool row and reply
 *     fold into the turn above them, unlabelled.
 *   - tool-only: the agent's turn wrote only a tool row.
 *
 * Query: ?state=after|before|tool-only  ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ChatPane from '../src/components/ChatPane'
import { ThemeProvider } from '../src/hooks/useTheme'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { sseSlots } from '../src/store/dashboardSlice'
import { hydrateSlotMessages } from '../src/store/chatSlice'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const state = params.get('state') || 'after'
const theme = params.get('theme') || 'dark'
localStorage.setItem('mc-theme', theme === 'light' ? 'light' : 'dark')
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLOT = 'chat-1-agent-started'
const ASK = 'Run `sleep 60; echo done` in the background and tell me when it finishes.'
const STARTED = 'Started it in the background. I will report back when it finishes.'
const FINISHED = 'The background command finished with exit code 0 and printed `done`.'
const STATUS = 'Nothing else is running.'
const TOOL = { role: 'tool', content: '🔧 Read background task output', meta: { kind: 'read' } }

const opening = [
  { role: 'user', content: ASK, ts: '2026-10-30T18:00:00Z' },
  { role: 'assistant', content: STARTED, ts: '2026-10-30T18:00:04Z', meta: { turn_stats: { elapsed_ms: 4100, cost_usd: 0.0112 } } },
]
// The rows main writes mark the agent's reply only; this change also marks the
// turn's first row, which opens its group.
const AGENT = { kind: 'agent' }
const OPENER = { kind: 'agent', turn_start: true }
const agentTool = { ...TOOL, ts: '2026-10-30T18:01:05Z', meta: { ...TOOL.meta, ...(state === 'before' ? {} : { origin: OPENER }) } }
const agentReply = {
  role: 'assistant',
  content: FINISHED,
  ts: '2026-10-30T18:01:07Z',
  meta: { origin: AGENT, turn_stats: { elapsed_ms: 2600, cost_usd: 0.0094 } },
}
const messages = [
  ...opening,
  agentTool,
  ...(state === 'tool-only' ? [] : [agentReply]),
  { role: 'user', content: 'status?', ts: '2026-10-30T18:04:10Z' },
  { role: 'assistant', content: STATUS, ts: '2026-10-30T18:04:12Z', meta: { turn_stats: { elapsed_ms: 2100, cost_usd: 0.0078 } } },
]

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

async function mount() {
  // The transcript read answers with the same rows the store is seeded with.
  const realFetch = window.fetch.bind(window)
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    if (new URL(url, location.origin).pathname === `/api/chat/slots/${SLOT}`) {
      const body = { title: 'background job', running: false, messages, total: messages.length }
      return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    return realFetch(input, init)
  }
  store.dispatch(sseSlots([{ key: SLOT, title: 'background job', messages: messages.length, running: false }] as never))
  store.dispatch(
    hydrateSlotMessages({ slot: SLOT, messages, hasMore: false, total: messages.length, running: false } as never),
  )
  await initI18n()
  createRoot(document.getElementById('root')!).render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <ThemeProvider>
          <MemoryRouter>
            <div className="h-screen bg-bg text-text p-4" data-capture-page>
              <div className="h-full max-w-[760px] mx-auto" data-capture-root>
                <ChatPane slotKey={SLOT} agentLocked />
              </div>
            </div>
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
}

void mount()
