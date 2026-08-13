// Download progress + download-failure rendering in Settings > About.
//
// Contract under test:
// - a `downloading` state with `percent` renders a DETERMINATE progress bar
// - without `percent` it falls back to the indeterminate label (the field is
//   optional in the emit and absent on the first downloading transition)
// - a DOWNLOAD-phase error keeps the update card mounted and offers a retry,
//   instead of unmounting it and reporting "Couldn't check for updates"
// - a CHECK-phase error still renders as the check status line
// - user-facing copy comes from the failure `code`, not from the raw library
//   `message`
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, act, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'

import { store } from '../store'
import { MemoryRouter } from 'react-router-dom'
import { AboutPanel } from '../pages/settings/AboutPanel'

type UpdateState = Record<string, unknown>

/**
 * Mount the panel and return a setter that publishes an update state.
 *
 * AboutPanel does NOT subscribe to the bridge itself: useUpdateSubscription is
 * mounted in App.tsx and writes every payload into the shared ['update-state']
 * React Query cache, which the panel reads with a disabled query. So the test
 * seeds that cache directly -- pushing through a locally-captured onState
 * callback would never reach the component.
 */
function mountWithStates(info: Record<string, unknown> = {}) {
  const download = vi.fn().mockResolvedValue({ ok: true })
  ;(window as unknown as { updateAPI?: unknown }).updateAPI = {
    onState: () => () => {},
    check: vi.fn().mockResolvedValue({ ok: true }),
    download,
    install: vi.fn().mockResolvedValue({ ok: true }),
    getInfo: vi.fn().mockResolvedValue({
      version: '0.1.2',
      channel: 'nightly',
      packaged: true,
      stampedChannel: 'nightly',
      channelSwitchable: false,
      ...info,
    }),
  }
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <AboutPanel />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
  return {
    ...utils,
    download,
    setState: (s: UpdateState) => act(() => { qc.setQueryData(['update-state'], s) }),
  }
}

describe('AboutPanel download states', () => {
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

  it('renders a determinate progress bar when percent is present', async () => {
    const { setState } = mountWithStates()
    setState({ state: 'downloading', version: '0.1.3', percent: 47, bytesPerSecond: 3.2 * 1024 * 1024 })

    const bar = await screen.findByTestId('update-progress')
    expect(bar.getAttribute('aria-valuenow')).toBe('47')
    // The label carries the percentage and the transfer rate.
    expect(screen.getByTestId('update-progress-label').textContent).toContain('47%')
    expect(screen.getByTestId('update-progress-label').textContent).toContain('MB/s')
  })

  it('falls back to an indeterminate label before the first progress event', async () => {
    const { setState } = mountWithStates()
    setState({ state: 'downloading', version: '0.1.3' })

    const bar = await screen.findByTestId('update-progress')
    // No aria-valuenow: the bar is explicitly indeterminate, not "0%".
    expect(bar.hasAttribute('aria-valuenow')).toBe(false)
    expect(screen.getByTestId('update-progress-label').textContent).not.toContain('%')
  })

  it('clamps an out-of-range percent instead of overflowing the bar', async () => {
    const { setState } = mountWithStates()
    setState({ state: 'downloading', version: '0.1.3', percent: 140 })
    const bar = await screen.findByTestId('update-progress')
    expect(bar.getAttribute('aria-valuenow')).toBe('100')
  })

  it('keeps the update card and offers a retry when the DOWNLOAD phase fails', async () => {
    const { setState } = mountWithStates()
    setState({ state: 'found', version: '0.1.3' })
    expect(await screen.findByTestId('update-card')).toBeTruthy()

    setState({ state: 'error', phase: 'download', code: 'offline', version: '0.1.3', message: 'net::ERR' })

    // The card MUST survive: the user consented to this version, and losing it
    // on a transient error strands them with no way back.
    expect(await screen.findByTestId('update-download-error')).toBeTruthy()
    expect(screen.getByTestId('update-card')).toBeTruthy()
    expect(screen.getByText(/Retry/)).toBeTruthy()
    // NOT the check-failure wording.
    expect(screen.queryByText(/couldn.t check for updates/i)).toBeNull()
  })

  it('renders a CHECK phase failure as the status line, not inside the card', async () => {
    const { setState } = mountWithStates()
    setState({ state: 'error', phase: 'check', code: 'offline' })

    // Apostrophe-agnostic: the catalog string may use a typographic quote.
    expect(await screen.findByText(/couldn.t check for updates/i)).toBeTruthy()
    expect(screen.queryByTestId('update-card')).toBeNull()
  })

  it('uses curated copy from the code, not the raw library message', async () => {
    const { setState } = mountWithStates()
    setState({ state: 'found', version: '0.1.3' })
    setState({
      state: 'error',
      phase: 'download',
      code: 'integrity',
      version: '0.1.3',
      // What electron-updater actually produces -- unactionable for a user.
      message: 'sha512 checksum mismatch, expected AbC…, got XyZ…',
    })

    const row = await screen.findByTestId('update-download-error')
    expect(row.textContent).toContain('integrity check')
    expect(row.textContent).not.toContain('sha512')
  })

  it('prefers localized copy over raw library text for an unclassified failure', async () => {
    // st.message is electron-updater's developer-facing English. The localized
    // generic must win.
    const { setState } = mountWithStates()
    setState({ state: 'error', phase: 'check', code: 'unknown', message: 'ShipIt could not replace the application bundle.' })
    const row = await screen.findByText(/couldn.t check for updates/i)
    expect(row.textContent).not.toMatch(/ShipIt/)
    expect(row.textContent).toMatch(/something went wrong/i)
  })

  it('keeps the install button terminal after dispatch (no re-arm before the quit)', async () => {
    // `update:install` resolves as soon as the install is DISPATCHED; on macOS the
    // platform installer then works for several more seconds before the app quits.
    // The button must NOT become clickable again in that window -- a clickable
    // "Restart & Update" followed by an unexplained quit reads as a crash.
    const { setState } = mountWithStates()
    setState({ state: 'downloaded', version: '0.1.3' })

    const btn = await screen.findByRole('button', { name: /restart & update/i })
    expect(btn.hasAttribute('disabled')).toBe(false)
    fireEvent.click(btn)

    const restarting = await screen.findByRole('button', { name: /restarting/i })
    // The card's last words before the gateway goes down must explain the
    // coming silence -- the dashboard disconnects for the whole handoff.
    expect((await screen.findByTestId('update-card')).textContent).toMatch(/go quiet/i)
    expect(restarting.hasAttribute('disabled')).toBe(true)
    expect(screen.queryByRole('button', { name: /restart & update/i })).toBeNull()
  })

  // When an update downloads but never applies, the card just re-offers the
  // same update after relaunch, leaving users with no next step. These pin the
  // escape hatch and its reassurance, so neither can be dropped silently.
  it('offers a platform-correct manual download once the update is staged', async () => {
    const { setState } = mountWithStates({ downloadUrl: 'https://download.crew.kiro.dev/desktop/nightly/latest/KiroCrew.dmg' })
    setState({ state: 'downloaded', version: '9.9.9' })
    const fallback = await screen.findByTestId('update-manual-fallback')
    const link = fallback.querySelector('a') as HTMLAnchorElement
    expect(link.getAttribute('href')).toBe(
      'https://download.crew.kiro.dev/desktop/nightly/latest/KiroCrew.dmg',
    )
    expect(link.target).toBe('_blank')
    // Reinstalling over the top must not read as destructive.
    expect(fallback.textContent).toMatch(/kept/i)
  })

  it('renders whatever lane the main process resolved (Linux AppImage)', async () => {
    const { setState } = mountWithStates({ downloadUrl: 'https://download.crew.kiro.dev/desktop/stable/latest/KiroCrew-x86_64.AppImage' })
    setState({ state: 'downloaded', version: '9.9.9' })
    const link = (await screen.findByTestId('update-manual-fallback')).querySelector('a')
    expect(link?.getAttribute('href')).toBe(
      'https://download.crew.kiro.dev/desktop/stable/latest/KiroCrew-x86_64.AppImage',
    )
  })

  it('offers the manual download when the download itself failed', async () => {
    const { setState } = mountWithStates({ downloadUrl: 'https://download.crew.kiro.dev/desktop/nightly/latest/KiroCrew.dmg' })
    setState({ state: 'error', phase: 'download', code: 'integrity', version: '9.9.9' })
    expect(await screen.findByTestId('update-manual-fallback')).toBeTruthy()
  })

  it('does not preempt the primary action while the update is only discovered', async () => {
    const { setState } = mountWithStates({ downloadUrl: 'https://download.crew.kiro.dev/desktop/nightly/latest/KiroCrew.dmg' })
    setState({ state: 'found', version: '9.9.9' })
    await screen.findByTestId('update-card')
    expect(screen.queryByTestId('update-manual-fallback')).toBeNull()
  })

  it('omits the link when the main process reports no publish lane', async () => {
    const { setState } = mountWithStates({ downloadUrl: null })
    setState({ state: 'downloaded', version: '9.9.9' })
    await screen.findByTestId('update-card')
    expect(screen.queryByTestId('update-manual-fallback')).toBeNull()
  })

  // auto-update.js emits phase:'install'; the panel must branch on it, or an
  // install failure is labelled "couldn't check for updates", unmounts the card,
  // and hides the manual-reinstall link at the exact moment it exists for.
  it('keeps an INSTALL failure in the card, labelled honestly, with the escape hatch', async () => {
    const { setState } = mountWithStates({
      downloadUrl: 'https://download.crew.kiro.dev/desktop/nightly/latest/KiroCrew.dmg',
    })
    setState({ state: 'error', phase: 'install', code: 'unknown', message: 'ShipIt failed', version: '9.9.9' })
    const row = await screen.findByTestId('update-download-error')
    expect(row.textContent).toMatch(/couldn.t install the update/i)
    expect(row.textContent).not.toMatch(/check for updates/i)
    // The card survives and still offers the manual route.
    expect(screen.getByTestId('update-card')).toBeTruthy()
    expect(screen.getByTestId('update-manual-fallback')).toBeTruthy()
  })

  it('renders the fallback sentence from ONE template so clause order is translatable', async () => {
    const { setState } = mountWithStates({
      downloadUrl: 'https://download.crew.kiro.dev/desktop/nightly/latest/KiroCrew.dmg',
    })
    setState({ state: 'downloaded', version: '9.9.9' })
    const fallback = await screen.findByTestId('update-manual-fallback')
    // The placeholder must be consumed, not rendered.
    expect(fallback.textContent).not.toContain('{{link}}')
    expect(fallback.querySelector('a')).toBeTruthy()
    expect(fallback.textContent).toMatch(/kept/i)
  })
})
