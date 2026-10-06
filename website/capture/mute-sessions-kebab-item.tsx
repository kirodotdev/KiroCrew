/**
 * Isolated capture entry for the "Mute sessions this one opens" row this PR
 * adds to the session kebab menu (SessionActionsMenu).
 *
 * WHY ISOLATED: the menu only renders inside a live session surface (header or
 * sidebar row) wired to a store with slots; the full SPA shell would need a
 * gateway and a seeded websocket, and a half-stubbed shell renders its error
 * boundary instead. So this mounts the REAL `SessionActionsMenu` (unmodified)
 * inside the REAL `DropdownMenu`/`DropdownMenuContent` primitives it ships
 * behind -- the same open-menu markup `ChatPageMessageContent` uses -- against
 * the real store, theme tokens and live i18n catalog. Only the menu's data
 * comes from a seeded slot; the component and its new row are the shipped ones.
 *
 * Scene comes from the query string:
 *   ?scene=unmuted  -> the row reads "Mute sessions this one opens" (default)
 *   ?scene=muted    -> the creator carries mutes_opened; the row reads
 *                      "Unmute sessions this one opens"
 * `?theme=dark|light` selects the theme. The menu is forced open so the frame
 * documents the shipped row, not a trigger.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import SessionActionsMenu from '../src/components/SessionActionsMenu'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from '../src/components/ui/dropdown-menu'
import { ThemeProvider } from '../src/hooks/useTheme'
import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { sseSlots, setSlotMutesOpenedError } from '../src/store/dashboardSlice'
import type { ChatSlot } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') || 'unmuted'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
// ThemeProvider reads its initial theme from storage; seed it so the provider
// resolves the requested theme instead of its default.
try { localStorage.setItem('mc-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark') } catch { /* ignore */ }

// Gateway-free: answer the one read the menu makes (`/api/chat/folders`, via
// useFolderSortMode) with an empty success so its ErrorNotice row never renders
// and does not stretch the menu. The feature under test (the mute row) issues no
// fetch of its own; only the menu's unrelated folder read does.
const realFetch = window.fetch.bind(window)
window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/chat/folders')) {
    return Promise.resolve(new Response('[]', { status: 200, headers: { 'content-type': 'application/json' } }))
  }
  if (url.includes('/api/')) {
    return Promise.resolve(new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } }))
  }
  return realFetch(input as RequestInfo, init)
}) as typeof window.fetch

// Seed one conductor slot the menu acts on. In the `muted` scene it already
// carries the flag, so the connected menu derives the "Unmute…" label from the
// real store -- exactly as the row does in production. In the `error` scene a
// mute-toggle failure is seeded so the row's ErrorNotice + retry item render.
const slot = {
  key: 'capture-conductor',
  title: 'Ship the release',
  running: false,
  messages: 4,
  agent: 'kirocrew',
  mutes_opened: scene === 'muted',
} as ChatSlot
store.dispatch(sseSlots([slot]))
if (scene === 'error') {
  store.dispatch(setSlotMutesOpenedError({
    key: 'capture-conductor',
    message: 'PATCH /api/chat/slots/capture-conductor/mutes-opened failed: 503',
  }))
}

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Harness() {
  return (
    <div className="bg-bg min-h-screen p-10 flex items-start" data-capture-root>
      <DropdownMenu open>
        <DropdownMenuTrigger asChild>
          <button className="px-2 py-1 rounded-md text-text bg-card border border-border-subtle">
            Ship the release ⋯
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="min-w-[180px]">
          <SessionActionsMenu variant="dropdown" slotKey="capture-conductor" />
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  )
}

initI18n('en')
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
