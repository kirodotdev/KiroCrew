/**
 * AppDetailPage — auto-action deep-link safety.
 *
 * The Discover "Get" action navigates here and expects the install to start on
 * arrival. That trigger must be reachable ONLY from an in-app navigation
 * (react-router state), never from the URL: a cross-site page can navigate an
 * authenticated browser to any detail URL and the Lax session cookie rides
 * along, so a URL-driven trigger would run third-party setup code with gateway
 * privileges without user intent.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { i18nT } from '../i18n/t'

const getApp = vi.fn()
const listRegistry = vi.fn()
const system = vi.fn()
const installFromRegistryStream = vi.fn()
const updateApp = vi.fn()
const trustApp = vi.fn()
const untrustApp = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    getApp: (...a: unknown[]) => getApp(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    system: (...a: unknown[]) => system(...a),
    installFromRegistryStream: (...a: unknown[]) => installFromRegistryStream(...a),
    updateApp: (...a: unknown[]) => updateApp(...a),
    trustApp: (...a: unknown[]) => trustApp(...a),
    untrustApp: (...a: unknown[]) => untrustApp(...a),
    enableApp: vi.fn(),
    disableApp: vi.fn(),
    uninstallApp: vi.fn(),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'light' }) }))
vi.mock('../components/AppIcon', () => ({ default: () => <div data-testid="app-icon" /> }))

import AppDetailPage from '../pages/AppDetailPage'

const REGISTRY_APP = {
  name: 'secretary',
  displayName: 'Secretary',
  description: 'Slack inbox manager.',
  version: '1.1.0',
  author: 'zezhexu',
  installed: false,
}

/** Render the detail route, optionally with router state or a query string. */
function renderDetail({ search = '', state }: { search?: string; state?: unknown } = {}) {
  // `useTrustGate` invalidates the ['trusted-apps'] / ['apps'] queries after a
  // grant, so it needs a QueryClient in scope. The app root always provides
  // one; the harness has to as well.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[{ pathname: '/apps/detail/secretary', search, state }]}>
      <Routes>
        <Route path="/apps/detail/:name" element={<AppDetailPage />} />
        <Route path="/apps" element={<div>apps list</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('AppDetailPage — auto-action deep links', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    system.mockResolvedValue({ hostname: '' })
    installFromRegistryStream.mockResolvedValue({ ok: true })
    getApp.mockRejectedValue(Object.assign(new Error('not installed'), { status: 404 }))
    listRegistry.mockResolvedValue({ apps: [REGISTRY_APP], serverPlatform: { os: 'darwin', arch: 'arm64' } })
  })

  it('starts the install when navigated in-app with autoAction state', async () => {
    renderDetail({ state: { autoAction: 'install' } })
    await waitFor(() => expect(installFromRegistryStream).toHaveBeenCalledWith(
      'secretary', expect.any(Function), expect.anything(),
    ))
  })

  it('does NOT install from a URL query param (cross-site navigation)', async () => {
    renderDetail({ search: '?action=install' })
    // Wait for the page to finish loading so the effect has certainly run.
    await screen.findByText('Slack inbox manager.')
    expect(installFromRegistryStream).not.toHaveBeenCalled()
  })

  it('does NOT re-install an already-installed app via autoAction state', async () => {
    getApp.mockResolvedValue({
      name: 'secretary', displayName: 'Secretary', version: '1.1.0', enabled: true,
      origin: 'registry', resources: 'gateway', lifecycle: 'gateway', installedAt: '2026-07-01T00:00:00Z',
      manifest: { displayName: 'Secretary', description: 'Slack inbox manager.', author: 'zezhexu' },
    })
    listRegistry.mockResolvedValue({
      apps: [{ ...REGISTRY_APP, installed: true, installedVersion: '1.1.0' }],
      serverPlatform: { os: 'darwin', arch: 'arm64' },
    })
    renderDetail({ state: { autoAction: 'install' } })
    await screen.findByText('Slack inbox manager.')
    expect(installFromRegistryStream).not.toHaveBeenCalled()
  })

  it('does NOT update from a URL query param either (update installs absent apps)', async () => {
    getApp.mockResolvedValue({
      name: 'secretary', displayName: 'Secretary', version: '1.0.0', enabled: true,
      origin: 'registry', resources: 'gateway', lifecycle: 'gateway', installedAt: '2026-07-01T00:00:00Z',
      manifest: { displayName: 'Secretary', description: 'Slack inbox manager.', author: 'zezhexu' },
    })
    renderDetail({ search: '?action=update' })
    await screen.findByText('Slack inbox manager.')
    expect(installFromRegistryStream).not.toHaveBeenCalled()
  })

  it('starts an update when navigated in-app with autoAction=update state', async () => {
    getApp.mockResolvedValue({
      name: 'secretary', displayName: 'Secretary', version: '1.0.0', enabled: true,
      origin: 'registry', resources: 'gateway', lifecycle: 'gateway', installedAt: '2026-07-01T00:00:00Z',
      manifest: { displayName: 'Secretary', description: 'Slack inbox manager.', author: 'zezhexu' },
    })
    renderDetail({ state: { autoAction: 'update' } })
    await waitFor(() => expect(installFromRegistryStream).toHaveBeenCalled())
  })

  it('syncs a PATH-installed app from its directory, never through the registry', async () => {
    // The registry install stream can only serve an app the registry lists, so
    // an app installed from a directory used to fail its own Sync with
    // "app not found in registry" while being installed, enabled and working.
    getApp.mockResolvedValue({
      name: 'secretary', displayName: 'Secretary', version: '0.1.0', enabled: true,
      source: '/home/u/apps/secretary', origin: 'local',
      resources: 'gateway', lifecycle: 'gateway', installedAt: '2026-07-01T00:00:00Z',
      manifest: { displayName: 'Secretary', description: 'Slack inbox manager.', author: 'zezhexu' },
    })
    listRegistry.mockResolvedValue({ apps: [], serverPlatform: { os: 'darwin', arch: 'arm64' } })
    renderDetail({ state: { autoAction: 'update' } })
    await waitFor(() => expect(updateApp).toHaveBeenCalledWith('secretary'))
    expect(installFromRegistryStream).not.toHaveBeenCalled()
  })

  it('installs the registry version of a PATH install that a registry row offers an update for', async () => {
    // The tile says Update here, not Sync, and this page's Update button runs
    // the registry install, so the navigation must too: re-copying the folder
    // reported success while the listed version never landed.
    getApp.mockResolvedValue({
      name: 'secretary', displayName: 'Secretary', version: '1.0.0', enabled: true,
      source: '/home/u/apps/secretary', origin: 'local',
      resources: 'gateway', lifecycle: 'gateway', installedAt: '2026-07-01T00:00:00Z',
      manifest: { displayName: 'Secretary', description: 'Slack inbox manager.', author: 'zezhexu' },
    })
    listRegistry.mockResolvedValue({
      apps: [{ ...REGISTRY_APP, version: '1.2.0', installed: true, installedVersion: '1.0.0', updateAvailable: true }],
      serverPlatform: { os: 'darwin', arch: 'arm64' },
    })
    renderDetail({ state: { autoAction: 'update' } })
    await waitFor(() => expect(installFromRegistryStream).toHaveBeenCalled())
    expect(updateApp).not.toHaveBeenCalled()
  })

  it('keeps the registry stream for an app whose source IS a registry ref', async () => {
    getApp.mockResolvedValue({
      name: 'secretary', displayName: 'Secretary', version: '1.0.0', enabled: true,
      source: 'registry:secretary', origin: 'registry',
      resources: 'gateway', lifecycle: 'gateway', installedAt: '2026-07-01T00:00:00Z',
      manifest: { displayName: 'Secretary', description: 'Slack inbox manager.', author: 'zezhexu' },
    })
    renderDetail({ state: { autoAction: 'update' } })
    await waitFor(() => expect(installFromRegistryStream).toHaveBeenCalled())
    expect(updateApp).not.toHaveBeenCalled()
  })

  it('dispatches update when there is no installed record and a catalog row supplies a non-string source', async () => {
    // With no installed record, the page spreads the CATALOG row into its app
    // object. registry_pipeline/ copies index keys verbatim for a row it has
    // not installed, so `source` can be an object; an unguarded startsWith
    // throws inside this effect and Sync never dispatches at all.
    getApp.mockRejectedValue(Object.assign(new Error('not installed'), { status: 404 }))
    listRegistry.mockResolvedValue({
      apps: [{
        ...REGISTRY_APP, installed: true, installedVersion: '1.1.0',
        origin: 'local', source: { type: 'git' },
      }],
      serverPlatform: { os: 'darwin', arch: 'arm64' },
    })
    renderDetail({ state: { autoAction: 'update' } })
    // origin 'local' means path-installed, so it must reach the update endpoint.
    await waitFor(() => expect(updateApp).toHaveBeenCalledWith('secretary'))
    expect(installFromRegistryStream).not.toHaveBeenCalled()
  })

  it('states that a path-installed sync succeeded, since the page is otherwise unchanged', async () => {
    // Both surfaces this fix wires need the reflection: a same-version re-copy
    // leaves the page byte-identical, so silence reads as a no-op.
    getApp.mockResolvedValue({
      name: 'secretary', displayName: 'Secretary', version: '0.1.0', enabled: true,
      source: '/home/u/apps/secretary', origin: 'local',
      resources: 'gateway', lifecycle: 'gateway', installedAt: '2026-07-01T00:00:00Z',
      manifest: { displayName: 'Secretary', description: 'Slack inbox manager.', author: 'zezhexu' },
    })
    listRegistry.mockResolvedValue({ apps: [], serverPlatform: { os: 'darwin', arch: 'arm64' } })
    updateApp.mockResolvedValue({ ok: true })
    renderDetail({ state: { autoAction: 'update' } })
    await waitFor(() => expect(updateApp).toHaveBeenCalledWith('secretary'))
    expect(await screen.findByText(/Synced Secretary from its source directory/)).toBeInTheDocument()
  })

  describe('a local-only grant refusing the registry update', () => {
    const REPO = 'https://github.com/acme/secretary'
    const LOCAL_ONLY = {
      ok: false,
      name: 'secretary',
      code: 'app_trust_local_only',
      error: 'this app is trusted for its local source only, so that trust does not cover the repository version this update installs',
      trustRepository: REPO,
    }

    beforeEach(() => {
      getApp.mockResolvedValue({
        name: 'secretary', displayName: 'Secretary', version: '1.0.0', enabled: true,
        source: '/home/u/apps/secretary', origin: 'local',
        resources: 'gateway', lifecycle: 'gateway', installedAt: '2026-07-01T00:00:00Z',
        manifest: { displayName: 'Secretary', description: 'Slack inbox manager.', author: 'zezhexu' },
      })
      listRegistry.mockResolvedValue({
        apps: [{ ...REGISTRY_APP, version: '1.2.0', installed: true, installedVersion: '1.0.0', updateAvailable: true }],
        serverPlatform: { os: 'darwin', arch: 'arm64' },
      })
      installFromRegistryStream.mockResolvedValue(LOCAL_ONLY)
    })

    it('shows the refusal with a button that trusts the repository and runs the update', async () => {
      updateApp.mockResolvedValue({ ok: true })
      renderDetail({ state: { autoAction: 'update' } })

      // The banner says what happened in the page's own words and names the
      // repository, never the server's lowercase prose.
      const sentence = await screen.findByText(
        `Secretary was installed from a folder on this computer. A newer version is published at ${REPO}. To update, trust that repository. Updating replaces the folder copy and keeps the app's data.`,
      )
      // A refused update is an error, with its recovery actions in the same notice.
      const alert = screen.getByRole('alert')
      expect(alert).toContainElement(sentence)
      expect(within(alert).getByRole('button', {
        name: i18nT('components.askAgent.ask_the_agent'),
      })).toBeInTheDocument()
      expect(screen.queryByText(/trusted for its local source only/)).not.toBeInTheDocument()
      // Refused before the clone: no empty install log beside the banner.
      expect(screen.queryByText('Install failed')).not.toBeInTheDocument()
      fireEvent.click(await screen.findByRole('button', { name: /Trust the repository and update/ }))

      // The dialog names the repository the refusal carried and says the folder
      // copy is replaced, before anything is trusted.
      const dialog = await screen.findByRole('dialog')
      expect(within(dialog).getByText(REPO)).toBeInTheDocument()
      expect(within(dialog).getByText(/is installed from a folder on this computer/)).toBeInTheDocument()
      expect(updateApp).not.toHaveBeenCalled()

      // The dialog's confirm uses the banner button's words.
      fireEvent.click(within(dialog).getByRole('button', { name: 'Trust the repository and update' }))
      // One call carries the update and its consent proof. The dialog writes no
      // grant of its own: the server switches the grant for this update and
      // puts the local one back if it fails.
      await waitFor(() => expect(updateApp).toHaveBeenCalledWith('secretary', 'registry:secretary', REPO))
      expect(trustApp).not.toHaveBeenCalled()
      // The success names what the user agreed to trust: the repository.
      expect(await screen.findByText(/Updated Secretary from its repository/)).toBeInTheDocument()
      expect(screen.queryByText(/from the registry/)).not.toBeInTheDocument()
    })

    it('keeps the dialog open with the reason when the switched update fails', async () => {
      updateApp.mockRejectedValue(new Error('clone failed'))
      renderDetail({ state: { autoAction: 'update' } })

      fireEvent.click(await screen.findByRole('button', { name: /Trust the repository and update/ }))
      fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: /Trust the repository and update/ }))

      expect(await screen.findByText('clone failed')).toBeInTheDocument()
      // The not-saved state: the server put the local grant back, so the dialog
      // must not claim trust was saved, and nothing here wrote or revoked trust.
      expect(screen.getByText(/could not be updated, and trust was not saved/)).toBeInTheDocument()
      expect(trustApp).not.toHaveBeenCalled()
      expect(untrustApp).not.toHaveBeenCalled()
    })

    it('names the listing, not a local install, as the source of the repository it trusts', async () => {
      renderDetail({ state: { autoAction: 'update' } })
      fireEvent.click(await screen.findByRole('button', { name: /Trust the repository and update/ }))
      const dialog = within(await screen.findByRole('dialog'))
      expect(dialog.getByText(i18nT('components.appstore.types.kirocrew_registry'))).toBeInTheDocument()
      expect(dialog.queryByText(i18nT('appStoreSources.local'))).not.toBeInTheDocument()
    })

    it('offers no trust button for any other install failure', async () => {
      installFromRegistryStream.mockResolvedValue({ ok: false, error: 'clone failed', code: '' })
      renderDetail({ state: { autoAction: 'update' } })
      expect(await screen.findAllByText('clone failed')).not.toHaveLength(0)
      expect(screen.queryByRole('button', { name: /Trust the repository and update/ })).not.toBeInTheDocument()
    })
  })

  it('names the registry, not a source directory, when a registry-sourced app is synced', async () => {
    // The Sync button is not gated on source, and the backend re-clones a
    // registry-sourced app from the registry — so the source-directory wording
    // would be a false statement about where the update came from.
    getApp.mockResolvedValue({
      name: 'secretary', displayName: 'Secretary', version: '1.0.0', enabled: true,
      source: 'registry:secretary', origin: 'registry',
      resources: 'gateway', lifecycle: 'gateway', installedAt: '2026-07-01T00:00:00Z',
      manifest: { displayName: 'Secretary', description: 'Slack inbox manager.', author: 'zezhexu' },
    })
    listRegistry.mockResolvedValue({ apps: [], serverPlatform: { os: 'darwin', arch: 'arm64' } })
    updateApp.mockResolvedValue({ ok: true })
    renderDetail({})
    const sync = await screen.findByRole('button', { name: /Sync/i })
    fireEvent.click(sync)
    await waitFor(() => expect(updateApp).toHaveBeenCalledWith('secretary'))
    expect(await screen.findByText(/Updated Secretary from the registry/)).toBeInTheDocument()
    expect(screen.queryByText(/from its source directory/)).not.toBeInTheDocument()
  })
})
