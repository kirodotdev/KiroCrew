/**
 * Issue #3702: one Browse-tab card whose render throws must NOT unmount the
 * whole /apps route. Each Browse render site — the featured spotlight, the
 * secondary feature cards, and the app list rows — is wrapped in an
 * ErrorBoundary that renders a compact degraded placeholder in place of the
 * broken card, while sibling cards and the page chrome keep rendering.
 * (Follow-up to the Library-card boundary added for #3689.)
 *
 * Each card component is mocked to throw for one specific app so the tests
 * stay deterministic regardless of the real components' own guards. Throwing
 * is driven by app IDENTITY (not a mutable counter): React re-invokes a
 * throwing render to rebuild the component stack, so a "throw once" mock
 * would silently pass on the retry.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { i18nT } from '../i18n/t'

// --- Mocks -----------------------------------------------------------------
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

vi.mock('../components/appstore/FeaturedSpotlight', () => ({
  default: ({ apps, title }: { apps: { name: string }[]; title?: string }) => {
    // Member-driven throw, plus a section-field-driven one: `title ===
    // 'zzq-crash-title'` models a crash caused by a section-level field
    // (title/blurb/artwork) that a corrected payload fixes without changing
    // the member refs (the section-boundary latched-recovery case).
    if (apps[0]?.name === 'zzq-broken-spot' || title === 'zzq-crash-title') throw new Error('zzq-spotlight-render-broke')
    return <div data-testid={`zzq-spot-${apps[0]?.name}`} />
  },
}))

vi.mock('../components/appstore/FeatureCard', () => ({
  default: ({ app }: { app: { name: string } }) => {
    if (app.name === 'zzq-broken-feature') throw new Error('zzq-feature-card-render-broke')
    return <div data-testid={`zzq-feature-${app.name}`} />
  },
}))

vi.mock('../components/appstore/AppListRow', () => ({
  default: ({ app }: { app: { name: string; description?: string } }) => {
    // Identity-driven throw, plus a data-driven one: `description ===
    // 'zzq-crash'` models a crash caused by a metadata field that a corrected
    // payload fixes WITHOUT changing name or version (the latched-boundary
    // recovery case).
    if (app.name === 'zzq-broken-row' || app.description === 'zzq-crash') throw new Error('zzq-list-row-render-broke')
    return <div data-testid={`zzq-row-${app.name}`} />
  },
}))

import AppsPage from '../pages/AppsPage'

/** Minimal registry row; `normalizeRegistryApp` fills the rest. */
function registryApp(name: string, displayName: string) {
  return { name, displayName, description: 'zzq', version: '1.0.0', author: 'zzq', tags: [] }
}

function setup(apps: { name: string; displayName: string }[], editorialSections: unknown[] = []) {
  listApps.mockResolvedValue([])
  listRegistry.mockResolvedValue({ apps, editorialSections })
  listRegistries.mockResolvedValue({ registries: [] })
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/apps']}>
        <Routes>
          <Route path="/apps" element={<AppsPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return qc
}

describe('AppsPage Browse-tab per-card error boundaries (#3702)', () => {
  beforeEach(() => {
    sessionStorage.clear()
    // The boundary journals the caught throw (by design); keep test output clean.
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })
  afterEach(() => {
    vi.restoreAllMocks()
    listApps.mockReset(); listRegistry.mockReset(); listRegistries.mockReset()
  })

  it('a throwing app list row degrades alone; sibling rows and chrome survive', async () => {
    // Featured (top 3 by displayName): Alpha (spotlight), Beta + Casa
    // (secondary). Zulu only renders as a list row — the throwing site.
    setup([
      registryApp('alpha', 'Alpha'),
      registryApp('beta', 'Beta'),
      registryApp('casa', 'Casa'),
      registryApp('zzq-broken-row', 'Zulu'),
    ])
    renderPage()

    // Fallback shows the app label + the i18n'd notice…
    expect(await screen.findByText('Zulu')).toBeInTheDocument()
    expect(screen.getByText(i18nT('pages.appsPage.this_app_could_not_be_displayed'))).toBeInTheDocument()
    // …while sibling rows and the spotlight still render.
    expect(screen.getByTestId('zzq-row-alpha')).toBeInTheDocument()
    expect(screen.getByTestId('zzq-row-beta')).toBeInTheDocument()
    expect(screen.getByTestId('zzq-row-casa')).toBeInTheDocument()
    expect(screen.getByTestId('zzq-spot-alpha')).toBeInTheDocument()
    expect(screen.queryByTestId('zzq-row-zzq-broken-row')).not.toBeInTheDocument()
  })

  it('a throwing secondary feature card degrades alone; spotlight and rows survive', async () => {
    // Featured: Alpha (spotlight), "Broken" + Casa (secondary).
    setup([
      registryApp('alpha', 'Alpha'),
      registryApp('zzq-broken-feature', 'Broken'),
      registryApp('casa', 'Casa'),
      registryApp('delta', 'Delta'),
    ])
    renderPage()

    expect(await screen.findByText('Broken')).toBeInTheDocument()
    expect(screen.getByText(i18nT('pages.appsPage.this_app_could_not_be_displayed'))).toBeInTheDocument()
    expect(screen.getByTestId('zzq-spot-alpha')).toBeInTheDocument()
    expect(screen.getByTestId('zzq-feature-casa')).toBeInTheDocument()
    // Every list row still renders (AppListRow only throws for zzq-broken-row).
    expect(screen.getByTestId('zzq-row-alpha')).toBeInTheDocument()
    expect(screen.getByTestId('zzq-row-zzq-broken-feature')).toBeInTheDocument()
  })

  it('a throwing featured spotlight degrades alone; feature cards and rows survive', async () => {
    // "AAA Spot" sorts first → spotlight (the throwing site); Beta + Casa
    // become the secondary feature cards.
    setup([
      registryApp('zzq-broken-spot', 'AAA Spot'),
      registryApp('beta', 'Beta'),
      registryApp('casa', 'Casa'),
    ])
    renderPage()

    expect(await screen.findByText('AAA Spot')).toBeInTheDocument()
    expect(screen.getByText(i18nT('pages.appsPage.this_app_could_not_be_displayed'))).toBeInTheDocument()
    expect(screen.getByTestId('zzq-feature-beta')).toBeInTheDocument()
    expect(screen.getByTestId('zzq-feature-casa')).toBeInTheDocument()
    expect(screen.getByTestId('zzq-row-beta')).toBeInTheDocument()
    expect(screen.getByTestId('zzq-row-casa')).toBeInTheDocument()
  })

  it('a throwing editorial section degrades alone; sibling sections and rows survive', async () => {
    // A published layout replaces the derived spotlight/secondary entirely,
    // so this drives the featuredSections.map boundary (dormant in production
    // until a registry publishes sections — locked here so removing the
    // boundary cannot silently restore the whole-route blank).
    setup([
      registryApp('zzq-broken-spot', 'AAA Spot'),
      registryApp('beta', 'Beta'),
      registryApp('casa', 'Casa'),
      registryApp('delta', 'Delta'),
    ], [
      { type: 'collection', title: 'Zzq Picks', appRefs: ['zzq-broken-spot', 'beta'] },
      { type: 'collection', title: 'Zzq Safe', appRefs: ['casa', 'delta'] },
    ])
    renderPage()

    // Section fallback shows the published section title + the section notice…
    expect(await screen.findByText('Zzq Picks')).toBeInTheDocument()
    expect(screen.getByText(i18nT('pages.appsPage.this_section_could_not_be_displayed'))).toBeInTheDocument()
    // …while the sibling section and all list rows still render.
    expect(screen.getByTestId('zzq-spot-casa')).toBeInTheDocument()
    expect(screen.getByTestId('zzq-row-beta')).toBeInTheDocument()
    expect(screen.getByTestId('zzq-row-delta')).toBeInTheDocument()
  })

  it('a corrected payload clears a latched fallback even when name and version are unchanged', async () => {
    // The boundary latches its error state; its key is the row's FULL data
    // identity (cardDataKey), so a metadata-only correction — same name, same
    // version — must still remount the boundary and restore the healthy card.
    const crashing = { ...registryApp('reco', 'Reco'), description: 'zzq-crash' }
    setup([registryApp('alpha', 'Alpha'), registryApp('beta', 'Beta'), crashing])
    const qc = renderPage()

    expect(await screen.findByText('Reco')).toBeInTheDocument()
    expect(screen.getByText(i18nT('pages.appsPage.this_app_could_not_be_displayed'))).toBeInTheDocument()
    expect(screen.queryByTestId('zzq-row-reco')).not.toBeInTheDocument()

    // Corrected registry payload: identical name + version, fixed metadata.
    const { act } = await import('@testing-library/react')
    act(() => {
      qc.setQueryData(['registry'], {
        apps: [registryApp('alpha', 'Alpha'), registryApp('beta', 'Beta'), { ...crashing, description: 'fixed' }],
        categoryOrder: [],
        editorialSections: [],
      })
    })

    expect(await screen.findByTestId('zzq-row-reco')).toBeInTheDocument()
    expect(screen.queryByText(i18nT('pages.appsPage.this_app_could_not_be_displayed'))).not.toBeInTheDocument()
  })

  it('a corrected section-level field clears a latched section fallback with unchanged member refs', async () => {
    // The section boundary key is position + cardDataKey(section), so a
    // correction to a section-level field (title/blurb/artwork) must remount
    // the boundary even though the member refs did not change.
    const apps = [registryApp('alpha', 'Alpha'), registryApp('beta', 'Beta'), registryApp('casa', 'Casa')]
    setup(apps, [{ type: 'collection', title: 'zzq-crash-title', appRefs: ['alpha', 'beta'] }])
    const qc = renderPage()

    expect(await screen.findByText(i18nT('pages.appsPage.this_section_could_not_be_displayed'))).toBeInTheDocument()
    expect(screen.queryByTestId('zzq-spot-alpha')).not.toBeInTheDocument()

    // Corrected payload: same members, fixed section title.
    const { act } = await import('@testing-library/react')
    act(() => {
      qc.setQueryData(['registry'], {
        apps,
        categoryOrder: [],
        editorialSections: [{ type: 'collection', title: 'Zzq Fixed', appRefs: ['alpha', 'beta'] }],
      })
    })

    expect(await screen.findByTestId('zzq-spot-alpha')).toBeInTheDocument()
    expect(screen.queryByText(i18nT('pages.appsPage.this_section_could_not_be_displayed'))).not.toBeInTheDocument()
  })
})
