/**
 * AppDetailPage — a disable that landed with gateway warnings says so.
 *
 * The disable route still succeeds when a hook, the config sync or the
 * service notify fails, and names that failure in `warnings`. The page must
 * show it instead of dropping it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const getApp = vi.fn()
const listRegistry = vi.fn()
const system = vi.fn()
const disableApp = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    getApp: (...a: unknown[]) => getApp(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    system: (...a: unknown[]) => system(...a),
    disableApp: (...a: unknown[]) => disableApp(...a),
    updateApp: vi.fn(),
    installFromRegistryStream: vi.fn(),
    trustApp: vi.fn(),
    untrustApp: vi.fn(),
    enableApp: vi.fn(),
    uninstallApp: vi.fn(),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'light' }) }))
vi.mock('../components/AppIcon', () => ({ default: () => <div data-testid="app-icon" /> }))

import AppDetailPage from '../pages/AppDetailPage'

function renderDetail() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/apps/detail/secretary']}>
        <Routes>
          <Route path="/apps/detail/:name" element={<AppDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('AppDetailPage — disable warnings', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    system.mockResolvedValue({ hostname: '' })
    getApp.mockResolvedValue({
      name: 'secretary', displayName: 'Secretary', version: '1.0.0', enabled: true,
      source: 'registry:secretary', origin: 'registry',
      resources: 'gateway', lifecycle: 'gateway', installedAt: '2026-07-01T00:00:00Z',
      manifest: { displayName: 'Secretary', description: 'Slack inbox manager.', author: 'zezhexu' },
    })
    listRegistry.mockResolvedValue({ apps: [], serverPlatform: { os: 'darwin', arch: 'arm64' } })
  })

  it('shows the warning the disable response carried', async () => {
    disableApp.mockResolvedValue({ name: 'secretary', enabled: false, warnings: ['hooks failed: on_disable exited 1'] })
    renderDetail()
    await userEvent.click(await screen.findByRole('button', { name: /^Disable/ }))
    await waitFor(() => expect(disableApp).toHaveBeenCalledWith('secretary'))
    expect(await screen.findByText(/Secretary is disabled, but not every step finished\. Details: hooks failed: on_disable exited 1/)).toBeInTheDocument()
  })

  it('shows nothing extra when the disable carried no warnings', async () => {
    disableApp.mockResolvedValue({ name: 'secretary', enabled: false })
    renderDetail()
    await userEvent.click(await screen.findByRole('button', { name: /^Disable/ }))
    await waitFor(() => expect(disableApp).toHaveBeenCalledWith('secretary'))
    // The handler reloads the app after a disable; check only once that settled.
    await waitFor(() => expect(getApp.mock.calls.length).toBeGreaterThan(1))
    await waitFor(() => expect(screen.getByRole('button', { name: /^Disable/ })).not.toBeDisabled())
    expect(screen.queryByText(/not every step finished/)).not.toBeInTheDocument()
  })
})
