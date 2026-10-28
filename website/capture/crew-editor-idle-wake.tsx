/**
 * Capture page: the Issue Radar crew editor's read-only "Idle wake" field and the
 * line under it, which names both gaps a crew loop runs at (the active gap while an
 * item is in progress, the backstop otherwise).
 *
 * The dialog is the real component inside the real provider; only the network is a
 * fixture: the editor's four Issue Radar calls are stubbed on `issueRadarApi`, and
 * every other `/api/**` read answers an empty JSON body. `?theme=light` for the
 * light palette.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { store } from '../src/store'
import { initI18n } from '../src/i18n/all'
import { issueRadarApi } from '../src/apps/issue-radar/api'
import { IssueRadarProvider } from '../src/apps/issue-radar/context'
import CrewEditor from '../src/apps/issue-radar/components/CrewEditor'
import '../src/index.css'

const params = new URLSearchParams(location.search)
document.documentElement.setAttribute('data-theme', params.get('theme') === 'light' ? 'light' : 'dark')

const realFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (new URL(url, location.href).pathname.startsWith('/api/')) {
    return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } })
  }
  return realFetch(input, init)
}

const ACTIVE = { owner: 'acme', repo: 'widgets', provider: 'github', host: 'github.com' }
const LABELS = [
  { name: 'area: core', color: 'ededed', description: '' },
  { name: 'bug', color: 'd73a4a', description: '' },
]
Object.assign(issueRadarApi, {
  suggestCrewNames: async () => ({ names: ['Sombrero', 'Bode', 'Carina'] }),
  labels: async () => ({ ...ACTIVE, from_cache: true, labels: LABELS }),
  createCrew: async () => ({}),
  updateCrew: async () => ({}),
})

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <IssueRadarProvider
        repos={[{ ...ACTIVE, enabled: true, permissions: { push: true, triage: true } }] as never}
        active={ACTIVE as never}
        onSwitch={() => {}}
        onAddRepo={() => {}}
      >
        <div data-testid="capture-frame" style={{ minHeight: '100vh' }}>
          <CrewEditor open onClose={() => {}} crew={null} />
        </div>
      </IssueRadarProvider>
    </QueryClientProvider>
  </Provider>,
)
