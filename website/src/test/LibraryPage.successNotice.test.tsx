/**
 * LibraryPage — the transient success notice (Sync / Update outcome).
 *
 *  - a newer success is shown for its own full 4s: the timer an earlier
 *    success armed must not wipe it early;
 *  - the notice is a polite live region, so a screen reader hears the outcome
 *    of an action whose result is otherwise invisible (an in-place sync).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, within, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createConnectedTestStore } from './helpers'
import { MemoryRouter } from 'react-router-dom'

vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))

const listApps = vi.fn()
const updateApp = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    listApps: (...a: unknown[]) => listApps(...a),
    listRegistry: vi.fn(async () => ({ apps: [] })),
    listRegistries: vi.fn(async () => ({ registries: [] })),
    updateRegistries: vi.fn(),
    refreshRegistries: vi.fn(),
    enableApp: vi.fn(),
    disableApp: vi.fn(),
    updateApp: (...a: unknown[]) => updateApp(...a),
    uninstallApp: vi.fn(),
    uninstallPreview: vi.fn(),
    installApp: vi.fn(),
    openApp: vi.fn(),
    trustApp: vi.fn(),
    untrustApp: vi.fn(),
    getApp: vi.fn(),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark' }) }))

import LibraryPage from '../pages/apps/LibraryPage'

/** A gateway-lifecycle path install: its menu offers Sync, which updates in place. */
function pathApp(name: string, displayName: string) {
  return {
    name, displayName, version: '1.0.0', enabled: true,
    installedAt: '2026-08-01T00:00:00Z', origin: 'local', source: `/home/u/apps/${name}`,
    resources: 'gateway', lifecycle: 'gateway',
    manifest: {
      name, version: '1.0.0', displayName, description: `${displayName} does things.`,
      author: 'zezhexu', tags: [name],
      ui: { pages: [{ route: `/${name}-ui`, label: displayName, icon: 'Bot' }] },
    },
  }
}

function renderLibrary() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  return render(
    <Provider store={createConnectedTestStore()}>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/apps/library']}>
          <LibraryPage />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

/** Run Sync from a tile's overflow menu and let the update settle. */
async function sync(name: string, display: string) {
  const scope = within(await screen.findByTestId(`launchpad-tile-${name}`))
  fireEvent.click(scope.getByRole('button', { name: `More actions for ${display}` }))
  await act(async () => { fireEvent.click(scope.getByRole('menuitem', { name: 'Sync' })) })
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  listApps.mockResolvedValue([pathApp('secretary', 'Secretary'), pathApp('radar', 'Radar')])
  updateApp.mockResolvedValue({ ok: true })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('LibraryPage — success notice', () => {
  it('keeps a newer success for its full 4s after an earlier one', async () => {
    renderLibrary()
    await screen.findByTestId('launchpad-tile-radar')
    vi.useFakeTimers({ shouldAdvanceTime: true })

    const notice = () => screen.getByRole('status')

    await sync('secretary', 'Secretary')
    expect(notice()).toHaveTextContent('Synced Secretary from its source directory.')

    await act(async () => { vi.advanceTimersByTime(3000) })
    await sync('radar', 'Radar')
    expect(notice()).toHaveTextContent('Synced Radar from its source directory.')

    // 4s after the FIRST success: its timer must not clear the second one.
    await act(async () => { vi.advanceTimersByTime(1500) })
    expect(notice()).toHaveTextContent('Synced Radar from its source directory.')

    // 4s after the second, it goes.
    await act(async () => { vi.advanceTimersByTime(3000) })
    expect(notice()).toHaveTextContent('')
    expect(screen.queryByRole('button', { name: 'Dismiss message' })).toBeNull()
  })

  it('announces the success through a live region that is mounted before it', async () => {
    renderLibrary()
    await screen.findByTestId('launchpad-tile-secretary')
    // Present and empty before any action: a region inserted already holding
    // its text is not announced by every screen reader.
    const region = screen.getByRole('status')
    expect(region).toHaveTextContent('')

    await sync('secretary', 'Secretary')
    expect(screen.getByRole('status')).toBe(region)
    expect(region).toHaveTextContent('Synced Secretary from its source directory.')
    // The visible copy is hidden from AT, so the message is read once.
    expect(screen.getAllByText('Synced Secretary from its source directory.')
      .filter(el => el !== region && !el.closest('[aria-hidden="true"]'))).toHaveLength(0)
  })
})
