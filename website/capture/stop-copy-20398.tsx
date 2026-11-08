/**
 * Isolated capture entry for the Stop escalation's one name and the plain
 * refused-Stop notice (#20398).
 *
 * Renders the REAL ChatPane or ChatEmbed against the real stylesheet, theme
 * tokens and i18n catalog, with `fetch` stubbed. `?fail=404|500` makes the
 * slot's `/stop` refuse the way the gateway does (`slot_not_found`, or a
 * server error with its own text), so the harness can press Stop and capture
 * the notice.
 *
 * Query: ?surface=pane|embed  ?theme=dark|light  ?state=idle|soft_pending|killing  ?fail=404|500
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ChatPane from '../src/components/ChatPane'
import { AppApiProvider, ChatEmbed } from '../src/app-sdk/index'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { sseSlots } from '../src/store/dashboardSlice'
import { hydrateSlotMessages, sseChatMessage } from '../src/store/chatSlice'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const surface = params.get('surface') || 'pane'
const theme = params.get('theme') || 'dark'
const stopPhase = (params.get('state') || 'idle') as 'idle' | 'soft_pending' | 'killing'
const fail = params.get('fail') || ''
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLOT = surface === 'embed' ? 'chat-embed-stop-copy' : 'chat-1-stop-copy'
const messages = [
  { role: 'user', content: 'Go through the incident reports and summarise each one.', cls: 'msg msg-u', ts: '2026-09-01T00:00:00Z' },
  { role: 'assistant', content: 'Reading the first report now.', cls: 'msg msg-a', ts: '2026-09-01T00:00:05Z' },
]

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
const realFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const path = new URL(url, location.origin).pathname
  if (path === `/api/chat/slots/${SLOT}/stop`) {
    if (fail === '404') return json({ error: 'not found', code: 'slot_not_found' }, 404)
    if (fail === '500') return json({ error: 'turn worker crashed while cancelling' }, 500)
    return json({ ok: true })
  }
  if (path === `/api/chat/slots/${SLOT}`) {
    return json({ title: 'radar', running: true, stop_state: stopPhase, stopping: stopPhase !== 'idle', turn: 1, turn_gen: 1, messages })
  }
  // Every other read answers empty: a list for the list endpoints the pane
  // maps over, an object for the rest.
  if (path === '/api/slash-commands' || path === '/api/skills') return json([])
  if (path.startsWith('/api/')) return json({})
  return realFetch(input, init)
}

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

async function mountPane() {
  store.dispatch(
    sseSlots([
      { key: SLOT, title: 'radar', messages: 2, running: true, stopping: stopPhase !== 'idle', stop_state: stopPhase, mode: 'member', agent: 'radar' },
    ] as never),
  )
  store.dispatch(hydrateSlotMessages({ slot: SLOT, messages, hasMore: false, total: 2, running: true } as never))
  // A tool row flips the pane's stream state to tool_running: a live turn.
  store.dispatch(
    sseChatMessage({ slot: SLOT, role: 'tool', content: '🔧 gh issue list --state open', ts: '2026-09-01T00:00:06Z', meta: { kind: 'shell' } }),
  )
  await initI18n()
  createRoot(document.getElementById('root')!).render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <div className="h-screen bg-bg text-text p-4" data-capture-page>
            <div className="h-full max-w-[760px] mx-auto" data-capture-root>
              <ChatPane slotKey={SLOT} agentLocked />
            </div>
          </div>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

function mountEmbed() {
  initI18n('en')
  createRoot(document.getElementById('root')!).render(
    <QueryClientProvider client={queryClient}>
      <AppApiProvider appName="capture" allowedApiPaths={['/api/chat']} allowedEvents={[]} subscribeFn={() => () => {}} navigateFn={() => {}} notifyFn={() => {}}>
        <div className="h-screen bg-bg text-text p-4" data-capture-page>
          <div data-capture-root style={{ height: 420, maxWidth: 760, margin: '0 auto' }}>
            <ChatEmbed slotKey={SLOT} agent="radar" />
          </div>
        </div>
      </AppApiProvider>
    </QueryClientProvider>,
  )
}

if (surface === 'embed') mountEmbed()
else void mountPane()
