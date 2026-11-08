/**
 * LibraryPage acting on a request from the sidebar's app menu
 * (`pages/apps/libraryRailIntent.ts`). The menu sends Open in Library, Disable
 * and Uninstall here as router state, so this file pins what the page does
 * with each:
 *
 * - every request focuses the named app's tile face and is cleared from the
 *   history entry, so a reload or Back does not run it again;
 * - Disable disables the app through the page's own handler, says where the
 *   app went and how to turn it back on, and puts focus on the tile's Enable
 *   button. A Disable from the tile's own menu shows no such notice;
 * - Uninstall opens the page's confirmation dialog and loads its dependency
 *   preview, and removes nothing until that dialog is confirmed;
 * - a request naming an app that is not installed is dropped;
 * - a Disable or Uninstall that arrives while another action runs on the page
 *   runs nothing, as the tile menu's own lock does, and a notice says why;
 * - an app whose tile crashed still takes the request, on the fallback card's
 *   recovery button, so the sidebar's Disable works for the app that most
 *   needs it.
 *
 * The sidebar side, which sends these requests, is pinned at App level in
 * `App.appRailContextMenu.test.tsx`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route, useLocation, useNavigate } from 'react-router-dom'
import { Provider } from 'react-redux'
import { createConnectedTestStore } from './helpers'
import { libraryRailState, type LibraryRailIntent } from '../pages/apps/libraryRailIntent'

vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))

const listApps = vi.fn()
const disableApp = vi.fn()
const uninstallApp = vi.fn()
const uninstallPreview = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    listApps: (...a: unknown[]) => listApps(...a),
    listRegistry: vi.fn().mockResolvedValue({ apps: [] }),
    listRegistries: vi.fn().mockResolvedValue({ registries: [] }),
    updateRegistries: vi.fn(),
    refreshRegistries: vi.fn(),
    enableApp: vi.fn().mockResolvedValue({ ok: true }),
    disableApp: (...a: unknown[]) => disableApp(...a),
    updateApp: vi.fn(),
    uninstallApp: (...a: unknown[]) => uninstallApp(...a),
    uninstallPreview: (...a: unknown[]) => uninstallPreview(...a),
    installApp: vi.fn(),
    openApp: vi.fn(),
    trustApp: vi.fn(),
    untrustApp: vi.fn(),
    getApp: vi.fn(),
  },
}))
vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark' }) }))
// The app named `broken` has a tile that throws while rendering, so the page's
// ErrorBoundary shows its fallback card. Every other tile is the real one.
vi.mock('../pages/apps/LaunchpadTile', async () => {
  const actual = await vi.importActual<typeof import('../pages/apps/LaunchpadTile')>('../pages/apps/LaunchpadTile')
  const Real = actual.default
  return {
    ...actual,
    default: (props: Parameters<typeof Real>[0]) => {
      if (props.app.name === 'broken') throw new Error('tile crashed')
      return <Real {...props} />
    },
  }
})

import LibraryPage from '../pages/apps/LibraryPage'

function installedApp(name: string, displayName: string) {
  return {
    name, displayName, version: '1.0.0', enabled: true,
    installedAt: '2026-08-01T00:00:00Z', origin: 'registry', resources: 'gateway', lifecycle: 'gateway',
    manifest: {
      name, version: '1.0.0', displayName, description: `${displayName} does things.`,
      ui: { pages: [{ route: `/${name}-ui`, label: displayName, icon: 'Bot' }] },
    },
  }
}

/** The history entry's router state, read beside the page. */
function StateProbe() {
  const location = useLocation()
  return <div data-testid="state-probe" data-state={JSON.stringify(location.state ?? null)} />
}

/** Sends a second request to the mounted page, the way the sidebar menu does. */
function RailSender() {
  const navigate = useNavigate()
  const send = (app: string) => () => navigate('/apps/library', { state: libraryRailState({ app, action: 'disable' }) })
  return (
    <>
      <button type="button" onClick={send('secretary')}>send Disable for secretary</button>
      <button type="button" onClick={send('gone')}>send Disable for gone</button>
    </>
  )
}

function renderWithIntent(intent: LibraryRailIntent) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  const view = render(
    <Provider store={createConnectedTestStore()}>
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[{ pathname: '/apps/library', state: libraryRailState(intent) }]}>
        <Routes>
          <Route path="/apps/library" element={<><LibraryPage /><StateProbe /><RailSender /></>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
    </Provider>,
  )
  return { ...view, qc }
}

const tileFace = async (name: string, display: string) =>
  within(await screen.findByTestId(`launchpad-tile-${name}`)).getByRole('button', { name: display })
const stateCleared = () => waitFor(() => expect(screen.getByTestId('state-probe').dataset.state).toBe('null'))

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  listApps.mockResolvedValue([installedApp('secretary', 'Secretary'), installedApp('oncall-radar', 'Oncall Radar')])
  disableApp.mockResolvedValue({ ok: true })
  uninstallApp.mockResolvedValue({ ok: true })
  uninstallPreview.mockResolvedValue({ dependencies: { removable: [], shared: [], userInstalled: [] } })
})

describe('LibraryPage: a request from the sidebar app menu', () => {
  it('Open in Library focuses the app\'s tile, runs nothing, and clears the request', async () => {
    renderWithIntent({ app: 'oncall-radar', action: 'focus' })

    const face = await tileFace('oncall-radar', 'Oncall Radar')
    await waitFor(() => expect(face).toHaveFocus())
    await stateCleared()
    expect(disableApp).not.toHaveBeenCalled()
    expect(uninstallPreview).not.toHaveBeenCalled()
  })

  it('Disable disables the named app once, says how to undo it, and focuses the tile\'s Enable button', async () => {
    const { qc } = renderWithIntent({ app: 'secretary', action: 'disable' })

    await waitFor(() => expect(disableApp).toHaveBeenCalledTimes(1))
    expect(disableApp).toHaveBeenCalledWith('secretary')
    // The app shell's mc:apps-changed listener owns the refetch and is not
    // mounted here, so refetch the way it would, with the app now disabled.
    // The refetch lands before the router clears the request. In that order
    // the request must still run once, or it takes focus back to the face.
    listApps.mockResolvedValue([{ ...installedApp('secretary', 'Secretary'), enabled: false }, installedApp('oncall-radar', 'Oncall Radar')])
    await qc.invalidateQueries({ queryKey: ['apps'] })
    // The notice is announced through its live region as well as shown.
    expect(await screen.findByText(
      'Secretary is off and left the sidebar. Press Enable on its tile to turn it back on.',
      { selector: '[role="status"]' },
    )).toBeInTheDocument()
    const tile = within(await screen.findByTestId('launchpad-tile-secretary'))
    await waitFor(() => expect(tile.getByRole('button', { name: 'Enable' })).toHaveFocus())
    await stateCleared()
  })

  it('a Disable from the tile\'s own menu shows no sidebar notice', async () => {
    renderWithIntent({ app: 'secretary', action: 'focus' })
    const tile = within(await screen.findByTestId('launchpad-tile-secretary'))
    fireEvent.click(tile.getByRole('button', { name: 'More actions for Secretary' }))
    fireEvent.click(tile.getByRole('menuitem', { name: /Disable/ }))

    await waitFor(() => expect(disableApp).toHaveBeenCalledWith('secretary'))
    expect(screen.queryByText(/left the sidebar/)).toBeNull()
  })

  it('Uninstall opens the confirmation dialog with its preview and removes nothing', async () => {
    renderWithIntent({ app: 'secretary', action: 'uninstall' })

    expect(await screen.findByRole('dialog', { name: 'Confirm uninstall' })).toBeInTheDocument()
    await waitFor(() => expect(uninstallPreview).toHaveBeenCalledWith('secretary'))
    await stateCleared()
    expect(uninstallApp).not.toHaveBeenCalled()
  })

  it('a Disable sent while another action runs focuses the tile, disables nothing, and says why', async () => {
    let finish: (value: unknown) => void = () => {}
    disableApp.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    renderWithIntent({ app: 'oncall-radar', action: 'focus' })
    const radar = within(await screen.findByTestId('launchpad-tile-oncall-radar'))
    const trigger = radar.getByRole('button', { name: 'More actions for Oncall Radar' })
    fireEvent.click(trigger)
    fireEvent.click(radar.getByRole('menuitem', { name: /Disable/ }))
    await waitFor(() => expect(disableApp).toHaveBeenCalledWith('oncall-radar'))
    // The closing menu puts focus back on its trigger a frame later. The
    // sidebar cannot be used before that, so the request is sent after it.
    await waitFor(() => expect(trigger).toHaveFocus())

    fireEvent.click(screen.getByRole('button', { name: 'send Disable for secretary' }))

    expect(await screen.findByText(
      'Another app action is still running, so Secretary was left as it is. Try again once that action finishes.',
    )).toBeInTheDocument()
    const face = await tileFace('secretary', 'Secretary')
    await waitFor(() => expect(face).toHaveFocus())
    expect(disableApp).toHaveBeenCalledTimes(1)
    await stateCleared()
    await act(async () => { finish({ ok: true }) })
  })

  it('a Disable for an app whose tile crashed runs on the fallback card, once', async () => {
    const quiet = vi.spyOn(console, 'error').mockImplementation(() => {})
    listApps.mockResolvedValue([installedApp('broken', 'Broken App'), installedApp('secretary', 'Secretary')])
    renderWithIntent({ app: 'broken', action: 'disable' })

    await waitFor(() => expect(disableApp).toHaveBeenCalledWith('broken'))
    expect(disableApp).toHaveBeenCalledTimes(1)
    expect(within(screen.getByTestId('launchpad-tile-broken')).getByText('This app could not be displayed.')).toBeInTheDocument()
    await stateCleared()
    quiet.mockRestore()
  })

  it('drops a request for an app that is not installed, and leaves the search as it was', async () => {
    renderWithIntent({ app: 'oncall-radar', action: 'focus' })
    await stateCleared()
    const search = screen.getByRole('textbox', { name: 'Search library' })
    fireEvent.change(search, { target: { value: 'sec' } })

    fireEvent.click(screen.getByRole('button', { name: 'send Disable for gone' }))

    await stateCleared()
    // A request the page takes clears the search so its tile shows. This one
    // names no installed app, so the page must not touch the search.
    expect(search).toHaveValue('sec')
    expect(disableApp).not.toHaveBeenCalled()
  })
})
