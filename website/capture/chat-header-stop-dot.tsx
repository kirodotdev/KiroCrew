/**
 * Isolated capture entry for the chat header status dot while a stop is under
 * way (#20397).
 *
 * Renders the REAL header surface, NOT frameless, so its header row and status
 * dot render, against the real stylesheet, theme tokens and i18n catalog. Two
 * surfaces, selected by ?surface:
 *   - pane  (default): the shared ChatPane header (split-view / Crew Member DM),
 *     seeded in the store for a WARM slot exactly as the WS reducers would.
 *   - embed: the app-sdk ChatEmbed header, driven by a stubbed slot-detail poll.
 * In the `stopping` scene the stop signal is set (the slots frame's `stopping`
 * for the pane; `stop_state: soft_pending` + `stopping` for the embed). The dot
 * reads green while working and danger while a stop is under way.
 *
 * Query: ?surface=pane|embed  ?theme=dark|light  ?state=running|stopping|killing
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
const state = params.get('state') || 'running'
const stopUW = state === 'stopping' || state === 'killing'
const stopPhase = state === 'killing' ? 'killing' : state === 'stopping' ? 'soft_pending' : 'idle'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

async function mountPane() {
  const SLOT = 'chat-1-stop-dot'
  store.dispatch(
    sseSlots([
      { key: SLOT, title: 'radar', messages: 2, running: true, stopping: stopUW, stop_state: stopPhase, mode: 'member', agent: 'radar' },
    ] as never),
  )
  store.dispatch(
    hydrateSlotMessages({
      slot: SLOT,
      messages: [
        { role: 'user', content: 'Scan tonight’s new issues and open PRs.', ts: '2026-09-01T00:00:00Z' },
        { role: 'assistant', content: 'On it — walking the issue queue and the linked PRs now.', ts: '2026-09-01T00:00:05Z' },
      ],
      hasMore: false,
      total: 2,
      running: true,
    } as never),
  )
  // A tool row also flips the pane's stream state to tool_running — the working
  // state the dot colours.
  store.dispatch(
    sseChatMessage({ slot: SLOT, role: 'tool', content: '🔧 gh issue list --state open', ts: '2026-09-01T00:00:06Z', meta: { kind: 'shell' } }),
  )
  await initI18n()
  createRoot(document.getElementById('root')!).render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <div className="h-screen bg-bg text-text p-4" data-capture-page>
            {/* NOT frameless: the header row and its dot render. */}
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
  const SLOT = 'chat-embed-stop-dot'
  const transcript = {
    title: 'radar',
    running: true,
    stop_state: stopPhase,
    stopping: stopUW,
    turn: 1,
    turn_gen: 1,
    messages: [
      { role: 'user', content: 'Scan tonight’s new issues and open PRs.', cls: 'msg msg-u', ts: '2026-09-01T00:00:00Z' },
      { role: 'assistant', content: 'On it — walking the issue queue and the linked PRs now.', cls: 'msg msg-a', ts: '2026-09-01T00:00:05Z' },
    ],
  }
  const realFetch = window.fetch.bind(window)
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    const path = new URL(url, location.origin)
    if (path.pathname === `/api/chat/slots/${SLOT}`) {
      return new Response(JSON.stringify(transcript), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    if (path.pathname.startsWith('/api/')) {
      return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    return realFetch(input, init)
  }
  initI18n('en')
  createRoot(document.getElementById('root')!).render(
    <QueryClientProvider client={queryClient}>
      <AppApiProvider appName="capture" allowedApiPaths={['/api/chat']} allowedEvents={[]} subscribeFn={() => () => {}} navigateFn={() => {}} notifyFn={() => {}}>
        <div className="h-screen bg-bg text-text p-4" data-capture-page>
          {/* NOT frameless: the embed draws its own header row with the dot. */}
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
