/**
 * AppsPage — action paths, uninstall dialog, and failure branches.
 *
 * `AppsPageDiscover.test.tsx` covers the editorial/browse rendering surface.
 * This file covers what it does not touch: the navigation helpers (Cmd-click,
 * `autoAction` router state), the enable path including the trust-denied
 * consent hand-off, the Library disable toast, Update All's failure report,
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

import AppsPage, { pickFeatured } from '../pages/AppsPage'
import type { RegistryApp } from '../components/appstore/types'

/** Detail-route probe: reports the app name and the `autoAction` router state. */
function DetailProbe() {
  const loc = useLocation()
  const state = loc.state as { autoAction?: string } | null
  return (
    <div data-testid="detail-route" data-path={loc.pathname} data-auto={state?.autoAction || ''} />
  )
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/apps']}>
        <Routes>
          <Route path="/apps" element={<AppsPage />} />
          <Route path="/apps/detail/:name" element={<DetailProbe />} />
          <Route path="/apps/:name" element={<DetailProbe />} />
          <Route path="/secretary-ui" element={<div data-testid="app-ui-route" />} />
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

const NO_DEPS = { dependencies: { removable: [], shared: [], userInstalled: [] } }

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers({ shouldAdvanceTime: true })
  sessionStorage.clear()
  listApps.mockResolvedValue([BUILTIN_OFF, SECRETARY])
  listRegistry.mockResolvedValue({ apps: REGISTRY_APPS })
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

const goLibrary = () => fireEvent.click(screen.getByText('Library'))

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
    expect(await screen.findByText('Trust Pets to run its own code?')).toBeInTheDocument()
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
    expect(screen.queryByText('Trust Pets to run its own code?')).toBeNull()
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
    expect(screen.getByText('Add an app source (gear icon above) or install from a local path.')).toBeInTheDocument()
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
    renderPage()
    await screen.findByText('No apps available')
    goLibrary()
    expect(await screen.findByText('No apps installed yet')).toBeInTheDocument()
  })

  it('shows the no-match Library state when the search excludes every installed app', async () => {
    renderPage()
    await catalogReady()
    goLibrary()
    await screen.findByText('Slack inbox manager.')
    fireEvent.change(screen.getByLabelText('Search apps'), { target: { value: 'nothing-matches-this' } })
    expect(await screen.findByText('No matching apps')).toBeInTheDocument()
    expect(screen.getByText('Try a different search term')).toBeInTheDocument()
  })

  it('matches an installed app by its manifest tags', async () => {
    renderPage()
    await catalogReady()
    goLibrary()
    await screen.findByText('Slack inbox manager.')
    fireEvent.change(screen.getByLabelText('Search apps'), { target: { value: 'inbox' } })
    // The tag match keeps the row; nothing falls through to the empty state.
    await waitFor(() => expect(screen.queryByText('No matching apps')).toBeNull())
    expect(screen.getByText('Slack inbox manager.')).toBeInTheDocument()
  })
})

describe('AppsPage — Library actions', () => {
  it('opens the app UI page from the card and the detail page from its name', async () => {
    renderPage()
    await catalogReady()
    goLibrary()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    expect(await screen.findByTestId('app-ui-route')).toBeInTheDocument()
  })

  it('routes to the detail page when the card name is clicked', async () => {
    renderPage()
    await catalogReady()
    goLibrary()
    fireEvent.click(await screen.findByRole('button', { name: 'Secretary' }))
    const probe = await screen.findByTestId('detail-route')
    expect(probe).toHaveAttribute('data-path', '/apps/detail/secretary')
    expect(probe).toHaveAttribute('data-auto', '')
  })

  it('toasts after disabling a builtin and clears the toast on dismiss', async () => {
    listApps.mockResolvedValue([{ ...BUILTIN_OFF, enabled: true }])
    renderPage()
    await catalogReady()
    goLibrary()
    fireEvent.click(await screen.findByRole('button', { name: 'Disable' }))
    await waitFor(() => expect(disableApp).toHaveBeenCalledWith('pets'))
    expect(await screen.findByText('Disabled. You can re-enable it from the Discover tab.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss message' }))
    await waitFor(() =>
      expect(screen.queryByText('Disabled. You can re-enable it from the Discover tab.')).toBeNull())
  })

  it('reports a failed disable with the action-failed message', async () => {
    disableApp.mockRejectedValue({})
    renderPage()
    await catalogReady()
    goLibrary()
    fireEvent.click(await screen.findByRole('button', { name: 'Disable' }))
    expect(await screen.findByText('Failed to disable secretary')).toBeInTheDocument()
  })

  it('names the apps that failed during Update All', async () => {
    updateApp.mockRejectedValue(new Error('clone failed'))
    renderPage()
    await catalogReady()
    goLibrary()
    fireEvent.click(await screen.findByRole('button', { name: 'Update All' }))
    expect(await screen.findByText('Failed to update: secretary')).toBeInTheDocument()
  })

  it('reports success after Update All and shows no error', async () => {
    renderPage()
    await catalogReady()
    goLibrary()
    fireEvent.click(await screen.findByRole('button', { name: 'Update All' }))
    expect(await screen.findByText('Updated 1 app.')).toBeInTheDocument()
    expect(screen.queryByText(/Failed to update/)).toBeNull()
  })
})

describe('AppsPage — uninstall dialog', () => {
  const openDialog = async () => {
    renderPage()
    await catalogReady()
    goLibrary()
    fireEvent.click(await screen.findByRole('button', { name: 'Uninstall' }))
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

    fireEvent.click(screen.getByRole('button', { name: 'Uninstall' }))
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

  it('enables from the spotlight and Gets from the feature card', async () => {
    // Pets sorts ahead of Zeta App (both verified), so the switched-off builtin
    // takes the spotlight and the uninstalled core app takes the feature card.
    listApps.mockResolvedValue([BUILTIN_OFF])
    listRegistry.mockResolvedValue({
      apps: [{
        name: 'zeta-app', displayName: 'Zeta App', author: 'kirocrew', description: 'Later in the alphabet.',
        version: '1.0.0', tags: ['github'], installed: false, provenance: 'core',
      }],
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
    renderPage()
    await catalogReady()
    goLibrary()
    fireEvent.click(await screen.findByRole('button', { name: 'Update' }))
    const probe = await screen.findByTestId('detail-route')
    expect(probe).toHaveAttribute('data-path', '/apps/detail/secretary')
    expect(probe).toHaveAttribute('data-auto', 'update')
    // Navigation only — the page must not call the update endpoint itself.
    expect(updateApp).not.toHaveBeenCalled()
  })

  it('enables a switched-off installed app from the Library', async () => {
    listApps.mockResolvedValue([{ ...SECRETARY, enabled: false }])
    renderPage()
    await catalogReady()
    goLibrary()
    fireEvent.click(await screen.findByRole('button', { name: 'Enable' }))
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
    renderPage()
    await screen.findByText('No apps available')
    goLibrary()
    fireEvent.click(await screen.findByRole('button', { name: 'Enable' }))
    expect(await screen.findByText('Trust Local App to run its own code?')).toBeInTheDocument()
    expect(screen.getByText('https://github.com/z/secretary')).toBeInTheDocument()
  })
})

describe('AppsPage — toast expiry', () => {
  it('the disable toast clears itself after four seconds', async () => {
    listApps.mockResolvedValue([{ ...BUILTIN_OFF, enabled: true }])
    renderPage()
    await catalogReady()
    goLibrary()
    fireEvent.click(await screen.findByRole('button', { name: 'Disable' }))
    await screen.findByText('Disabled. You can re-enable it from the Discover tab.')
    await act(async () => { vi.advanceTimersByTime(4000) })
    expect(screen.queryByText('Disabled. You can re-enable it from the Discover tab.')).toBeNull()
  })

  it('the Update All toast clears itself after four seconds', async () => {
    renderPage()
    await catalogReady()
    goLibrary()
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
