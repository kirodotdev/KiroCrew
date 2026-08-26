/**
 * Issue #3689, part 2: one installed-app card whose render throws must NOT
 * unmount the whole /apps route. Each card in the Library list is wrapped in
 * an ErrorBoundary that renders a compact degraded placeholder (app name +
 * i18n'd notice) in place of the broken card, while sibling cards and the
 * page chrome keep rendering.
 *
 * InstalledAppCard is mocked to throw for one specific app so the test stays
 * deterministic even after the card's own null-guards are fixed.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
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

// Throw from ONE card's render. Driven by app identity (not a mutable
// counter): React re-invokes a throwing render to rebuild the component
// stack, so a "throw once" mock would silently pass on the retry.
// Every other card records the `app` prop it received, so tests can assert
// on the record AppsPage actually hands the card (e.g. that the query
// boundary normalized it).
const cardMock = vi.hoisted(() => ({ apps: [] as { name: string; manifest?: Record<string, unknown> }[] }))
vi.mock('../components/appstore/InstalledAppCard', () => ({
  default: ({ app }: { app: { name: string; manifest?: Record<string, unknown>; _newVersion?: string } }) => {
    if (app.name === 'zzq-broken' || (app.manifest as { description?: string })?.description === 'zzq-crash') {
      throw new Error('zzq-card-render-broke')
    }
    cardMock.apps.push(app)
    return <div data-testid={`zzq-card-${app.name}`} />
  },
}))

import AppsPage from '../pages/AppsPage'

function installed(name: string) {
  return {
    name,
    version: '1.0.0',
    displayName: name,
    enabled: true,
    installedAt: '2026-08-02T00:00:00Z',
    origin: 'registry',
    lifecycle: 'gateway',
    manifest: { name, version: '1.0.0', displayName: name, description: 'zzq', author: 'zzq' },
  }
}

function renderPage(qc?: QueryClient) {
  const client = qc ?? new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/apps']}>
        <Routes>
          <Route path="/apps" element={<AppsPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('AppsPage per-card error boundary (#3689)', () => {
  let consoleError: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    sessionStorage.setItem('appstore-tab', 'library')
    listApps.mockResolvedValue([installed('zzq-healthy'), installed('zzq-broken')])
    listRegistry.mockResolvedValue({ apps: [], categoryOrder: [], editorialSections: [] })
    listRegistries.mockResolvedValue([])
    // The boundary journals the caught throw via console.error by contract.
    consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
  })
  afterEach(() => {
    consoleError.mockRestore()
    sessionStorage.clear()
    vi.clearAllMocks()
  })

  it('keeps the route and sibling cards alive when one card render throws', async () => {
    renderPage()
    // Sibling card still renders — the route was NOT unmounted.
    expect(await screen.findByTestId('zzq-card-zzq-healthy')).toBeInTheDocument()
    // The broken card degrades to the placeholder naming the app.
    expect(screen.getByText('zzq-broken')).toBeInTheDocument()
    expect(screen.getByText(i18nT('pages.appsPage.this_app_could_not_be_displayed'))).toBeInTheDocument()
    // And the crashed card's content is gone, not duplicated.
    expect(screen.queryByTestId('zzq-card-zzq-broken')).not.toBeInTheDocument()
  })

  it('keeps a recovery action on the degraded card (Disable for an enabled app)', async () => {
    renderPage()
    await screen.findByTestId('zzq-card-zzq-healthy')
    // The crashed card replaced the app's whole management surface, so the
    // fallback must not be a dead end: an enabled app keeps a Disable action.
    const disable = screen.getByRole('button', { name: i18nT('components.appstore.installedAppCard.disable') })
    expect(disable).toBeInTheDocument()
  })

  it('a corrected payload clears a latched fallback without a route remount (#3719)', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    listApps.mockResolvedValue([
      installed('zzq-healthy'),
      {
        ...installed('zzq-fixable'),
        manifest: { ...installed('zzq-fixable').manifest, description: 'zzq-crash' },
      },
    ])

    renderPage(qc)

    expect(await screen.findByTestId('zzq-card-zzq-healthy')).toBeInTheDocument()
    expect(screen.getByText('zzq-fixable')).toBeInTheDocument()
    expect(screen.getByText(i18nT('pages.appsPage.this_app_could_not_be_displayed'))).toBeInTheDocument()

    // Simulate query refetch / data correction for the installed app:
    await act(async () => {
      qc.setQueryData(['apps'], [
        installed('zzq-healthy'),
        {
          ...installed('zzq-fixable'),
          manifest: { ...installed('zzq-fixable').manifest, description: 'corrected' },
        },
      ])
    })

    expect(await screen.findByTestId('zzq-card-zzq-fixable')).toBeInTheDocument()
    expect(screen.queryByText(i18nT('pages.appsPage.this_app_could_not_be_displayed'))).not.toBeInTheDocument()
  })
})

describe('AppsPage normalizes installed apps at the query boundary (#3706)', () => {
  beforeEach(() => {
    sessionStorage.setItem('appstore-tab', 'library')
    listRegistry.mockResolvedValue({ apps: [], categoryOrder: [], editorialSections: [] })
    listRegistries.mockResolvedValue([])
    cardMock.apps.length = 0
  })
  afterEach(() => {
    sessionStorage.clear()
    vi.clearAllMocks()
  })

  it('hands the card a normalized record even for a manifest-less API row', async () => {
    // A drifted record can lack the manifest entirely; the query boundary
    // must rebuild it so no render site needs per-site null defenses.
    listApps.mockResolvedValue([
      { name: 'zzq-bare', enabled: true },
      { name: 'zzq-mistyped', enabled: true, manifest: { name: 'zzq-mistyped', tags: 'not-an-array', skills: ['ok', 7] } },
    ])
    renderPage()
    expect(await screen.findByTestId('zzq-card-zzq-bare')).toBeInTheDocument()
    expect(screen.getByTestId('zzq-card-zzq-mistyped')).toBeInTheDocument()

    const bare = cardMock.apps.find(a => a.name === 'zzq-bare')
    expect(bare?.manifest).toBeTruthy()
    expect(bare?.manifest?.agents).toEqual([])
    expect((bare?.manifest?.ui as { pages?: unknown[] })?.pages).toEqual([])

    const mistyped = cardMock.apps.find(a => a.name === 'zzq-mistyped')
    expect(mistyped?.manifest?.tags).toEqual([])
    expect(mistyped?.manifest?.skills).toEqual(['ok'])
  })
})
