/**
 * Evidence for the "N tabs were not restored" notice on the chat pane.
 *
 * THE CHANGE: a tab the gateway's startup restore listed and could not rebuild
 * used to leave no trace a person could see. The session was intact, nothing was
 * closed and nothing was deleted, so the sidebar simply had fewer rows than
 * before the restart -- the only way to notice was to remember what used to be
 * there. This notice is that trace, and its link is the remedy.
 *
 * The scene mounts the REAL `UnrestoredTabsNotice` from `src/` against the real
 * stylesheet, theme tokens and live i18n catalog, so the band in the frame is
 * the component's own render and its sentence comes from the catalog through
 * `i18nT` -- a frame also proves the new plural keys resolve. Nothing here
 * re-implements the notice, its classes or its strings.
 *
 * `fetch` is stubbed rather than the api module so the component's real arrival
 * read runs and answers exactly what the new endpoint answers.
 *
 *   ?theme=dark|light&count=<n>
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { UnrestoredTabsNotice } from '../src/pages/chat/page/ChatPaneNotices'
import { store } from '../src/store'
import { initI18n } from '../src/i18n'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const count = Number(params.get('count') || '16')
/** `?fail=1` photographs the read-failure branch instead of the count. */
const fail = params.get('fail') === '1'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// A dismissal is remembered per count, so a stale one from an earlier frame in
// the same browser would silence the next shot.
try { sessionStorage.clear() } catch { /* ignore */ }

const json = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/chat/slots/unrestored')) {
    if (fail) {
      // What the retry ladder hands the hook once it gives up: the state where the
      // notice must say the CHECK failed rather than claim a count it never got.
      return Promise.resolve(new Response('upstream unavailable', { status: 503 }))
    }
    // Verbatim the shipped response shape, including `reported` -- the field that
    // separates "no tabs were dropped" from "the restore has not answered yet".
    return Promise.resolve(json({ reported: true, count }))
  }
  if (url.includes('/api/')) return Promise.resolve(json({}))
  return realFetch(input, init)
}) as typeof globalThis.fetch

initI18n('en')

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  // The real store and query client, because the notice reads the WebSocket's
  // connected flag and fetches through the shared query lifecycle.
  <QueryClientProvider client={queryClient}>
    <Provider store={store}>
      {/* The width the chat pane gives its notices, and the pane's own `mx-4`
          gutter is the component's, so the frame shows the band at its inset. */}
      <div data-capture-root className="bg-bg text-text w-[900px] py-4">
        <UnrestoredTabsNotice />
      </div>
    </Provider>
  </QueryClientProvider>,
)
