// Contract under test: Settings > About on an edition's build of a stable
// release.
//
// An edition that repackages one stable release as several builds of its own
// stamps the backend `0.13.0.11` (BUILD_VERSION) and the shell `0.13.0-11`
// (packaging/build-desktop.sh carries the package.json stamp, since
// electron-builder takes only SemVer). The shell's lane fields come from the
// shell's own classifier, loaded here from electron/auto-update.js, so this
// covers the payload a managed (Toolbox) install really reports:
//
// - the App chip names the build (`v0.13.0.11`), not the release it builds,
//   and a shell beside its own gateway reads as one build
// - a shell beside a different build's gateway shows both, labelled
// - the "thanks for running early builds" note stays hidden: these bytes are
//   a build of the stable release, and a managed lane never answers the feed
//   comparison that exempts a promoted stable release
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createRequire } from 'node:module'
import { render, screen, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { sseStatus } from '../store/dashboardSlice'
import { MemoryRouter } from 'react-router-dom'
import { AboutPanel } from '../pages/settings/AboutPanel'
import { editionBuildVersion, foldStableStamp } from '../utils/displayVersion'

type Channel = 'nightly' | 'insider' | 'stable' | null
const { channelForVersion, resolveChannel } = createRequire(import.meta.url)('../../electron/auto-update.js') as {
  channelForVersion: (version: string) => Channel
  resolveChannel: (stamped: Channel, preference: string) => Channel
}

const BLANK_STATUS = {
  uptime: '1m', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
} as const

function gateway(version: string) {
  return { ...BLANK_STATUS, version, version_display: version, branch: 'main', commit: 'abc1234' }
}

/** What a Toolbox-managed shell's getInfo() reports for `version`, no preference set. */
function managedShell(version: string) {
  const stampedChannel = channelForVersion(version)
  return {
    version,
    stampedChannel,
    channel: resolveChannel(stampedChannel, ''),
    channelSwitchable: false,
    channelPreference: '',
    managedBy: 'toolbox',
    updateCommand: 'toolbox update kirocrew',
    laneVersion: '',
    runningAheadOfLane: null,
    platform: 'linux-x64',
    packaged: true,
  }
}

function mount(info: Record<string, unknown>) {
  ;(window as unknown as { updateAPI?: unknown }).updateAPI = {
    onState: () => () => {},
    check: vi.fn().mockResolvedValue({ ok: true }),
    download: vi.fn().mockResolvedValue({ ok: true }),
    armsOnQuit: true,
    install: vi.fn().mockResolvedValue({ ok: true }),
    getInfo: vi.fn().mockResolvedValue(info),
    setChannel: vi.fn().mockResolvedValue({ ok: true }),
  }
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <AboutPanel />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

describe('AboutPanel on an edition build', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true, status: 200,
      json: async () => ({}),
      text: async () => '',
      headers: new Headers({ 'content-type': 'application/json' }),
    }))
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
    store.dispatch(sseStatus({ ...BLANK_STATUS } as never))
  })

  it('names the build on the App chip and shows no prerelease note on a managed install', async () => {
    store.dispatch(sseStatus(gateway('0.13.0.11') as never))
    mount(managedShell('0.13.0-11'))

    // Wait for getInfo(): until it lands the chip shows the gateway's version.
    await screen.findByTestId('managed-update-command')
    const badge = screen.getByTestId('about-version')
    expect(badge.textContent).toBe('v0.13.0.11')
    // The shell and the gateway it launched are one build in two spellings.
    expect(screen.queryByTestId('about-gateway-lane')).toBeNull()
    expect(screen.queryByTestId('prerelease-report-note')).toBeNull()
  })

  it('tells two builds of one release apart', async () => {
    store.dispatch(sseStatus(gateway('0.13.0.9') as never))
    mount(managedShell('0.13.0-11'))

    const lane = await screen.findByTestId('about-gateway-lane')
    expect(screen.getByTestId('about-version').textContent).toContain('v0.13.0.11')
    expect(within(lane).getByTestId('about-gateway-version').textContent).toContain('v0.13.0.9')
    expect(screen.queryByTestId('prerelease-report-note')).toBeNull()
  })

  it('shows no prerelease note from the version string alone either', async () => {
    // main.js's init-failure fallback reports only {version, packaged}.
    mount({ version: '0.13.0-11', packaged: true, disabled: 'init-failed' })
    await screen.findByText(/automatic updates are unavailable/i)
    expect(screen.queryByTestId('prerelease-report-note')).toBeNull()
  })
})

describe('editionBuildVersion', () => {
  it('reads one numeric -N over a bare numeric base as that build', () => {
    expect(editionBuildVersion('0.13.0-11')).toBe('0.13.0.11')
    expect(foldStableStamp('0.13.0-11', 'stable', null)).toBe('0.13.0.11')
    expect(foldStableStamp('0.13.0-11', 'insider')).toBe('0.13.0.11')
  })

  it.each(['0.13.0', '0.13.0-rc.11', '0.13.0-insider.11', '0.13.0-11.2', '0.13.0-1a', '0.13.0.11'])(
    '%s is not one', (version) => {
      expect(editionBuildVersion(version)).toBeNull()
    },
  )

  it('leaves the promoted-stable fold alone', () => {
    expect(foldStableStamp('0.4.1-insider.1', 'stable', false)).toBe('0.4.1')
    expect(foldStableStamp('0.5.0-insider.2', 'stable', true)).toBe('0.5.0-insider.2')
  })
})
