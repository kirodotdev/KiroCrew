// Channel explainer + prerelease report prompt in Settings > About.
//
// Contract under test:
// - the stable/insider explanation is collapsed by default and toggles open
// - a prerelease BUILD always shows the "please report issues" note, without
//   needing the disclosure opened
// - the note keys on stampedChannel (the bytes running), NOT on channel (the
//   feed followed) — those diverge for the whole window between flipping the
//   switcher and the other channel's build actually landing
// - the note covers nightly too, which renders the read-only (non-switchable)
//   channel row rather than the switcher
// - a stable build shows no note at all
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { MemoryRouter } from 'react-router-dom'
import { AboutPanel } from '../pages/settings/AboutPanel'

function mountWithUpdateApi(info: Record<string, unknown>, setChannel?: (c: string) => Promise<{ ok: boolean }>) {
  ;(window as unknown as { updateAPI?: unknown }).updateAPI = {
    onState: () => () => {},
    check: vi.fn().mockResolvedValue({ ok: true }),
    download: vi.fn().mockResolvedValue({ ok: true }),
    install: vi.fn().mockResolvedValue({ ok: true }),
    getInfo: vi.fn().mockResolvedValue(info),
    ...(setChannel ? { setChannel } : {}),
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

const SWITCHABLE = {
  version: '0.1.0',
  channelSwitchable: true,
  platform: 'darwin-arm64',
  packaged: true,
}

const ok = () => vi.fn().mockResolvedValue({ ok: true })

describe('AboutPanel channel explainer', () => {
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
  })

  it('keeps the explanation collapsed until the disclosure is clicked', async () => {
    mountWithUpdateApi({ ...SWITCHABLE, channel: 'stable', stampedChannel: 'stable' }, ok())
    const toggle = await screen.findByTestId('channel-help-toggle')
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByTestId('channel-help')).toBeNull()

    fireEvent.click(toggle)
    const help = screen.getByTestId('channel-help')
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    // Both channels are explained, so the user can compare before switching.
    expect(help.textContent).toMatch(/tested releases only/i)
    expect(help.textContent).toMatch(/early builds/i)
    // Reversibility is the question that actually blocks the decision.
    expect(help.textContent).toMatch(/switch back at any time/i)

    fireEvent.click(toggle)
    expect(screen.queryByTestId('channel-help')).toBeNull()
  })

  it('shows the report prompt on an insider build WITHOUT opening the disclosure', async () => {
    mountWithUpdateApi({ ...SWITCHABLE, channel: 'insider', stampedChannel: 'insider' }, ok())
    const note = await screen.findByTestId('prerelease-report-note')
    // The disclosure is still closed — the ask must not depend on it.
    expect(screen.queryByTestId('channel-help')).toBeNull()
    const link = note.querySelector('a')
    expect(link?.getAttribute('href')).toBe('https://github.com/kirodotdev/KiroCrew/issues/new')
    expect(link?.getAttribute('target')).toBe('_blank')
    // The destination is named, so a GitHub form is not a surprise.
    expect(link?.textContent).toMatch(/github/i)
    // Trans must have substituted the tag — a leaked mustache or a bare
    // <report> in the output means the components map did not bind.
    expect(note.textContent).not.toContain('{{')
    expect(note.textContent).not.toContain('<report>')
  })

  it('KEEPS the prompt for an insider build whose feed was switched to stable', async () => {
    // The divergence window: preference already flipped to stable, but the
    // running bytes are still the insider build, so the ask still applies.
    mountWithUpdateApi(
      { ...SWITCHABLE, channel: 'stable', stampedChannel: 'insider', channelPreference: 'stable' },
      ok(),
    )
    await screen.findByTestId('prerelease-report-note')
  })

  it('shows NO prompt for a stable build whose feed was switched to insider', async () => {
    // Mirror case: opting into insider does not make the stable build you are
    // still running "less tested than Stable".
    mountWithUpdateApi(
      { ...SWITCHABLE, channel: 'insider', stampedChannel: 'stable', channelPreference: 'insider' },
      ok(),
    )
    await screen.findByTestId('channel-switcher')
    expect(screen.queryByTestId('prerelease-report-note')).toBeNull()
  })

  it('shows the report prompt on nightly, which has no switcher', async () => {
    mountWithUpdateApi({
      version: '0.1.0-nightly.20260722233638',
      channel: 'nightly',
      stampedChannel: 'nightly',
      channelSwitchable: false,
      packaged: true,
    })
    await screen.findByTestId('prerelease-report-note')
    expect(screen.queryByTestId('channel-switcher')).toBeNull()
  })

  it('shows no report prompt on an unstamped dev build', async () => {
    // stampedChannel=null: there is no published release for these bytes to be
    // "less tested" than, and updates are disabled anyway.
    mountWithUpdateApi({
      version: '0.1.0',
      channel: 'stable',
      stampedChannel: null,
      channelSwitchable: false,
      packaged: false,
      disabled: 'dev',
    })
    await screen.findByText('stable')
    expect(screen.queryByTestId('prerelease-report-note')).toBeNull()
  })

  // main.js's init-failure fallback getInfo() returns ONLY {version, packaged} —
  // no channel and no stampedChannel — so the predicate has to fall back to the
  // version string or it hides the ask from a packaged prerelease build whose
  // updater is broken.
  it('falls back to the version string when the payload has no channel fields', async () => {
    mountWithUpdateApi({ version: '0.5.0-insider.2', packaged: true, disabled: 'init-failed' })
    await screen.findByTestId('prerelease-report-note')
  })

  it('shows no report prompt for a bare-semver build with no channel fields', async () => {
    mountWithUpdateApi({ version: '0.5.0', packaged: true, disabled: 'init-failed' })
    await screen.findByText(/automatic updates are unavailable/i)
    expect(screen.queryByTestId('prerelease-report-note')).toBeNull()
  })

  it('shows no report prompt for an unpackaged build with no channel fields', async () => {
    // Unpackaged means a dev tree — the version suffix is not a release lane.
    mountWithUpdateApi({ version: '0.5.0-insider.2', packaged: false, disabled: 'init-failed' })
    await screen.findByText(/automatic updates are unavailable/i)
    expect(screen.queryByTestId('prerelease-report-note')).toBeNull()
  })

  it('shows no report prompt on a stable build', async () => {
    mountWithUpdateApi({ ...SWITCHABLE, channel: 'stable', stampedChannel: 'stable' }, ok())
    await screen.findByTestId('channel-switcher')
    expect(screen.queryByTestId('prerelease-report-note')).toBeNull()
  })
})
