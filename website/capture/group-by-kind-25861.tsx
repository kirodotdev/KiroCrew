/**
 * Isolated capture entry for #25861's flat-lane "Group by kind" option.
 *
 * Mounts the REAL `ChatSidebar` with folders present and the flat lane chosen
 * (the surface the option applies to), seeded with one session of each kind:
 * a Crew Mode root, a normal chat, and a crewmate chat, plus a pinned one.
 * Nothing in the sidebar is mocked; only the `/api/**` calls are answered inert.
 *
 * Query string:
 *   ?theme=dark|light   theme (default dark)
 *   &group=1            pre-arm "Group by kind" on, so the grouped list renders
 *   &menu=1             open the Sort and filter menu (shows the toggle row)
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { sseSlots } from '../src/store/dashboardSlice'
import { ThemeProvider } from '../src/hooks/useTheme'
import ChatSidebar from '../src/pages/ChatSidebar'
import type { ChatSlot, ChatFolder } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
document.documentElement.setAttribute('data-mode', theme)

// The lane the capture shows: tree (the real default) unless ?lane=flat. The
// stale collapse is off so every seeded row stays visible, and the option is
// pre-armed when asked -- the same localStorage keys the real menu writes.
if (params.get('lane') === 'flat') localStorage.setItem('mc-sidebar-flat-view', '1')
localStorage.setItem('mc-session-stale-collapse-ms', '0')
if (params.get('group') === '1') localStorage.setItem('mc-sidebar-group-by-kind', '1')

const FOLDERS: ChatFolder[] = [
  { id: 'f1', name: 'Alpha', order: 0 },
  { id: 'f2', name: 'Beta', order: 1 },
]

// Every /api/** call answered inert so the sidebar's lazy queries resolve.
const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/')) {
    const body = /folders/.test(url) ? JSON.stringify(FOLDERS)
      : /commands|skills|agents|models|sessions|files|artifacts|tags|columns|instances|crews/.test(url) ? '[]' : '{}'
    return Promise.resolve(new Response(body, { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
qc.setQueryData(['chat-folders'], FOLDERS)
qc.setQueryData(['chat-tags'], [])

// UNFILED sessions (no folder_id): one of each kind plus a pinned one. These are
// what the tree lane's top-level list groups. One FOLDER (Alpha) carries its own
// sessions, shown to stay in the folder, as they are -- the option never touches
// a folder's own list. In the flat lane every row is exploded into one list.
const SLOTS: ChatSlot[] = [
  { key: 'chat-pin', title: 'Release checklist', messages: 12, running: false, modified: 5000, pinned: true },
  { key: 'chat-crew', title: 'Ship the sidebar fix', messages: 8, running: true, modified: 4000, crew_mode_root: true },
  { key: 'chat-normal-1', title: 'Draft the release notes', messages: 5, running: false, modified: 3500 },
  { key: 'chat-normal-2', title: 'Triage the inbox', messages: 3, running: false, modified: 3000 },
  { key: 'chat-mate-1', title: 'Astra: research spike', messages: 6, running: false, modified: 2500, agent_kind: 'member' },
  { key: 'chat-mate-2', title: 'Nova: write the changelog', messages: 4, running: false, modified: 2000, agent_kind: 'member' },
  { key: 'chat-infolder', title: 'Alpha planning', messages: 2, running: false, folder_id: 'f1', modified: 4500 },
] as unknown as ChatSlot[]
store.dispatch(sseSlots(SLOTS))

function Scene() {
  return (
    <div className="bg-bg text-text" style={{ height: '100vh', width: '360px' }} data-capture-root>
      <ChatSidebar
        slots={SLOTS}
        activeSlot={null}
        unreadSlots={[]}
        history={[]}
        historyHasMore={false}
        defaultAgent=""
        installedAgents={[]}
      />
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <MemoryRouter>
          <Scene />
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>
  </Provider>,
)
