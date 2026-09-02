/**
 * AppsPage — action paths, uninstall dialog, and failure branches.
 *
 * `AppsPageDiscover.test.tsx` covers the editorial/browse rendering surface.
 * This file covers what it does not touch: the navigation helpers (Cmd-click,
 * `autoAction` router state), the enable path including the trust-denied
 * consent hand-off, the Library disable toast and updates hint row, Update
 * All's failure report (on Discover's Updates sub-tab, its PR2 home),
 * the whole uninstall confirmation dialog (every provenance notice, the
 * dependency preview, keep-data / keep-dependency wiring), the query-error
 * card, the empty states, and `pickFeatured`'s trust filter.
 *
 * Timers are faked (`shouldAdvanceTime`) because the disable and Update All
 * success paths arm a 4s toast dismissal — a real timer would fire after
 * teardown and make vitest exit non-zero with every test passing, which
 * suppresses the coverage report entirely.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'

// --- Mocks -----------------------------------------------------------------
const listApps = vi.fn()
const listRegistry = vi.fn()
const listRegistries = vi.fn()
const enableApp = vi.fn()
const disableApp = vi.fn()
const updateApp = vi.fn()
const uninstallApp = vi.fn()
const uninstallPreview = vi.fn()

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
    openApp: vi.fn(),
    trustApp: vi.fn(),
    untrustApp: vi.fn(),
    getApp: vi.fn(),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark' }) }))

// The Library tiles' overflow menu is a Radix DropdownMenu, which happy-dom
// cannot drive — swap in the repo's stateful mock (FileExplorerPageCoverage
// pattern): Trigger click toggles, items render inline as role="menuitem".
vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))

vi.mock('../components/AppIcon', () => ({
  default: ({ icon }: { icon?: string }) => <div data-testid="app-icon" data-icon={icon || ''} />,
}))

/* SegmentedControl measures its container (0px under happy-dom) and collapses
   to a dropdown, hiding the tab labels — stub it with plain buttons. Each
   button gets its OWN wrapper element so the row never reads as three peer
   actions (`max-two-buttons-per-row`), regardless of how many segments a
   caller passes. */
vi.mock('../components/SegmentedControl', () => ({
  default: ({ segments, onChange }: {
    segments: { key: string; label: string }[]
    onChange: (key: string) => void
  }) => (
    <div>
      {segments.map(s => (
        <div key={s.key}>
          <button type="button" onClick={() => onChange(s.key)}>{s.label}</button>
        </div>
      ))}
    </div>
  ),
}))

/* SimpleSelect wraps a Radix Select, which commits inside `flushSync` and
   needs an open-then-click cycle. The sort control is not the code under test
   — its `onChange` is — so stub it with always-rendered options, each in its
   own wrapper for the same button-row reason as above. */
vi.mock('../components/SimpleSelect', () => ({
  default: ({ options, optionLabels, value, onChange, 'aria-label': ariaLabel }: {
    options: string[]
    optionLabels?: string[]
    value: string
    onChange: (v: string) => void
    'aria-label'?: string
  }) => (
    <span role="group" aria-label={ariaLabel}>
      {options.map((o, i) => (
        <span key={o}>
          <button type="button" role="option" aria-selected={o === value} onClick={() => onChange(o)}>
            {optionLabels?.[i] ?? o}
          </button>
        </span>
      ))}
    </span>
  ),
}))

import DiscoverPage from '../pages/apps/DiscoverPage'
import LibraryPage from '../pages/apps/LibraryPage'
import { pickFeatured } from '../pages/apps/useAppsData'
import type { RegistryApp } from '../components/appstore/types'

/** Detail-route probe: reports the app name and the `autoAction` router state. */
function DetailProbe() {
  const loc = useLocation()
  const state = loc.state as { autoAction?: string } | null
  return (
    <div data-testid="detail-route" data-path={loc.pathname} data-auto={state?.autoAction || ''} />
  )
}

/** Mounts the split pages under their real routes. Library tests mount at
 *  '/apps/library' directly — the in-page Library tab is gone (PR1 split). */
function renderPage(initialRoute = '/apps') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialRoute]}>
        <Routes>
          <Route path="/apps" element={<DiscoverPage />} />
          {/* Static segments BEFORE the param routes, mirroring App.tsx's
              route order. `/apps/-/updates` is Discover's Updates sub-tab
              (PR2) — the pending-updates worklist and Update All live there. */}
          <Route path="/apps/-/updates" element={<DiscoverPage />} />
          <Route path="/apps/library" element={<LibraryPage />} />
          <Route path="/apps/detail/:name" element={<DetailProbe />} />
          <Route path="/apps/:name" element={<DetailProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/** A hidden-by-default builtin: installed, switched OFF, so Discover offers Enable. */
const BUILTIN_OFF = {
  name: 'pets', displayName: 'Pets', version: '1.0.0', enabled: false,
  installedAt: '2026-06-01T00:00:00Z', origin: 'builtin', resources: 'gateway', lifecycle: 'locked',
  manifest: {
    name: 'pets', version: '1.0.0', displayName: 'Pets', description: 'A desk companion.',
    author: 'kirocrew', tags: ['fun'], ui: { pages: [{ route: '/pets', label: 'Pets', icon: 'Cat' }] },
  },
}

/** An installed third-party app with an update pending and resources to remove. */
const SECRETARY = {
  name: 'secretary', displayName: 'Secretary', version: '1.0.0', enabled: true,
  installedAt: '2026-07-01T00:00:00Z', origin: 'registry', resources: 'gateway', lifecycle: 'gateway',
  manifest: {
    name: 'secretary', version: '1.0.0', displayName: 'Secretary', description: 'Slack inbox manager.',
    author: 'zezhexu', tags: ['slack', 'inbox'], agents: ['secretary'], skills: ['mochi-slack'],
    crons: [{ name: 'inbox-sweep' }], repo: 'https://github.com/z/secretary',
    ui: { pages: [{ route: '/secretary-ui', label: 'Secretary', icon: 'Bot' }] },
  },
}

const REGISTRY_APPS = [
  {
    name: 'oncall-radar', displayName: 'Oncall Radar', author: 'kirocrew',
    description: 'Oncall operations dashboard.', version: '2.0.0',
    tags: ['oncall'], installed: false, updateAvailable: false, provenance: 'core',
  },
  {
    name: 'secretary', displayName: 'Secretary', author: 'zezhexu',
    description: 'Slack inbox manager.', version: '1.1.0', _registry: 'kirodotdev-labs',
    tags: ['slack'], installed: true, updateAvailable: true, provenance: 'external',
    repo: 'https://github.com/z/secretary',
  },
]

/**
 * The row the gateway actually returns for an installed built-in the published
 * catalog lists: display fields from the catalog, `origin` stamped from the
 * installed app, no `_registry`, first-party provenance.
 *
 * Explore renders the rows the server sends and synthesizes nothing, so a test
 * that installs a built-in must also let the registry response carry it --
 * otherwise the fixture describes a response the real server cannot produce.
 */
const builtinServerRow = (name: string, displayName: string, author = 'kirocrew') => ({
  name, displayName, author, description: 'A desk companion.', version: '1.0.0',
  tags: ['fun'], installed: true, updateAvailable: false,
  origin: 'builtin', lifecycle: 'locked', provenance: 'builtin', verified: true,
})

const NO_DEPS = { dependencies: { removable: [], shared: [], userInstalled: [] } }

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers({ shouldAdvanceTime: true })
  sessionStorage.clear()
  listApps.mockResolvedValue([BUILTIN_OFF, SECRETARY])
  listRegistry.mockResolvedValue({ apps: [...REGISTRY_APPS, builtinServerRow('pets', 'Pets')] })
  listRegistries.mockResolvedValue({
    registries: [{ name: 'kirodotdev-labs', repo: 'https://github.com/kirodotdev-labs/registry', branch: 'main' }],
  })
  enableApp.mockResolvedValue({ ok: true })
  disableApp.mockResolvedValue({ ok: true })
  updateApp.mockResolvedValue({ ok: true })
  uninstallApp.mockResolvedValue({ ok: true })
  uninstallPreview.mockResolvedValue(NO_DEPS)
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
})

/** Wait for the Discover catalog to be on screen. */
const catalogReady = () => screen.findByRole('heading', { name: 'All apps' })

/** The row for one app in Discover's dense list (the LAST such surface — the
 *  editorial cards render the same accessible name above it). */
async function browseRow(name: string) {
  const rows = await screen.findAllByRole('button', { name: `View details for ${name}` })
  return rows[rows.length - 1]
}

/** Mount the Library route directly — the split removed the in-page tab. */
const renderLibrary = () => renderPage('/apps/library')

/** Open a Library tile's overflow menu (the MoreHorizontal trigger). The
 *  mocked portal renders inline, so items land inside the tile scope. */
async function openTileMenu(display: string) {
  fireEvent.click(await screen.findByRole('button', { name: `More actions for ${display}` }))
  return screen
}

/** Mount Discover's Updates sub-tab, where Update All lives since PR2 demoted
 *  the Library banner to a hint row. */
const renderUpdates = () => renderPage('/apps/-/updates')

describe('AppsPage — Discover navigation', () => {
  it('Cmd-clicks a row into a new tab instead of routing in place', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    renderPage()
    await catalogReady()
    fireEvent.click(await browseRow('Oncall Radar'), { metaKey: true })
    expect(open).toHaveBeenCalledWith('/apps/detail/oncall-radar', '_blank', 'noopener,noreferrer')
    // The in-place navigation must NOT also happen.
    expect(screen.queryByTestId('detail-route')).toBeNull()
    open.mockRestore()
  })

  it('Install routes to the detail page carrying the install autoAction in router state', async () => {
    renderPage()
    await catalogReady()
    const row = await browseRow('Oncall Radar')
    fireEvent.click(within(row).getByRole('button', { name: 'Install' }))
    const probe = await screen.findByTestId('detail-route')
    expect(probe).toHaveAttribute('data-path', '/apps/detail/oncall-radar')
    expect(probe).toHaveAttribute('data-auto', 'install')
  })

  it('Update on an installed row routes with the update autoAction', async () => {
    renderPage()
    await catalogReady()
    const row = await browseRow('Secretary')
    fireEvent.click(within(row).getByRole('button', { name: 'Update' }))
    const probe = await screen.findByTestId('detail-route')
    expect(probe).toHaveAttribute('data-path', '/apps/detail/secretary')
    expect(probe).toHaveAttribute('data-auto', 'update')
  })

  it('Add source in the rail opens the Sources popover', async () => {
    renderPage()
    await catalogReady()
    fireEvent.click(screen.getByRole('button', { name: /Add source/ }))
    expect(await screen.findByText('Install from Path')).toBeInTheDocument()
  })

  it('sorting by category regroups the list without dropping rows', async () => {
    renderPage()
    await catalogReady()
    const sort = screen.getByRole('group', { name: 'Sort apps' })
    expect(within(sort).getByRole('option', { name: 'Name' })).toHaveAttribute('aria-selected', 'true')
    fireEvent.click(within(sort).getByRole('option', { name: 'Category' }))
    await waitFor(() =>
      expect(within(sort).getByRole('option', { name: 'Category' })).toHaveAttribute('aria-selected', 'true'))
    // All three catalog entries survive the re-sort (builtin + 2 registry rows).
    expect(screen.getByText('3 apps')).toBeInTheDocument()
  })
})

describe('AppsPage — enable path', () => {
  it('enables a switched-off builtin and broadcasts the apps-changed event', async () => {
    const onChanged = vi.fn()
    window.addEventListener('mc:apps-changed', onChanged)
    renderPage()
    await catalogReady()
    const row = await browseRow('Pets')
    fireEvent.click(within(row).getByRole('button', { name: /^Enable/ }))
    await waitFor(() => expect(enableApp).toHaveBeenCalledWith('pets'))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    window.removeEventListener('mc:apps-changed', onChanged)
  })

  it('opens the trust consent modal when enable is refused for missing trust', async () => {
    enableApp.mockRejectedValue({ body: JSON.stringify({ code: 'app_execution_denied' }) })
    renderPage()
    await catalogReady()
    const row = await browseRow('Pets')
    fireEvent.click(within(row).getByRole('button', { name: /^Enable/ }))
    expect(await screen.findByText('Trust “Pets” to run its own code?')).toBeInTheDocument()
    // A consent prompt is not an error — the error card must stay away.
    expect(screen.queryByText(/Failed to enable/)).toBeNull()
  })

  it('shows the raw failure in the error card for a non-trust enable failure', async () => {
    enableApp.mockRejectedValue(new Error('gateway is busy'))
    renderPage()
    await catalogReady()
    const row = await browseRow('Pets')
    fireEvent.click(within(row).getByRole('button', { name: /^Enable/ }))
    expect(await screen.findByText('gateway is busy')).toBeInTheDocument()
    expect(screen.queryByText('Trust “Pets” to run its own code?')).toBeNull()
  })

  it('falls back to the generic enable message when the failure carries no text', async () => {
    enableApp.mockRejectedValue({})
    renderPage()
    await catalogReady()
    const row = await browseRow('Pets')
    fireEvent.click(within(row).getByRole('button', { name: /^Enable/ }))
    expect(await screen.findByText('Failed to enable pets')).toBeInTheDocument()
  })
})

describe('AppsPage — query failures and empty states', () => {
  it('surfaces the apps-query failure and lets the user dismiss it', async () => {
    listApps.mockRejectedValue(new Error(''))
    renderPage()
    expect(await screen.findByText('Failed to load apps')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(screen.queryByText('Failed to load apps')).toBeNull())
  })

  it('surfaces the registry-query failure with its own message', async () => {
    listRegistry.mockRejectedValue(new Error('registry unreachable'))
    renderPage()
    expect(await screen.findByText('registry unreachable')).toBeInTheDocument()
  })

  it('shows the empty catalog state when nothing is browsable', async () => {
    listApps.mockResolvedValue([])
    listRegistry.mockResolvedValue({ apps: [] })
    renderPage()
    expect(await screen.findByText('No apps available')).toBeInTheDocument()
    expect(screen.getByText('Add an app source (Sources, top right) or install from a local path.')).toBeInTheDocument()
  })

  it('shows the no-match state when the search excludes every row', async () => {
    renderPage()
    await catalogReady()
    fireEvent.change(screen.getByLabelText('Search apps'), { target: { value: 'nothing-matches-this' } })
    expect(await screen.findByText('No matching apps')).toBeInTheDocument()
    expect(screen.getByText('Try a different search or category.')).toBeInTheDocument()
  })

  it('shows the empty Library state when no app is installed', async () => {
    listApps.mockResolvedValue([])
    listRegistry.mockResolvedValue({ apps: [] })
    renderLibrary()
    expect(await screen.findByText('No apps installed yet')).toBeInTheDocument()
  })

  it('shows the no-match Library state when the search excludes every installed app', async () => {
    renderLibrary()
    await screen.findByTestId('launchpad-tile-secretary')
    fireEvent.change(screen.getByLabelText('Search library'), { target: { value: 'nothing-matches-this' } })
    expect(await screen.findByText('No matching apps')).toBeInTheDocument()
    expect(screen.getByText('Try a different search term')).toBeInTheDocument()
  })

  it('matches an installed app by its manifest tags', async () => {
    renderLibrary()
    await screen.findByTestId('launchpad-tile-secretary')
    fireEvent.change(screen.getByLabelText('Search library'), { target: { value: 'inbox' } })
    // The tag match keeps the tile; nothing falls through to the empty state.
    await waitFor(() => expect(screen.queryByText('No matching apps')).toBeNull())
    expect(screen.getByTestId('launchpad-tile-secretary')).toBeInTheDocument()
  })
})

describe('AppsPage — Library actions', () => {
  it('opens the app at its AppHost route from the card, and the detail page from its name', async () => {
    renderLibrary()
    // Open must resolve through the same appNavTarget derivation the sidebar and
    // command palette use: a third-party (non-builtin) app is AppHost-routed at
    // /apps/<name>, NOT its raw manifest page route (which only a native builtin
    // serves, and which otherwise dead-ends at BuiltinAppRoute -> /chat).
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    const probe = await screen.findByTestId('detail-route')
    expect(probe).toHaveAttribute('data-path', '/apps/secretary')
  })

  it('routes to the detail page when the tile face of a non-openable app is clicked', async () => {
    // The tile face opens the app when it can open (previous test); an
    // enabled app with no UI and no openCommand has nowhere to open, so its
    // face falls back to the detail page — plain navigation, no autoAction.
    listApps.mockResolvedValue([{
      ...SECRETARY,
      manifest: { ...SECRETARY.manifest, ui: undefined },
    }])
    renderLibrary()
    fireEvent.click(await screen.findByRole('button', { name: 'Secretary' }))
    const probe = await screen.findByTestId('detail-route')
    expect(probe).toHaveAttribute('data-path', '/apps/detail/secretary')
    expect(probe).toHaveAttribute('data-auto', '')
  })

  it('keeps the builtin row listed after disabling it, with no toast', async () => {
    // The toast used to say "re-enable it from the Discover tab" because the row
    // vanished on disable. It stays now, so the Enable button is the recovery
    // path and nothing narrates a disappearance that no longer happens -- and the
    // old copy pointed at a dead end for a builtin with no catalog row.
    listApps.mockResolvedValue([{ ...BUILTIN_OFF, enabled: true }])
    renderLibrary()
    await openTileMenu('Pets')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Disable' }))
    await waitFor(() => expect(disableApp).toHaveBeenCalledWith('pets'))
    expect(screen.queryByText(/re-enable it from the Discover/)).toBeNull()
  })

  it('lists a builtin that is already disabled, with Enable in its overflow menu', async () => {
    // The reachability property itself: a disabled builtin is in Library at
    // all. Its Enable verb lives in the tile's overflow menu (the launchpad
    // grid caps direct actions at two peers).
    listApps.mockResolvedValue([{ ...BUILTIN_OFF, enabled: false }])
    renderLibrary()
    await openTileMenu('Pets')
    expect(await screen.findByRole('menuitem', { name: 'Enable' })).toBeInTheDocument()
  })

  it('orders enabled rows above disabled ones', async () => {
    // Listing disabled builtins adds ~20 rows on a fresh install and Library has
    // only a search box, so ordering is what keeps the apps in use on top. The
    // gateway returns them disabled-first here, so a pass-through would fail.
    listApps.mockResolvedValue([
      { ...BUILTIN_OFF, name: 'zeta-off', displayName: 'Zeta Off', enabled: false },
      { ...BUILTIN_OFF, name: 'alpha-on', displayName: 'Alpha On', enabled: true },
    ])
    renderLibrary()
    await screen.findByText('Alpha On')
    const rendered = screen.getAllByText(/Alpha On|Zeta Off/).map(n => n.textContent)
    expect(rendered.indexOf('Alpha On')).toBeLessThan(rendered.indexOf('Zeta Off'))
  })

  it('reports a failed disable with the action-failed message', async () => {
    disableApp.mockRejectedValue({})
    renderLibrary()
    await openTileMenu('Secretary')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Disable' }))
    expect(await screen.findByText('Failed to disable secretary')).toBeInTheDocument()
  })

  it('names the apps that failed during Update All', async () => {
    updateApp.mockRejectedValue(new Error('clone failed'))
    // Update All moved with the banner demotion (PR2): the batch runs from
    // Discover's Updates sub-tab, whose page owns the error notice surface.
    renderUpdates()
    fireEvent.click(await screen.findByRole('button', { name: 'Update All' }))
    expect(await screen.findByText('Failed to update: secretary')).toBeInTheDocument()
  })

  it('reports success after Update All and shows no error', async () => {
    renderUpdates()
    fireEvent.click(await screen.findByRole('button', { name: 'Update All' }))
    expect(await screen.findByText('Updated 1 app.')).toBeInTheDocument()
    expect(screen.queryByText(/Failed to update/)).toBeNull()
  })

  it('Library shows the muted updates hint row, not the old banner', async () => {
    renderLibrary()
    // PR2 demoted the banner: a one-line hint with the count and a hand-off
    // link to the Updates sub-page — no Update All button on Library.
    expect(await screen.findByText('1 update available')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'View updates' }))
      .toHaveAttribute('href', '/apps/-/updates')
    expect(screen.queryByRole('button', { name: 'Update All' })).toBeNull()
    // The affected tile still offers per-app Update — in its overflow menu
    // (the redesigned bar caps at Open + menu).
    await openTileMenu('Secretary')
    expect(screen.getByRole('menuitem', { name: 'Update' })).toBeInTheDocument()
  })
})

describe('AppsPage — uninstall dialog', () => {
  const openDialog = async () => {
    renderLibrary()
    await openTileMenu('Secretary')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Uninstall' }))
    return screen.findByRole('dialog', { name: 'Confirm uninstall' })
  }

  it('lists the resources the removal takes with it', async () => {
    const dialog = await openDialog()
    expect(within(dialog).getByText('Uninstall Secretary?')).toBeInTheDocument()
    expect(within(dialog).getByText('1 agent')).toBeInTheDocument()
    expect(within(dialog).getByText('1 skill')).toBeInTheDocument()
    expect(within(dialog).getByText('1 cron job')).toBeInTheDocument()
    // Registry provenance notice (gateway-managed resources, no app secret).
    expect(within(dialog).getByText(/the downloaded source code will be removed/)).toBeInTheDocument()
  })

  it('warns about a self-managed app whose resources live outside Kiro Crew', async () => {
    listApps.mockResolvedValue([{ ...SECRETARY, origin: 'local', resources: 'app' }])
    const dialog = await openDialog()
    expect(within(dialog).getByText(/This is a self-managed app/)).toBeInTheDocument()
  })

  it('warns when the manifest ships an uninstall script', async () => {
    listApps.mockResolvedValue([{
      ...SECRETARY,
      origin: 'local',
      resources: 'app',
      manifest: { ...SECRETARY.manifest, setup: { onUninstall: 'cleanup.sh' } },
    }])
    const dialog = await openDialog()
    expect(within(dialog).getByText(/This app has an uninstall script/)).toBeInTheDocument()
    expect(within(dialog).getByText(/your local source code will not be affected/)).toBeInTheDocument()
  })

  it('names the app secret for a self-managed registry install', async () => {
    listApps.mockResolvedValue([{ ...SECRETARY, resources: 'app' }])
    const dialog = await openDialog()
    expect(within(dialog).getByText(/the app secret, and the downloaded source code/)).toBeInTheDocument()
    expect(within(dialog).getByText(/The app itself is managed externally/)).toBeInTheDocument()
  })

  it('renders the dependency preview and passes the kept dependency to the uninstall call', async () => {
    uninstallPreview.mockResolvedValue({
      dependencies: {
        removable: [{ id: 'skills/mochi-slack', reason: 'installed with this app' }],
        shared: [{ id: 'skills/widgets', reason: 'also used by Pets' }],
        userInstalled: [{ id: 'skills/grill' }],
      },
    })
    const dialog = await openDialog()
    expect(within(dialog).getByText('Dependencies:')).toBeInTheDocument()
    expect(within(dialog).getByText('installed with this app')).toBeInTheDocument()
    expect(within(dialog).getByText(/also used by Pets/)).toBeInTheDocument()
    expect(within(dialog).getByText('Kept — installed by you')).toBeInTheDocument()

    fireEvent.click(within(dialog).getByLabelText('Keep dependency mochi-slack'))
    fireEvent.click(within(dialog).getByLabelText('Keep app data'))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Uninstall' }))
    await waitFor(() =>
      expect(uninstallApp).toHaveBeenCalledWith('secretary', false, false, ['skills/mochi-slack']))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Confirm uninstall' })).toBeNull())
  })

  it('unticking then re-ticking a dependency leaves it out of the keep list', async () => {
    uninstallPreview.mockResolvedValue({
      dependencies: { removable: [{ id: 'skills/mochi-slack', reason: 'installed with this app' }], shared: [], userInstalled: [] },
    })
    const dialog = await openDialog()
    const box = within(dialog).getByLabelText('Keep dependency mochi-slack')
    fireEvent.click(box)
    fireEvent.click(box)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Uninstall' }))
    await waitFor(() => expect(uninstallApp).toHaveBeenCalledWith('secretary', true, false, []))
  })

  it('hides the dependency block when the preview reports none', async () => {
    const dialog = await openDialog()
    expect(within(dialog).queryByText('Dependencies:')).toBeNull()
  })

  it('still opens the dialog when the preview request fails', async () => {
    uninstallPreview.mockRejectedValue(new Error('preview unavailable'))
    const dialog = await openDialog()
    expect(within(dialog).queryByText('Dependencies:')).toBeNull()
    expect(within(dialog).getByText('Uninstall Secretary?')).toBeInTheDocument()
  })

  it('reports a failed uninstall and closes the dialog', async () => {
    uninstallApp.mockRejectedValue({})
    const dialog = await openDialog()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Uninstall' }))
    expect(await screen.findByText('Failed to uninstall secretary')).toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: 'Confirm uninstall' })).toBeNull()
  })

  it('Cancel closes the dialog without calling the API', async () => {
    const dialog = await openDialog()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Confirm uninstall' })).toBeNull())
    expect(uninstallApp).not.toHaveBeenCalled()
  })

  it('Escape and a backdrop click both close the dialog', async () => {
    const first = await openDialog()
    fireEvent.keyDown(first, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Confirm uninstall' })).toBeNull())

    await openTileMenu('Secretary')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Uninstall' }))
    const second = await screen.findByRole('dialog', { name: 'Confirm uninstall' })
    fireEvent.click(second)
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Confirm uninstall' })).toBeNull())
    expect(uninstallApp).not.toHaveBeenCalled()
  })
})

describe('AppsPage — editorial layer wiring', () => {
  /** Default catalog order (no curator flags): verified first, then name —
   *  Oncall Radar spotlights, Pets and Secretary take the feature cards. */
  it('routes to the detail page from the spotlight body', async () => {
    renderPage()
    await catalogReady()
    const surfaces = screen.getAllByRole('button', { name: 'View details for Oncall Radar' })
    fireEvent.click(surfaces[0])
    expect(await screen.findByTestId('detail-route')).toHaveAttribute('data-path', '/apps/detail/oncall-radar')
  })

  it('Get on the spotlight carries the install autoAction', async () => {
    renderPage()
    await catalogReady()
    const spotlight = screen.getAllByRole('button', { name: 'View details for Oncall Radar' })[0]
    fireEvent.click(within(spotlight).getByRole('button', { name: 'Get' }))
    expect(await screen.findByTestId('detail-route')).toHaveAttribute('data-auto', 'install')
  })

  it('a feature card opens the detail page and can enable its app', async () => {
    renderPage()
    await catalogReady()
    const card = screen.getAllByRole('button', { name: 'View details for Pets' })[0]
    fireEvent.click(within(card).getByRole('button', { name: /^Enable/ }))
    await waitFor(() => expect(enableApp).toHaveBeenCalledWith('pets'))
    fireEvent.click(screen.getAllByRole('button', { name: 'View details for Pets' })[0])
    expect(await screen.findByTestId('detail-route')).toHaveAttribute('data-path', '/apps/detail/pets')
  })

  it('enables from the lead card and Gets from a row card', async () => {
    // Pets sorts ahead of Zeta App (both verified), so the switched-off builtin
    // takes the derived `full` lead; Zeta App and Zulu Utility fill the derived
    // `row` (which needs TWO cards -- with one leftover pick the lead stands
    // alone and Zeta would only render as a list row).
    listApps.mockResolvedValue([BUILTIN_OFF])
    listRegistry.mockResolvedValue({
      apps: [
        {
          name: 'zeta-app', displayName: 'Zeta App', author: 'kirocrew', description: 'Later in the alphabet.',
          version: '1.0.0', tags: ['github'], installed: false, provenance: 'core',
        },
        {
          name: 'zulu-utility', displayName: 'Zulu Utility', author: 'kirocrew', description: 'Fills the second row slot.',
          version: '1.0.0', tags: ['github'], installed: false, provenance: 'core',
        },
        // Explore renders server rows, so the installed built-in reaches the
        // shelf the same way it does in production -- via the catalog.
        { ...builtinServerRow('pets', 'Pets'), enabled: false },
      ],
    })
    renderPage()
    await catalogReady()
    const spotlight = screen.getAllByRole('button', { name: 'View details for Pets' })[0]
    fireEvent.click(within(spotlight).getByRole('button', { name: 'Enable' }))
    await waitFor(() => expect(enableApp).toHaveBeenCalledWith('pets'))

    const card = screen.getAllByRole('button', { name: 'View details for Zeta App' })[0]
    fireEvent.click(within(card).getByRole('button', { name: 'Get' }))
    const probe = await screen.findByTestId('detail-route')
    expect(probe).toHaveAttribute('data-path', '/apps/detail/zeta-app')
    expect(probe).toHaveAttribute('data-auto', 'install')
  })
})

describe('AppsPage — Library enable and update', () => {
  it('Update on a Library card routes to the streaming detail page', async () => {
    renderLibrary()
    await openTileMenu('Secretary')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Update' }))
    const probe = await screen.findByTestId('detail-route')
    expect(probe).toHaveAttribute('data-path', '/apps/detail/secretary')
    expect(probe).toHaveAttribute('data-auto', 'update')
    // Navigation only — the page must not call the update endpoint itself.
    expect(updateApp).not.toHaveBeenCalled()
  })

  it('syncs a PATH-installed app in place instead of routing at the registry', async () => {
    // The grid only offers Update while one is pending (a registry row later
    // published for this name), but ROUTING stays keyed on the RECORDED
    // source: a directory install refreshes via POST /api/apps/{name}/update
    // (re-copying the recorded directory), never the streaming registry
    // install the detail page runs.
    listApps.mockResolvedValue([{
      ...SECRETARY, name: 'orchestrator-switch', displayName: 'Orchestrator Switch',
      source: '/home/u/apps/orchestrator-switch', origin: 'local',
    }])
    listRegistry.mockResolvedValue({
      apps: [{ ...REGISTRY_APPS[1], name: 'orchestrator-switch', displayName: 'Orchestrator Switch' }],
    })
    renderLibrary()
    await openTileMenu('Orchestrator Switch')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Update' }))
    await waitFor(() => expect(updateApp).toHaveBeenCalledWith('orchestrator-switch'))
    expect(screen.queryByTestId('detail-route')).toBeNull()
  })

  it('reflects a completed in-place sync, which is otherwise invisible', async () => {
    // Re-copying a source directory usually carries the SAME version, so the
    // tile re-renders identically and silence is indistinguishable from a
    // no-op. Without this the primary action the fix creates has no success
    // signal at all.
    listApps.mockResolvedValue([{
      ...SECRETARY, name: 'orchestrator-switch', displayName: 'Orchestrator Switch',
      source: '/home/u/apps/orchestrator-switch', origin: 'local',
    }])
    listRegistry.mockResolvedValue({
      apps: [{ ...REGISTRY_APPS[1], name: 'orchestrator-switch', displayName: 'Orchestrator Switch' }],
    })
    renderLibrary()
    await openTileMenu('Orchestrator Switch')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Update' }))
    expect(await screen.findByText(/Synced Orchestrator Switch from its source directory/)).toBeInTheDocument()
  })

  it('surfaces a failed in-place sync instead of leaving the card silent', async () => {
    listApps.mockResolvedValue([{
      ...SECRETARY, name: 'orchestrator-switch', displayName: 'Orchestrator Switch',
      source: '/home/u/apps/orchestrator-switch', origin: 'local',
    }])
    listRegistry.mockResolvedValue({
      apps: [{ ...REGISTRY_APPS[1], name: 'orchestrator-switch', displayName: 'Orchestrator Switch' }],
    })
    updateApp.mockRejectedValue(new Error('source path no longer exists'))
    renderLibrary()
    await openTileMenu('Orchestrator Switch')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Update' }))
    expect(await screen.findByText('source path no longer exists')).toBeInTheDocument()
  })

  it('offers no update affordance on a tile when nothing is pending', async () => {
    // Grid contract: Update appears in the overflow menu only WHEN an update
    // is pending. With no registry row flagging one, the menu carries no
    // Update item at all — the recorded-source routing for that case is the
    // shared hook's contract (useAppUpdates.test.tsx).
    listApps.mockResolvedValue([{ ...SECRETARY, source: 'registry:secretary' }])
    listRegistry.mockResolvedValue({ apps: [] })
    renderLibrary()
    await screen.findByTestId('launchpad-tile-secretary')
    await openTileMenu('Secretary')
    expect(screen.queryByRole('menuitem', { name: 'Update' })).toBeNull()
    expect(updateApp).not.toHaveBeenCalled()
  })

  it('enables a switched-off installed app from the Library', async () => {
    listApps.mockResolvedValue([{ ...SECRETARY, enabled: false }])
    renderLibrary()
    await openTileMenu('Secretary')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Enable' }))
    await waitFor(() => expect(enableApp).toHaveBeenCalledWith('secretary'))
  })

  it('opens consent from the Library using the installed record when the catalog has no row', async () => {
    // origin `local` and absent from the registry, so `trustTarget` has to fall
    // back to the installed record for the name shown in the consent dialog.
    listApps.mockResolvedValue([{
      ...SECRETARY, name: 'localapp', displayName: 'Local App', enabled: false, origin: 'local',
    }])
    listRegistry.mockResolvedValue({ apps: [] })
    enableApp.mockRejectedValue({ code: 'app_execution_denied' })
    renderLibrary()
    await openTileMenu('Local App')
    fireEvent.click(screen.getByRole('menuitem', { name: 'Enable' }))
    expect(await screen.findByText('Trust “Local App” to run its own code?')).toBeInTheDocument()
    expect(screen.queryByText('https://github.com/z/secretary')).toBeNull()
  })
})

describe('AppsPage — toast expiry', () => {
  // The disable-a-builtin toast is gone (the row stays listed, so nothing needs
  // narrating), and with it the case that used to pin the four-second self-clear
  // here. The Update All toast below pins the same timer on a toast that remains.

  it('the Update All toast clears itself after four seconds', async () => {
    // Update All lives on Discover's Updates sub-tab since PR2; its success
    // toast is DiscoverPage's notice surface with the same 4s dismissal.
    renderUpdates()
    fireEvent.click(await screen.findByRole('button', { name: 'Update All' }))
    await screen.findByText('Updated 1 app.')
    await act(async () => { vi.advanceTimersByTime(4000) })
    expect(screen.queryByText('Updated 1 app.')).toBeNull()
  })
})

describe('AppsPage — sources rail', () => {
  it('counts built-ins, a stale registry, and the core registry separately', async () => {
    listRegistry.mockResolvedValue({
      apps: [
        { name: 'core-app', displayName: 'Core App', author: 'kirocrew', description: 'From the core file.', version: '1.0.0', tags: ['github'], installed: false, provenance: 'core' },
        { name: 'ghost-app', displayName: 'Ghost App', author: 'someone', description: 'From a registry no longer configured.', version: '1.0.0', tags: ['github'], installed: false, _registry: 'ghost-registry', provenance: 'external' },
        // A built-in reaches the rail as a CATALOG row, not as client-side
        // synthesis, so the Built-in bucket needs one on the wire to count.
        builtinServerRow('pets', 'Pets'),
      ],
    })
    renderPage()
    await catalogReady()
    const rail = screen.getByText('SOURCES').parentElement as HTMLElement
    // Configured registry keeps its row at zero; the stale tag gets its own.
    expect(within(rail).getByText('Built-in · kirocrew')).toBeInTheDocument()
    expect(within(rail).getByText('kirodotdev-labs')).toBeInTheDocument()
    expect(within(rail).getByText('ghost-registry')).toBeInTheDocument()
    expect(within(rail).getByText('Kiro Crew registry')).toBeInTheDocument()
    expect(within(rail).getByText('0 apps')).toBeInTheDocument()
  })
})

describe('pickFeatured', () => {
  const app = (over: Partial<RegistryApp>): RegistryApp => ({
    name: 'x', displayName: 'X', description: '', version: '1.0.0', author: '', installed: false, ...over,
  })

  it('ignores a featured flag published by an external registry', () => {
    const picked = pickFeatured([
      app({ name: 'plain', displayName: 'Plain' }),
      app({ name: 'shouty', displayName: 'Shouty', featured: 0, _registry: 'evil' }),
    ])
    // The external row cannot buy the first slot; it falls to deterministic order.
    expect(picked.map(a => a.name)).toEqual(['plain', 'shouty'])
  })

  it('drops a provenance-external row out of the flagged group too', () => {
    const picked = pickFeatured([
      app({ name: 'plain', displayName: 'Plain' }),
      app({ name: 'sneaky', displayName: 'Sneaky', featured: 0, provenance: 'external' }),
    ])
    expect(picked[0].name).toBe('plain')
  })

  it('orders numbered flags first, then hero art, then verified, then name', () => {
    const picked = pickFeatured([
      app({ name: 'named-last', displayName: 'Zeta' }),
      app({ name: 'verified', displayName: 'Alpha', verified: true }),
      app({ name: 'art', displayName: 'Beta', heroImage: '/hero.png' }),
      app({ name: 'flag-2', displayName: 'Second', featured: 2 }),
      app({ name: 'flag-1', displayName: 'First', featured: 1 }),
    ])
    expect(picked.map(a => a.name)).toEqual(['flag-1', 'flag-2', 'art'])
  })

  it('breaks a shared flag rank by display name', () => {
    const picked = pickFeatured([
      app({ name: 'b', displayName: 'Bravo', featured: true }),
      app({ name: 'a', displayName: 'Alpha', featured: true }),
    ])
    expect(picked.map(a => a.name)).toEqual(['a', 'b'])
  })
})
