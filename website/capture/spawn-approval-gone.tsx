/**
 * Isolated capture entry for a spawn approval that is already gone, seen from
 * BOTH surfaces that resolve it: the composer's spawn banner and the activity
 * panel's sub-agent card (#11180 follow-up).
 *
 * WHY ISOLATED: a gone approval needs a pending spawn whose future the gateway
 * no longer holds (decided or expired across a reconnect), which a live stack
 * cannot stage on demand. This mounts the REAL ChatInput and the REAL
 * ActivityViewer on one store seeded with a pending spawn, against the real
 * stylesheet. The panel reads its sub-agents from that store, as ChatPage
 * does. Every approval POST answers the gateway's own refusal for a missing
 * future (`api_approval_resolve`: 404 `not found or expired`).
 *
 * Theme comes from the query string: ?theme=dark
 *
 * ?scenario= selects the staged state (default: one agent, every POST gone):
 *   retry  one agent; every POST fails with a non-terminal 500
 *   multi  three agents; only spawn:a2's POST is gone
 *   liveness-fail  one agent; every POST gone, and every read of the spawn
 *          inventory (`GET /api/spawn`) fails with a 500, so the card's
 *          liveness check stays open and reports its own ErrorNotice
 */
import { createRoot } from 'react-dom/client'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import ChatInput from '../src/components/ChatInput'
import ActivityViewer from '../src/pages/chat/ActivityViewer'
import SubagentProgressBar from '../src/pages/chat/SubagentProgressBar'
import SubagentRunCard from '../src/pages/chat/SubagentRunCard'
import chatReducer, {
  selectComposerBusy,
  selectSidebarApprovalCounts,
  selectSidebarSubagentCounts,
  selectSlotSubagentsActive,
} from '../src/store/chatSlice'
import dashboardReducer from '../src/store/dashboardSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import instancesReducer from '../src/store/instancesSlice'
import { useAppSelector, type RootState } from '../src/store'
import type { SubagentActivity } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scenario = params.get('scenario') || 'gone'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/approvals/') && scenario === 'retry') {
    return Promise.resolve(new Response('{"error": "gateway busy"}', {
      status: 500,
      headers: { 'Content-Type': 'application/json' },
    }))
  }
  if (url.includes('/api/approvals/') && (scenario !== 'multi' || url.includes('spawn%3Aa2') || url.includes('spawn:a2'))) {
    return Promise.resolve(new Response('{"error": "not found or expired"}', {
      status: 404,
      headers: { 'Content-Type': 'application/json' },
    }))
  }
  if (url.endsWith('/api/spawn') && scenario === 'liveness-fail') {
    return Promise.resolve(new Response('{"error": "inventory unavailable"}', {
      status: 500,
      headers: { 'Content-Type': 'application/json' },
    }))
  }
  if (url.endsWith('/api/spawn')) {
    // Keep the terminal-refusal / authoritative-list race visible long enough
    // to capture its conservative unresolved state, then prove retirement.
    return new Promise(resolve => setTimeout(() => resolve(new Response('{"agents": []}', {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })), 4000))
  }
  if (url.includes('/api/')) {
    // The composer's slash-command menu reads a list; everything else an object.
    const body = url.includes('/api/slash-commands') ? '[]' : '{}'
    return Promise.resolve(new Response(body, { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  return realFetch(input, init)
}) as typeof fetch

const SLOT = 'slot-1'
const TASKS = [
  'Audit the release notes for the insider build',
  'Check the changelog links',
  'Draft the upgrade FAQ',
]
const pendingSpawn = (n: number): SubagentActivity => ({
  id: `a${n}`,
  task: TASKS[n - 1],
  agent: 'kirocrew',
  status: 'pending',
  streaming: '',
  lastTool: '',
  startedAt: Date.now() - 42_000,
  elapsed: 42,
  approval_id: `spawn:a${n}`,
})
const IDS = scenario === 'multi' ? ['a1', 'a2', 'a3'] : ['a1']
const pendingSubs = Object.fromEntries(IDS.map((id, i) => [id, pendingSpawn(i + 1)]))

const chatInit = chatReducer(undefined, { type: '@@capture/init' })
const dashboardInit = dashboardReducer(undefined, { type: '@@capture/init' })
const store = configureStore({
  reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer, instances: instancesReducer },
  preloadedState: {
    chat: { ...chatInit, activeSlot: SLOT, messages: [{ role: 'user', content: 'Audit the release notes' }], subagents: pendingSubs } as RootState['chat'],
    dashboard: { ...dashboardInit, connected: true } as RootState['dashboard'],
  },
})

await initI18n()

const qc = new QueryClient({
  defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false } },
})

/** The panel is fed from the store, exactly as ChatPage feeds it. */
function Panel() {
  const subagents = useAppSelector(s => s.chat.subagents)
  return <ActivityViewer subagents={subagents} toolLog={[]} open onToggle={() => {}} slot={SLOT} view="subagents" />
}

/** A compact readout of the real shared selectors consumed by sidebar counts,
 * composer busy state, tip suppression, and SessionActionsMenu's Reload gate. */
function ContractReadout() {
  const running = useAppSelector(s => selectSidebarSubagentCounts(s)[SLOT] ?? 0)
  const approvals = useAppSelector(s => selectSidebarApprovalCounts(s)[SLOT] ?? 0)
  const busy = useAppSelector(s => selectComposerBusy(s, SLOT))
  const reloadBlocked = useAppSelector(s => selectSlotSubagentsActive(s, SLOT))
  return (
    <div data-capture-contract className="m-4 mb-2 rounded-md border border-border bg-card px-3 py-2 text-[12px] text-muted flex flex-wrap gap-x-4 gap-y-1">
      <strong className="text-text">Shared state</strong>
      <span data-testid="capture-running-count">Running count: {running}</span>
      <span data-testid="capture-approval-count">Approval count: {approvals}</span>
      <span data-testid="capture-composer-state">Composer: {busy ? 'Busy' : 'Idle'}</span>
      <span data-testid="capture-reload-state">Reload: {reloadBlocked ? 'Blocked' : 'Available'}</span>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <div data-capture-root style={{ display: 'flex', height: '100vh', background: 'var(--bg)' }}>
          <div data-capture-composer style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
            <ContractReadout />
            <div style={{ padding: '8px 16px', maxWidth: 760 }}>
              <SubagentRunCard launch={{ ids: IDS, announced: IDS.length }} slot={SLOT} />
            </div>
            <div style={{ flex: 1 }} />
            <SubagentProgressBar slot={SLOT} />
            <div style={{ padding: 16, paddingTop: 8 }}>
              <ChatInput value="" onChange={() => {}} onSend={() => {}} />
            </div>
          </div>
          <div data-capture-panel style={{ width: 400, display: 'flex', flexDirection: 'column', borderLeft: '1px solid var(--border)' }}>
            <Panel />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
