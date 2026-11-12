/**
 * AppDetailPage — Sync keeps its focus while it runs.
 *
 * Sync re-copies an installed app from its source and stays on screen through
 * the whole run and after it, success or failure. A native `disabled` on the
 * button a reader just pressed blurs it to <body>, so a keyboard user lost
 * their place on the page. Sync is held with aria-disabled instead, and a
 * second press starts nothing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
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

describe('AppDetailPage — Sync focus', () => {
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

  it('keeps focus on Sync while it runs, refuses a second press, and keeps it after a failure', async () => {
    let fail: () => void = () => {}
    updateApp.mockImplementation(() => new Promise((_resolve, reject) => {
      fail = () => reject(new Error('the source directory is gone'))
    }))
    renderDetail()
    const sync = await screen.findByRole('button', { name: /Sync/i })

    sync.focus()
    await userEvent.keyboard('{Enter}')
    await waitFor(() => expect(updateApp).toHaveBeenCalledTimes(1))
    expect(sync.getAttribute('aria-disabled')).toBe('true')
    expect(sync.hasAttribute('disabled')).toBe(false)
    expect(document.activeElement).toBe(sync)

    await userEvent.keyboard('{Enter}')
    expect(updateApp).toHaveBeenCalledTimes(1)

    await act(async () => { fail() })
    expect(await screen.findByText(/the source directory is gone/)).toBeTruthy()
    expect(screen.getByRole('button', { name: /Sync/i })).toBe(sync)
    expect(document.activeElement).toBe(sync)
    expect(sync.hasAttribute('aria-disabled')).toBe(false)
  })

  it('keeps the same Sync button, and its focus, through the refresh after a success', async () => {
    updateApp.mockResolvedValue({})
    renderDetail()
    const sync = await screen.findByRole('button', { name: /Sync/i })

    // Hold the refresh read open, so the page is seen mid-reload.
    const record = await getApp.mock.results[0].value
    let finishReload: () => void = () => {}
    getApp.mockImplementation(() => new Promise((resolve) => { finishReload = () => resolve(record) }))
    const readsBefore = getApp.mock.calls.length
    sync.focus()
    await userEvent.keyboard('{Enter}')
    await waitFor(() => expect(getApp.mock.calls.length).toBeGreaterThan(readsBefore))
    expect(sync.isConnected).toBe(true)
    expect(document.activeElement).toBe(sync)

    await act(async () => { finishReload() })
    await waitFor(() => expect(sync.hasAttribute('aria-disabled')).toBe(false))
    expect(screen.getByRole('button', { name: /Sync/i })).toBe(sync)
    expect(document.activeElement).toBe(sync)
  })
})
