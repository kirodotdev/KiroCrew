import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router-dom'

// --- Mocks -----------------------------------------------------------------
const listApps = vi.fn()
const listRegistry = vi.fn()
const listRegistries = vi.fn()
const enableApp = vi.fn()
const updateApp = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    listApps: (...a: unknown[]) => listApps(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    listRegistries: (...a: unknown[]) => listRegistries(...a),
    updateRegistries: vi.fn(),
    refreshRegistries: vi.fn(),
    enableApp: (...a: unknown[]) => enableApp(...a),
    disableApp: vi.fn(),
    updateApp: (...a: unknown[]) => updateApp(...a),
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

// SegmentedControl measures its container (0px in jsdom) and collapses to a
// dropdown, hiding tab labels — stub it with plain buttons.
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

const REGISTRY_APPS = [
  {
    name: 'code-review-sage', displayName: 'Code Review Sage', author: 'kirocrew',
    description: 'Self-evolving deep code reviewer for GitHub PRs.', version: '3.2.0',
    tags: ['code-review', 'github'], featured: 1, installed: false, updateAvailable: false,
    heroImage: '/api/apps/blob?repo=Sage&path=hero-light.png',
    heroImageDark: '/api/apps/blob?repo=Sage&path=hero-dark.png',
  },
  {
    name: 'oncall-radar', displayName: 'Oncall Radar', author: 'kirocrew',
    description: 'Oncall operations dashboard.', version: '2.0.0',
    tags: ['oncall', 'tickets'], featured: 2, installed: false, updateAvailable: false,
  },
  {
    name: 'secretary', displayName: 'Secretary', author: 'zezhexu',
    description: 'Slack inbox manager.', version: '1.1.0', _registry: 'kirodotdev-labs',
    tags: ['slack', 'inbox'], installed: true, updateAvailable: true,
  },
]

const INSTALLED = [
  {
    name: 'secretary', displayName: 'Secretary', version: '1.0.0', enabled: true,
    installedAt: '2026-07-01T00:00:00Z', origin: 'registry', resources: 'gateway', lifecycle: 'gateway',
    manifest: { name: 'secretary', version: '1.0.0', displayName: 'Secretary', description: 'Slack inbox manager.', author: 'zezhexu', tags: ['slack', 'inbox'] },
  },
]

describe('AppsPage — hybrid Discover', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    qc.clear()
    sessionStorage.clear()
    listApps.mockResolvedValue(INSTALLED)
    listRegistry.mockResolvedValue({ apps: REGISTRY_APPS, serverPlatform: { os: 'darwin', arch: 'arm64' } })
    listRegistries.mockResolvedValue({ registries: [{ name: 'kirodotdev-labs', repo: 'https://github.com/kirodotdev-labs/registry', branch: 'main' }] })
  })

  it('lands on Discover with the top-flagged app as spotlight', async () => {
    renderPage()
    // Spotlight renders the FEATURED kicker with the featured=1 app
    await screen.findByText('FEATURED')
    const heading = await screen.findByRole('heading', { level: 2, name: 'Code Review Sage' })
    expect(heading).toBeInTheDocument()
    // Secondary feature card shows the featured=2 app: it renders twice —
    // once as the feature card, once in the All-apps list below.
    expect(screen.getAllByRole('button', { name: 'View details for Oncall Radar' })).toHaveLength(2)
  })

  it('shows category rail with counts and sources with provenance', async () => {
    renderPage()
    await screen.findByText('FEATURED')
    expect(screen.getByRole('button', { name: /All apps 3/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Developer Tools 1/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /On-call & Ops 1/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Productivity 1/ })).toBeInTheDocument()
    // Sources block: external registry row with its app count
    expect(screen.getByText('kirodotdev-labs')).toBeInTheDocument()
    expect(screen.getByText('1 app')).toBeInTheDocument()
  })

  it('selecting a category filters the list and hides the editorial layer', async () => {
    renderPage()
    await screen.findByText('FEATURED')
    fireEvent.click(screen.getByRole('button', { name: /On-call & Ops 1/ }))
    await waitFor(() => expect(screen.queryByText('FEATURED')).not.toBeInTheDocument())
    // Section heading switches to the category, list shows only the match
    expect(screen.getByRole('heading', { name: 'On-call & Ops' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /View details for Oncall Radar/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /View details for Code Review Sage/ })).not.toBeInTheDocument()
  })

  it('search filters rows and hides editorial; row click routes to detail', async () => {
    renderPage()
    await screen.findByText('FEATURED')
    fireEvent.change(screen.getByLabelText('Search apps'), { target: { value: 'secretary' } })
    await waitFor(() => expect(screen.queryByText('FEATURED')).not.toBeInTheDocument())
    const row = screen.getByRole('button', { name: /View details for Secretary/ })
    // Installed app row shows update action; provenance line carries the registry
    expect(within(row).getByText(/kirodotdev-labs/)).toBeInTheDocument()
    fireEvent.click(row)
    expect(await screen.findByTestId('detail-route')).toBeInTheDocument()
  })

  it('shows the pending-updates banner on Library and runs Update All', async () => {
    updateApp.mockResolvedValue({ ok: true })
    renderPage()
    await screen.findByText('FEATURED')
    fireEvent.click(screen.getByText('Library'))
    expect(await screen.findByText('1 update available')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Update All' }))
    await waitFor(() => expect(updateApp).toHaveBeenCalledWith('secretary'))
  })

  it('persists and migrates the stored tab (installed → library)', async () => {
    sessionStorage.setItem('appstore-tab', 'installed')
    renderPage()
    // Lands on Library (migrated), which shows the installed management card
    expect(await screen.findByText('1 update available')).toBeInTheDocument()
    expect(sessionStorage.getItem('appstore-tab')).toBe('library')
  })
})

describe('AppsPage — hero art and provenance trust', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    qc.clear()
    sessionStorage.clear()
    listApps.mockResolvedValue(INSTALLED)
    listRegistry.mockResolvedValue({ apps: REGISTRY_APPS, serverPlatform: { os: 'darwin', arch: 'arm64' } })
    listRegistries.mockResolvedValue({ registries: [{ name: 'kirodotdev-labs', repo: 'https://github.com/kirodotdev-labs/registry', branch: 'main' }] })
  })

  it('renders developer hero art on the editorial surface only, theme-appropriate', async () => {
    renderPage()
    await screen.findByText('FEATURED')
    // EXACTLY one, not "at least one": this app is featured, so the spotlight is
    // the only surface that renders its art. It used to be two -- the spotlight
    // and the dense row -- and a row rendering a 96x54 crop of marketing art is
    // the thing that changed. Asserting the exact count is what makes a
    // regression that puts art back into rows fail here.
    const heroes = document.querySelectorAll('img[src="/api/apps/blob?repo=Sage&path=hero-dark.png"]')
    expect(heroes.length).toBe(1)
    // useTheme is stubbed to dark, so the light asset is never requested.
    expect(document.querySelector('img[src="/api/apps/blob?repo=Sage&path=hero-light.png"]')).toBeNull()
  })

  it('falls back to gradient art when a hero image fails to load', async () => {
    renderPage()
    await screen.findByText('FEATURED')
    const sel = 'img[src="/api/apps/blob?repo=Sage&path=hero-dark.png"]'
    const before = document.querySelectorAll(sel).length
    // Fail the first hero (the spotlight); it must drop to gradient art while
    // the other surfaces keep their own independent attempt.
    fireEvent.error(document.querySelector(sel) as HTMLImageElement)
    await waitFor(() => {
      expect(document.querySelectorAll(sel).length).toBe(before - 1)
    })
  })

  it('does not badge an external app that claims kirocrew authorship', async () => {
    listRegistry.mockResolvedValue({
      apps: [
        { name: 'impostor', displayName: 'Impostor', author: 'KiroCrew', _registry: 'evil', description: 'Trust me.', version: '9.9.9', tags: ['github'], installed: false },
      ],
      serverPlatform: { os: 'darwin', arch: 'arm64' },
    })
    renderPage()
    // A one-app catalog renders it in both the spotlight and the dense list;
    // the badge must be absent from every surface.
    await screen.findByText('FEATURED')
    const surfaces = screen.getAllByRole('button', { name: /View details for Impostor/ })
    for (const s of surfaces) expect(within(s).queryByLabelText('Verified publisher')).toBeNull()
    // Provenance is still surfaced so the user can see where it came from.
    expect(within(surfaces[surfaces.length - 1]).getByText(/evil/)).toBeInTheDocument()
  })

  it('renders a minimal registry entry (failed manifest fetch) without crashing', async () => {
    listRegistry.mockResolvedValue({
      apps: [{ name: 'orphan-app', repo: 'Orphan' }],
      serverPlatform: { os: 'darwin', arch: 'arm64' },
    })
    renderPage()
    // Falls back to the app name; no TypeError during sort/filter/render.
    await screen.findByText('FEATURED')
    expect(screen.getAllByRole('button', { name: /View details for orphan-app/ }).length).toBeGreaterThan(0)
  })
})
