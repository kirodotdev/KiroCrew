/**
 * Isolated capture entry for the settings deep-link collapsed-group reveal
 * (#12703). Mounts the SHIPPED Voice panel (`SttSettings`) with the REAL
 * `useSettingHighlight` above it, under a router carrying the url a command
 * palette pick emits. Nothing is mocked except the two STT reads, which are
 * seeded straight into the query cache so the panel commits its rows on the
 * first render -- the probe strips its parameter 100 ms after it mounts, so a
 * panel that arrived one round-trip later would make the frame a statement
 * about load latency, not about the reveal.
 *
 * The Voice tab is the surface that makes ownership visible, because it holds
 * two collapsible groups and one is NESTED in the other: `PushToTalkConfig`'s
 * "Start dictation with a key" is the last child of "Fine-tuning". A reveal
 * that is not scoped to the group holding the target opens both.
 *
 * Scenes: ?scene=plain    -> an ordinary visit (/settings/voice); Fine-tuning
 *                            stays collapsed, Streaming is not on the page.
 *         ?scene=deeplink  -> /settings/voice?highlight=voice.streaming; only
 *                            the owning group reveals and the row is ringed.
 * Theme:  &theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { useSettingHighlight } from '../src/hooks/useSettingHighlight'
import SttSettings from '../src/pages/settings/SttSettings'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const scene = params.get('scene') === 'deeplink' ? 'deeplink' : 'plain'
document.documentElement.setAttribute('data-theme', params.get('theme') || 'dark')

const entry = scene === 'deeplink'
  ? '/settings/voice?highlight=voice.streaming'
  : '/settings/voice'

// What the real `api.sttConfig()` / `api.sttStatus()` would answer, seeded
// straight into the cache (staleTime Infinity) so the queryFns never fire and
// the panel renders its rows on the first commit. Mirrors the test's fixture.
const sttConfig = {
  enabled: true,
  provider: 'local',
  model: 'base',
  streaming: true,
  endpointing: true,
  dictation_panel: true,
  language_code: 'en-US',
  providers: ['local', 'transcribe'],
  streaming_providers: ['local'],
  language_codes: ['auto', 'en-US'],
  prereqs: [],
}
const sttStatus = {
  available: true,
  code: '',
  detail: '',
  provider: 'local',
  model: 'base',
  models: [{ name: 'base', size_bytes: 147951465, present: true }],
  download: { step: 'idle', model: '', downloaded_bytes: 0, total_bytes: 0, error: '' },
  backend: {
    name: 'cpu',
    accelerated: false,
    encoder_only: false,
    detail: 'NEON',
    cpu_features: ['NEON'],
    requested: 'auto',
    honoured: true,
    threads: 8,
  },
  timings: { loads: 1, hashes: 1, last_load: null, last_final: null, partials: 0, partials_aborted: 0, decodes: [] },
}

const qc = new QueryClient({
  defaultOptions: { queries: { retry: false, staleTime: Infinity } },
})
qc.setQueryData(['sttConfig'], sttConfig)
qc.setQueryData(['sttStatus'], sttStatus)

function Probe() {
  useSettingHighlight()
  return <SttSettings />
}

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[entry]}>
        <div style={{ background: 'var(--bg)', color: 'var(--text)', padding: 24 }} data-capture-root>
          <div style={{ maxWidth: 760 }}>
            <Probe />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
