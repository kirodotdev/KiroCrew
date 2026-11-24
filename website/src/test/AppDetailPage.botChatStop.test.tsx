/**
 * AppDetailPage -- a disable or uninstall that could not stop every Bot chat
 * running the app's tools says so, with the Library's notice and retry.
 *
 * Both routes answer `bot_chat_stop_failed` on a landed action. Hand the disable
 * result to `appActionNotice` alone and the page shows nothing; navigate away
 * after an uninstall without carrying it and the notice is lost.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createConnectedTestStore } from './helpers'

vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))

const getApp = vi.fn()
const disableApp = vi.fn()
const uninstallApp = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    getApp: (...a: unknown[]) => getApp(...a),
    listRegistry: vi.fn(async () => []),
    listRegistries: vi.fn(async () => []),
    listApps: vi.fn(async () => []),
    system: vi.fn(async () => ({ hostname: '' })),
    disableApp: (...a: unknown[]) => disableApp(...a),
    uninstallApp: (...a: unknown[]) => uninstallApp(...a),
    uninstallPreview: vi.fn(async () => ({ dependencies: { removable: [], shared: [], userInstalled: [] } })),
    updateApp: vi.fn(),
    updateRegistries: vi.fn(),
    refreshRegistries: vi.fn(),
    installFromRegistryStream: vi.fn(),
    installApp: vi.fn(),
    openApp: vi.fn(),
    trustApp: vi.fn(),
    untrustApp: vi.fn(),
    enableApp: vi.fn(),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'light' }) }))
vi.mock('../components/AppIcon', () => ({ default: () => <div data-testid="app-icon" /> }))

import AppDetailPage from '../pages/AppDetailPage'
import LibraryPage from '../pages/apps/LibraryPage'

function renderDetail() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  return render(
    <Provider store={createConnectedTestStore()}>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/apps/detail/secretary']}>
          <Routes>
            <Route path="/apps/detail/:name" element={<AppDetailPage />} />
            <Route path="/apps/library" element={<LibraryPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

const retry = (verb: string) => screen.findByRole('button', { name: `Remove ${verb}'s tools from the bot "atlas"` })

describe('AppDetailPage -- Bot chats a stop did not end', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    getApp.mockResolvedValue({
      name: 'secretary', displayName: 'Secretary', version: '1.0.0', enabled: true,
      source: 'registry:secretary', origin: 'registry',
      resources: 'gateway', lifecycle: 'gateway', installedAt: '2026-07-01T00:00:00Z',
      manifest: { displayName: 'Secretary', description: 'Slack inbox manager.', author: 'zezhexu' },
    })
  })

  it('shows the notice and its retry after a disable, and the retry disables again', async () => {
    disableApp.mockResolvedValue({ ok: true, name: 'secretary', enabled: false, bot_chat_stop_failed: ['atlas'] })
    renderDetail()
    fireEvent.click(await screen.findByRole('button', { name: /^Disable/ }))
    expect(await screen.findByText(/^Secretary is disabled, but the bot "atlas" still has its tools\./)).toBeInTheDocument()
    fireEvent.click(await retry('Secretary'))
    await waitFor(() => expect(disableApp).toHaveBeenCalledTimes(2))
  })

  it('drops the disable notice once the app is enabled again, so its retry cannot disable it again', async () => {
    // Keep the notice and "Remove ... tools" re-runs the disable on an app the
    // person just turned back on.
    disableApp.mockResolvedValue({ ok: true, name: 'secretary', enabled: false, bot_chat_stop_failed: ['atlas'] })
    const base = await getApp()
    renderDetail()
    getApp.mockResolvedValue({ ...base, enabled: false })
    fireEvent.click(await screen.findByRole('button', { name: /^Disable/ }))
    expect(await retry('Secretary')).toBeInTheDocument()
    getApp.mockResolvedValue({ ...base, enabled: true })
    fireEvent.click(await screen.findByRole('button', { name: /^Enable/ }))
    await waitFor(() => expect(screen.queryByText(/still has its tools/)).toBeNull())
    expect(disableApp).toHaveBeenCalledTimes(1)
  })

  it('carries the notice to the Library after an uninstall', async () => {
    uninstallApp.mockResolvedValue({ ok: true, name: 'secretary', bot_chat_stop_failed: ['atlas'] })
    renderDetail()
    fireEvent.click((await screen.findAllByRole('button', { name: /^Uninstall/ }))[0])
    const dialog = await screen.findByRole('dialog', { name: 'Confirm uninstall' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Uninstall' }))
    expect(await screen.findByText(/^Secretary is uninstalled, but the bot "atlas" still has its tools\./)).toBeInTheDocument()
    expect(await retry('Secretary')).toBeInTheDocument()
  })
})
