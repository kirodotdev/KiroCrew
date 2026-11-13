/**
 * AppDetailRoute — opening another app's detail page starts it fresh.
 *
 * The nav rail and the migration banner navigate straight from one app's
 * detail page to another's. The router reuses the element across names, so
 * the route keys the page by name; without that, the first app's success
 * message, install log or notices stayed on the second app's page.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Routes, Route, useNavigate } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const getApp = vi.fn()
const listRegistry = vi.fn()
const system = vi.fn()
const updateApp = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    getApp: (...a: unknown[]) => getApp(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    system: (...a: unknown[]) => system(...a),
    updateApp: (...a: unknown[]) => updateApp(...a),
    installFromRegistryStream: vi.fn(),
    trustApp: vi.fn(),
    untrustApp: vi.fn(),
    enableApp: vi.fn(),
    disableApp: vi.fn(),
    uninstallApp: vi.fn(),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'light' }) }))
vi.mock('../components/AppIcon', () => ({ default: () => <div data-testid="app-icon" /> }))

import AppDetailRoute from '../pages/AppDetailRoute'

function installed(name: string, displayName: string) {
  return {
    name, displayName, version: '1.0.0', enabled: true,
    source: '/home/u/apps/' + name, origin: 'local',
    resources: 'gateway', lifecycle: 'gateway', installedAt: '2026-07-01T00:00:00Z',
    manifest: { displayName, description: `${displayName} does things.`, author: 'acme' },
  }
}

/** Stands in for the nav rail's "Details" item: navigates without leaving the route. */
function OpenWeather() {
  const navigate = useNavigate()
  return <button type="button" onClick={() => navigate('/apps/detail/weather')}>open weather</button>
}

function renderRoute() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/apps/detail/secretary']}>
        <OpenWeather />
        <Routes>
          <Route path="/apps/detail/:name" element={<AppDetailRoute />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('AppDetailRoute', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    system.mockResolvedValue({ hostname: '' })
    listRegistry.mockResolvedValue({ apps: [], serverPlatform: { os: 'darwin', arch: 'arm64' } })
    getApp.mockImplementation((name: string) => Promise.resolve(
      name === 'weather' ? installed('weather', 'Weather') : installed('secretary', 'Secretary'),
    ))
    updateApp.mockResolvedValue({ ok: true })
  })

  it("does not carry one app's success message onto the next app's page", async () => {
    renderRoute()
    await userEvent.click(await screen.findByRole('button', { name: /^Sync/ }))
    expect(await screen.findByText('Synced Secretary from its source directory.')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'open weather' }))
    await waitFor(() => expect(getApp).toHaveBeenCalledWith('weather'))
    expect(await screen.findByText('Weather does things.')).toBeInTheDocument()
    expect(screen.queryByText(/from its source directory/)).not.toBeInTheDocument()
  })
})
