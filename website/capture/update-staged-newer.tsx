/**
 * Isolated capture entry for the two update surfaces when the version staged
 * beside the running gateway and the version the feed offers differ.
 *
 * WHY ISOLATED: the state needs a managed-venv gateway whose stable link
 * already resolves to a tree the feed's cached offer does not name (the CLI's
 * update command ran in a terminal, then the feed moved, or the cache lagged),
 * which a browser visiting a development gateway cannot produce. The gateway
 * status is dispatched into the real store, the `/api` reads each surface
 * makes on mount are answered the way their own tests answer them, and the
 * AboutPanel, UpdateFoundModal, stylesheet and theme tokens are the shipped ones.
 *
 * Surfaces: ?surface=about (Settings > About update card) | dialog (the update popup)
 * Scenes:   &scene=staged-newer (offer 0.14.0rc5, staged 0.14.1rc2: a build from another lane)
 *           &scene=staged-older (offer 0.14.0rc6, staged 0.14.0rc5)
 * Theme:    &theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { sseStatus } from '../src/store/dashboardSlice'
import { AboutPanel } from '../src/pages/settings/AboutPanel'
import UpdateFoundModal from '../src/components/UpdateFoundModal'
import type { StatusData } from '../src/types'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const surface = params.get('surface') === 'dialog' ? 'dialog' : 'about'
const scene = params.get('scene') === 'staged-older' ? 'staged-older' : 'staged-newer'
document.documentElement.setAttribute('data-theme', params.get('theme') || 'dark')

const RUNNING = '0.14.0rc4'
const offered = scene === 'staged-newer' ? '0.14.0rc5' : '0.14.0rc6'
const staged = scene === 'staged-newer' ? '0.14.1rc2' : '0.14.0rc5'
// staged-newer is the lane switch: an insider build staged, then Stable picked,
// whose feed offers an older release (shown folded, as the gateway reports it).
const channel = scene === 'staged-newer' ? 'stable' : 'insider'
const offeredDisplay = scene === 'staged-newer' ? '0.14.0' : offered

// No gateway serves this page: the config read the popup gates on answers an
// empty nudge record, every other `/api` read answers an empty object.
const realFetch = window.fetch.bind(window)
window.fetch = (input, init) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const json = (body: unknown) =>
    Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } }))
  if (url.includes('/api/config/kirocrew')) return json({ dashboard: { update_nudge: {} } })
  if (url.includes('/api/')) return json({})
  return realFetch(input, init)
}

store.dispatch(sseStatus({
  uptime: '3d', sessions: 2, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
  version: RUNNING, version_display: RUNNING,
  update_available: true,
  update_check_status: 'succeeded',
  // The About card shows the managed-venv arm flow; the popup's primary action
  // needs a self-applying install, as its own tests use.
  update_can_apply: surface === 'dialog',
  update_can_arm: surface === 'about',
  update_latest_version: offered,
  update_latest_version_display: offeredDisplay,
  update_channel: channel,
  update_managed_by: 'kirocrew',
  update_command: `curl -fsSL https://updates.crew.kiro.dev/cli.sh | sh -s -- --channel ${channel}`,
  update_restart_ready: true,
  update_restart_ready_version: staged,
  update_restart_ready_reason: 'staged',
} as StatusData))

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={['/settings']}>
        {surface === 'about' ? (
          <div style={{ background: 'var(--bg)', color: 'var(--text)', padding: 24 }} data-capture-root>
            <div style={{ maxWidth: 720 }}>
              <AboutPanel />
            </div>
          </div>
        ) : (
          <div style={{ width: '100vw', height: '100vh' }} className="bg-bg" data-capture-root>
            <UpdateFoundModal />
          </div>
        )}
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
