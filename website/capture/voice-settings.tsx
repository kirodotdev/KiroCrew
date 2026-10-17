/**
 * Isolated capture entry for the Speech-to-Text card (#17601, #17602).
 *
 * Renders the shipped SttSettings component with the gateway API stubbed, so
 * the microphone picker, the mic test and the model controls can be shot
 * without a gateway. The microphone side is NOT stubbed: run Chromium with
 * --use-fake-device-for-media-stream --use-fake-ui-for-media-stream and the
 * browser's own fake device (a beeping tone) feeds getUserMedia.
 *
 * Theme:  &theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { api } from '../src/api/client'
import SttSettings from '../src/pages/settings/SttSettings'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
document.documentElement.setAttribute('data-theme', params.get('theme') || 'dark')

const TURBO = 1_624_555_275
const installed = new Set(['base', 'small'])
let model = 'base'

const status = () => ({
  provider: 'local',
  available: true,
  code: '',
  detail: '',
  model,
  model_present: installed.has(model),
  model_bytes: 0,
  models: [
    { name: 'tiny', size_bytes: 77_691_713, present: installed.has('tiny') },
    { name: 'base', size_bytes: 147_951_465, present: installed.has('base') },
    { name: 'small', size_bytes: 487_601_967, present: installed.has('small') },
    { name: 'large-v3-turbo', size_bytes: TURBO, present: installed.has('large-v3-turbo') },
  ],
  engine_loaded: false,
  download: { step: 'idle', model: '', downloaded_bytes: 0, total_bytes: 0, error: '' },
  ffmpeg: { present: true, source: 'system', auto_fetch: 'available', os: 'Linux', arch: 'x86_64', download: { stage: 'idle' } },
})

const config = () => ({
  enabled: true,
  provider: 'local',
  model,
  available: true,
  streaming: false,
  providers: ['local', 'transcribe'],
  streaming_providers: ['local', 'transcribe'],
  language_codes: ['auto', 'en-US'],
  language_code: 'auto',
  prereqs: [],
})

const stub = api as unknown as Record<string, unknown>
stub.sttConfig = async () => config()
stub.sttStatus = async () => status()
stub.saveSttConfig = async (patch: { model?: string }) => {
  if (patch.model) model = patch.model
  return config()
}
stub.sttPrepare = async () => ({ model, download: status().download })

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <div data-capture-root style={{ maxWidth: 760, padding: 24 }} className="bg-bg text-text">
          <SttSettings />
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
