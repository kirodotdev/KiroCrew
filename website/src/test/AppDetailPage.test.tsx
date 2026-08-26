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
    useCases: ['raw use case'],
    configuration: ['raw configuration'],
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

  it('renders localized use-case and configuration guidance from the manifest', async () => {
    getApp.mockResolvedValue(BUILTIN)
    renderDetail()

    expect(await screen.findByText('Use cases')).toBeInTheDocument()
    expect(screen.getByText('Configuration')).toBeInTheDocument()
    expect(screen.getByText(/second-screen view of live agents/)).toBeInTheDocument()
    expect(screen.getByText(/no credentials or external service/)).toBeInTheDocument()
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

// An external registry app as returned by /api/apps/{name}: its art is declared
// as REPO-RELATIVE paths, which only the blob proxy can serve.
const EXTERNAL = {
  name: 'some-app',
  version: '1.2.0',
  displayName: 'Some App',
  enabled: true,
  origin: 'registry',
  resources: 'gateway',
  lifecycle: 'gateway',
  installed: true,
  sourceUrl: 'https://example.invalid/octocat/some-app',
  manifest: {
    displayName: 'Some App',
    description: 'An external app.',
    iconPath: 'assets/icon.webp',
    heroImage: 'assets/hero.webp',
    heroImageDetail: 'assets/hero-detail.webp',
    screenshots: ['assets/screenshots/one.webp'],
    ui: { pages: [{ route: '/some-app', label: 'Some App', iconUrl: 'icon.svg' }] },
  },
}

/** The registry row for an installed app whose art fields never got enriched. */
const ART_LESS_ROW = {
  name: 'some-app',
  displayName: 'Some App',
  version: '1.2.0',
  repo: 'https://example.invalid/octocat/some-app',
  installed: true,
  origin: 'registry',
}

const proxied = (path: string, repo = 'https://example.invalid/octocat/some-app') =>
  `/api/apps/blob?repo=${encodeURIComponent(repo)}&path=${encodeURIComponent(path)}`

describe('AppDetailPage — installed external app whose registry row carries no art', () => {
  beforeEach(() => {
    getApp.mockReset()
    listRegistry.mockReset()
    system.mockReset()
    system.mockResolvedValue({ hostname: '' })
  })

  it('proxies the manifest icon, banner, and screenshot through the blob proxy', async () => {
    getApp.mockResolvedValue(EXTERNAL)
    listRegistry.mockResolvedValue({
      apps: [ART_LESS_ROW],
      serverPlatform: { os: 'linux', arch: 'x86_64' },
    })
    renderDetail('some-app')

    const icon = await screen.findByTestId('app-icon')
    expect(icon.getAttribute('data-icon-url')).toBe(proxied('assets/icon.webp'))
    expect(document.querySelector(`img[src="${proxied('assets/hero-detail.webp')}"]`))
      .not.toBeNull()
    expect(document.querySelector(`img[src="${proxied('assets/screenshots/one.webp')}"]`))
      .not.toBeNull()
  })

  it('falls back to the recorded install URL when no row and no manifest repo name one', async () => {
    getApp.mockResolvedValue(EXTERNAL)
    listRegistry.mockResolvedValue({ apps: [], serverPlatform: { os: 'linux', arch: 'x86_64' } })
    renderDetail('some-app')

    const icon = await screen.findByTestId('app-icon')
    expect(icon.getAttribute('data-icon-url')).toBe(proxied('assets/icon.webp'))
  })

  it('renders no repo-relative src when no repo can be resolved', async () => {
    // A repo-relative path with nothing to resolve against is unfetchable: the
    // browser would resolve it against /apps/detail/... and get the SPA shell.
    // Degrading to the gradient is the only honest outcome.
    const { sourceUrl: _unused, ...noProvenance } = EXTERNAL
    getApp.mockResolvedValue(noProvenance)
    listRegistry.mockResolvedValue({ apps: [], serverPlatform: { os: 'linux', arch: 'x86_64' } })
    renderDetail('some-app')

    await screen.findByTestId('app-icon')
    const srcs = [...document.querySelectorAll('img')].map((i) => i.getAttribute('src') || '')
    expect(srcs.some((s) => s.startsWith('assets/'))).toBe(false)
  })

  it('resolves a page icon against the app UI route, not the current route', async () => {
    // `iconPath` outranks the page icon, so drop it to reach that fallback.
    const { iconPath: _dropped, ...manifest } = EXTERNAL.manifest
    getApp.mockResolvedValue({ ...EXTERNAL, manifest })
    listRegistry.mockResolvedValue({ apps: [], serverPlatform: { os: 'linux', arch: 'x86_64' } })
    renderDetail('some-app')

    const icon = await screen.findByTestId('app-icon')
    expect(icon.getAttribute('data-icon-url')).toBe('/apps/some-app/ui/icon.svg')
  })

  it('still renders the page when the manifest declares art of the wrong TYPE', async () => {
    // `app.json` is JSON from disk and the installed-app normalizer coerces only
    // some list fields, so wrong types reach this page. They must degrade to no
    // art, not blank the page — the app is already installed, so a crash here
    // leaves the user with no way to reach its Disable/Uninstall controls.
    getApp.mockResolvedValue({
      ...EXTERNAL,
      manifest: {
        ...EXTERNAL.manifest,
        iconPath: {},
        heroImageDetail: 42,
        screenshots: { 0: 'assets/one.svg' },
        screenshotsDark: {},
        ui: { pages: [{ route: '/some-app', label: 'Some App', iconUrl: {} }] },
      },
    })
    listRegistry.mockResolvedValue({ apps: [], serverPlatform: { os: 'linux', arch: 'x86_64' } })
    renderDetail('some-app')

    // The page reached its body: the name renders and the icon slot is present.
    const icon = await screen.findByTestId('app-icon')
    expect(icon.getAttribute('data-icon-url')).toBe('')
    expect(screen.getByText('An external app.')).toBeTruthy()
  })

  it('requests no external host named by the manifest', async () => {
    // Every field an untrusted manifest can point off-origin, including the
    // protocol-relative form a leading-slash test would pass through.
    getApp.mockResolvedValue({
      ...EXTERNAL,
      manifest: {
        ...EXTERNAL.manifest,
        iconPath: 'https://evil.example/icon.png',
        iconUrl: '/\\evil.example/icon2.png',
        heroImageDetail: '//evil.example/banner.png',
        screenshots: ['\\\\evil.example/shot.png'],
        ui: { pages: [{ route: '/some-app', label: 'Some App', iconUrl: 'https://evil.example/p.svg' }] },
      },
    })
    listRegistry.mockResolvedValue({ apps: [], serverPlatform: { os: 'linux', arch: 'x86_64' } })
    renderDetail('some-app')

    const icon = await screen.findByTestId('app-icon')
    expect(icon.getAttribute('data-icon-url')).toBe('')
    const srcs = [...document.querySelectorAll('img')].map((i) => i.getAttribute('src') || '')
    expect(srcs.some((s) => s.includes('evil.example'))).toBe(false)
  })
})

describe('AppDetailPage — malformed registry guidance', () => {
  beforeEach(() => {
    getApp.mockReset()
    listRegistry.mockReset()
    system.mockReset()
    getApp.mockResolvedValue(null)
    system.mockResolvedValue({ hostname: '' })
  })

  it('does not render empty guidance cards for rejected third-party fields', async () => {
    listRegistry.mockResolvedValue({
      apps: [{
        ...ART_LESS_ROW,
        installed: false,
        useCases: 'not an array',
        configuration: ['valid-looking', null],
      }],
      serverPlatform: { os: 'linux', arch: 'x86_64' },
    })
    renderDetail('some-app')

    await screen.findByTestId('app-icon')
    expect(screen.queryByText('Use cases')).toBeNull()
    expect(screen.queryByText('Configuration')).toBeNull()
  })
})
