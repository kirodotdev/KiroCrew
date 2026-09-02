/**
 * LibraryPage — the launchpad grid contract (PR3 App Store split, approved
 * mockup frame #a, redesigned action bar per review findings).
 *
 * The Library list is an icon GRID of LaunchpadTile, one tile per installed
 * app. The pin badge sits INSIDE the tile-face button, anchored to the icon's
 * corner, and toggles whether the app appears in the sidebar, persisted
 * through the `appNavHidden` module (`mc-app-nav-hidden` +
 * `mc:app-nav-hidden-changed`). The action bar is IN FLOW under the caption
 * with at most two peers: an Open button (only when the app can open) and an
 * overflow menu (Details, Pin/Unpin, Update, Disable/Enable, Uninstall).
 * This file pins that surface:
 *
 *  - one tile per `installedApps` entry;
 *  - the pin badge WRITES the hidden set, DISPATCHES the sync event, and
 *    does NOT open the tile (stopPropagation);
 *  - ids absent from storage — and a malformed stored value — read as
 *    pinned ("In sidebar"), so a fresh install needs no migration;
 *  - an enabled app with no sidebar destination captions "No sidebar page"
 *    and offers no pin affordance;
 *  - a disabled app renders greyscale, carries NO pin badge, and its menu
 *    offers Details/Enable/Uninstall;
 *  - Uninstall is absent from the menu for lifecycle 'locked';
 *  - Open runs `api.openApp` for an `openCommand` app (remote answers show
 *    the remote-command banner) and navigates otherwise;
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

/** Route probe for tile-face / Open-button navigation. */
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

function renderLibrary() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
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
})

describe('LibraryPage — pin badge and persistence', () => {
  it('an id absent from storage defaults to pinned', async () => {
    renderLibrary()
    const t = within(await tile('secretary'))
    const badge = t.getByRole('button', { name: 'Unpin Secretary from the sidebar' })
    expect(badge).toHaveAttribute('aria-pressed', 'true')
    expect(t.getByText('In sidebar')).toBeInTheDocument()
  })

  it('a malformed stored value is treated as the empty set (everything pinned)', async () => {
    localStorage.setItem(APP_NAV_HIDDEN_KEY, '{broken json[')
    renderLibrary()
    const t = within(await tile('secretary'))
    expect(t.getByRole('button', { name: 'Unpin Secretary from the sidebar' }))
      .toHaveAttribute('aria-pressed', 'true')
  })

  it('toggling the pin badge writes mc-app-nav-hidden and dispatches the sync event', async () => {
    const synced = vi.fn()
    window.addEventListener(APP_NAV_HIDDEN_CHANGED_EVENT, synced)
    try {
      renderLibrary()
      const scope = within(await tile('secretary'))
      fireEvent.click(scope.getByRole('button', { name: 'Unpin Secretary from the sidebar' }))

      // Persistence: the sidebar nav id (`app-<name>` for an AppHost-routed
      // app) lands in the HIDDEN set, and same-tab listeners are notified —
      // the only path by which the App.tsx sidebar filter learns of it.
      expect(JSON.parse(localStorage.getItem(APP_NAV_HIDDEN_KEY)!)).toEqual(['app-secretary'])
      expect(synced).toHaveBeenCalled()

      // The badge lives INSIDE the tile-face button; its click must not
      // bubble into the tile open (stopPropagation) — no navigation.
      expect(screen.queryByTestId('route-probe')).toBeNull()

      // The tile repaints from the event: hollow badge, unpinned caption.
      const badge = await scope.findByRole('button', { name: 'Pin Secretary to the sidebar' })
      expect(badge).toHaveAttribute('aria-pressed', 'false')
      expect(scope.getByText('Not in sidebar')).toBeInTheDocument()

      // The other tile's pin state is untouched.
      expect(within(screen.getByTestId('launchpad-tile-oncall-radar'))
        .getByRole('button', { name: 'Unpin Oncall Radar from the sidebar' })).toBeInTheDocument()

      // Toggling back empties the stored set again.
      fireEvent.click(badge)
      expect(JSON.parse(localStorage.getItem(APP_NAV_HIDDEN_KEY)!)).toEqual([])
      await scope.findByRole('button', { name: 'Unpin Secretary from the sidebar' })
    } finally {
      window.removeEventListener(APP_NAV_HIDDEN_CHANGED_EVENT, synced)
    }
  })

  it('the menu Unpin item toggles the same persisted state as the badge', async () => {
    renderLibrary()
    const scope = await openTileMenu('secretary', 'Secretary')
    fireEvent.click(scope.getByRole('menuitem', { name: 'Unpin' }))
    expect(JSON.parse(localStorage.getItem(APP_NAV_HIDDEN_KEY)!)).toEqual(['app-secretary'])
    // Re-open: the item now reads Pin (state came back through the event).
    fireEvent.click(scope.getByRole('button', { name: 'More actions for Secretary' }))
    expect(await scope.findByRole('menuitem', { name: 'Pin' })).toBeInTheDocument()
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
    // No pin badge on the icon, and no Pin/Unpin in the menu.
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

  it('renders greyscale without a pin badge, and the menu offers Details/Enable/Uninstall', async () => {
    renderLibrary()
    const el = await tile('secretary')
    const scope = within(el)
    // Greyscale icon at reduced opacity (mockup frame #a's disabled state).
    expect(el.querySelector('.grayscale.opacity-45')).not.toBeNull()
    expect(scope.getByText('Disabled')).toBeInTheDocument()
    // No pin badge and no Open button: a disabled app is not in the sidebar
    // regardless of the stored pin, and it cannot open.
    expect(scope.queryByRole('button', { name: /the sidebar$/ })).toBeNull()
    expect(scope.queryByRole('button', { name: 'Open' })).toBeNull()
    // Menu verbs: Details + Enable + Uninstall; no Disable, no Pin/Unpin.
    fireEvent.click(scope.getByRole('button', { name: 'More actions for Secretary' }))
    expect(scope.getByRole('menuitem', { name: 'Details' })).toBeInTheDocument()
    expect(scope.getByRole('menuitem', { name: 'Enable' })).toBeInTheDocument()
    expect(scope.getByRole('menuitem', { name: 'Uninstall' })).toBeInTheDocument()
    expect(scope.queryByRole('menuitem', { name: 'Disable' })).toBeNull()
    expect(scope.queryByRole('menuitem', { name: 'Pin' })).toBeNull()
    expect(scope.queryByRole('menuitem', { name: 'Unpin' })).toBeNull()
    // The enabled sibling keeps its badge — the suppression is per-tile.
    expect(within(screen.getByTestId('launchpad-tile-oncall-radar'))
      .getByRole('button', { name: 'Unpin Oncall Radar from the sidebar' })).toBeInTheDocument()
  })

  it('Enable in the menu dispatches to the enable hook', async () => {
    renderLibrary()
    const scope = await openTileMenu('secretary', 'Secretary')
    fireEvent.click(scope.getByRole('menuitem', { name: 'Enable' }))
    await waitFor(() => expect(enableApp).toHaveBeenCalledWith('secretary'))
  })
})

describe('LibraryPage — action dispatch', () => {
  it('Open navigates to the app’s nav-target route', async () => {
    renderLibrary()
    fireEvent.click(within(await tile('secretary')).getByRole('button', { name: 'Open' }))
    expect(await screen.findByTestId('route-probe')).toHaveAttribute('data-path', '/apps/secretary')
    // A route-navigated open never touches the open endpoint.
    expect(openApp).not.toHaveBeenCalled()
  })

  it('Open on an openCommand app runs api.openApp instead of navigating', async () => {
    listApps.mockResolvedValue([installedApp('term-tool', 'Term Tool', {
      manifest: {
        name: 'term-tool', version: '1.0.0', displayName: 'Term Tool',
        description: 'Opens a terminal.', author: 'zezhexu', tags: [],
        openCommand: 'term-tool open',
      },
    })])
    renderLibrary()
    fireEvent.click(within(await tile('term-tool')).getByRole('button', { name: 'Open' }))
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
    fireEvent.click(within(await tile('term-tool')).getByRole('button', { name: 'Open' }))
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
  // `!disabled` guard on the pin badge. This direct render pins that guard
  // independently: even when a caller claims pinnable, a disabled app must
  // not offer a pin toggle (mutation-testing found the page-level tests
  // alone could not kill an inverted `!disabled`).
  it('hides the pin badge for a disabled app even when the caller passes pinnable', async () => {
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
    // Both pin affordances stay suppressed: the icon-corner badge (aria-label
    // “… the sidebar”) and the menu’s Pin/Unpin item.
    expect(scope.queryByRole('button', { name: /the sidebar$/ })).toBeNull()
    fireEvent.click(scope.getByRole('button', { name: 'More actions for Secretary' }))
    expect(scope.getByRole('menuitem', { name: 'Enable' })).toBeInTheDocument()
    expect(scope.queryByRole('menuitem', { name: 'Pin' })).toBeNull()
    expect(scope.queryByRole('menuitem', { name: 'Unpin' })).toBeNull()
  })
})
