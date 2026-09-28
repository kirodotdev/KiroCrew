/**
 * Isolated capture entry for the three channel/version states in Settings >
 * About that this change is about.
 *
 * WHY ISOLATED: each state is a property of a REMOTE FEED, not of anything a
 * browser session can reach. The gateway scenes need a status frame whose
 * `update_channel_move_pending` came from a real CDN comparison; the desktop
 * scene needs `window.updateAPI`, which exists only inside the packaged Electron
 * shell. So the feed answer and the desktop bridge are stubbed and everything
 * else is real: the REAL AboutPanel, the REAL stylesheet and theme tokens, and
 * the same payload shapes the gateway and the main process actually emit.
 *
 * Scenes (?scene=), each the state of one real population:
 *   gateway-move     insider bytes (0.5.0rc3) following stable, whose feed is at
 *                    0.4.1rc1 -- the reported bug. Chip must keep the rc stamp,
 *                    badge must not say "up to date", note must name v0.4.1.
 *   gateway-promoted a PROMOTED stable install (0.4.1rc1 IS the 0.4.1 release).
 *                    Chip folds, badge is green, and NOTHING pending is shown --
 *                    the false positive that used to render here forever.
 *   desktop-move     the same move on the desktop app, where the lane pair comes
 *                    from the updater instead of the gateway.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

// Initialise i18next exactly as main.tsx does: importing the module only DEFINES
// initI18n, and without calling it every label in the frame renders blank.
import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { sseStatus } from '../src/store/dashboardSlice'
import { AboutPanel } from '../src/pages/settings/AboutPanel'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'gateway-move'
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme)

const INSTALLER = 'curl -fsSL https://download.crew.kiro.dev/cli.sh | sh -s -- --channel stable'

/** The status frame each gateway scene's install would really push. */
const STATUS = {
  'gateway-move': {
    version: '0.5.0rc3',
    // Not folded: the stable lane never published these bytes.
    version_display: '0.5.0rc3',
    release_channel: 'insider',
    update_channel: 'stable',
    update_channel_move_pending: true,
    update_available: false,
    update_check_status: 'succeeded',
    update_can_apply: false,
    update_latest_version: '0.4.1rc1',
    update_latest_version_display: '0.4.1',
    update_command: INSTALLER,
  },
  'gateway-promoted': {
    version: '0.4.1rc1',
    // Folded: these bytes ARE the 0.4.1 release, stamp and all.
    version_display: '0.4.1',
    // Reads `insider` off the version string, which is exactly why nothing may
    // be derived from it.
    release_channel: 'insider',
    update_channel: 'stable',
    update_channel_move_pending: false,
    update_available: false,
    update_check_status: 'succeeded',
    update_can_apply: false,
    update_latest_version: '0.4.1rc1',
    update_latest_version_display: '0.4.1',
    update_command: INSTALLER,
  },
} as const

const isDesktopScene = scene === 'desktop-move'

if (!isDesktopScene) {
  const frame = STATUS[scene as keyof typeof STATUS] ?? STATUS['gateway-move']
  store.dispatch(sseStatus({
    uptime: '4h', sessions: 2, messages: 0, cron_jobs: 0, lessons: 0, ...frame,
  } as never))
} else {
  const noop = async () => ({ ok: true })
  ;(window as unknown as { updateAPI?: unknown }).updateAPI = {
    onState: () => () => {},
    check: noop,
    download: noop,
    install: noop,
    getInfo: async () => ({
      version: '0.5.0-insider.2',
      channel: 'stable',
      // The byte stamp, kept for the prerelease ask; it cannot answer which lane
      // the install is on, which is what the pair below is for.
      stampedChannel: 'insider',
      channelSwitchable: true,
      channelPreference: 'stable',
      platform: 'darwin-arm64',
      packaged: true,
      autoDownload: true,
      laneVersion: '0.4.1-insider.1',
      runningAheadOfLane: true,
      downloadUrl: 'https://download.crew.kiro.dev/desktop/stable/latest',
    }),
    setChannel: noop,
  }
}

// The panel's own fetches: a capture page has no gateway behind it, and an
// unanswered /api/update/check would leave the card in its loading state.
const realFetch = window.fetch
window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(typeof input === 'string' ? input : (input as Request).url ?? input)
  if (url.includes('/api/')) {
    const frame = isDesktopScene ? {} : STATUS[scene as keyof typeof STATUS] ?? {}
    return new Response(JSON.stringify({ ...frame, auto_update: false }), {
      status: 200, headers: { 'content-type': 'application/json' },
    })
  }
  return realFetch(input, init)
}) as typeof window.fetch

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/settings']}>
        <div
          style={{ background: 'var(--bg)', color: 'var(--text)', padding: 24 }}
          data-capture-root
        >
          <div style={{ maxWidth: 760 }}>
            <AboutPanel />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
