/**
 * Shared boot for a capture page that photographs the LOCAL chat sidebar: the
 * sidebar preferences a conductor-lane frame needs, the row seed shape, and the
 * mount.
 *
 * The peer-lineage harnesses have `peerSidebarFixture` for the federated listing;
 * this is its local counterpart, for a page whose rows arrive on the slots payload
 * rather than through the hub. Extracted so a second local page does not clone the
 * first one's boot -- jscpd runs at a 0% duplication threshold.
 *
 * What it does NOT own is the fixture data or the frames: each page states its own
 * rows and its own query-string switches, because those are the thing under review.
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
import ChatSidebar from '../src/pages/ChatSidebar'
import type { ChatSlot } from '../src/types'
import '../src/index.css'

export const MIN = 60_000
const now = Date.now()
/** An ISO timestamp *msAgo* milliseconds before this page loaded. */
export const at = (msAgo: number) => new Date(now - msAgo).toISOString()

/** The shape a slots-payload row takes in these fixtures. */
export interface LocalSidebarRow {
  key: string
  title: string
  messages: number
  running: boolean
  agent: string
  last_ts: string
  last_message: string
  mode?: string
  surface?: string
  needs_input?: boolean
  created_by?: string
  lineage_minted?: boolean
  parent?: { slot?: string; key?: string | null; ancestor?: boolean } | null
  source_links?: NonNullable<ChatSlot['source_links']>
}

export type LocalSidebarSeed = Partial<LocalSidebarRow> &
  Pick<LocalSidebarRow, 'key' | 'title' | 'last_ts' | 'last_message'>

/** One row, with the defaults a quiet dashboard session carries. */
export const localRow = (over: LocalSidebarSeed): LocalSidebarRow => ({
  messages: 12,
  running: false,
  agent: 'kirocrew',
  ...over,
})

/**
 * Put the browser in the state a conductor-lane frame is photographed in: the lane
 * selected, stale collapsing off so no row is hidden by age, both fold memories
 * cleared so the default is what is seen, and a width that fits a nested subtree.
 */
export function prepareLocalSidebar(params: URLSearchParams): void {
  const theme = params.get('theme') || 'dark'
  localStorage.setItem('mc-theme', theme === 'light' ? 'light' : 'dark')
  localStorage.setItem('mc-color-theme', 'kiro')
  document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
  localStorage.setItem('mc-sidebar-lane', 'conductor')
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
  localStorage.removeItem('mc-sidebar-conductor-expanded')
  localStorage.removeItem('mc-sidebar-conductor-collapsed')
  localStorage.setItem('mc-sidebar-width', '520')
}

/**
 * Mount the REAL `ChatSidebar` over *rows*, with the store told the stream is up so
 * the lane renders rather than waiting. `data-capture-ready` is what the Playwright
 * harness waits for.
 */
export function mountLocalSidebar(rows: LocalSidebarRow[], agents: string[]): void {
  const slots = rows as unknown as ChatSlot[]
  function Harness() {
    useEffect(() => {
      store.dispatch(sseConnected())
      store.dispatch(sseSlots(slots))
    }, [])
    return (
      <div className="flex h-screen bg-bg" data-capture-ready="">
        <ChatSidebar
          slots={slots}
          activeSlot={null}
          unreadSlots={[]}
          history={[]}
          historyHasMore={false}
          defaultAgent="kirocrew"
          installedAgents={agents.map((name) => ({ name, source: 'builtin' as const }))}
        />
      </div>
    )
  }
  initI18n()
  createRoot(document.getElementById('root')!).render(
    <Provider store={store}>
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <ThemeProvider>
          <MemoryRouter>
            <Harness />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
}
