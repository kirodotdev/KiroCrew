/**
 * Isolated capture entry for Settings -> Security -> Redaction (PR #29423 evidence).
 *
 * Mounts the REAL RedactionSection against the real stylesheet, theme tokens and
 * i18n catalog, with the gateway's answers replaced by fixtures on the same `api`
 * methods the section calls. No gateway, token or backend.
 *
 * Query string:
 *   ?scene=default|off|engine-write-failed|seams-write-failed
 *     off: both switches off, legacy engine
 *     *-write-failed: default state, and the named writer rejects; the capture
 *     script clicks that toggle so the inline write-failure state renders
 *   &theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { api } from '../src/api/client'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { RedactionSection } from '../src/pages/settings/SecurityPanelRedaction'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') || 'default'
document.documentElement.dataset.theme = theme
document.documentElement.classList.toggle('dark', theme === 'dark')

const SCENES: Record<string, Record<string, unknown>> = {
  default: { enabled: true, changed_at: '', seams_enabled: true, seams_changed_at: '', engine: 'structured', engine_changed_at: '' },
  off: {
    enabled: false, changed_at: '2026-11-12T08:00:00+00:00',
    seams_enabled: false, seams_changed_at: '2026-11-12T08:01:00+00:00',
    engine: 'legacy', engine_changed_at: '2026-11-12T08:02:00+00:00',
  },
}
const state = SCENES[scene] ?? SCENES.default
const rejectWrite = async () => {
  throw new Error('HTTP 503: gateway unavailable')
}

const stub = api as unknown as Record<string, unknown>
stub.credentialRedaction = async () => state
stub.setCredentialRedaction = async () => state
stub.setCredentialRedactionSeams = scene === 'seams-write-failed' ? rejectWrite : async () => state
stub.setCredentialRedactionEngine = scene === 'engine-write-failed' ? rejectWrite : async () => state
stub.redactionAllowedHosts = async () => ({ workspaces: { '~/work/demo': ['api.example.com'] } })
stub.redactionRevokeHost = async () => ({ ok: true, removed: true })

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

await initI18n()

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <div data-capture-root className="bg-bg text-text min-h-screen px-4 py-5 sm:p-8">
          <div className="max-w-[720px]">
            <RedactionSection />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
