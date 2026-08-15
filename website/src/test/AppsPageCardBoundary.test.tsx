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

// Throw from ONE card's render. Driven by app identity (not a mutable
// counter): React re-invokes a throwing render to rebuild the component
// stack, so a "throw once" mock would silently pass on the retry.
vi.mock('../components/appstore/InstalledAppCard', () => ({
  default: ({ app }: { app: { name: string } }) => {
    if (app.name === 'zzq-broken') throw new Error('zzq-card-render-broke')
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

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
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
})
