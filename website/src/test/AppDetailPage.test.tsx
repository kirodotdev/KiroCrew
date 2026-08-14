import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// --- Mocks -----------------------------------------------------------------
const getApp = vi.fn()
const listRegistry = vi.fn()
const system = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    getApp: (...a: unknown[]) => getApp(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    system: (...a: unknown[]) => system(...a),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'light' }) }))

// Capture the props AppIcon receives so we can assert icon resolution without
// depending on AppIcon's internal SVG-fetch/inline behavior.
vi.mock('../components/AppIcon', () => ({
  default: ({ icon, iconUrl }: { icon?: string; iconUrl?: string }) => (
    <div data-testid="app-icon" data-icon={icon || ''} data-icon-url={iconUrl || ''} />
  ),
}))

import AppDetailPage from '../pages/AppDetailPage'

function renderDetail(name = 'agent-worlds') {
  // `useTrustGate` invalidates the ['trusted-apps'] / ['apps'] queries after a
  // grant, so it needs a QueryClient in scope. The app root always provides
  // one; the harness has to as well.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/apps/detail/${name}`]}>
      <Routes>
        <Route path="/apps/detail/:name" element={<AppDetailPage />} />
        <Route path="/apps" element={<div>apps list</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

// A built-in app as returned by /api/apps/{name}: NOT present in the registry
// feed, with icon/hero metadata living on the manifest (preserved via
// AppManifest.extra on the backend).
const BUILTIN = {
  name: 'agent-worlds',
  version: '1.0.0',
  displayName: 'Agent Worlds',
  enabled: false,
  origin: 'builtin',
  resources: 'gateway',
  lifecycle: 'locked',
  installed: true,
  manifest: {
    displayName: 'Agent Worlds',
    description: 'Visualize your agents in interactive pixel-art scenes',
    iconUrl: '/app-assets/worlds/icon.svg',
    heroImage: '/app-assets/worlds/hero-light.svg',
    heroImageDark: '/app-assets/worlds/hero-dark.svg',
    ui: { pages: [{ route: '/worlds', label: 'Worlds', icon: 'Gamepad2' }] },
  },
}

describe('AppDetailPage — built-in icon/hero resolution', () => {
  beforeEach(() => {
    getApp.mockReset()
    listRegistry.mockReset()
    system.mockReset()
    system.mockResolvedValue({ hostname: '' })
    // Registry feed never contains built-ins — the condition under which the
    // detail page must resolve the icon/hero from the manifest instead of
    // falling back to a generic Package icon and no hero.
    listRegistry.mockResolvedValue({ apps: [], serverPlatform: { os: 'linux', arch: 'x86_64' } })
  })

  it('resolves the icon from the manifest for a built-in absent from the registry', async () => {
    getApp.mockResolvedValue(BUILTIN)
    renderDetail()

    const icon = await screen.findByTestId('app-icon')
    expect(icon.getAttribute('data-icon-url')).toBe('/app-assets/worlds/icon.svg')
  })

  it('renders the hero banner from the manifest heroImage (light theme)', async () => {
    getApp.mockResolvedValue(BUILTIN)
    renderDetail()

    await screen.findByTestId('app-icon')
    const hero = document.querySelector('img[src="/app-assets/worlds/hero-light.svg"]')
    expect(hero).not.toBeNull()
  })

  it('shows no hero banner when the built-in ships no hero image', async () => {
    getApp.mockResolvedValue({
      ...BUILTIN,
      manifest: {
        displayName: 'Agent Worlds',
        description: 'no hero here',
        iconUrl: '/app-assets/worlds/icon.svg',
        ui: { pages: [{ route: '/worlds', label: 'Worlds', icon: 'Gamepad2' }] },
      },
    })
    renderDetail()

    await screen.findByTestId('app-icon')
    // Icon still resolves, but no hero <img> is present.
    expect(document.querySelector('img[src^="/app-assets/worlds/hero"]')).toBeNull()
  })
})
