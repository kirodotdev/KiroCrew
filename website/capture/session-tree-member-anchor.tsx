/**
 * Isolated capture entry for the conductor lane's CREATOR ANCHOR: a crew whose
 * conductor is a crew MEMBER (its session is the member's own DM thread), photographed
 * through the real `ChatSidebar`.
 *
 * WHY ISOLATED: the defect needs a member-driven crew mid-dispatch, and a member's
 * session is not something a harness can open on a live gateway on cue. What stays
 * faithful is the exact split the product has: `ChatPage` filters `dashboard.slots` by
 * surface before it reaches the sidebar, so the member's row (`surface: 'member'`) is
 * in the STORE and absent from the PROP, while every worker row cites it with a live
 * `parent.key`. The harness reproduces that split and nothing else -- the sidebar
 * decides the nesting, the glyphs and the anchor's dimming on its own.
 *
 * Query string: ?theme=dark|light
 * Window hooks: __withoutMember() -- the store holds only the listed rows (what the lane
 *                                     had before the fix: every worker a top-level orphan)
 *               __withMember()    -- the store also holds the member's row (the fix
 *                                     borrows it as a dimmed anchor and nests the workers)
 */
import { useCallback, useEffect } from 'react'
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

const MIN = 60_000
const now = Date.now()
const at = (msAgo: number) => new Date(now - msAgo).toISOString()

interface Row {
  key: string
  title: string
  messages: number
  running: boolean
  agent: string
  mode?: string
  surface?: string
  last_ts: string
  last_message: string
  needs_input?: boolean
  parent?: { slot?: string; key?: string | null } | null
  source_links?: NonNullable<ChatSlot['source_links']>
}
type RowSeed = Partial<Row> & Pick<Row, 'key' | 'title' | 'last_ts' | 'last_message'>
const row = (over: RowSeed): Row => ({ messages: 12, running: false, agent: 'kirocrew', ...over })

/** The member's own thread: what `ChatPage` never hands the sidebar. */
const MEMBER = 'member-kirocrew-pipeline-conductor'
const MEMBER_ROW: Row = row({
  key: MEMBER,
  title: 'Pipeline Work Focus On Issues',
  agent: 'kirocrew-pipeline-conductor',
  mode: 'member',
  surface: 'member',
  running: true,
  messages: 571,
  last_ts: at(2 * MIN),
  last_message: 'Wave 4 dispatched: ten conflict PRs, drive each to green.',
})

const link = (n: number, ci: 'passed' | 'running' | 'failed') => ([
  { provider: 'github', number: n, url: `https://example.invalid/pull/${n}`, label: `#${n}`, state: 'open', ci },
] as NonNullable<ChatSlot['source_links']>)

/** What the chat page lists: the member's workers, plus one unrelated chat. */
const LISTED: Row[] = [
  row({
    key: 'chat-2124', title: 'issue-fix: #13957 — conflict, drive to green', agent: 'kirocrew-worker',
    running: true, last_ts: at(40_000), last_message: 'Rebased on main; board is mid-flight.',
    parent: { slot: MEMBER, key: MEMBER }, source_links: link(13957, 'running'),
  }),
  row({
    key: 'chat-2125', title: 'issue-fix: #13715 — conflict, drive to green', agent: 'kirocrew-worker',
    last_ts: at(3 * MIN), last_message: 'Diagnosis block. Nothing changed. No push.',
    parent: { slot: MEMBER, key: MEMBER }, source_links: link(13715, 'passed'),
  }),
  row({
    key: 'chat-2130', title: 'issue-fix: #13273 — conflict, drive to green', agent: 'kirocrew-worker',
    needs_input: true, last_ts: at(7 * MIN), last_message: 'RULING: mask the apps tree from the cron fixture?',
    parent: { slot: MEMBER, key: MEMBER }, source_links: link(13273, 'failed'),
  }),
  row({
    key: 'chat-2134', title: 'Crew Dashboard Template Design',
    last_ts: at(12 * MIN), last_message: 'Loop 0/8 · W1 mockup dispatched.',
  }),
  row({
    key: 'chat-2135', title: 'W1 editorial crew-panel template — mockup', agent: 'kirocrew-worker',
    last_ts: at(9 * MIN), last_message: 'Three directions drafted.',
    parent: { slot: 'chat-2134', key: 'chat-2134' },
  }),
]

function Harness() {
  const withoutMember = useCallback(() => store.dispatch(sseSlots(LISTED as unknown as ChatSlot[])), [])
  const withMember = useCallback(() => store.dispatch(sseSlots([MEMBER_ROW, ...LISTED] as unknown as ChatSlot[])), [])
  useEffect(() => {
    store.dispatch(sseConnected())
    withoutMember()
    Object.assign(window as unknown as Record<string, unknown>, {
      __withoutMember: withoutMember,
      __withMember: withMember,
    })
  }, [withoutMember, withMember])
  return (
    <div className="flex h-screen bg-bg" data-capture-ready="">
      <ChatSidebar
        slots={LISTED as unknown as ChatSlot[]}
        activeSlot={null}
        unreadSlots={[]}
        history={[]}
        historyHasMore={false}
        defaultAgent="kirocrew"
        installedAgents={[
          { name: 'kirocrew', source: 'builtin' },
          { name: 'kirocrew-worker', source: 'builtin' },
          { name: 'kirocrew-pipeline-conductor', source: 'builtin' },
        ]}
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
