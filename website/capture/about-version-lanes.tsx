/**
 * Isolated capture entry for the About hero's two version lanes (#11356).
 *
 * The lanes only split when a DESKTOP shell (`window.updateAPI.getInfo()`
 * answering with the app bundle's version) is attached to a GATEWAY whose
 * `status.version` is a different release — the dev-fleet / launchd / SSH-tunnel
 * shape. A browser visiting the real dashboard has no `updateAPI`, so it never
 * renders the shell badge at all; and a packaged shell that spawned its own
 * gateway has equal lanes. Neither can show the split, so the bridge is stubbed
 * and the gateway status is dispatched into the real store; the AboutPanel,
 * stylesheet and theme tokens are the shipped ones.
 *
 * Scenes: ?scene=differ (shell 0.6.0-insider.6 on a 0.8.0 gateway)
 *         ?scene=same   (shell and gateway both 0.8.0 — today's single badge)
 * Theme:  &theme=dark|light
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
const scene = params.get('scene') === 'same' ? 'same' : 'differ'
document.documentElement.setAttribute('data-theme', params.get('theme') || 'dark')

const GATEWAY_VERSION = '0.8.0'
const shellVersion = scene === 'same' ? GATEWAY_VERSION : '0.6.0-insider.6'

// What the launchd / dev-fleet gateway pushes: its own checkout's version and
// git stamps. The branch and commit belong to THIS lane, never to the shell.
store.dispatch(sseStatus({
  uptime: '7d', sessions: 3, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
  version: GATEWAY_VERSION, version_display: GATEWAY_VERSION,
  branch: 'main', commit: '2ed1f603d',
} as never))

const resolved = async () => ({ ok: true })
;(window as unknown as { updateAPI?: unknown }).updateAPI = {
  onState: () => () => {},
  check: resolved,
  download: resolved,
  install: resolved,
  setChannel: resolved,
  getInfo: async () => ({
    version: shellVersion,
    channel: 'insider',
    stampedChannel: 'insider',
    channelSwitchable: true,
    channelPreference: 'insider',
    platform: 'darwin-arm64',
    packaged: true,
  }),
}

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
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
