/**
 * AppDetailPage — one action at a time.
 *
 * Install, Update, Sync, Disable and Uninstall all change the same app, so while
 * any one of them runs every action button stays disabled, and the lock lifts
 * only when that action ends. Each button checking only its own in-flight flag
 * left Disable and Uninstall clickable during an Install, and one action ending
 * unlocked the page while another still ran.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const getApp = vi.fn()
const listRegistry = vi.fn()
const system = vi.fn()
const installFromRegistryStream = vi.fn()
const updateApp = vi.fn()
const disableApp = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    getApp: (...a: unknown[]) => getApp(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    system: (...a: unknown[]) => system(...a),
    installFromRegistryStream: (...a: unknown[]) => installFromRegistryStream(...a),
    updateApp: (...a: unknown[]) => updateApp(...a),
    disableApp: (...a: unknown[]) => disableApp(...a),
    enableApp: vi.fn(),
    uninstallApp: vi.fn(),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'light' }) }))
vi.mock('../components/AppIcon', () => ({ default: () => <div data-testid="app-icon" /> }))

import AppDetailPage from '../pages/AppDetailPage'

const INSTALLED = {
  name: 'secretary', displayName: 'Secretary', version: '1.0.0', enabled: true,
  source: 'registry:secretary', origin: 'registry',
  resources: 'gateway', lifecycle: 'gateway', installedAt: '2026-07-01T00:00:00Z',
  manifest: { displayName: 'Secretary', description: 'Slack inbox manager.', author: 'zezhexu' },
}

function registryRow(updateAvailable: boolean) {
  return {
    apps: [{
      name: 'secretary', displayName: 'Secretary', description: 'Slack inbox manager.',
      version: updateAvailable ? '1.1.0' : '1.0.0', author: 'zezhexu',
      installed: true, installedVersion: '1.0.0', updateAvailable,
    }],
    serverPlatform: { os: 'darwin', arch: 'arm64' },
  }
}

/** A promise the test settles by hand, so "still running" is a fact, not a race. */
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(r => { resolve = r })
  return { promise, resolve }
}

function renderDetail() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/apps/detail/secretary']}>
        <Routes>
          <Route path="/apps/detail/:name" element={<AppDetailPage />} />
          <Route path="/apps" element={<div>apps list</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const button = (name: RegExp) => screen.getByRole('button', { name })

describe('AppDetailPage — every action locks every action button', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    system.mockResolvedValue({ hostname: '' })
    getApp.mockResolvedValue(INSTALLED)
  })

  it('keeps Disable and Uninstall disabled until an Update install ends', async () => {
    listRegistry.mockResolvedValue(registryRow(true))
    const install = deferred<{ ok: boolean }>()
    installFromRegistryStream.mockReturnValue(install.promise)
    renderDetail()

    fireEvent.click(await screen.findByRole('button', { name: /^Update$/i }))
    expect(await screen.findByRole('button', { name: /Updating/i })).toBeDisabled()
    expect(button(/^Disable$/i)).toBeDisabled()
    expect(button(/^Uninstall$/i)).toBeDisabled()

    // Clicking a locked button starts nothing.
    fireEvent.click(button(/^Disable$/i))
    expect(disableApp).not.toHaveBeenCalled()

    install.resolve({ ok: true })
    await waitFor(() => expect(button(/^Disable$/i)).toBeEnabled())
    expect(button(/^Uninstall$/i)).toBeEnabled()
    expect(button(/^Update$/i)).toBeEnabled()
  })

  it('keeps Disable and Uninstall disabled until a Sync ends', async () => {
    listRegistry.mockResolvedValue(registryRow(false))
    const sync = deferred<{ ok: boolean }>()
    updateApp.mockReturnValue(sync.promise)
    renderDetail()

    fireEvent.click(await screen.findByRole('button', { name: /^Sync$/i }))
    await waitFor(() => expect(updateApp).toHaveBeenCalledWith('secretary'))
    expect(button(/^Sync$/i)).toBeDisabled()
    expect(button(/^Disable$/i)).toBeDisabled()
    expect(button(/^Uninstall$/i)).toBeDisabled()

    sync.resolve({ ok: true })
    await waitFor(() => expect(button(/^Disable$/i)).toBeEnabled())
    expect(button(/^Uninstall$/i)).toBeEnabled()
    expect(button(/^Sync$/i)).toBeEnabled()
  })
})
