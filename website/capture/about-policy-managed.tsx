/**
 * Isolated capture entry for the About panel's "managed by policy" notice
 * (GH #15797).
 *
 * WHY ISOLATED: the notice requires (a) `window.updateAPI` (Electron shell)
 * and (b) a gateway status with `update_bundled_by_app: true`,
 * `update_managed_by: 'command'`, and `update_can_apply: true`. That
 * three-way combination only exists on a real desktop install with a policy
 * provider that can run on the current platform. The shell and gateway status
 * are stubbed; everything else is the real AboutPanel and stylesheet.
 *
 * Scenes (?scene=):
 *   policy   bundled install + policy provider can apply → notice replaces
 *            the update card; AppAutoDownloadSwitch is hidden.
 *   normal   bundled install, no policy provider (managed_by: 'electron') →
 *            the regular desktop update row; no notice. The comparison frame.
 * Theme: &theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { sseStatus } from '../src/store/dashboardSlice'
import { AboutPanel } from '../src/pages/settings/AboutPanel'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const scene = params.get('scene') === 'normal' ? 'normal' : params.get('scene') === 'reused-gateway' ? 'reused-gateway' : 'policy'
document.documentElement.setAttribute('data-theme', params.get('theme') || 'dark')

const noop = async () => ({ ok: true })
;(window as unknown as { updateAPI?: unknown }).updateAPI = {
  onState: () => () => {},
  check: noop,
  download: noop,
  install: noop,
  getInfo: async () => ({
    version: '0.8.0',
    channel: 'stable',
    stampedChannel: 'stable',
    channelSwitchable: false, // policy-managed install has no channel switcher
    channelPreference: 'stable',
    platform: 'darwin-arm64',
    packaged: true,
  }),
}

const baseStatus = {
  uptime: '1d', sessions: 1, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
  version: '0.8.0', version_display: '0.8.0',
  update_bundled_by_app: true,
  update_check_status: 'succeeded',
  update_latest_version: '0.8.1',
  update_available: true,
} as const

if (scene === 'policy') {
  store.dispatch(sseStatus({
    ...baseStatus,
    update_managed_by: 'command',
    update_can_apply: true,
    // 'notify' keeps the switch hidden in the policy scene: the switch
    // is now intentionally hidden when policy governs, so this matches
    // the real-code behavior and avoids an error state in the stub.
    update_auto_effect: 'notify',
  } as never))
} else if (scene === 'reused-gateway') {
  // Topology 2: desktop app connected to a reused (non-bundled) CLI-service or
  // SSH-tunnel gateway. bundledByApp=false makes updateSwitchesShown render the
  // gateway auto-update switch regardless of effect.
  store.dispatch(sseStatus({
    ...baseStatus,
    update_bundled_by_app: false,
    update_managed_by: 'electron',
    update_can_apply: false,
    update_auto_effect: 'install',
  } as never))
} else {
  store.dispatch(sseStatus({
    ...baseStatus,
    update_managed_by: 'electron',
    update_can_apply: false,
    update_auto_effect: 'unknown',
  } as never))
}

// For the reused-gateway scene, pre-populate the react-query cache so the
// GatewayAutoUpdateSwitch shows a working state rather than the
// "couldn't load" error state that appears when no gateway is present.
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
if (scene === 'reused-gateway') {
  qc.setQueryData(['kirocrewConfig'], { auto_update: true })
}

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/settings']}>
        <div style={{ background: 'var(--bg)', color: 'var(--text)', padding: 24 }} data-capture-root>
          <div style={{ maxWidth: 720 }}>
            <AboutPanel />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
