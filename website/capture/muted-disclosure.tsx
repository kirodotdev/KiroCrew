/**
 * Isolated capture entry for the notification feed's muted-channel disclosure.
 *
 * WHY ISOLATED: the disclosure renders only when a notification arrived on a
 * MUTED channel, which is a property of gateway-side channel settings no browser
 * session can arrange on demand. So the two notifications are dispatched
 * directly and everything else is real: the REAL NotificationFeed, the REAL
 * catalog lookup, the REAL stylesheet and theme tokens.
 *
 * The button's own state is left to the shot script, which clicks it and asserts
 * `aria-pressed` before firing -- a frame must not be able to photograph the
 * collapsed state while claiming to show the expanded one.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

// Initialise i18next exactly as main.tsx does: importing the module only DEFINES
// initI18n, and without calling it every label in the frame renders blank.
import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { addNotification } from '../src/store/notificationsSlice'
import NotificationFeed from '../src/components/notifications/NotificationFeed'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
document.documentElement.setAttribute('data-theme', params.get('theme') || 'dark')

// A capture page has no gateway behind it; an unanswered /api/notifications
// would leave the feed in its loading state.
const realFetch = window.fetch
window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(typeof input === 'string' ? input : (input as Request).url ?? input)
  if (url.includes('/api/')) {
    return new Response(JSON.stringify({ notifications: [] }), {
      status: 200, headers: { 'content-type': 'application/json' },
    })
  }
  return realFetch(input, init)
}) as typeof window.fetch

// One ordinary row so the feed is not empty, plus one row on a muted channel --
// the only thing that makes the disclosure render at all.
store.dispatch(addNotification({
  kind: 'cron', ts: '2026-09-01T09:00:00Z', acked: false,
  title: 'Nightly digest ready', body: 'Cron job digest finished in 41s.',
} as never))
store.dispatch(addNotification({
  kind: 'webhook', ts: '2026-09-01T08:12:00Z', acked: false, priority: 'passive',
  silenced: true, title: 'Deploy webhook fired', body: 'staging deploy accepted.',
} as never))

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/notifications']}>
        <div
          style={{ background: 'var(--bg)', color: 'var(--text)', padding: 20, width: 420 }}
          data-capture-root
        >
          <NotificationFeed selectedTs={null} onSelect={() => {}} />
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
