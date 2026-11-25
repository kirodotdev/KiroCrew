/**
 * Isolated capture entry for the managed-environment lane's dashboard side: the launch
 * form on Remote Crew's setup tab and the row actions on the lane's own crews.
 *
 * Mounts the REAL RemoteCrewPanel with the lane's registrations, the real stylesheet and
 * the English catalog. API answers come from the capture script's route interception.
 * The `crew_client` preview is on and the lane is the remembered provisioner.
 *
 * Query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { RemoteCrewPanel } from '../src/pages/settings/RemoteCrewPanel'
import '../src/index.css'

localStorage.setItem('mc-preview-crew-client', '1')
localStorage.setItem('mc-cloud-provisioner', 'kiro_managed_env')
initI18n('en')

const params = new URLSearchParams(location.search)
document.documentElement.setAttribute('data-theme', params.get('theme') || 'dark')

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={['/settings/remote-crew']}>
        <div style={{ background: 'var(--bg)', color: 'var(--text)', padding: 24, minHeight: '100vh' }} data-capture-root>
          <div style={{ maxWidth: 860 }}>
            <RemoteCrewPanel />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
