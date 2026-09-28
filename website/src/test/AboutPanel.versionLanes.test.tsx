// Contract under test — the About hero's two version lanes (#11356).
//
// The hero badge is the DESKTOP SHELL's version whenever `getInfo()` answers,
// while the branch and commit chips are always the GATEWAY's stamps. A shell
// attached to a gateway it did not spawn (dev-fleet, launchd, an SSH tunnel)
// therefore composed `v0.6.0-insider.6 · main · 2ed1f603d` — a build that never
// existed — with nothing on the card saying which number belonged to what.
//
// - shell and gateway versions DIFFER -> the badge is labelled "App", a second
//   line labelled "Gateway" carries the gateway version, and the branch/commit
//   chips move onto that line (they render exactly once, on the gateway line)
// - shell and gateway versions AGREE -> today's single unlabelled badge, no
//   gateway line, chips beside the license chip
// - one build stamped twice (`0.6.0-insider.4` shell, `0.6.0rc4` wheel) -> one
//   lane on every channel, via `sameBuildVersion` on the raw strings
// - a promoted stable build whose raw stamp differs from the gateway's raw
//   `version` but whose DISPLAY values fold to the same release -> one lane,
//   because the comparison is on what the reader sees, not the raw strings
// - no desktop bridge (a plain browser) -> one lane, whatever the gateway says
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { sseStatus } from '../store/dashboardSlice'
import { MemoryRouter } from 'react-router-dom'
import { AboutPanel } from '../pages/settings/AboutPanel'

/** A minimal-but-valid status payload; `sseStatus` dereferences it, so never null. */
const BLANK_STATUS = {
  uptime: '1m', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
} as const

const GATEWAY_080 = {
  ...BLANK_STATUS,
  version: '0.8.0', version_display: '0.8.0', branch: 'main', commit: '2ed1f603d',
}

function stubDesktop(info: Record<string, unknown> | null) {
  const w = window as unknown as { updateAPI?: unknown }
  if (!info) { delete w.updateAPI; return }
  w.updateAPI = {
    onState: () => () => {},
    check: vi.fn().mockResolvedValue({ ok: true }),
    download: vi.fn().mockResolvedValue({ ok: true }),
    install: vi.fn().mockResolvedValue({ ok: true }),
    getInfo: vi.fn().mockResolvedValue(info),
    setChannel: vi.fn().mockResolvedValue({ ok: true }),
  }
}

function mount() {
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

const SHELL = {
  channel: 'insider', stampedChannel: 'insider', channelSwitchable: true,
  platform: 'darwin-arm64', packaged: true,
}

describe('AboutPanel version lanes', () => {
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
    stubDesktop(null)
    store.dispatch(sseStatus({ ...BLANK_STATUS } as never))
  })

  it('labels both lanes and moves the build chips under the gateway when the versions differ', async () => {
    store.dispatch(sseStatus(GATEWAY_080 as never))
    stubDesktop({ ...SHELL, version: '0.6.0-insider.6' })
    mount()

    const lane = await screen.findByTestId('about-gateway-lane')
    // The hero badge still names the shell, but now says so.
    const badge = screen.getByTestId('about-version')
    expect(badge.textContent).toContain('v0.6.0-insider.6')
    expect(screen.getByTestId('about-version-lane').textContent).toBe('App')
    // The tooltips must EXPLAIN each lane, not restate its label: a reader who
    // does not know what a "gateway" is learns what it does and that the
    // branch/commit chips belong to it.
    expect(badge.getAttribute('title')).toMatch(/desktop app you installed/i)
    // The gateway line names its own version, labelled.
    const gw = within(lane).getByTestId('about-gateway-version')
    expect(gw.textContent).toContain('Gateway')
    expect(gw.textContent).toContain('v0.8.0')
    expect(gw.textContent).not.toContain('0.6.0')
    expect(gw.getAttribute('title')).toMatch(/background service that runs your agents/i)
    expect(gw.getAttribute('title')).toMatch(/branch and commit chips are its build/i)
    // The chips are gateway stamps, so they sit on the gateway line — and ONLY
    // there: rendering them twice would put them back beside the shell badge.
    expect(within(lane).getByText('main')).toBeTruthy()
    expect(within(lane).getByText(/2ed1f603d/)).toBeTruthy()
    expect(screen.getAllByText('main')).toHaveLength(1)
    expect(screen.getAllByText(/2ed1f603d/)).toHaveLength(1)
  })

  it('keeps the single unlabelled badge and the bottom chip row when the versions agree', async () => {
    store.dispatch(sseStatus(GATEWAY_080 as never))
    stubDesktop({ ...SHELL, version: '0.8.0' })
    mount()

    const badge = await screen.findByTestId('about-version')
    // The chips still render (gateway status is present) — the wait is for
    // the getInfo() read, which is what could flip the lanes apart.
    await screen.findByText('main')
    expect(badge.textContent).toBe('v0.8.0')
    expect(screen.queryByTestId('about-version-lane')).toBeNull()
    expect(screen.queryByTestId('about-gateway-lane')).toBeNull()
    expect(badge.getAttribute('title')).toBeNull()
    expect(screen.getAllByText(/2ed1f603d/)).toHaveLength(1)
  })

  it('compares what the reader sees: a promoted stable build folds onto a matching gateway', async () => {
    // Promotion keeps the soaked candidate's stamp in the shell bytes, and the
    // gateway keeps its RC stamp in raw `version` while folding it for display.
    // Raw strings differ (`0.4.1-insider.1` vs `0.4.1rc3`); both display as
    // 0.4.1. Splitting them into two lanes would announce a disagreement the
    // user cannot see.
    store.dispatch(sseStatus({
      ...BLANK_STATUS, version: '0.4.1rc3', version_display: '0.4.1', branch: 'main', commit: 'abc1234',
    } as never))
    stubDesktop({
      ...SHELL, channel: 'stable', stampedChannel: 'insider', channelPreference: '',
      version: '0.4.1-insider.1', laneVersion: '0.4.1-insider.1', runningAheadOfLane: false,
    })
    mount()

    const badge = await screen.findByTestId('about-version')
    await screen.findByText('main')
    expect(badge.textContent).toBe('v0.4.1')
    expect(screen.queryByTestId('about-gateway-lane')).toBeNull()
  })

  it('reads one build stamped twice as one lane: an insider shell on its own bundled wheel', async () => {
    // The common self-spawned case on the insider channel, where NEITHER side
    // folds its stamp: the shell says `0.6.0-insider.4`, the wheel it launched
    // says `0.6.0rc4`. Same tag, two spellings. A display-only comparison
    // would label every insider install as two builds.
    store.dispatch(sseStatus({
      ...BLANK_STATUS, version: '0.6.0rc4', version_display: '0.6.0rc4', branch: 'main', commit: 'abc1234',
    } as never))
    stubDesktop({ ...SHELL, version: '0.6.0-insider.4' })
    mount()

    const badge = await screen.findByTestId('about-version')
    await screen.findByText('main')
    expect(badge.textContent).toBe('v0.6.0-insider.4')
    expect(screen.queryByTestId('about-version-lane')).toBeNull()
    expect(screen.queryByTestId('about-gateway-lane')).toBeNull()
    expect(screen.getAllByText(/abc1234/)).toHaveLength(1)
  })

  it('shows one lane in a plain browser, where there is no shell version to disagree with', async () => {
    store.dispatch(sseStatus(GATEWAY_080 as never))
    stubDesktop(null)
    mount()

    const badge = await screen.findByTestId('about-version')
    expect(badge.textContent).toBe('v0.8.0')
    expect(screen.queryByTestId('about-version-lane')).toBeNull()
    expect(screen.queryByTestId('about-gateway-lane')).toBeNull()
    expect(screen.getAllByText('main')).toHaveLength(1)
  })
})
