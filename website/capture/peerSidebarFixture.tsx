/**
 * Shared boot for the conductor-lane captures that photograph FEDERATED rows: the
 * sidebar preferences that select the lane, the fetch shim answering the two routes
 * the federated path reads (`/api/instances` and the hub's chat-slots route), and the
 * mount of the real `ChatSidebar` over a local slot list.
 *
 * Extracted from `session-tree-peer-lineage.tsx` so `session-tree-hub-driven.tsx`
 * could share it instead of cloning it (jscpd runs at a 0% duplication threshold).
 * Each scenario still owns its fixtures and its query-string switch.
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

export const MIN = 60_000
const now = Date.now()
export const at = (msAgo: number) => new Date(now - msAgo).toISOString()

/** A row as the HUB's chat-slots route sends it (`_clean_peer_slot`'s allowlist). */
export interface PeerRow {
  key: string
  title: string
  agent: string
  running: boolean
  pending_approval: boolean
  last_turn_ts: string
  last_ts: string
  created: string
  row_identity: string
  parent?: { slot?: string; key?: string; hub_key?: string }
}

/** A local slot row, as the slots broadcast carries it. */
export interface LocalRow {
  key: string
  title: string
  messages: number
  running: boolean
  agent: string
  last_ts: string
  last_message: string
  executor?: 'remote'
  instance_id?: string
  parent?: { slot?: string; key?: string | null } | null
}

export const stripParent = (rows: PeerRow[]): PeerRow[] => rows.map(({ parent: _p, ...rest }) => rest)

/** Select the conductor lane, the theme and the preview flag the federated rows sit behind. */
export function prepareSidebarPreferences(params: URLSearchParams): void {
  const theme = params.get('theme') || 'dark'
  localStorage.setItem('mc-theme', theme === 'light' ? 'light' : 'dark')
  localStorage.setItem('mc-color-theme', 'kiro')
  document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
  localStorage.setItem('mc-sidebar-lane', 'conductor')
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
  localStorage.removeItem('mc-sidebar-conductor-expanded')
  localStorage.removeItem('mc-sidebar-conductor-collapsed')
  localStorage.setItem('mc-sidebar-width', '520')
  localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
}

/** The wire: answer the two routes the federated path reads with one connected peer. */
export function installPeerFetch(peer: string, reply: PeerRow[]): void {
  const realFetch = window.fetch.bind(window)
  const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
  window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    const path = url.startsWith('http') ? new URL(url).pathname : url.split('?')[0]
    if (path === '/api/instances') {
      return Promise.resolve(json({
        active: true, warm_set_cap: 4, sso: { state: 'none' },
        instances: [{
          id: peer, name: peer, ssh_host: `${peer}.internal`, remote_port: 5488, local_port: 6301, ttl: '8h',
          remote_bin: 'kirocrew', connection_method: 'ssh', ssm_target: '', aws_profile: '', aws_region: '', ssm_run_as: '',
          was_connected: true, status: { instance_id: peer, state: 'connected', local_port: 6301, remote_port: 5488 },
        }],
      }))
    }
    if (path === `/api/instances/${peer}/chat-slots`) return Promise.resolve(json(reply))
    return realFetch(input, init)
  }) as typeof window.fetch
}

function Harness({ local }: { local: LocalRow[] }) {
  useEffect(() => {
    store.dispatch(sseConnected())
    store.dispatch(sseSlots(local as unknown as ChatSlot[]))
  }, [local])
  return (
    <div className="flex h-screen bg-bg" data-capture-ready="">
      <ChatSidebar
        slots={local as unknown as ChatSlot[]}
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

/** Mount the real sidebar over *local*, with the peer rows arriving through the shim. */
export function mountPeerSidebar(local: LocalRow[]): void {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  initI18n()
  createRoot(document.getElementById('root')!).render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <ThemeProvider>
          <MemoryRouter>
            <Harness local={local} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
}
