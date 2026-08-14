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
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const getApp = vi.fn()
const listRegistry = vi.fn()
const system = vi.fn()
const installFromRegistryStream = vi.fn()
const updateApp = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    getApp: (...a: unknown[]) => getApp(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    system: (...a: unknown[]) => system(...a),
    installFromRegistryStream: (...a: unknown[]) => installFromRegistryStream(...a),
    updateApp: (...a: unknown[]) => updateApp(...a),
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
    getApp.mockRejectedValue(new Error('not installed'))
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
})
