/**
 * LibraryPage — a disable that landed with gateway warnings says so.
 *
 * The disable route still succeeds when a hook, the config sync or the
 * service notify fails, and names that failure in `warnings`; the tile menu's
 * Disable must show it in the page notice instead of dropping it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { createConnectedTestStore, createTestStore } from './helpers'

// happy-dom cannot drive real Radix menus — swap in the repo's stateful mock.
vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))

const listApps = vi.fn()
const enableApp = vi.fn()
const disableApp = vi.fn()
const uninstallApp = vi.fn()
const uninstallPreview = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    listApps: (...a: unknown[]) => listApps(...a),
    listRegistry: vi.fn().mockResolvedValue({ apps: [] }),
    listRegistries: vi.fn().mockResolvedValue({ registries: [] }),
    enableApp: (...a: unknown[]) => enableApp(...a),
    disableApp: (...a: unknown[]) => disableApp(...a),
    updateApp: vi.fn(),
    uninstallApp: (...a: unknown[]) => uninstallApp(...a),
    uninstallPreview: (...a: unknown[]) => uninstallPreview(...a),
    openApp: vi.fn(),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark' }) }))

import LibraryPage from '../pages/apps/LibraryPage'

function installedApp(name: string, displayName: string, enabled: boolean) {
  return {
    name, displayName, version: '1.0.0', enabled,
    installedAt: '2026-08-01T00:00:00Z', origin: 'registry', resources: 'gateway', lifecycle: 'gateway',
    manifest: {
      name, version: '1.0.0', displayName, description: `${displayName} does things.`,
      ui: { pages: [{ route: `/${name}-ui`, label: displayName, icon: 'Bot' }] },
    },
  }
}

function renderLibrary(store: ReturnType<typeof createTestStore>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/apps/library']}>
          <LibraryPage />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  listApps.mockResolvedValue([installedApp('secretary', 'Secretary', true), installedApp('weather', 'Weather', false)])
  enableApp.mockResolvedValue({ ok: true })
  disableApp.mockResolvedValue({ ok: true })
  uninstallApp.mockResolvedValue({ ok: true })
  uninstallPreview.mockResolvedValue({ dependencies: { removable: [], shared: [], userInstalled: [] } })
})

describe('LibraryPage disable warnings', () => {
  it('shows the warning the disable response carried', async () => {
    disableApp.mockResolvedValue({ name: 'secretary', enabled: false, warnings: ['config sync failed: permission denied'] })
    renderLibrary(createConnectedTestStore())
    const scope = within(await screen.findByTestId('launchpad-tile-secretary'))
    fireEvent.click(scope.getByRole('button', { name: 'More actions for Secretary' }))
    fireEvent.click(scope.getByRole('menuitem', { name: 'Disable' }))
    await waitFor(() => expect(disableApp).toHaveBeenCalledWith('secretary'))
    expect(await screen.findByText(/Secretary is disabled, but not every step finished\. Details: config sync failed: permission denied/)).toBeInTheDocument()
  })
})
