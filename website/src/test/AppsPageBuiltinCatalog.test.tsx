import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router-dom'

// --- Mocks (same shape as AppsPageDiscover.test.tsx) -------------------------
const listApps = vi.fn()
const listRegistry = vi.fn()
const listRegistries = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    listApps: (...a: unknown[]) => listApps(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    listRegistries: (...a: unknown[]) => listRegistries(...a),
    updateRegistries: vi.fn(),
    refreshRegistries: vi.fn(),
    enableApp: vi.fn(),
    disableApp: vi.fn(),
    updateApp: vi.fn(),
    uninstallApp: vi.fn(),
    uninstallPreview: vi.fn().mockResolvedValue({ dependencies: { removable: [], shared: [], userInstalled: [] } }),
    installApp: vi.fn(),
    openApp: vi.fn(),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark' }) }))

vi.mock('../components/AppIcon', () => ({
  default: ({ icon, iconUrl }: { icon?: string; iconUrl?: string }) => (
    <div data-testid="app-icon" data-icon={icon || ''} data-icon-url={iconUrl || ''} />
  ),
}))

vi.mock('../components/SegmentedControl', () => ({
  default: ({ segments, onChange }: {
    segments: { key: string; label: string }[]
    onChange: (key: string) => void
  }) => (
    <div>
      {segments.map(s => (
        <button key={s.key} type="button" onClick={() => onChange(s.key)}>{s.label}</button>
      ))}
    </div>
  ),
}))

import AppsPage from '../pages/AppsPage'

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function renderPage() {
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/apps']}>
        <Routes>
          <Route path="/apps" element={<AppsPage />} />
          <Route path="/apps/detail/:name" element={<div data-testid="detail-route" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/**
 * The published catalog is the storefront for BUILT-IN rows too: when the
 * registry response carries a builtin row (server-enriched with origin,
 * install state, and trust fields), that row is what renders — the locally
 * installed manifest only fills fields the catalog does not publish, and local
 * synthesis is the OFFLINE FALLBACK. Before this contract, AppsPage filtered
 * server rows out by name and re-synthesized builtin cards from
 * GET /api/apps, so the catalog's curated builtin artwork and taxonomy were
 * dead data.
 *
 * SCOPE: the words are NOT the catalog's to supply for a builtin.
 * `appDisplayName` / `appDescription` resolve a first-party builtin through
 * the i18n catalog (`APP_MANIFEST_KEY`), which is deliberate — builtin copy is
 * translated into every shipped locale while the catalog document is
 * English-only. What the catalog does own here is artwork, taxonomy, author,
 * and the row's existence plus its server-stamped trust/state. `version` runs
 * the other way: it describes the local install, so the installed value wins.
 */
const LOCAL_BUILTIN = {
  name: 'meetings', displayName: 'Meetings', version: '1.0.0', enabled: true,
  installedAt: '2026-07-01T00:00:00Z', origin: 'builtin', resources: 'gateway', lifecycle: 'locked',
  manifest: {
    name: 'meetings', version: '1.0.0', displayName: 'Meetings',
    description: 'Local manifest copy.', author: 'kirocrew',
    // Local taxonomy and NO artwork: the catalog row below carries different
    // tags and a content-addressed icon, so which source won is observable.
    tags: ['productivity'],
  },
}

const SERVER_BUILTIN_ROW = {
  name: 'meetings', displayName: 'Meetings', author: 'Kiro Crew',
  description: 'Curated catalog copy.', version: '1.2.0',
  tags: ['code-review'], installed: true, enabled: true, updateAvailable: false,
  origin: 'builtin', lifecycle: 'locked', provenance: 'builtin', verified: true,
  iconUrl: '/api/apps/assets/icons/abc123.png',
  source: { type: 'builtin' },
}

describe('AppsPage — builtin rows come from the catalog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    qc.clear()
    sessionStorage.clear()
    listRegistries.mockResolvedValue({ registries: [] })
  })

  it("renders the catalog row's artwork for an installed builtin", async () => {
    listApps.mockResolvedValue([LOCAL_BUILTIN])
    listRegistry.mockResolvedValue({ apps: [SERVER_BUILTIN_ROW], serverPlatform: { os: 'darwin', arch: 'arm64' } })
    renderPage()
    await screen.findAllByRole('button', { name: /View details for Meetings/ })
    // The content-addressed icon exists only on the catalog row; the local
    // manifest publishes none, so this asserts the catalog row reached render.
    const icons = screen.getAllByTestId('app-icon')
    expect(icons.some(el => el.getAttribute('data-icon-url') === '/api/apps/assets/icons/abc123.png')).toBe(true)
  })

  it("classifies the builtin by the catalog's tags, not the local manifest's", async () => {
    listApps.mockResolvedValue([LOCAL_BUILTIN])
    listRegistry.mockResolvedValue({ apps: [SERVER_BUILTIN_ROW], serverPlatform: { os: 'darwin', arch: 'arm64' } })
    renderPage()
    await screen.findAllByRole('button', { name: /View details for Meetings/ })
    // Catalog says code-review -> Developer Tools; the local manifest's
    // 'productivity' must NOT produce a Productivity rail entry.
    expect(screen.getByRole('button', { name: /Developer Tools 1/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Productivity 1/ })).not.toBeInTheDocument()
  })

  it('renders the builtin exactly once (no duplicate synthesized card)', async () => {
    listApps.mockResolvedValue([LOCAL_BUILTIN])
    listRegistry.mockResolvedValue({ apps: [SERVER_BUILTIN_ROW], serverPlatform: { os: 'darwin', arch: 'arm64' } })
    renderPage()
    await screen.findAllByRole('button', { name: /View details for Meetings/ })
    // The rail counts browse rows, so it is the stable duplicate detector: a
    // single app legitimately renders two view-details affordances (spotlight
    // + list row), but the server row and a locally synthesized twin both
    // surviving would read "All apps 2".
    expect(screen.getByRole('button', { name: /All apps 1/ })).toBeInTheDocument()
  })

  it('shows NOTHING local when the registry response is empty (the shelf is the registry)', async () => {
    // EXPLORE HAS TWO SOURCES: official registry + user-added registries. A
    // built-in is on the shelf because the published catalog lists it, never
    // because this client read the wheel's manifests. With an empty registry
    // response the shelf is empty even though a built-in IS installed -- that
    // app stays fully visible under Library, which reads GET /api/apps.
    listApps.mockResolvedValue([LOCAL_BUILTIN])
    listRegistry.mockResolvedValue({ apps: [], serverPlatform: { os: 'darwin', arch: 'arm64' } })
    renderPage()
    // POSITIVE settle beacon first: the Discover empty state only renders once
    // the registry query has resolved AND the shelf is genuinely empty, so the
    // absence assertion below cannot pass merely by running early.
    await screen.findByText(/no apps available/i)
    expect(screen.queryByRole('button', { name: /View details for Meetings/ })).not.toBeInTheDocument()
  })

  it('refuses the built-in label to an external row name-squatting an installed builtin', async () => {
    // The server stamps `origin` from the INSTALLED app of the same name, so this
    // row arrives claiming origin:'builtin' while `_registry`/`provenance` say
    // external. Its own copy is all that renders -- nothing is merged in -- but
    // it must not be counted or badged as first-party.
    const squatter = {
      name: 'meetings', displayName: 'Meetings', author: 'attacker',
      description: 'Untrusted copy.', version: '9.9.9', tags: ['code-review'],
      installed: true, enabled: true, updateAvailable: false,
      origin: 'builtin', lifecycle: 'locked',
      _registry: 'evil-org', provenance: 'external', verified: false,
    }
    listApps.mockResolvedValue([LOCAL_BUILTIN])
    listRegistry.mockResolvedValue({ apps: [squatter], serverPlatform: { os: 'darwin', arch: 'arm64' } })
    renderPage()
    await screen.findAllByRole('button', { name: /View details for Meetings/ })
    // Present as a registry row, but the SOURCES rail must not count it as built-in.
    expect(screen.queryByRole('button', { name: /Built-in 1/ })).not.toBeInTheDocument()
  })

  it('keeps a hidden builtin hidden even when the catalog lists it, and refuses a squatter on its name', async () => {
    const hiddenLocal = {
      ...LOCAL_BUILTIN,
      manifest: { ...LOCAL_BUILTIN.manifest, hidden: true },
    }
    // Two ways in: the catalog's own builtin row, and an EXTERNAL row squatting
    // the hidden name. A hidden built-in is concealed by the wheel's own
    // manifest, so neither may put it (or anything wearing its name) on the shelf.
    const squatterOnHidden = {
      name: 'meetings', displayName: 'Meetings', author: 'attacker',
      description: 'Untrusted copy.', version: '9.9.9',
      installed: true, origin: 'builtin', _registry: 'evil-org',
      provenance: 'external', verified: false,
    }
    const beacon = {
      name: 'secretary', displayName: 'Secretary', author: 'zezhexu',
      description: 'Slack inbox manager.', version: '1.1.0',
      installed: false, updateAvailable: false,
    }
    listApps.mockResolvedValue([hiddenLocal])
    listRegistry.mockResolvedValue({ apps: [SERVER_BUILTIN_ROW, squatterOnHidden, beacon], serverPlatform: { os: 'darwin', arch: 'arm64' } })
    renderPage()
    await screen.findAllByRole('button', { name: /View details for Secretary/ })
    expect(screen.queryByRole('button', { name: /View details for Meetings/ })).not.toBeInTheDocument()
    expect(screen.queryByText(/attacker/)).not.toBeInTheDocument()
  })

  it('drops a catalog-only builtin this wheel does not ship (no dead Install control)', async () => {
    // source.type === 'builtin' but nothing installed locally: a builtin has
    // no install coordinates, so rendering the generic Install card would
    // offer a control that cannot work.
    const catalogOnlyBuiltin = {
      name: 'future-app', displayName: 'Future App', author: 'Kiro Crew',
      description: 'Ships in a newer Kiro Crew.', version: '9.9.9',
      installed: false, updateAvailable: false,
      source: { type: 'builtin' },
    }
    listApps.mockResolvedValue([LOCAL_BUILTIN])
    listRegistry.mockResolvedValue({ apps: [SERVER_BUILTIN_ROW, catalogOnlyBuiltin], serverPlatform: { os: 'darwin', arch: 'arm64' } })
    renderPage()
    await screen.findAllByRole('button', { name: /View details for Meetings/ })
    expect(screen.queryByText(/Future App/)).not.toBeInTheDocument()
  })
})
