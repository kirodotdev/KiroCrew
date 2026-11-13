/**
 * LibraryPage — every app management verb is refused while the gateway is
 * offline (#10463): the control dims, says why in its own tooltip, and its
 * click fires no request, instead of a write that cannot land.
 *
 * Driven through the page: the Enable button stays focusable, so its click
 * reaches the page's handler, which refuses; menu rows refuse through
 * offlineMenuRow and keep the menu open; the uninstall confirm is disabled.
 * The connected path of the same controls is pinned by LibraryPageLaunchpad
 * and AppsPageW3Coverage, which render on a connected store.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen, fireEvent, within } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { createConnectedTestStore, createTestStore } from './helpers'
import { sseDisconnected } from '../store/dashboardSlice'

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

const OFFLINE_TITLE = 'Gateway offline — reconnect to manage apps'

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

describe('LibraryPage offline', () => {
  it("a disabled app's Enable button is inert, says why, and enables nothing", async () => {
    renderLibrary(createTestStore())
    const enable = within(await screen.findByTestId('launchpad-tile-weather'))
      .getByRole('button', { name: 'Enable disabled — gateway offline' })
    expect(enable).toHaveAttribute('aria-disabled', 'true')
    expect(enable).toHaveAttribute('title', OFFLINE_TITLE)
    fireEvent.click(enable)
    expect(enableApp).not.toHaveBeenCalled()
  })

  it('the tile menu dims its management verbs, names why, and fires none of them', async () => {
    renderLibrary(createTestStore())
    const scope = within(await screen.findByTestId('launchpad-tile-secretary'))
    fireEvent.click(scope.getByRole('button', { name: 'More actions for Secretary' }))
    const disable = scope.getByRole('menuitem', { name: 'Disable disabled — gateway offline' })
    const uninstall = scope.getByRole('menuitem', { name: 'Uninstall disabled — gateway offline' })
    expect(disable).toHaveAttribute('aria-disabled', 'true')
    expect(uninstall).toHaveAttribute('title', OFFLINE_TITLE)
    expect(scope.getByTestId('app-tile-offline-reason')).toBeInTheDocument()
    fireEvent.click(disable)
    fireEvent.click(uninstall)
    // A refused row keeps the menu open; an accepted one would close it.
    expect(uninstall).toBeInTheDocument()
    expect(disableApp).not.toHaveBeenCalled()
    expect(uninstallPreview).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog', { name: 'Confirm uninstall' })).toBeNull()
  })

  it('an uninstall confirm left open across a disconnect refuses to remove the app', async () => {
    const store = createConnectedTestStore()
    renderLibrary(store)
    const scope = within(await screen.findByTestId('launchpad-tile-weather'))
    fireEvent.click(scope.getByRole('button', { name: 'More actions for Weather' }))
    fireEvent.click(scope.getByRole('menuitem', { name: 'Uninstall' }))
    const dialog = await screen.findByRole('dialog', { name: 'Confirm uninstall' })
    act(() => { store.dispatch(sseDisconnected()) })
    const confirm = await within(dialog).findByRole('button', { name: 'Uninstall disabled — gateway offline' })
    expect(confirm).toBeDisabled()
    expect(confirm).toHaveAttribute('title', OFFLINE_TITLE)
    fireEvent.click(confirm)
    expect(uninstallApp).not.toHaveBeenCalled()
  })
})
