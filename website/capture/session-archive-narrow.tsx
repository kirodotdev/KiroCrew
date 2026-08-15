/**
 * Isolated capture entry for the Session Archive filter row at narrow widths.
 *
 * WHY ISOLATED: the page is a dashboard route behind the app shell and a live
 * gateway, but the row under review — filter input + Reload button inside the
 * `w-1/3` `overflow-hidden` left pane — is decided entirely by that flex line.
 * This mounts the REAL SessionArchive against the real stylesheet and theme
 * tokens with `fetch` stubbed at the network seam, so the layout captured is the
 * shipped one.
 *
 * The isolated frame is the CONSERVATIVE case for this defect: in the app the
 * pane also gives up width to the sidebar and page padding, so the left pane is
 * narrower there than here and the row can only clip harder.
 *
 * Theme and language come from the query string: ?theme=dark|light&lang=en
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

// Initialise i18next exactly as main.tsx does. Importing the module only DEFINES
// initI18n — without calling it every label in the frame is blank, which would
// silently measure a row whose Reload button has no text.
import { ThemeProvider } from '../src/hooks/useTheme'
import { initI18n } from '../src/i18n'
import SessionArchive from '../src/pages/SessionArchive'
import { store } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// A populated list, so the pane renders its normal content rather than the
// empty state — the filter row is the subject either way, but an empty pane
// would not be a representative screenshot.
const archives = [
  { name: 'a1', key: 'dashboard:chat-7', stamp: '20260815T090000', size: 4096, mtime: 0 },
  { name: 'a2', key: 'slack:C123-U456', stamp: '20260814T113000', size: 20480, mtime: 0 },
  { name: 'a3', key: 'wecom:Wei', stamp: '20260813T204500', size: 1048576, mtime: 0 },
]

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/session/archive')) {
    return Promise.resolve(
      new Response(JSON.stringify({ archives }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  }
  if (url.includes('/api/')) {
    return Promise.resolve(
      new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }),
    )
  }
  return realFetch(input as RequestInfo, init)
}) as typeof globalThis.fetch

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n(params.get('lang') || 'en')

// The page is `h-full`, so it needs an ancestor with a real height to lay out
// against — the app shell supplies one in production.
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <MemoryRouter>
          <div style={{ height: '100vh', padding: 8 }}>
            <SessionArchive />
          </div>
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>
  </Provider>,
)
