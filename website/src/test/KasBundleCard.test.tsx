/**
 * KasBundleCard — Settings > Agent Harness > KAS: which engine KAS runs on.
 *
 * What is pinned:
 * - the built-in engine can only be chosen once the pinned engine is installed (an
 *   "on" with nothing installed refuses every KAS session), and Kiro CLI is always
 *   allowed;
 * - the choice writes `agent.kas_bundle` through the ordinary config PATCH;
 * - unsaved, the choice shows what the build prefers (`engine_choice`);
 * - "Install" starts the gateway's install, and the card shows its progress from
 *   the status the server returns;
 * - an install failure renders the server's remedy text;
 * - a host that cannot run the engine names why in place of Install, once;
 * - a saved switch is reported as pending a restart, because the setting is
 *   boot-only, and nothing says so before a save;
 * - a status poll that fails while an earlier status is still cached is reported,
 *   so stale progress never reads as current;
 * - a status read that fails with nothing cached offers Retry, which re-reads;
 * - the copy the desktop app shipped (`installed.bundled`) is named as such;
 * - the installed line's tick is green only once the engine is what runs: muted
 *   on Kiro CLI, muted again while a save waits for a restart.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { api, type KasBundleStatus } from '../api/client'
import { KAS_BUNDLE_CONFIG_KEY, KasBundleCard } from '../pages/developer/KasBundleCard'
import {
  __resetErrorJournalForTests, __resetGatewayConnectedForTests, attachReport, consumeChatHandoff, recordError, setGatewayConnected,
} from '../utils/errorReport'

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

function builtin(): HTMLElement {
  return screen.getByRole('radio', { name: 'Built-in harness' })
}

function kiroCli(): HTMLElement {
  return screen.getByRole('radio', { name: 'Kiro CLI' })
}

describe('KasBundleCard', () => {
  beforeEach(() => {
    kasBundleStatus.mockReset()
    kasBundleInstall.mockReset()
    patchConfig.mockClear()
  })

  it('names the sharing preview, not kiro-cli, when the preview picked the engine', async () => {
    kasBundleStatus.mockResolvedValue({
      ...INSTALLED,
      route: { route: 'bundle', automatic: true, reason: 'chat_runtime_sharing', summary: '' },
    })
    renderWithProviders(<KasBundleCard />)
    const note = await screen.findByTestId('kas-bundle-route-auto')
    expect(note).toHaveTextContent('The ACP runtime sharing preview is on')
    expect(note).not.toHaveTextContent('Kiro CLI')
  })

  it('offers Install and holds the built-in engine off while nothing is installed', async () => {
    kasBundleStatus.mockResolvedValue(BASE)
    renderWithProviders(<KasBundleCard />)
    await screen.findByTestId('kas-bundle-install')
    expect(screen.getByText('Built-in engine 0.66.33 is not installed yet.')).toBeInTheDocument()
    expect(builtin()).toBeDisabled()
    expect(kiroCli()).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByTestId('restart-badge')).toHaveAttribute('data-key', KAS_BUNDLE_CONFIG_KEY)
  })

  it('names why a host cannot run the engine instead of offering an install it refuses', async () => {
    const reason = "Kiro Crew cannot install its KAS engine: this host's glibc (2.26) is older than KAS requires (glibc 2.28+)."
    kasBundleStatus.mockResolvedValue({ ...BASE, host_unsupported: reason })
    renderWithProviders(<KasBundleCard />)
    expect(await screen.findByTestId('kas-bundle-host-unsupported')).toHaveTextContent(reason)
    expect(screen.queryByTestId('kas-bundle-install')).toBeNull()
    expect(screen.queryByText('Built-in engine 0.66.33 is not installed yet.')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(builtin()).toBeDisabled()
  })

  it('reports a refused install on such a host once, as an error, with no Install', async () => {
    const reason = "Kiro Crew cannot install its KAS engine: this host's glibc (2.26) is older than KAS requires (glibc 2.28+)."
    kasBundleStatus.mockResolvedValue({ ...BASE, host_unsupported: reason, error: reason })
    renderWithProviders(<KasBundleCard />)
    expect(await screen.findByRole('alert')).toHaveTextContent(reason)
    expect(screen.getAllByText(reason)).toHaveLength(1)
    expect(screen.queryByTestId('kas-bundle-host-unsupported')).toBeNull()
    expect(screen.queryByTestId('kas-bundle-install')).toBeNull()
  })

  it('lets the built-in engine be chosen once installed, through the config PATCH', async () => {
    kasBundleStatus.mockResolvedValue(INSTALLED)
    renderWithProviders(<KasBundleCard />)
    await screen.findByText('Built-in engine 0.66.33')
    expect(screen.queryByTestId('kas-bundle-install')).toBeNull()
    expect(builtin()).not.toBeDisabled()
    fireEvent.click(builtin())
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
    await screen.findByText('Built-in engine 0.66.33 (included with the app)')
    expect(screen.queryByText('Built-in engine 0.66.33')).toBeNull()
    expect(builtin()).not.toBeDisabled()
  })

  it("shows the build's own pick while nothing is saved, and saves that pick when pressed", async () => {
    // A packaged build's pick is not the user's: pressing it saves it, so the
    // choice holds after a sign-in. A saved choice pressed again sends nothing.
    kasBundleStatus.mockResolvedValue({ ...INSTALLED, engine_choice: 'kiro_cli', choice_saved: false })
    const { unmount } = renderWithProviders(<KasBundleCard />)
    await screen.findByText('Built-in engine 0.66.33')
    expect(kiroCli()).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByTestId('kas-bundle-installed-tick')).toHaveClass('text-muted')
    fireEvent.click(kiroCli())
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('agent.kas_bundle', false))
    unmount()
    patchConfig.mockClear()
    kasBundleStatus.mockResolvedValue({ ...INSTALLED, engine_choice: 'engine', choice_saved: true, enabled: true })
    renderWithProviders(<KasBundleCard />)
    await screen.findByText('Built-in engine 0.66.33')
    expect(builtin()).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByTestId('kas-bundle-installed-tick')).toHaveClass('text-ok')
    fireEvent.click(builtin())
    expect(patchConfig).not.toHaveBeenCalled()
  })

  it('saves Kiro Agent first when the saved harness is a Kiro CLI pick running on it', async () => {
    // Saving only the engine would stop the Kiro CLI pick reaching Kiro Agent.
    kasBundleStatus.mockResolvedValue({ ...INSTALLED, engine_choice: 'kiro_cli', choice_saved: false })
    renderWithProviders(<KasBundleCard kiroPickRunsOnKas />)
    await screen.findByText('Built-in engine 0.66.33')
    fireEvent.click(builtin())
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('agent.kas_bundle', true))
    expect(patchConfig.mock.calls.map(c => c[0])).toEqual(['agent.acp_backend', 'agent.kas_bundle'])
    expect(patchConfig).toHaveBeenNthCalledWith(1, 'agent.acp_backend', 'kas')
    // Choosing Kiro CLI leaves the harness alone.
    patchConfig.mockClear()
    fireEvent.click(kiroCli())
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('agent.kas_bundle', false))
    expect(patchConfig).toHaveBeenCalledTimes(1)
  })

  it('ticks the selected engine option, and only that one', async () => {
    kasBundleStatus.mockResolvedValue({ ...INSTALLED, engine_choice: 'kiro_cli', choice_saved: false })
    const { unmount } = renderWithProviders(<KasBundleCard />)
    await screen.findByText('Built-in engine 0.66.33')
    expect(screen.getAllByTestId('kas-engine-tick')).toHaveLength(1)
    expect(kiroCli().contains(screen.getByTestId('kas-engine-tick'))).toBe(true)
    unmount()
    kasBundleStatus.mockResolvedValue({ ...INSTALLED, engine_choice: 'engine', choice_saved: true, enabled: true })
    renderWithProviders(<KasBundleCard />)
    await screen.findByText('Built-in engine 0.66.33')
    expect(builtin().contains(screen.getByTestId('kas-engine-tick'))).toBe(true)
  })

  it('says why the recommended engine is not chosen while Kiro Crew is signed out', async () => {
    kasBundleStatus.mockResolvedValue({
      ...INSTALLED,
      engine_choice: 'kiro_cli',
      choice_saved: false,
      sign_in_moves_to_engine: true,
    })
    const { unmount } = renderWithProviders(<KasBundleCard />)
    expect(await screen.findByTestId('kas-bundle-sign-in-reason')).toHaveTextContent(
      'Sign in to Kiro Crew to switch to the built-in engine.',
    )
    // One action: the Kiro account tab, where the sign-in choices sit in the card.
    expect(screen.getByTestId('kas-bundle-sign-in-link')).toHaveAttribute(
      'href',
      expect.stringContaining('/settings/kiro-account'),
    )
    unmount()
    // A saved Kiro CLI choice is the user's own: no reason line.
    kasBundleStatus.mockResolvedValue({
      ...INSTALLED,
      engine_choice: 'kiro_cli',
      choice_saved: true,
      sign_in_moves_to_engine: true,
    })
    renderWithProviders(<KasBundleCard />)
    await screen.findByText('Built-in engine 0.66.33')
    expect(screen.queryByTestId('kas-bundle-sign-in-reason')).toBeNull()
  })

  it('names no route line for the build default: the choice already says it', async () => {
    kasBundleStatus.mockResolvedValue({
      ...INSTALLED,
      engine_choice: 'engine',
      route: { route: 'bundle', automatic: true, reason: 'build_default', summary: '' },
    })
    renderWithProviders(<KasBundleCard />)
    await screen.findByText('Built-in engine 0.66.33')
    expect(screen.queryByTestId('kas-bundle-route-auto')).toBeNull()
  })

  it('reports a saved switch as pending a restart, and only after the save', async () => {
    kasBundleStatus.mockResolvedValue(INSTALLED)
    renderWithProviders(<KasBundleCard />)
    await screen.findByText('Built-in engine 0.66.33')
    expect(screen.queryByTestId('kas-bundle-restart-pending')).toBeNull()
    // A stable anchor, so a deep link waits for the lazy tab instead of dropping.
    expect(document.querySelector('[data-setting-id="agent.engine"][data-setting-key="agent.kas_bundle"]')).not.toBeNull()
    // The restart line beside the choice, until the pending notice takes over.
    expect(screen.getByTestId('kas-bundle-restart-note')).toHaveTextContent('Chats switch after a restart.')
    fireEvent.click(builtin())
    const pending = await screen.findByTestId('kas-bundle-restart-pending')
    expect(pending.textContent).toContain('Restart gateway')
    expect(pending).toHaveAttribute('role', 'status')
    expect(screen.queryByTestId('kas-bundle-restart-note')).toBeNull()
  })

  it('greens the installed tick only once the engine is what runs', async () => {
    // Off: muted. On, after a restart (a fresh card reading enabled): green.
    // On, saved in this card but not yet restarted onto: muted again.
    kasBundleStatus.mockResolvedValue(INSTALLED)
    const { unmount } = renderWithProviders(<KasBundleCard />)
    await screen.findByText('Built-in engine 0.66.33')
    expect(screen.getByTestId('kas-bundle-installed-tick')).toHaveClass('text-muted')
    unmount()

    kasBundleStatus.mockResolvedValue({ ...INSTALLED, enabled: true })
    const second = renderWithProviders(<KasBundleCard />)
    await screen.findByText('Built-in engine 0.66.33')
    expect(screen.getByTestId('kas-bundle-installed-tick')).toHaveClass('text-ok')
    second.unmount()

    kasBundleStatus.mockResolvedValueOnce(INSTALLED)
    kasBundleStatus.mockResolvedValue({ ...INSTALLED, enabled: true })
    renderWithProviders(<KasBundleCard />)
    await screen.findByText('Built-in engine 0.66.33')
    fireEvent.click(builtin())
    await screen.findByTestId('kas-bundle-restart-pending')
    await waitFor(() => expect(builtin()).toHaveAttribute('aria-checked', 'true'))
    expect(screen.getByTestId('kas-bundle-installed-tick')).toHaveClass('text-muted')
    // The generic pill is hidden in place, so the row (and the choice) keeps its width.
    expect(screen.getByTestId('kas-bundle-restart-pill')).toHaveClass('invisible')
    expect(screen.getByTestId('restart-badge')).toBeInTheDocument()
  })

  it('keeps the pressed choice focusable and refuses a second press while its own save runs', async () => {
    // A native disabled takes the pressed button out of the tab order, dropping a
    // keyboard user to the page while the save runs; a guard refuses the second press.
    kasBundleStatus.mockResolvedValue(INSTALLED)
    let release: () => void = () => undefined
    patchConfig.mockImplementationOnce(() => new Promise(resolve => { release = () => resolve({}) }))
    renderWithProviders(<KasBundleCard />)
    await screen.findByText('Built-in engine 0.66.33')
    const button = builtin()
    button.focus()
    fireEvent.click(button)
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('agent.kas_bundle', true))
    expect(button).not.toBeDisabled()
    expect(button).toHaveFocus()
    fireEvent.click(kiroCli())
    expect(patchConfig.mock.calls.filter(c => c[0] === 'agent.kas_bundle')).toHaveLength(1)
    act(() => { release() })
    await screen.findByTestId('kas-bundle-restart-pending')
  })

  it('always lets Kiro CLI be chosen, even with nothing installed', async () => {
    // A broken or removed install must never trap anyone on this route.
    kasBundleStatus.mockResolvedValue({ ...BASE, enabled: true })
    renderWithProviders(<KasBundleCard />)
    await screen.findByTestId('kas-bundle-install')
    expect(kiroCli()).not.toBeDisabled()
    fireEvent.click(kiroCli())
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('agent.kas_bundle', false))
  })

  it("hands the agent the failed install request's own report", async () => {
    // The notice words the failure from the catalog, so it cannot find the
    // report by its text; the request's report must ride with the error.
    __resetErrorJournalForTests()
    kasBundleStatus.mockResolvedValue(BASE)
    const report = recordError({
      source: 'api', message: 'download failed: 503', status: 503, code: 'kas_download_failed',
      endpoint: '/api/kas-bundle/install',
    })
    kasBundleInstall.mockRejectedValue(attachReport(new Error('download failed: 503'), report))
    renderWithProviders(<KasBundleCard />)
    fireEvent.click(await screen.findByTestId('kas-bundle-install'))
    const alert = await screen.findByRole('alert')
    fireEvent.click(within(alert).getByRole('button', { name: 'Ask the agent' }))
    const prompt = consumeChatHandoff() ?? ''
    expect(prompt).toContain('HTTP 503')
    expect(prompt).toContain('kas_download_failed')
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
    expect(live.textContent).toContain('Installing the built-in engine 0.66.33')
    expect(live.textContent).toContain('50')
    expect(builtin()).toBeDisabled()
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
    await screen.findByText("Could not read the built-in engine's status.", {}, { timeout: 5000 })
    expect(screen.getByTestId('kas-bundle-card')).toBeInTheDocument()
  })

  it('offers Retry when the status cannot be read, and Retry re-reads it', async () => {
    kasBundleStatus.mockRejectedValueOnce(new Error('gateway gone'))
    kasBundleStatus.mockResolvedValue(INSTALLED)
    renderWithProviders(<KasBundleCard />)
    await screen.findByText("Could not read the built-in engine's status.")
    fireEvent.click(screen.getByTestId('kas-bundle-retry'))
    await screen.findByText('Built-in engine 0.66.33')
    expect(kasBundleStatus).toHaveBeenCalledTimes(2)
  })

  it('keeps Retry and drops the alert for a read that failed while the gateway was down, then re-reads it on reconnect (#32966)', async () => {
    kasBundleStatus.mockRejectedValueOnce(new Error('Failed to fetch'))
    kasBundleStatus.mockResolvedValue(INSTALLED)
    setGatewayConnected(false)
    try {
      renderWithProviders(<KasBundleCard />)
      await screen.findByTestId('kas-bundle-retry')
      expect(screen.queryByText("Could not read the built-in engine's status.")).toBeNull()
      act(() => { setGatewayConnected(true) })
      await screen.findByText('Built-in engine 0.66.33')
      expect(kasBundleStatus).toHaveBeenCalledTimes(2)
    } finally {
      __resetGatewayConnectedForTests()
    }
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
