/**
 * Isolated capture entry for the memory-wait copy: the queued chip, the chat's
 * run card and the Subagents panel for runs a memory wait ended before they
 * started.
 *
 * WHY ISOLATED: a memory wait needs a host short of memory and a wave held past
 * `agent.subagent_queue_max_wait_secs`, neither of which a capture run has. This
 * mounts the REAL ActivityViewer and SubagentRunCard against the real
 * stylesheet, theme tokens and i18n catalog, fed through the same seams the
 * chat page uses: the `subagent_queued` reducer for the chip, the chat slice's
 * `subagents` for the card, and the panel's own `subagents` prop.
 *
 * `?scene=chip` shows a wave waiting under macOS pressure with a 30-minute
 * bound; `?scene=ended` shows a wave where two members ran out the low-memory
 * wait and one the pressure hold, on the card and in the panel.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { sseSubagentQueued, sseSubagentSpawn, sseSubagentDone, switchSlot } from '../src/store/chatSlice'
import ActivityViewer from '../src/pages/chat/ActivityViewer'
import SubagentRunCard from '../src/pages/chat/SubagentRunCard'
import type { SubagentActivity } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') || 'ended'
const SLOT = 'capture-slot'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// Stub the network seam: the capture server has no gateway.
const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/')) {
    return Promise.resolve(new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

// The gate's two terminals, verbatim (`subagent_wait_reasons.py`).
const LOW_MEMORY = 'never started: waiting for memory (not enough memory freed up in time); retry once memory frees up'
const PRESSURE = 'never started: waiting for memory (macOS memory pressure did not ease in time); retry once it eases or a running agent finishes'

const ended: Array<[string, string]> = [['a1b2c3d4', LOW_MEMORY], ['e5f6a7b8', LOW_MEMORY], ['c9d0e1f2', PRESSURE]]

store.dispatch(switchSlot({ key: SLOT }))
if (scene === 'chip') {
  store.dispatch(sseSubagentQueued({ slot: SLOT, queued: 2, reason: 'memory_pressure', max_wait_secs: 1800 }))
} else {
  for (const [id, error] of ended) {
    store.dispatch(sseSubagentSpawn({ slot: SLOT, id, task: 'Summarize the release notes', agent: 'kirocrew' }))
    store.dispatch(sseSubagentDone({ slot: SLOT, id, elapsed: 0, error, outcome: 'failed' }))
  }
}

const panelAgents: Record<string, SubagentActivity> = scene === 'chip'
  ? {}
  : Object.fromEntries(ended.map(([id, error]) => [id, {
    id, task: 'Summarize the release notes', agent: 'kirocrew', status: 'error', error,
    streaming: '', lastTool: '', startedAt: Date.now() - 60_000, elapsed: 0,
  } as SubagentActivity]))

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <div data-capture-root style={{ width: 420, minHeight: '100vh', display: 'flex', flexDirection: 'column', gap: 12, padding: 12, background: 'var(--bg)' }}>
          {scene !== 'chip' && (
            <div data-capture-card>
              <SubagentRunCard launch={{ ids: ended.map(([id]) => id), announced: ended.length }} slot={SLOT} />
            </div>
          )}
          <div data-capture-panel style={{ border: '1px solid var(--border)', borderRadius: 8, display: 'flex', flexDirection: 'column', minHeight: 320 }}>
            <ActivityViewer
              subagents={panelAgents}
              toolLog={[]}
              open
              onToggle={() => {}}
              slot={SLOT}
              view="subagents"
            />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
