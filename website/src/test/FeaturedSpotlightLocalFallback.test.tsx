/**
 * FeaturedSpotlight local-art fallback (#6887) — the port of #6804/#6864's
 * two-latch second chance to the Discover page's featured band.
 *
 * An INSTALLED lead app's registry hero stays the primary `src` (no precedence
 * change — #6804 rejects a flip), but when that asset fails to LOAD the app's
 * own bytes are on local disk, so the band must swap once to the installed-app
 * art route instead of degrading straight to the designed gradient. When the
 * fallback fails too — or the lead is not installed — the gradient stays the
 * terminal state, exactly as before.
 *
 * Per the standard #6804 set, these tests FIRE the element's `error` event and
 * assert the rendered `src` actually changed — asserting a handler is attached
 * proves nothing. The suite also locks the default-inert contract for callers
 * that pass no installed source (every pre-existing `useHeroArt` caller).
 */
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mockTheme = vi.hoisted(() => ({ value: 'light' as 'light' | 'dark' }))
vi.mock('../hooks/useTheme', () => ({
  useTheme: () => ({ theme: mockTheme.value }),
}))

vi.mock('../components/AppIcon', () => ({
  default: () => <div data-testid="app-icon" />,
}))

// The DiscoverPage threading tests below need the api surface useAppsData
// reads; the hook- and component-level suites never touch it, so one
// file-level mock serves all three.
const { listApps, listRegistry, listRegistries } = vi.hoisted(() => ({
  listApps: vi.fn(),
  listRegistry: vi.fn(),
  listRegistries: vi.fn(),
}))
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
    uninstallPreview: vi.fn(),
    installApp: vi.fn(),
    openApp: vi.fn(),
  },
}))

import FeaturedSpotlight from '../components/appstore/FeaturedSpotlight'
import DiscoverPage from '../pages/apps/DiscoverPage'
import { useHeroArt, type InstalledArtSource } from '../components/appstore/useHeroArt'
import type { RegistryApp } from '../components/appstore/types'

// Registry (blob-proxy) primaries.
const R_HERO = '/api/apps/blob?repo=demo%2Fdemo-app&path=assets%2Fhero.png'
const R_HERO_DARK = '/api/apps/blob?repo=demo%2Fdemo-app&path=assets%2Fhero-dark.png'
// Local install routes the manifest paths resolve to.
const LOCAL_HERO = '/apps/demo-app/art/assets/hero.png'
const LOCAL_HERO_DARK = '/apps/demo-app/art/assets/hero-dark.png'
const LOCAL_SHOT = '/apps/demo-app/art/shots/a.png'

const HERO_APP = {
  heroImage: R_HERO,
  heroImageDark: R_HERO_DARK,
  screenshots: [],
} as unknown as RegistryApp

const INSTALLED: InstalledArtSource = {
  name: 'demo-app',
  manifest: { heroImage: 'assets/hero.png', heroImageDark: 'assets/hero-dark.png' },
}

beforeEach(() => { mockTheme.value = 'light' })

describe('useHeroArt — local second chance (#6887)', () => {
  function mount(app?: RegistryApp, installed?: InstalledArtSource) {
    return renderHook(
      ({ a, i }: { a?: RegistryApp; i?: InstalledArtSource }) => useHeroArt(a, i),
      { initialProps: { a: app, i: installed } },
    )
  }

  it('primary error swaps src to the local route; a second error latches to the gradient', () => {
    const { result } = mount(HERO_APP, INSTALLED)
    expect(result.current.src).toBe(R_HERO)
    act(() => result.current.onError())
    expect(result.current.src).toBe(LOCAL_HERO)
    act(() => result.current.onError())
    expect(result.current.src).toBe('')
  })

  it('stays behaviour-identical with no installed source: one error latches to the gradient', () => {
    const { result } = mount(HERO_APP)
    expect(result.current.src).toBe(R_HERO)
    act(() => result.current.onError())
    expect(result.current.src).toBe('')
  })

  it('a theme flip changes the resolved primary and resets BOTH latches', () => {
    const { result } = mount(HERO_APP, INSTALLED)
    act(() => result.current.onError())
    act(() => result.current.onError())
    expect(result.current.src).toBe('')
    // The resolved URL changes, so the art deserves a fresh attempt — and the
    // fallback family follows the theme, so a failure there retries dark local.
    act(() => { mockTheme.value = 'dark' })
    const dark = mount(HERO_APP, INSTALLED)
    expect(dark.result.current.src).toBe(R_HERO_DARK)
    act(() => dark.result.current.onError())
    expect(dark.result.current.src).toBe(LOCAL_HERO_DARK)
  })

  it('rerendering with a changed primary clears the terminal latch in place', () => {
    const { result, rerender } = mount(HERO_APP, INSTALLED)
    act(() => result.current.onError())
    act(() => result.current.onError())
    expect(result.current.src).toBe('')
    const next = { ...HERO_APP, heroImage: '/api/apps/blob?repo=demo%2Fdemo-app&path=assets%2Fhero-v2.png' } as RegistryApp
    rerender({ a: next, i: INSTALLED })
    expect(result.current.src).toBe('/api/apps/blob?repo=demo%2Fdemo-app&path=assets%2Fhero-v2.png')
    // And the fallback gets retried on the new primary's failure.
    act(() => result.current.onError())
    expect(result.current.src).toBe(LOCAL_HERO)
  })

  it('skips a fallback identical to the failed primary rather than retrying it', () => {
    // The local candidate already won the primary pick (the row's own field
    // carries the local route); retrying the URL that just errored is a
    // second doomed request.
    const app = { heroImage: LOCAL_HERO, screenshots: [] } as unknown as RegistryApp
    const { result } = mount(app, INSTALLED)
    expect(result.current.src).toBe(LOCAL_HERO)
    act(() => result.current.onError())
    expect(result.current.src).toBe('')
  })

  it('no registry art at all still answers the gradient — the second chance is not a precedence flip', () => {
    const app = { screenshots: [] } as unknown as RegistryApp
    const { result } = mount(app, INSTALLED)
    expect(result.current.src).toBe('')
  })

  it('falls through a refused manifest field to the next usable local candidate', () => {
    // An absolute URL out of a manifest is refused (a third-party manifest
    // must not point this <img> at another host); the next field still serves.
    const { result } = mount(HERO_APP, {
      name: 'demo-app',
      manifest: { heroImage: 'https://evil.example/x.png', screenshots: ['shots/a.png'] },
    })
    act(() => result.current.onError())
    expect(result.current.src).toBe(LOCAL_SHOT)
  })

  it('answers the gradient when every manifest candidate is refused', () => {
    const { result } = mount(HERO_APP, {
      name: 'demo-app',
      manifest: { heroImage: 'https://evil.example/x.png', screenshots: {} },
    })
    act(() => result.current.onError())
    expect(result.current.src).toBe('')
  })
})

describe('FeaturedSpotlight — installed lead swaps to local art on a failed registry hero', () => {
  const noop = () => {}

  function lead(over: Partial<RegistryApp> = {}): RegistryApp {
    return {
      name: 'demo-app',
      displayName: 'Demo App',
      description: 'About demo-app.',
      author: 'Kiro Crew',
      version: '1.0.0',
      tags: ['agents'],
      installed: true,
      ...over,
    } as RegistryApp
  }

  function mountCard(props: Partial<Parameters<typeof FeaturedSpotlight>[0]> = {}) {
    return render(
      <FeaturedSpotlight
        type="app"
        apps={[lead({ heroImage: R_HERO } as Partial<RegistryApp>)]}
        onOpenApp={noop}
        onGet={noop}
        onEnable={noop}
        {...props}
      />,
    )
  }

  function imgBySrc(src: string): HTMLImageElement | null {
    return document.querySelector(`img[src="${src}"]`)
  }

  it('with leadInstalled, a failed registry hero swaps to the local route, then the gradient', () => {
    mountCard({ leadInstalled: INSTALLED })
    const primary = imgBySrc(R_HERO)
    expect(primary).not.toBeNull()
    fireEvent.error(primary!)
    expect(imgBySrc(R_HERO)).toBeNull()
    const local = imgBySrc(LOCAL_HERO)
    expect(local).not.toBeNull()
    // Local bytes gone too: terminal is the designed gradient plate, no
    // broken-image frame left in the band.
    fireEvent.error(local!)
    expect(document.querySelector('img')).toBeNull()
    expect(screen.getByTestId('app-icon')).toBeInTheDocument()
  })

  it('without leadInstalled (a non-installed lead), a failed hero degrades straight to the gradient', () => {
    mountCard()
    fireEvent.error(imgBySrc(R_HERO)!)
    expect(document.querySelector('img')).toBeNull()
    expect(screen.getByTestId('app-icon')).toBeInTheDocument()
  })

  it('a curated card is untouched: editorial art keeps its own error path', () => {
    mountCard({
      curated: true,
      artwork: { url: 'https://apps.crew.kiro.dev/assets/editorial/aaa.png' },
      leadInstalled: INSTALLED,
    })
    const img = document.querySelector('img') as HTMLImageElement
    expect(img.getAttribute('src')).toContain('assets/editorial/aaa.png')
    fireEvent.error(img)
    // The editorial latch drops the band (curated cards never borrow the
    // lead's art, local or registry) — the installed source changes nothing.
    expect(document.querySelector('img')).toBeNull()
  })
})

describe('DiscoverPage — threads the installed record to the featured lead (#6887)', () => {
  function installedRow(name: string) {
    return {
      name,
      version: '1.0.0',
      displayName: 'Demo App',
      enabled: true,
      installedAt: '2026-01-01T00:00:00Z',
      origin: 'registry',
      resources: 'app',
      lifecycle: 'app',
      manifest: {
        name,
        version: '1.0.0',
        displayName: 'Demo App',
        description: 'An installed registry app',
        author: 'demo',
        heroImage: 'assets/hero.png',
      },
    }
  }

  function registryRow(name: string, over: Record<string, unknown> = {}) {
    return {
      name,
      displayName: 'Demo App',
      description: 'About it.',
      version: '1.0.0',
      author: 'demo',
      tags: ['agents'],
      featured: 1,
      installed: false,
      heroImage: R_HERO,
      ...over,
    }
  }

  function renderDiscover() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/apps']}>
          <Routes>
            <Route path="/apps" element={<DiscoverPage />} />
            <Route path="/apps/detail/:name" element={<div data-testid="detail-route" />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
  }

  beforeEach(() => {
    listApps.mockReset()
    listRegistry.mockReset()
    listRegistries.mockReset()
    listRegistries.mockResolvedValue({ registries: [] })
  })

  it('an INSTALLED featured lead gets the local second chance end to end', async () => {
    listApps.mockResolvedValue([installedRow('demo-app')])
    listRegistry.mockResolvedValue({
      apps: [registryRow('demo-app', { installed: true })],
      serverPlatform: { os: 'linux', arch: 'x86_64' },
    })
    renderDiscover()
    const primary = await waitFor(() => {
      const el = document.querySelector(`img[src="${R_HERO}"]`)
      expect(el).not.toBeNull()
      return el as HTMLImageElement
    })
    fireEvent.error(primary)
    await waitFor(() => expect(document.querySelector(`img[src="${LOCAL_HERO}"]`)).not.toBeNull())
    expect(document.querySelector(`img[src="${R_HERO}"]`)).toBeNull()
  })

  it('a NON-installed featured lead stays on the plain gradient path', async () => {
    listApps.mockResolvedValue([])
    listRegistry.mockResolvedValue({
      apps: [registryRow('demo-app')],
      serverPlatform: { os: 'linux', arch: 'x86_64' },
    })
    renderDiscover()
    const primary = await waitFor(() => {
      const el = document.querySelector(`img[src="${R_HERO}"]`)
      expect(el).not.toBeNull()
      return el as HTMLImageElement
    })
    fireEvent.error(primary)
    await waitFor(() => expect(document.querySelector('img')).toBeNull())
    expect(document.querySelector(`img[src^="/apps/"]`)).toBeNull()
  })
})
