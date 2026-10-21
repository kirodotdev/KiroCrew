/**
 * Isolated capture entry for the two discoverability actions #11823 adds to the
 * shared session row menu: "Reference in chat" and "Copy session ID".
 *
 * Mounts the REAL `SessionActionsMenu` inside an open dropdown, seeded with one
 * session in the store, exactly as a sidebar row renders it: `onReferenceInChat`
 * is passed (the sidebar-row case — the header omits it as a self-reference), and
 * `copyId` copies the bare key through the real hook. Nothing inside the menu is
 * mocked; a stub `clipboard`/`onReferenceInChat` only records the calls.
 *
 * Query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { sseSlots } from '../src/store/dashboardSlice'
import { ThemeProvider } from '../src/hooks/useTheme'
import SessionActionsMenu from '../src/components/SessionActionsMenu'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from '../src/components/ui/dropdown-menu'
import type { ChatSlot } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
document.documentElement.setAttribute('data-mode', theme)

// Every /api/** call answered inert so the menu's lazy queries (folders) resolve.
const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/')) {
    const body = /folders|commands|skills|agents|models|sessions|files|artifacts/.test(url) ? '[]' : '{}'
    return Promise.resolve(new Response(body, { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

// One ordinary session the menu acts on. `messages` feeds the staged-ref chip's
// secondary line when referenced.
store.dispatch(sseSlots([{ key: 'chat-42-1700000000', title: 'Composer PR investigation', messages: 37, running: false } as ChatSlot]))

declare global { interface Window { __refCalls: number } }
window.__refCalls = 0

function Scene() {
  return (
    <div className="bg-bg text-text" style={{ height: '100vh', padding: '24px' }} data-capture-root>
      <DropdownMenu defaultOpen>
        <DropdownMenuTrigger asChild>
          <button type="button">session actions</button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="min-w-[200px]">
          <SessionActionsMenu
            variant="dropdown"
            slotKey="chat-42-1700000000"
            sidebarOnScreen
            onReferenceInChat={() => { window.__refCalls += 1 }}
          />
        </DropdownMenuContent>
      </DropdownMenu>
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
