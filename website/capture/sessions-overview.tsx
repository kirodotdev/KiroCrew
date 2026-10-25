/**
 * Isolated capture entry for the SidePanel Sessions overview tab.
 *
 * Seeded scenes for the UX evidence the tab owes its reviewers:
 *   - ?scene=populated — a member-driven slot stuck on approval (Blocked +
 *     Driver), a quiet slot (Active), a done run (Complete) and a running
 *     run (Active with Stop run);
 *   - ?scene=empty — no live slots, so the unfiltered empty state renders;
 *   - ?scene=menu — the open `+` menu with the Sessions entry;
 *   - ?scene=error-roster — the roster read fails, so the roster ErrorNotice
 *     renders instead of silently falling back to the slug;
 *   - ?scene=error-close — `spawnDelete` refuses, so stopping the running
 *     run surfaces the close-failed ErrorNotice.
 *
 * Query string: ?scene=populated|empty|menu|error-roster|error-close
 * &theme=dark|light. The confirm dialogs are driven from the page: the shoot
 * script clicks Close / Stop run on the populated scene.
 */
import { useMemo } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { LayoutDashboard, Bot, Workflow } from 'lucide-react'
import { initI18n } from '../src/i18n'
import { i18nT } from '../src/i18n/t'
import { api } from '../src/api/client'
import dashboardReducer from '../src/store/dashboardSlice'
import chatReducer from '../src/store/chatSlice'
import SessionsBody from '../src/pages/chat/SessionsBody'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '../src/components/ui/dropdown-menu'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'populated'
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute(
  'data-theme',
  theme === 'light' ? 'kiro-light' : 'kiro-dark',
)

const ROSTER = [{ name: 'Nova', slug: 'nova', slot_key: 'member-nova' }]

const SLOTS = [
  {
    key: 'chat-alpha',
    title: 'Alpha checkout',
    messages: 5,
    running: false,
    pending_approval: true,
    created_by: 'member-nova',
    model: 'opus',
    served_model: '',
    effective_agent: 'crew:dev',
    agent: 'crew',
  },
  {
    key: 'chat-beta',
    title: 'Beta scratch',
    messages: 2,
    running: false,
    model: 'haiku',
    served_model: '',
    effective_agent: 'default',
    agent: 'default',
  },
  {
    key: 'chat-gamma',
    title: 'Gamma deploy',
    messages: 9,
    running: false,
    model: 'sonnet',
    served_model: '',
    effective_agent: 'crew:ops',
    agent: 'crew',
  },
]

const LIVE_RUNS = {
  'run-live': {
    id: 'run-live',
    task: 'Indexing docs',
    agent: 'sage',
    model: 'haiku',
    status: 'running',
  },
}

const BACKGROUND = {
  'chat-gamma': {
    subagents: {
      'run-done': {
        id: 'run-done',
        task: 'Nightly audit',
        agent: 'sage',
        model: 'sonnet',
        status: 'done',
      },
    },
  },
}

if (scene === 'error-roster') {
  api.members = async () => {
    throw new Error('roster unavailable')
  }
} else {
  api.members = async () => ({ members: ROSTER }) as never
}

if (scene === 'error-close') {
  api.spawnDelete = async () => {
    throw { code: 'refused' }
  }
}

function SessionsScene({ empty }: { empty: boolean }) {
  const store = useMemo(
    () =>
      configureStore({
        reducer: { dashboard: dashboardReducer, chat: chatReducer },
        preloadedState: {
          dashboard: { slots: empty ? [] : SLOTS, unreadSlots: [] } as never,
          chat: {
            activeSlot: empty ? null : 'chat-beta',
            subagents: empty ? {} : LIVE_RUNS,
            slotActivity: empty ? {} : BACKGROUND,
          } as never,
        },
      }),
    [empty],
  )
  const client = useMemo(
    () =>
      new QueryClient({
        defaultOptions: { queries: { retry: false, staleTime: Infinity } },
      }),
    [],
  )
  return (
    <QueryClientProvider client={client}>
      <Provider store={store}>
        <MemoryRouter>
          <div
            className="bg-bg text-text mx-auto flex min-h-screen flex-col"
            style={{ maxWidth: 420 }}
          >
            <div className="border-b border-border px-3 py-2 text-[13px] text-muted">
              SidePanel · Sessions
            </div>
            <SessionsBody active />
          </div>
        </MemoryRouter>
      </Provider>
    </QueryClientProvider>
  )
}

/** The session-output `+`-menu group, spelled locally: `SidePanelAddMenu`
 *  owns the real group, and its boundary test forbids importing it here.
 *  Literal label keys keep `check-i18n-keys` able to resolve them. */
const MENU_ITEMS = [
  { kind: 'subagents', icon: <Bot size={15} />, labelKey: 'pages.chat.sidePanel.menu_subagents' },
  { kind: 'sessions', icon: <LayoutDashboard size={15} />, labelKey: 'pages.chat.sidePanel.menu_sessions' },
  { kind: 'workflows', icon: <Workflow size={15} />, labelKey: 'pages.chat.sidePanel.menu_workflows' },
]

function MenuScene() {
  return (
    <div className="bg-bg text-text flex min-h-screen flex-col items-center pt-16">
      <DropdownMenu open modal={false}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className="rounded-md border border-border bg-transparent px-3 py-1 text-text"
          >
            {i18nT('pages.chat.sidePanel.open_side_panel_tab')}
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="center" className="min-w-[260px]">
          {MENU_ITEMS.map(item => (
            <DropdownMenuItem key={item.kind} onSelect={() => {}} className="gap-2.5 py-2">
              <span className="text-muted shrink-0">{item.icon}</span>
              <span className="flex-1">{i18nT(item.labelKey)}</span>
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  )
}

function Scene() {
  if (scene === 'menu') return <MenuScene />
  if (scene === 'empty') return <SessionsScene empty />
  return <SessionsScene empty={false} />
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Scene />)
