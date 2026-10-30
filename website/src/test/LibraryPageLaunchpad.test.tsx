/**
 * LibraryPage — the launchpad grid contract (PR3 App Store split, approved
 * mockup frame #a, redesigned action bar per review findings).
 *
 * The Library list is an icon GRID of LaunchpadTile, one tile per installed
 * app, split into an Enabled section and, below it, a Disabled section. The
 * tile face (icon + name) is the open control. Under it one row carries the
 * pin caption (or a disabled app's Enable button) and an overflow menu
 * (Details, Pin/Unpin, Update, Disable/Enable, Uninstall). Pin/Unpin persists
 * through the `appNavHidden` module (`mc-app-nav-hidden` +
 * `mc:app-nav-hidden-changed`). This file pins that surface:
 *
 *  - one tile per `installedApps` entry, each in the section its live
 *    enabled state names, with headings only when both kinds exist;
 *  - menu Pin/Unpin WRITES the hidden set, DISPATCHES the sync event, and
 *    the caption follows it;
 *  - ids absent from storage — and a malformed stored value — read as
 *    pinned ("In sidebar"), so a fresh install needs no migration;
 *  - an enabled app with no sidebar destination captions "No sidebar page"
 *    and offers no pin affordance;
 *  - a disabled app renders greyscale with an Enable button, and its menu
 *    offers Details/Enable/Uninstall;
 *  - Uninstall is absent from the menu for lifecycle 'locked';
 *  - the tile face runs `api.openApp` for an `openCommand` app (remote
 *    answers show the remote-command banner) and navigates otherwise;
 *  - the menu verbs dispatch to the page's hooks, not per-tile logic;
 *  - search filters tiles.
 *
 * Radix DropdownMenu cannot be driven in happy-dom, so the suite routes it
 * through the repo's stateful mock (same pattern as
 * FileExplorerPageCoverage.test.tsx): Trigger click toggles, items render
 * inline with role="menuitem" and respond to fireEvent.click.
 *
 * `AppsPageW3Coverage.test.tsx` owns the action FAILURE branches, the
 * uninstall dialog internals, and the updates hint row; the sidebar side of
 * the pin contract lives at App level (App.appNavHiddenFilter.test.tsx).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import {
  APP_NAV_HIDDEN_CHANGED_EVENT, APP_NAV_HIDDEN_KEY,
} from '../lib/appNavHidden'

// happy-dom cannot drive real Radix menus — swap in the repo's stateful mock.
vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))

// --- Mocks -----------------------------------------------------------------
const listApps = vi.fn()
const listRegistry = vi.fn()
const listRegistries = vi.fn()
const enableApp = vi.fn()
const disableApp = vi.fn()
const updateApp = vi.fn()
const uninstallApp = vi.fn()
const uninstallPreview = vi.fn()
const openApp = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    listApps: (...a: unknown[]) => listApps(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    listRegistries: (...a: unknown[]) => listRegistries(...a),
    updateRegistries: vi.fn(),
    refreshRegistries: vi.fn(),
    enableApp: (...a: unknown[]) => enableApp(...a),
    disableApp: (...a: unknown[]) => disableApp(...a),
    updateApp: (...a: unknown[]) => updateApp(...a),
    uninstallApp: (...a: unknown[]) => uninstallApp(...a),
    uninstallPreview: (...a: unknown[]) => uninstallPreview(...a),
    installApp: vi.fn(),
    openApp: (...a: unknown[]) => openApp(...a),
    trustApp: vi.fn(),
    untrustApp: vi.fn(),
    getApp: vi.fn(),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark' }) }))

import LibraryPage from '../pages/apps/LibraryPage'

/** Route probe for tile-face navigation. */
function RouteProbe() {
  const loc = useLocation()
  return <div data-testid="route-probe" data-path={loc.pathname} />
}

/** An installed, enabled third-party app with one UI page (nav id `app-<name>`). */
function installedApp(name: string, displayName: string, over: Record<string, unknown> = {}) {
  return {
    name, displayName, version: '1.0.0', enabled: true,
    installedAt: '2026-08-01T00:00:00Z', origin: 'registry', resources: 'gateway', lifecycle: 'gateway',
    manifest: {
      name, version: '1.0.0', displayName, description: `${displayName} does things.`,
      author: 'zezhexu', tags: [name],
      ui: { pages: [{ route: `/${name}-ui`, label: displayName, icon: 'Bot' }] },
    },
    ...over,
  }
}

const SECRETARY = installedApp('secretary', 'Secretary')
const RADAR = installedApp('oncall-radar', 'Oncall Radar')

function renderLibrary(
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } }),
) {
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/apps/library']}>
        <Routes>
          <Route path="/apps/library" element={<LibraryPage />} />
          <Route path="*" element={<RouteProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const tile = (name: string) => screen.findByTestId(`launchpad-tile-${name}`)

/** Open a tile's overflow menu (the MoreHorizontal trigger) and return the tile scope. */
async function openTileMenu(name: string, display: string) {
  const scope = within(await tile(name))
  fireEvent.click(scope.getByRole('button', { name: `More actions for ${display}` }))
  // The mock renders the portal inline, so items live inside the tile scope.
  return scope
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  listApps.mockResolvedValue([SECRETARY, RADAR])
  listRegistry.mockResolvedValue({ apps: [] })
  listRegistries.mockResolvedValue({ registries: [] })
  enableApp.mockResolvedValue({ ok: true })
  disableApp.mockResolvedValue({ ok: true })
  updateApp.mockResolvedValue({ ok: true })
  uninstallApp.mockResolvedValue({ ok: true })
  uninstallPreview.mockResolvedValue({ dependencies: { removable: [], shared: [], userInstalled: [] } })
  openApp.mockResolvedValue({ ok: true })
})

afterEach(() => {
  localStorage.clear()
})

describe('LibraryPage — launchpad grid', () => {
  it('renders one tile per installed app', async () => {
    renderLibrary()
    expect(await tile('secretary')).toBeInTheDocument()
    expect(await tile('oncall-radar')).toBeInTheDocument()
    expect(screen.getAllByTestId(/^launchpad-tile-/)).toHaveLength(2)
  })

  it('search filters tiles down to the matches', async () => {
    renderLibrary()
    await tile('secretary')
    fireEvent.change(screen.getByLabelText('Search library'), { target: { value: 'radar' } })
    await waitFor(() => expect(screen.queryByTestId('launchpad-tile-secretary')).toBeNull())
    expect(screen.getByTestId('launchpad-tile-oncall-radar')).toBeInTheDocument()
  })

  it('a tile has no Open button and no pin badge: the face opens it, the menu pins it', async () => {
    renderLibrary()
    const scope = within(await tile('secretary'))
    expect(scope.queryByRole('button', { name: 'Open' })).toBeNull()
    expect(scope.queryByRole('button', { name: /the sidebar$/ })).toBeNull()
    expect(scope.getByRole('button', { name: 'Secretary' })).toBeInTheDocument()
  })
})

describe('LibraryPage — Enabled and Disabled sections', () => {
  const OFF = installedApp('weather', 'Weather', { enabled: false })

  it('lists disabled apps in their own section below the enabled ones, always shown', async () => {
    listApps.mockResolvedValue([OFF, SECRETARY, RADAR])
    renderLibrary()
    const disabled = await screen.findByTestId('library-section-disabled')
    const enabled = screen.getByTestId('library-section-enabled')
    expect(within(disabled).getByTestId('launchpad-tile-weather')).toBeInTheDocument()
    expect(within(disabled).queryByTestId('launchpad-tile-secretary')).toBeNull()
    expect(within(enabled).getAllByTestId(/^launchpad-tile-/)).toHaveLength(2)
    // Below, in document order: the Disabled section follows the Enabled one.
    expect(enabled.compareDocumentPosition(disabled) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    const [enabledHeader, disabledHeader] = screen.getAllByTestId('panel-section-header')
    expect(enabledHeader).toHaveTextContent('Enabled2')
    expect(disabledHeader).toHaveTextContent('Disabled1')
    expect(screen.getByRole('region', { name: 'Disabled' })).toContainElement(disabled)
    // No view toggle to find first.
    expect(screen.queryByRole('button', { name: /disabled app/ })).toBeNull()
  })

  it('shows no headings when every app is enabled', async () => {
    renderLibrary()
    await tile('secretary')
    expect(screen.queryByTestId('panel-section-header')).toBeNull()
    expect(screen.queryByTestId('library-section-disabled')).toBeNull()
  })

  it('says nothing is enabled, and still lists the disabled apps, on a fresh install', async () => {
    listApps.mockResolvedValue([OFF])
    renderLibrary()
    expect(await screen.findByText('No enabled apps')).toBeInTheDocument()
    expect(within(screen.getByTestId('library-section-disabled')).getByTestId('launchpad-tile-weather')).toBeInTheDocument()
  })

  it('search filters both sections and drops one with no match', async () => {
    listApps.mockResolvedValue([OFF, SECRETARY])
    renderLibrary()
    await tile('weather')
    fireEvent.change(screen.getByLabelText('Search library'), { target: { value: 'weather' } })
    await waitFor(() => expect(screen.queryByTestId('launchpad-tile-secretary')).toBeNull())
    expect(screen.queryByTestId('library-section-enabled')).toBeNull()
    expect(screen.queryByText('No enabled apps')).toBeNull()
    expect(within(screen.getByTestId('library-section-disabled')).getByTestId('launchpad-tile-weather')).toBeInTheDocument()
  })

  it('a refetch after enabling moves the tile up into the Enabled section', async () => {
    listApps.mockResolvedValue([OFF, SECRETARY])
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    renderLibrary(qc)
    fireEvent.click(within(await tile('weather')).getByRole('button', { name: 'Enable' }))
    await waitFor(() => expect(enableApp).toHaveBeenCalledWith('weather'))
    // The app shell's mc:apps-changed listener owns the refetch and is not
    // mounted here, so invalidate the way it would.
    listApps.mockResolvedValue([{ ...OFF, enabled: true }, SECRETARY])
    await qc.invalidateQueries({ queryKey: ['apps'] })
    await waitFor(() => expect(screen.queryByTestId('library-section-disabled')).toBeNull())
    expect(within(screen.getByTestId('library-section-enabled')).getByTestId('launchpad-tile-weather')).toBeInTheDocument()
  })

  it('keeps keyboard focus on the tile while it enables and after it moves section', async () => {
    let resolveEnable: (v: unknown) => void = () => {}
    enableApp.mockReturnValue(new Promise(res => { resolveEnable = res }))
    listApps.mockResolvedValue([OFF, SECRETARY])
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    renderLibrary(qc)
    const enable = within(await tile('weather')).getByRole('button', { name: 'Enable' })
    enable.focus()
    fireEvent.click(enable)
    // In flight: the same button, relabelled, still holds focus.
    const working = await within(screen.getByTestId('launchpad-tile-weather')).findByRole('button', { name: 'Working…' })
    expect(working).toBe(enable)
    expect(working).toHaveAttribute('aria-disabled', 'true')
    expect(document.activeElement).toBe(enable)
    fireEvent.click(working)
    expect(enableApp).toHaveBeenCalledTimes(1)
    // Landed: the tile remounts in the Enabled section, and focus follows it
    // to its face instead of falling to the page.
    listApps.mockResolvedValue([{ ...OFF, enabled: true }, SECRETARY])
    resolveEnable({ ok: true })
    await waitFor(() => expect(enableApp).toHaveBeenCalledWith('weather'))
    await qc.invalidateQueries({ queryKey: ['apps'] })
    const face = await within(await screen.findByTestId('library-section-enabled'))
      .findByRole('button', { name: 'Weather' })
    await waitFor(() => expect(document.activeElement).toBe(face))
  })

  it('leaves focus alone when the reader moved it elsewhere before the tile landed', async () => {
    listApps.mockResolvedValue([OFF, SECRETARY])
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    renderLibrary(qc)
    fireEvent.click(within(await tile('weather')).getByRole('button', { name: 'Enable' }))
    await waitFor(() => expect(enableApp).toHaveBeenCalledWith('weather'))
    const search = screen.getByLabelText('Search library')
    search.focus()
    listApps.mockResolvedValue([{ ...OFF, enabled: true }, SECRETARY])
    await qc.invalidateQueries({ queryKey: ['apps'] })
    await waitFor(() => expect(screen.queryByTestId('library-section-disabled')).toBeNull())
    expect(document.activeElement).toBe(search)
  })
})

describe('LibraryPage — pin caption and persistence', () => {
  it('an id absent from storage defaults to pinned', async () => {
    renderLibrary()
    const scope = await openTileMenu('secretary', 'Secretary')
    expect(scope.getByText('In sidebar')).toBeInTheDocument()
    expect(scope.getByRole('menuitem', { name: 'Unpin' })).toBeInTheDocument()
  })

  it('a malformed stored value is treated as the empty set (everything pinned)', async () => {
    localStorage.setItem(APP_NAV_HIDDEN_KEY, '{broken json[')
    renderLibrary()
    const scope = await openTileMenu('secretary', 'Secretary')
    expect(scope.getByText('In sidebar')).toBeInTheDocument()
    expect(scope.getByRole('menuitem', { name: 'Unpin' })).toBeInTheDocument()
  })

  it('menu Unpin writes mc-app-nav-hidden, dispatches the sync event, and flips the caption', async () => {
    const synced = vi.fn()
    window.addEventListener(APP_NAV_HIDDEN_CHANGED_EVENT, synced)
    try {
      renderLibrary()
      const scope = await openTileMenu('secretary', 'Secretary')
      fireEvent.click(scope.getByRole('menuitem', { name: 'Unpin' }))

      // Persistence: the sidebar nav id (`app-<name>` for an AppHost-routed
      // app) lands in the HIDDEN set, and same-tab listeners are notified —
      // the only path by which the sidebar filter (shell/nav/appRail.tsx)
      // learns of it.
      expect(JSON.parse(localStorage.getItem(APP_NAV_HIDDEN_KEY)!)).toEqual(['app-secretary'])
      expect(synced).toHaveBeenCalled()
      expect(screen.queryByTestId('route-probe')).toBeNull()

      // The tile repaints from the event: unpinned caption.
      expect(await scope.findByText('Not in sidebar')).toBeInTheDocument()
      // The other tile's pin state is untouched.
      expect(within(screen.getByTestId('launchpad-tile-oncall-radar')).getByText('In sidebar')).toBeInTheDocument()

      // Re-open: the item now reads Pin, and pinning empties the stored set again.
      fireEvent.click(scope.getByRole('button', { name: 'More actions for Secretary' }))
      fireEvent.click(await scope.findByRole('menuitem', { name: 'Pin' }))
      expect(JSON.parse(localStorage.getItem(APP_NAV_HIDDEN_KEY)!)).toEqual([])
      expect(await scope.findByText('In sidebar')).toBeInTheDocument()
    } finally {
      window.removeEventListener(APP_NAV_HIDDEN_CHANGED_EVENT, synced)
    }
  })

  it('an enabled app with no sidebar destination captions "No sidebar page" and offers no pin', async () => {
    // No UI pages and no openCommand: appNavTarget is null, so pinnable is
    // false — the caption must SAY so instead of silently showing nothing.
    listApps.mockResolvedValue([installedApp('headless', 'Headless', {
      manifest: {
        name: 'headless', version: '1.0.0', displayName: 'Headless',
        description: 'No UI.', author: 'zezhexu', tags: [],
      },
    })])
    renderLibrary()
    const el = await tile('headless')
    const scope = within(el)
    expect(scope.getByText('No sidebar page')).toBeInTheDocument()
    expect(scope.queryByText('In sidebar')).toBeNull()
    // No pin control on the tile, and no Pin/Unpin in the menu.
    expect(scope.queryByRole('button', { name: /the sidebar$/ })).toBeNull()
    fireEvent.click(scope.getByRole('button', { name: 'More actions for Headless' }))
    expect(scope.queryByRole('menuitem', { name: 'Pin' })).toBeNull()
    expect(scope.queryByRole('menuitem', { name: 'Unpin' })).toBeNull()
  })

  it('shows an in-flight caption while a menu action runs (the menu closes on click)', async () => {
    // The overflow menu closes as soon as Disable is picked, so the caption
    // is the tile's only in-view signal that the action is running — without
    // it a multi-second Update/Disable reads as a no-op and invites a
    // re-click through the reopened menu.
    let resolveDisable: (v: unknown) => void = () => {}
    disableApp.mockReturnValue(new Promise(res => { resolveDisable = res }))
    renderLibrary()
    const el = await tile('secretary')
    const scope = within(el)
    fireEvent.click(scope.getByRole('button', { name: 'More actions for Secretary' }))
    fireEvent.click(await scope.findByRole('menuitem', { name: 'Disable' }))
    expect(await scope.findByText('Working…')).toBeInTheDocument()
    // Settle the promise so the run's teardown has no dangling act() work.
    resolveDisable({})
  })
})

describe('LibraryPage — disabled tiles', () => {
  beforeEach(() => {
    listApps.mockResolvedValue([installedApp('secretary', 'Secretary', { enabled: false }), RADAR])
  })

  it('renders greyscale with an Enable button, and the menu offers Details/Enable/Uninstall', async () => {
    renderLibrary()
    const el = await tile('secretary')
    const scope = within(el)
    // Greyscale icon at reduced opacity (mockup frame #a's disabled state).
    expect(el.querySelector('.grayscale.opacity-45')).not.toBeNull()
    // The Enable button takes the caption's place: a disabled app is not in
    // the sidebar regardless of the stored pin.
    expect(scope.getByRole('button', { name: 'Enable' })).toBeInTheDocument()
    expect(scope.queryByText('In sidebar')).toBeNull()
    // Menu verbs: Details + Enable + Uninstall; no Disable, no Pin/Unpin.
    fireEvent.click(scope.getByRole('button', { name: 'More actions for Secretary' }))
    expect(scope.getByRole('menuitem', { name: 'Details' })).toBeInTheDocument()
    expect(scope.getByRole('menuitem', { name: 'Enable' })).toBeInTheDocument()
    expect(scope.getByRole('menuitem', { name: 'Uninstall' })).toBeInTheDocument()
    expect(scope.queryByRole('menuitem', { name: 'Disable' })).toBeNull()
    expect(scope.queryByRole('menuitem', { name: 'Pin' })).toBeNull()
    expect(scope.queryByRole('menuitem', { name: 'Unpin' })).toBeNull()
    // The enabled sibling keeps its caption — the swap is per-tile.
    expect(within(screen.getByTestId('launchpad-tile-oncall-radar')).getByText('In sidebar')).toBeInTheDocument()
  })

  it('the Enable button dispatches to the enable hook, then carries the in-flight caption', async () => {
    let resolveEnable: (v: unknown) => void = () => {}
    enableApp.mockReturnValue(new Promise(res => { resolveEnable = res }))
    renderLibrary()
    const scope = within(await tile('secretary'))
    fireEvent.click(scope.getByRole('button', { name: 'Enable' }))
    await waitFor(() => expect(enableApp).toHaveBeenCalledWith('secretary'))
    expect(await scope.findByRole('button', { name: 'Working…' })).toHaveAttribute('aria-disabled', 'true')
    resolveEnable({ ok: true })
  })

  it('a failed enable puts the Enable button back, still the focused control', async () => {
    let rejectEnable: (e: unknown) => void = () => {}
    enableApp.mockReturnValue(new Promise((_, rej) => { rejectEnable = rej }))
    renderLibrary()
    const scope = within(await tile('secretary'))
    const enable = scope.getByRole('button', { name: 'Enable' })
    enable.focus()
    fireEvent.click(enable)
    await scope.findByRole('button', { name: 'Working…' })
    rejectEnable(new Error('boom'))
    expect(await scope.findByRole('button', { name: 'Enable' })).toBe(enable)
    expect(document.activeElement).toBe(enable)
    expect(enable).not.toHaveAttribute('aria-disabled')
  })

  it('Enable in the menu dispatches to the enable hook', async () => {
    renderLibrary()
    const scope = await openTileMenu('secretary', 'Secretary')
    fireEvent.click(scope.getByRole('menuitem', { name: 'Enable' }))
    await waitFor(() => expect(enableApp).toHaveBeenCalledWith('secretary'))
  })
})

describe('LibraryPage — action dispatch', () => {
  it('the tile face navigates to the app’s nav-target route', async () => {
    renderLibrary()
    fireEvent.click(within(await tile('secretary')).getByRole('button', { name: 'Secretary' }))
    expect(await screen.findByTestId('route-probe')).toHaveAttribute('data-path', '/apps/secretary')
    // A route-navigated open never touches the open endpoint.
    expect(openApp).not.toHaveBeenCalled()
  })

  it('the tile face of an openCommand app runs api.openApp instead of navigating', async () => {
    listApps.mockResolvedValue([installedApp('term-tool', 'Term Tool', {
      manifest: {
        name: 'term-tool', version: '1.0.0', displayName: 'Term Tool',
        description: 'Opens a terminal.', author: 'zezhexu', tags: [],
        openCommand: 'term-tool open',
      },
    })])
    renderLibrary()
    fireEvent.click(within(await tile('term-tool')).getByRole('button', { name: 'Term Tool' }))
    await waitFor(() => expect(openApp).toHaveBeenCalledWith('term-tool'))
    // No navigation — an openCommand app opens by RUNNING its command; the
    // `/apps/<name>` fallback would land on a detail page pretending to be
    // the app.
    expect(screen.queryByTestId('route-probe')).toBeNull()
  })

  it('a remote gateway answer shows the run-locally banner with the command', async () => {
    openApp.mockResolvedValue({ remote: true, command: 'kirocrew app open term-tool' })
    listApps.mockResolvedValue([installedApp('term-tool', 'Term Tool', {
      manifest: {
        name: 'term-tool', version: '1.0.0', displayName: 'Term Tool',
        description: 'Opens a terminal.', author: 'zezhexu', tags: [],
        openCommand: 'term-tool open',
      },
    })])
    renderLibrary()
    fireEvent.click(within(await tile('term-tool')).getByRole('button', { name: 'Term Tool' }))
    expect(await screen.findByText('Remote environment detected')).toBeInTheDocument()
    expect(screen.getByText('kirocrew app open term-tool')).toBeInTheDocument()
    // Dismissable.
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(screen.queryByText('Remote environment detected')).toBeNull())
  })

  it('Details in the menu routes to the detail page', async () => {
    renderLibrary()
    const scope = await openTileMenu('secretary', 'Secretary')
    fireEvent.click(scope.getByRole('menuitem', { name: 'Details' }))
    expect(await screen.findByTestId('route-probe')).toHaveAttribute('data-path', '/apps/detail/secretary')
  })

  it('the action row is visible at rest, so Details never needs hover or focus', async () => {
    renderLibrary()
    const el = await tile('secretary')
    const toolbar = within(el).getByRole('toolbar', { name: 'Actions for Secretary' })
    for (const cls of ['opacity-0', 'pointer-events-none']) {
      expect(toolbar.className.split(/\s+/)).not.toContain(cls)
    }
    // No hover, no focus: the Details route is one click into the menu.
    fireEvent.click(within(toolbar).getByRole('button', { name: 'More actions for Secretary' }))
    fireEvent.click(within(el).getByRole('menuitem', { name: 'Details' }))
    expect(await screen.findByTestId('route-probe')).toHaveAttribute('data-path', '/apps/detail/secretary')
  })

  it('Disable dispatches to the disable API', async () => {
    renderLibrary()
    const scope = await openTileMenu('secretary', 'Secretary')
    fireEvent.click(scope.getByRole('menuitem', { name: 'Disable' }))
    await waitFor(() => expect(disableApp).toHaveBeenCalledWith('secretary'))
  })

  it('Update goes through the shared useAppUpdates hook (in place for a path install)', async () => {
    listApps.mockResolvedValue([
      installedApp('secretary', 'Secretary', { origin: 'local', source: '/home/u/apps/secretary' }),
    ])
    listRegistry.mockResolvedValue({
      apps: [{
        name: 'secretary', displayName: 'Secretary', author: 'zezhexu', version: '1.1.0',
        description: 'x', tags: [], installed: true, updateAvailable: true, provenance: 'external',
      }],
    })
    renderLibrary()
    const scope = await openTileMenu('secretary', 'Secretary')
    fireEvent.click(scope.getByRole('menuitem', { name: 'Update' }))
    await waitFor(() => expect(updateApp).toHaveBeenCalledWith('secretary'))
  })

  it('Sync appears for a gateway-lifecycle path install with no pending update and syncs in place', async () => {
    // A dev/path install (gateway lifecycle, not registry-sourced, nothing to
    // update) is the case whose always-visible Sync button the old card
    // carried. The grid keeps the verb in the overflow menu, and the shared
    // runUpdate guard sends it down the in-place api.updateApp path.
    listApps.mockResolvedValue([
      installedApp('secretary', 'Secretary', { origin: 'local', source: '/home/u/apps/secretary' }),
    ])
    renderLibrary()
    const scope = await openTileMenu('secretary', 'Secretary')
    fireEvent.click(scope.getByRole('menuitem', { name: 'Sync' }))
    await waitFor(() => expect(updateApp).toHaveBeenCalledWith('secretary'))
  })

  it('Sync is absent for a lifecycle-locked app', async () => {
    listApps.mockResolvedValue([
      installedApp('pets', 'Pets', { origin: 'builtin', lifecycle: 'locked' }),
    ])
    renderLibrary()
    const scope = await openTileMenu('pets', 'Pets')
    expect(scope.queryByRole('menuitem', { name: 'Sync' })).toBeNull()
  })

  it('Uninstall opens the confirmation dialog; confirming calls the uninstall API', async () => {
    renderLibrary()
    const scope = await openTileMenu('secretary', 'Secretary')
    fireEvent.click(scope.getByRole('menuitem', { name: 'Uninstall' }))
    // The page intercepts the verb into its confirmation dialog — nothing is
    // uninstalled until the dialog's own button confirms.
    const dialog = await screen.findByRole('dialog', { name: 'Confirm uninstall' })
    expect(uninstallApp).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Uninstall' }))
    await waitFor(() =>
      expect(uninstallApp).toHaveBeenCalledWith('secretary', true, false, []))
  })

  it('Uninstall is absent from the menu for a lifecycle-locked app', async () => {
    listApps.mockResolvedValue([
      installedApp('pets', 'Pets', { origin: 'builtin', lifecycle: 'locked' }),
      SECRETARY,
    ])
    renderLibrary()
    const locked = await openTileMenu('pets', 'Pets')
    expect(locked.getByRole('menuitem', { name: 'Details' })).toBeInTheDocument()
    expect(locked.queryByRole('menuitem', { name: 'Uninstall' })).toBeNull()
    // The unlocked sibling keeps the verb — the suppression is per-tile.
    const open = await openTileMenu('secretary', 'Secretary')
    expect(open.getByRole('menuitem', { name: 'Uninstall' })).toBeInTheDocument()
  })
})

describe('LaunchpadTile — the disabled gate is the tile’s own branch', () => {
  // Through LibraryPage, `pinnable` is already false for a disabled app
  // (appNavTarget returns null when !enabled), which MASKS the tile's own
  // `!disabled` guard on the pin controls. This direct render pins that guard
  // independently: even when a caller claims pinnable, a disabled app must
  // not offer a pin toggle or a pin caption.
  it('offers no pin for a disabled app even when the caller passes pinnable', async () => {
    const { default: LaunchpadTile } = await import('../pages/apps/LaunchpadTile')
    render(
      <MemoryRouter>
        <LaunchpadTile
          app={installedApp('secretary', 'Secretary', { enabled: false }) as never}
          pinned
          pinnable
          actionLoading={null}
          onTogglePin={vi.fn()}
          onAction={vi.fn()}
          onOpen={vi.fn()}
          onDetail={vi.fn()}
        />
      </MemoryRouter>,
    )
    const scope = within(screen.getByTestId('launchpad-tile-secretary'))
    // The caption and the menu’s Pin/Unpin item both stay suppressed.
    expect(scope.queryByText('In sidebar')).toBeNull()
    expect(scope.getByRole('button', { name: 'Enable' })).toBeInTheDocument()
    fireEvent.click(scope.getByRole('button', { name: 'More actions for Secretary' }))
    expect(scope.getByRole('menuitem', { name: 'Enable' })).toBeInTheDocument()
    expect(scope.queryByRole('menuitem', { name: 'Pin' })).toBeNull()
    expect(scope.queryByRole('menuitem', { name: 'Unpin' })).toBeNull()
  })
})

describe('LibraryPage — a guide picks an app by its card menu', () => {
  it('reports the app whose ⋯ menu is open, by its own name, and outlines only the named card', async () => {
    const { selectionState, selectedName } = await import('../guide/guidePredicates')
    const { pickCandidate } = await import('../guide/guideActions')
    renderLibrary()
    await tile('secretary')
    await tile('oncall-radar')
    expect(selectionState('app_tile_menu_open')).toBe('none')
    const grid = document.querySelector<HTMLElement>('[data-ui-location="apps.library.app-list"]')!
    expect(grid.getAttribute('aria-label')).toBe('Installed apps')
    const shown = () => true
    // The pick names a card by the app's own name, never by text inside it.
    expect(pickCandidate(grid, shown, 'Oncall Radar')).toBe(screen.getByTestId('launchpad-tile-oncall-radar'))
    expect(pickCandidate(grid, shown, 'Radar')).toBeNull()
    await openTileMenu('secretary', 'Secretary')
    await waitFor(() => expect(selectionState('app_tile_menu_open')).toBe('selected'))
    expect(selectedName('app_tile_menu_open')).toBe('Secretary')
    // The menu holds the registered Uninstall item the guide's last step points at.
    expect(document.querySelectorAll('[data-ui-location="apps.library.tile-uninstall"]')).toHaveLength(1)
    // Closing it unpicks.
    fireEvent.click(within(await tile('secretary')).getByRole('button', { name: 'More actions for Secretary' }))
    await waitFor(() => expect(selectionState('app_tile_menu_open')).toBe('none'))
  })
})
