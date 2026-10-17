/**
 * Isolated capture entry for the Dashboard tab's own states.
 *
 * WHY ISOLATED: these are host chrome, not any page's pixels. Reaching them in
 * the full SPA needs an app shell, a live websocket, a seeded crewmate and a
 * gateway whose registry is in the state being photographed. Here the REAL
 * `CrewDynamicDashboard` is mounted and only its one read is stubbed, so every
 * branch the component takes is the production branch.
 *
 * Covers the state a crewmate with no adopted page is shown, which is the state
 * EVERY crewmate meets until a built-in page ships. The theme comes from the query
 * string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// Initialise i18next as main.tsx does -- `../src/i18n/all` registers every
// language catalog, so a shot renders real copy rather than a key.
import { initI18n } from '../src/i18n/all'
import { ThemeProvider } from '../src/hooks/useTheme'
import CrewDynamicDashboard from '../src/pages/members/CrewDynamicDashboard'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

// BOTH, in this order, as every other capture entry does. `ThemeProvider` seeds its
// own mode from `mc-theme` on mount, so setting only the attribute gets overwritten
// and every shot comes out in the default palette.
localStorage.setItem('mc-theme', theme)
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

//: Exactly what the route answers for a crewmate that adopted nothing: an EMPTY
//: body, not an absent key, which is what `instance._empty_instance` writes.
const EMPTY = {
  instance_version: 0,
  template: { id: '', version: 0 },
  html: '',
  manifest: {},
  state: 'empty',
  state_reason: 'no template adopted',
}

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/dashboard')) {
    return Promise.resolve(
      new Response(JSON.stringify(EMPTY), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )
  }
  // EVERY other gateway call is refused, which is what makes this entry isolated.
  // Letting them through reaches whatever the dev server proxies to, and the theme
  // provider is server-backed: it then paints that gateway's stored palette and the
  // shot is labelled with a theme it is not in.
  if (url.includes('/api/')) {
    return Promise.resolve(new Response('{}', { status: 404 }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

await initI18n()

//: No retry, so a read that answers once is the whole story this fixture needs.
const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={client}>
    <ThemeProvider>
      {/* The tab's own surface, so the shot carries the panel's background and
          text colours rather than a bare white page. */}
      <div className="h-screen bg-bg text-text">
        <CrewDynamicDashboard slug="oncall" member="oncall" displayName="On Call" />
      </div>
    </ThemeProvider>
  </QueryClientProvider>,
)
