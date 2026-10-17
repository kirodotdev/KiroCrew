/**
 * KasBundleCard — Settings > Developer > Feature Previews: run KAS on Kiro Crew's own engine.
 *
 * What is pinned:
 * - the switch can only be turned ON once the pinned engine is installed (an "on"
 *   with nothing installed refuses every KAS session), and OFF is always allowed;
 * - the switch writes `agent.kas_bundle` through the ordinary config PATCH;
 * - "Install" starts the gateway's install, and the card shows its progress from
 *   the status the server returns;
 * - an install failure renders the server's remedy text;
 * - a saved switch is reported as pending a restart, because the setting is
 *   boot-only, and nothing says so before a save;
 * - a status poll that fails while an earlier status is still cached is reported,
 *   so stale progress never reads as current;
 * - a status read that fails with nothing cached offers Retry, which re-reads;
 * - the copy the desktop app shipped (`installed.bundled`) is named as such;
 * - an installed engine under an OFF switch says it is not in use, and the note
 *   goes away once the switch is on;
 * - the installed line's tick is green only once the engine is what runs: muted
 *   with the switch off, muted again while a save waits for a restart.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { api, type KasBundleStatus } from '../api/client'
import { KAS_BUNDLE_CONFIG_KEY, KasBundleCard } from '../pages/developer/KasBundleCard'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kasBundleStatus: vi.fn(),
      kasBundleInstall: vi.fn(),
      patchConfig: vi.fn().mockResolvedValue({}),
    },
  }
})

// The restart pill reads the schema; its own behaviour is tested with the schema.
vi.mock('../components/settingRef/RestartRequiredBadge', () => ({
  SchemaRestartBadge: ({ configKey }: { configKey: string }) => (
    <span data-testid="restart-badge" data-key={configKey} />
  ),
}))

const kasBundleStatus = vi.mocked(api.kasBundleStatus)
const kasBundleInstall = vi.mocked(api.kasBundleInstall)
const patchConfig = vi.mocked(api.patchConfig)

const BASE: KasBundleStatus = {
  enabled: false,
  pinned_version: '0.66.33',
  runs_version: '0.66.33',
  installed: null,
  installing: false,
  progress: { done: 0, total: 0 },
  error: '',
}

const INSTALLED: KasBundleStatus = {
  ...BASE,
  installed: { version: '0.66.33', node_version: '22.23.3', bundled: false },
}

function toggle(): HTMLElement {
  return screen.getByRole('switch')
}

describe('KasBundleCard', () => {
  beforeEach(() => {
    kasBundleStatus.mockReset()
    kasBundleInstall.mockReset()
    patchConfig.mockClear()
  })

  it('offers Install and holds the switch off while nothing is installed', async () => {
    kasBundleStatus.mockResolvedValue(BASE)
    renderWithProviders(<KasBundleCard />)
    await screen.findByTestId('kas-bundle-install')
    expect(screen.getByText('KAS engine 0.66.33 is not installed yet.')).toBeInTheDocument()
    expect(toggle()).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByTestId('restart-badge')).toHaveAttribute('data-key', KAS_BUNDLE_CONFIG_KEY)
  })

  it('lets the switch be turned on once installed, through the config PATCH', async () => {
    kasBundleStatus.mockResolvedValue(INSTALLED)
    renderWithProviders(<KasBundleCard />)
    await screen.findByText('KAS engine 0.66.33 (installed by you)')
    expect(screen.queryByTestId('kas-bundle-install')).toBeNull()
    expect(toggle()).not.toHaveAttribute('aria-disabled')
    fireEvent.click(toggle())
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('agent.kas_bundle', true))
  })

  it('names the copy the desktop app shipped, so "installed" never reads as "mine"', async () => {
    // The app's own copy (BUNDLE_KAS) answers only while nothing is installed under
    // the data home; the card says which copy it is rather than showing one line for both.
    kasBundleStatus.mockResolvedValue({
      ...INSTALLED,
      installed: { version: '0.66.33', node_version: '22.23.3', bundled: true },
    })
    renderWithProviders(<KasBundleCard />)
    await screen.findByText('KAS engine 0.66.33 (included with the app)')
    expect(screen.queryByText('KAS engine 0.66.33 (installed by you)')).toBeNull()
    expect(toggle()).not.toHaveAttribute('aria-disabled')
  })

  it('says an installed engine is not in use while the switch is off, and only then', async () => {
    kasBundleStatus.mockResolvedValue(INSTALLED)
    const { unmount } = renderWithProviders(<KasBundleCard />)
    await screen.findByText('KAS engine 0.66.33 (installed by you)')
    expect(screen.getByTestId('kas-bundle-off-note')).toHaveTextContent(
      'Off: KAS uses your Kiro CLI install. On: it uses the engine this release ships with.',
    )
    unmount()
    kasBundleStatus.mockResolvedValue({ ...INSTALLED, enabled: true })
    renderWithProviders(<KasBundleCard />)
    await screen.findByText('KAS engine 0.66.33 (installed by you)')
    expect(screen.queryByTestId('kas-bundle-off-note')).toBeNull()
  })

  it('reports a saved switch as pending a restart, and only after the save', async () => {
    kasBundleStatus.mockResolvedValue(INSTALLED)
    renderWithProviders(<KasBundleCard />)
    await screen.findByText('KAS engine 0.66.33 (installed by you)')
    expect(screen.queryByTestId('kas-bundle-restart-pending')).toBeNull()
    fireEvent.click(toggle())
    const pending = await screen.findByTestId('kas-bundle-restart-pending')
    expect(pending.textContent).toContain('Restart gateway')
    expect(pending).toHaveAttribute('role', 'status')
  })

  it('greens the installed tick only once the engine is what runs', async () => {
    // Off: muted. On, after a restart (a fresh card reading enabled): green.
    // On, saved in this card but not yet restarted onto: muted again.
    kasBundleStatus.mockResolvedValue(INSTALLED)
    const { unmount } = renderWithProviders(<KasBundleCard />)
    await screen.findByText('KAS engine 0.66.33 (installed by you)')
    expect(screen.getByTestId('kas-bundle-installed-tick')).toHaveClass('text-muted')
    unmount()

    kasBundleStatus.mockResolvedValue({ ...INSTALLED, enabled: true })
    const second = renderWithProviders(<KasBundleCard />)
    await screen.findByText('KAS engine 0.66.33 (installed by you)')
    expect(screen.getByTestId('kas-bundle-installed-tick')).toHaveClass('text-ok')
    second.unmount()

    kasBundleStatus.mockResolvedValueOnce(INSTALLED)
    kasBundleStatus.mockResolvedValue({ ...INSTALLED, enabled: true })
    renderWithProviders(<KasBundleCard />)
    await screen.findByText('KAS engine 0.66.33 (installed by you)')
    fireEvent.click(toggle())
    await screen.findByTestId('kas-bundle-restart-pending')
    await waitFor(() => expect(screen.queryByTestId('kas-bundle-off-note')).toBeNull())
    expect(screen.getByTestId('kas-bundle-installed-tick')).toHaveClass('text-muted')
    // The generic pill is hidden in place, so the row (and the switch) keeps its width.
    expect(screen.getByTestId('kas-bundle-restart-pill')).toHaveClass('invisible')
    expect(screen.getByTestId('restart-badge')).toBeInTheDocument()
  })

  it('always lets an enabled switch be turned off, even with nothing installed', async () => {
    // A broken or removed install must never trap anyone on this route.
    kasBundleStatus.mockResolvedValue({ ...BASE, enabled: true })
    renderWithProviders(<KasBundleCard />)
    await screen.findByTestId('kas-bundle-install')
    expect(toggle()).not.toHaveAttribute('aria-disabled')
    fireEvent.click(toggle())
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('agent.kas_bundle', false))
  })

  it('starts the install and shows the progress the server reports', async () => {
    kasBundleStatus.mockResolvedValueOnce(BASE)
    kasBundleInstall.mockResolvedValue({
      ...BASE,
      installing: true,
      progress: { done: 50 * 1024 * 1024, total: 100 * 1024 * 1024 },
    })
    renderWithProviders(<KasBundleCard />)
    fireEvent.click(await screen.findByTestId('kas-bundle-install'))
    await waitFor(() => expect(kasBundleInstall).toHaveBeenCalledTimes(1))
    const live = await screen.findByRole('status')
    expect(live.textContent).toContain('Installing KAS 0.66.33')
    expect(live.textContent).toContain('50')
    expect(toggle()).toHaveAttribute('aria-disabled', 'true')
  })

  it('reports a failed re-read beside the cached status instead of showing stale progress as current', async () => {
    kasBundleStatus.mockResolvedValueOnce({
      ...BASE,
      installing: true,
      progress: { done: 10 * 1024 * 1024, total: 100 * 1024 * 1024 },
    })
    kasBundleStatus.mockRejectedValue(new Error('gateway gone'))
    renderWithProviders(<KasBundleCard />)
    await screen.findByRole('status')
    // The install poll fires once a second while the server says installing.
    await screen.findByText('Could not read the KAS engine status.', {}, { timeout: 5000 })
    expect(screen.getByTestId('kas-bundle-card')).toBeInTheDocument()
  })

  it('offers Retry when the status cannot be read, and Retry re-reads it', async () => {
    kasBundleStatus.mockRejectedValueOnce(new Error('gateway gone'))
    kasBundleStatus.mockResolvedValue(INSTALLED)
    renderWithProviders(<KasBundleCard />)
    await screen.findByText('Could not read the KAS engine status.')
    fireEvent.click(screen.getByTestId('kas-bundle-retry'))
    await screen.findByText('KAS engine 0.66.33 (installed by you)')
    expect(kasBundleStatus).toHaveBeenCalledTimes(2)
  })

  it("renders the server's remedy when the last install failed", async () => {
    kasBundleStatus.mockResolvedValue({
      ...BASE,
      error: 'publishes no Node runtime for linux-riscv64',
    })
    renderWithProviders(<KasBundleCard />)
    expect(await screen.findByText('publishes no Node runtime for linux-riscv64')).toBeInTheDocument()
  })
})
