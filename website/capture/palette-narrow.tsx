/**
 * Isolated capture entry for the Cmd+K command palette's header row at narrow
 * viewport widths.
 *
 * WHY ISOLATED: the palette is a portal-rendered modal that needs the redux
 * store, react-query and the router in scope, but nothing about its header
 * layout depends on real search results — the row's flex line is decided by the
 * search icon, the scope chip, the input, the Tab hint and the close button. So
 * the providers are left on the real code path and only `fetch` is stubbed, so
 * every provider resolves to an empty corpus instead of rendering an error.
 *
 * The palette owns `scope` and `query` as internal state, so the capture script
 * drives them the way a user does (type a sigil / a category prefix + Tab)
 * rather than the harness reaching in — that keeps the captured states ones the
 * product can actually be in.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

// Initialise i18next exactly as main.tsx does. Importing the module only DEFINES
// initI18n — without calling it every label in the frame is blank, which
// silently produces screenshots that misrepresent the real UI.
import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { ThemeProvider } from '../src/hooks/useTheme'
import CommandPalette from '../src/components/CommandPalette'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// Every provider search resolves against an empty corpus: the header row is the
// subject, and a failed request would render an error state over it.
const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.startsWith('/api/')) {
    return Promise.resolve(
      new Response('[]', { status: 200, headers: { 'Content-Type': 'application/json' } }),
    )
  }
  return realFetch(input as RequestInfo, init)
}) as typeof fetch

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n(params.get('lang') || 'en')

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <MemoryRouter>
          <CommandPalette open onClose={() => {}} />
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>
  </Provider>,
)
