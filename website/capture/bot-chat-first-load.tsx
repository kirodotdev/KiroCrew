/**
 * Isolated capture entry for a bot chat's first load: the real ChatPane, as
 * the Members page mounts it (frameless, with a crewmate identity), while the
 * slot's first page is still on its way.
 *
 * The capture script holds `GET /api/chat/slots/<slot>` open, so the pane
 * stays in its first load, then releases it to show the landed chat.
 *
 * Scenes via query string: ?theme=dark|light and ?history=1|0|unknown, the
 * value the host passes as `historyKnown` (1 = the roster row says the thread
 * holds a message, 0 = it says none, unknown = the host passes nothing).
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ChatPane from '../src/components/ChatPane'
import { ThemeProvider } from '../src/hooks/useTheme'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { sseSlots } from '../src/store/dashboardSlice'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const history = params.get('history') || '1'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLOT = 'member-radar-first-load'

store.dispatch(
  sseSlots([
    { key: SLOT, title: 'radar', messages: history === '1' ? 4 : 0, running: false, mode: 'member', agent: 'radar' },
  ] as never),
)

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

async function main() {
  await initI18n()
  createRoot(document.getElementById('root')!).render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <ThemeProvider>
        <MemoryRouter>
          <div className="h-screen bg-bg text-text" data-capture-root>
            <ChatPane
              slotKey={SLOT}
              frameless
              followContentWidth
              agentLocked
              crewmate={{ name: 'radar' }}
              historyKnown={history === 'unknown' ? undefined : history === '1'}
            />
          </div>
        </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
}

main()
