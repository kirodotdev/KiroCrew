/**
 * AppDetailPage — the reload after an install refreshes in place.
 *
 * A successful install reloads the app record. A full reload dropped the page
 * to "Loading app details…" and unmounted the install log the user was
 * reading until the read came back (#34452).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { i18nT } from '../i18n/t'

const getApp = vi.fn()
const listRegistry = vi.fn()
const system = vi.fn()
const installFromRegistryStream = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    getApp: (...a: unknown[]) => getApp(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    system: (...a: unknown[]) => system(...a),
    installFromRegistryStream: (...a: unknown[]) => installFromRegistryStream(...a),
    updateApp: vi.fn(),
    trustApp: vi.fn(),
    untrustApp: vi.fn(),
    enableApp: vi.fn(),
    disableApp: vi.fn(),
    uninstallApp: vi.fn(),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'light' }) }))
vi.mock('../components/AppIcon', () => ({ default: () => <div data-testid="app-icon" /> }))

import AppDetailPage from '../pages/AppDetailPage'

const INSTALLED = {
  name: 'secretary', displayName: 'Secretary', version: '1.1.0', enabled: true,
  origin: 'registry', resources: 'gateway', lifecycle: 'gateway', installedAt: '2026-07-01T00:00:00Z',
  manifest: { displayName: 'Secretary', description: 'Slack inbox manager.', author: 'zezhexu' },
}

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

describe('AppDetailPage — post-install reload', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    system.mockResolvedValue({ hostname: '' })
    getApp.mockRejectedValue(Object.assign(new Error('not installed'), { status: 404 }))
    listRegistry.mockResolvedValue({
      apps: [{
        name: 'secretary', displayName: 'Secretary', description: 'Slack inbox manager.',
        version: '1.1.0', author: 'zezhexu', installed: false,
      }],
      serverPlatform: { os: 'darwin', arch: 'arm64' },
    })
  })

  it('keeps the install log on screen while the app record reloads', async () => {
    installFromRegistryStream.mockImplementation(async (_name: string, onLine: (line: string) => void) => {
      onLine('cloning secretary')
      return { ok: true }
    })
    renderDetail()
    await screen.findByText('Slack inbox manager.')

    // Hold the post-install read open, so the page is seen mid-reload.
    let finishReload: () => void = () => {}
    getApp.mockImplementation(() => new Promise((resolve) => { finishReload = () => resolve(INSTALLED) }))
    const readsBefore = getApp.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: /^(Get|Install)$/i }))
    await waitFor(() => expect(getApp.mock.calls.length).toBeGreaterThan(readsBefore))

    expect(screen.getByText('cloning secretary')).toBeTruthy()
    expect(screen.queryByText(i18nT('pages.appDetailPage.loading_app_details'))).toBeNull()

    await act(async () => { finishReload() })
    await waitFor(() => expect(screen.getByText(i18nT('pages.appDetailPage.install_complete'))).toBeTruthy())
    expect(screen.getByText('cloning secretary')).toBeTruthy()
  })
})
