/**
 * Isolated capture entry for the conductor lane over FEDERATED rows: sessions a
 * connected crew ("worker-1") lists through `GET /api/instances/{id}/chat-slots`,
 * photographed through the real `ChatSidebar` and the real `useInstanceSessions`.
 *
 * WHY ISOLATED: the defect needs a second gateway mid-dispatch behind a live tunnel.
 * What stays faithful is the wire: the harness answers the hub's chat-slots route with
 * the shape the hub sends the browser, and nothing downstream is stubbed -- the hook
 * maps the rows, the lane resolves `parent.key` within the peer's own origin, draws
 * the chevron, the count and the glyphs on its own.
 *
 * Query string: ?theme=dark|light
 *               &parent=1  -- the chat-slots reply carries `parent: {slot, key}` as the
 *                             hub now forwards it: the peer conductor is one row with its
 *                             worker count, and opens to nest them. Without it the reply
 *                             carries no `parent`, which is what `_clean_peer_slot` sent
 *                             before this change: every remote worker a top-level stray.
 *                             A query parameter rather than a window hook because the two
 *                             payloads never meet in one product session, and swapping
 *                             them on a mounted page reads as a re-parent (see the
 *                             harness).
 */
import { useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { sseConnected, sseSlots } from '../src/store/dashboardSlice'
import { ThemeProvider } from '../src/hooks/useTheme'
import { PREVIEW_INSTANCE_SESSIONS } from '../src/utils/previewFlags'
import ChatSidebar from '../src/pages/ChatSidebar'
import type { ChatSlot } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
localStorage.setItem('mc-theme', theme === 'light' ? 'light' : 'dark')
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
localStorage.setItem('mc-sidebar-lane', 'conductor')
localStorage.setItem('mc-session-stale-collapse-ms', '0')
localStorage.removeItem('mc-sidebar-conductor-expanded')
localStorage.removeItem('mc-sidebar-conductor-collapsed')
localStorage.setItem('mc-sidebar-width', '520')
// The federated rows are behind the preview flag the sidebar reads.
localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')

const MIN = 60_000
const now = Date.now()
const at = (msAgo: number) => new Date(now - msAgo).toISOString()

/** A row as the HUB's chat-slots route sends it (`_clean_peer_slot`'s allowlist). */
interface PeerRow {
  key: string
  title: string
  agent: string
  running: boolean
  pending_approval: boolean
  last_turn_ts: string
  last_ts: string
  created: string
  row_identity: string
  parent?: { slot: string; key: string }
}
const PEER = 'worker-1'
const LEAD = 'chat-2201'
const WORKERS = ['chat-2202', 'chat-2203', 'chat-2204']
const peerRow = (key: string, title: string, agent: string, msAgo: number, extra: Partial<PeerRow> = {}): PeerRow => ({
  key, title, agent, running: false, pending_approval: false,
  last_turn_ts: at(msAgo), last_ts: at(msAgo), created: at(msAgo + 30 * MIN),
  row_identity: `${PEER}:${key}`,
  ...extra,
})
const cite = { parent: { slot: LEAD, key: LEAD } }
const PEER_ROWS: PeerRow[] = [
  peerRow(LEAD, 'Fix PR readiness commit status', 'kirocrew-lead', 2 * MIN, { running: true }),
  peerRow(WORKERS[0], 'Resolving issue with PR readiness check', 'kirocrew-worker', 40_000, { running: true, ...cite }),
  peerRow(WORKERS[1], 'PR Readiness Status Fix', 'kirocrew-worker', 3 * MIN, { pending_approval: true, ...cite }),
  peerRow(WORKERS[2], 'Fix PR Readiness commit status', 'kirocrew-worker', 6 * MIN, cite),
]
const stripParent = (rows: PeerRow[]): PeerRow[] => rows.map(({ parent: _p, ...rest }) => rest)

/** The hub's own two local sessions, for scale. */
interface Row {
  key: string
  title: string
  messages: number
  running: boolean
  agent: string
  last_ts: string
  last_message: string
  parent?: { slot?: string; key?: string | null } | null
}
const LOCAL: Row[] = [
  { key: 'chat-2190', title: 'Session tree mode worker grouping bug', messages: 41, running: false, agent: 'kirocrew', last_ts: at(MIN), last_message: 'Both hops now forward the citation.' },
  { key: 'chat-2188', title: 'Fargate feature progress check', messages: 17, running: true, agent: 'kirocrew', last_ts: at(4 * MIN), last_message: 'Loop 17/40 · Update monitor.' },
]

// ── the wire: a fetch shim answering the two routes the federated path reads ──
const currentReply: PeerRow[] = params.get('parent') === '1' ? PEER_ROWS : stripParent(PEER_ROWS)
const realFetch = window.fetch.bind(window)
const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const path = url.startsWith('http') ? new URL(url).pathname : url.split('?')[0]
  if (path === '/api/instances') {
    return Promise.resolve(json({
      active: true, warm_set_cap: 4, sso: { state: 'none' },
      instances: [{
        id: PEER, name: PEER, ssh_host: 'worker-1.internal', remote_port: 5488, local_port: 6301, ttl: '8h',
        remote_bin: 'kirocrew', connection_method: 'ssh', ssm_target: '', aws_profile: '', aws_region: '', ssm_run_as: '',
        was_connected: true, status: { instance_id: PEER, state: 'connected', local_port: 6301, remote_port: 5488 },
      }],
    }))
  }
  if (path === `/api/instances/${PEER}/chat-slots`) return Promise.resolve(json(currentReply))
  return realFetch(input, init)
}) as typeof window.fetch

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Harness() {
  useEffect(() => {
    store.dispatch(sseConnected())
    store.dispatch(sseSlots(LOCAL as unknown as ChatSlot[]))
  }, [])
  return (
    <div className="flex h-screen bg-bg" data-capture-ready="">
      <ChatSidebar
        slots={LOCAL as unknown as ChatSlot[]}
        activeSlot={null}
        unreadSlots={[]}
        history={[]}
        historyHasMore={false}
        defaultAgent="kirocrew"
        installedAgents={[
          { name: 'kirocrew', source: 'builtin' },
          { name: 'kirocrew-worker', source: 'builtin' },
          { name: 'kirocrew-lead', source: 'builtin' },
        ]}
      />
    </div>
  )
}

initI18n()
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <MemoryRouter>
          <Harness />
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>
  </Provider>,
)
