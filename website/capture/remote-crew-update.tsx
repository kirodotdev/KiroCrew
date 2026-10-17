/**
 * Isolated capture entry for a remote crew that runs an older build: the
 * "Behind" badge and Update on its Remote Crew row, and the offer that appears
 * after this machine updates.
 *
 * Mounts the REAL RemoteCrewPanel and RemoteUpdateOffer against the real
 * stylesheet, theme tokens and English catalog. API responses come from the
 * capture script's route interception (gateway-free); this entry only seeds the
 * gateway status the offer reads its version from.
 *
 * Query string:
 *   ?theme=dark|light
 *   ?view=panel   the Remote Crew panel (default)
 *   ?view=offer   the post-update offer: the browser last saw 0.9.0 and the
 *                 gateway now reports 0.9.1
 *   ?view=window  a crew window on the behind crew, at the pane width the
 *                 script sets (its header carries the badge)
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { sseStatus } from '../src/store/dashboardSlice'
import { RemoteCrewPanel } from '../src/pages/settings/RemoteCrewPanel'
import RemoteUpdateOffer, { REMOTE_OFFER_VERSION_KEY } from '../src/components/RemoteUpdateOffer'
import CrewChatWindow from '../src/pages/chat/crew-window/CrewChatWindow'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const view = params.get('view') === 'offer' ? 'offer' : params.get('view') === 'window' ? 'window' : 'panel'
document.documentElement.setAttribute('data-theme', params.get('theme') || 'dark')

if (view === 'offer') localStorage.setItem(REMOTE_OFFER_VERSION_KEY, '0.9.0')
store.dispatch(sseStatus({
  uptime: '2h', sessions: 1, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
  version: '0.9.1', version_display: '0.9.1',
} as never))

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={['/settings/remote-crew']}>
        <div style={{ background: 'var(--bg)', color: 'var(--text)', padding: 24, minHeight: '100vh' }} data-capture-root>
          {view === 'panel' ? (
            <div style={{ maxWidth: 860 }}>
              <RemoteCrewPanel />
            </div>
          ) : view === 'window' ? (
            <div style={{ height: 'calc(100vh - 48px)' }} className="border border-border rounded-lg overflow-hidden" data-crew-pane>
              <CrewChatWindow target={{ instanceId: 'dev', key: 'release-notes' }} onClose={() => {}} />
            </div>
          ) : (
            <RemoteUpdateOffer />
          )}
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
