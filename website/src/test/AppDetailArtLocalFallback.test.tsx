/**
 * Local-art fallback for the detail page's hero banner and screenshot strip
 * (#6864) — the port of #6804's icon fix to the page's two remaining art
 * surfaces.
 *
 * An INSTALLED app's registry hero/screenshots stay the primary `src` (no
 * precedence change — #6804 rejects a flip), but when a registry asset fails
 * to LOAD the app's own bytes are on local disk, so the surface must swap to
 * the local install route instead of hiding. When the fallback fails too, the
 * pre-#6864 terminal state (hidden) is preserved.
 *
 * Per the standard #6804 set, these tests FIRE the element's `error` event and
 * assert the rendered `src` actually changed — asserting a handler is attached
 * proves nothing. The suite also locks the default-inert contract for
 * untouched consumers and the not-installed page.
 */
import { fireEvent, render, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const theme = { current: 'light' as 'light' | 'dark' }
vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: theme.current }) }))

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

import AppDetailPage, { HeroBanner, ScreenshotGallery } from '../pages/AppDetailPage'

// Registry (blob-proxy) primaries.
const R_HERO = '/api/apps/blob?repo=demo%2Fdemo-app&path=assets%2Fhero.png'
const R_HERO_DARK = '/api/apps/blob?repo=demo%2Fdemo-app&path=assets%2Fhero-dark.png'
const R_S1 = '/api/apps/blob?repo=demo%2Fdemo-app&path=shots%2Fa.png'
const R_S2 = '/api/apps/blob?repo=demo%2Fdemo-app&path=shots%2Fb.png'
const R_S3 = '/api/apps/blob?repo=demo%2Fdemo-app&path=shots%2Fc.png'
const R_SD1 = '/api/apps/blob?repo=demo%2Fdemo-app&path=shots%2Fa-dark.png'
// Local install routes the manifest paths resolve to.
const LOCAL_HERO = '/apps/demo-app/art/assets/hero.png'
const LOCAL_HERO_DARK = '/apps/demo-app/art/assets/hero-dark.png'
const LOCAL_WIDE = '/apps/demo-app/art/assets/wide.png'
const LOCAL_A = '/apps/demo-app/art/shots/a.png'
const LOCAL_C = '/apps/demo-app/art/shots/c.png'
const LOCAL_AD = '/apps/demo-app/art/shots/a-dark.png'

function imgBySrc(src: string): HTMLImageElement | null {
  return document.querySelector(`img[src="${src}"]`)
}

function heroBox(): HTMLElement | null {
  // The hero container is the page's only aspect-ratio box.
  return document.querySelector('.aspect-video, .aspect-\\[25\\/6\\]')
}

function detailUi(name = 'demo-app') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/apps/detail/${name}`]}>
        <Routes>
          <Route path="/apps/detail/:name" element={<AppDetailPage />} />
          <Route path="/apps" element={<div>apps list</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

function installedApp(manifest: Record<string, unknown>) {
  return {
    name: 'demo-app',
    version: '1.0.0',
    displayName: 'Demo App',
    enabled: true,
    origin: 'registry',
    resources: 'app',
    lifecycle: 'app',
    installed: true,
    manifest: { displayName: 'Demo App', description: 'An installed registry app', ...manifest },
  }
}

function registryRow(fields: Record<string, unknown>) {
  return {
    name: 'demo-app',
    displayName: 'Demo App',
    version: '1.0.0',
    author: 'demo',
    ...fields,
  }
}

async function renderInstalled(manifest: Record<string, unknown>, rowFields: Record<string, unknown>) {
  getApp.mockResolvedValue(installedApp(manifest))
  listRegistry.mockResolvedValue({ apps: [registryRow(rowFields)], serverPlatform: { os: 'linux', arch: 'x86_64' } })
  const result = render(detailUi())
  return result
}

beforeEach(() => {
  theme.current = 'light'
  getApp.mockReset()
  listRegistry.mockReset()
  system.mockReset()
  system.mockResolvedValue({ hostname: '' })
})

describe('AppDetailPage hero — local-art fallback on load failure (#6864)', () => {
  it('registry hero stays primary; on error the src swaps to the local route; a second error unmounts the box', async () => {
    await renderInstalled({ heroImage: 'assets/hero.png' }, { heroImage: R_HERO })

    // Precedence unchanged: the registry asset is what renders.
    const primary = await waitFor(() => {
      const el = imgBySrc(R_HERO)
      expect(el).not.toBeNull()
      return el!
    })
    expect(imgBySrc(LOCAL_HERO)).toBeNull()

    // Registry host unreachable: the src must actually CHANGE to local art.
    fireEvent.error(primary)
    await waitFor(() => expect(imgBySrc(LOCAL_HERO)).not.toBeNull())
    expect(imgBySrc(R_HERO)).toBeNull()

    // Local bytes gone too: terminal state is hidden — the container
    // unmounts, leaving no empty bordered box where the banner was.
    fireEvent.error(imgBySrc(LOCAL_HERO)!)
    await waitFor(() => expect(heroBox()).toBeNull())
    expect(imgBySrc(LOCAL_HERO)).toBeNull()
  })

  it('no swap when the local candidate already won the precedence (identical URL is not retried)', async () => {
    // No registry hero: the primary IS the local route; retrying it after an
    // error is a guaranteed second failure, so the self-match guard hides.
    await renderInstalled({ heroImage: 'assets/hero.png' }, {})
    const primary = await waitFor(() => {
      const el = imgBySrc(LOCAL_HERO)
      expect(el).not.toBeNull()
      return el!
    })
    fireEvent.error(primary)
    await waitFor(() => expect(heroBox()).toBeNull())
  })

  it('cross-tier invariant: a manifest detail banner PROMOTES the primary to the detail tier, so no 25:6 art lands in a 16:9 box', async () => {
    // Registry supplies only a Browse hero, the manifest declares a detail
    // banner. The primary resolution already falls back to the local detail
    // art, so the DETAIL tier wins the primary pick and sizes the container
    // 25:6 — the feared "detail fallback into a 16:9 container" case cannot
    // arise. The local detail banner is the primary here; the registry Browse
    // hero is not rendered at all (detail tier outranks Browse).
    await renderInstalled({ heroImageDetail: 'assets/wide.png' }, { heroImage: R_HERO })
    const primary = await waitFor(() => {
      const el = imgBySrc(LOCAL_WIDE)
      expect(el).not.toBeNull()
      return el!
    })
    expect(heroBox()!.className).toContain('aspect-[25/6]')
    expect(imgBySrc(R_HERO)).toBeNull()
    // And the identical-URL guard holds when this local primary fails.
    fireEvent.error(primary)
    await waitFor(() => expect(heroBox()).toBeNull())
  })

  it('reachable cross-tier case: a failed registry detail banner borrows local Browse art, container ratio keyed on the primary', async () => {
    const R_WIDE = '/api/apps/blob?repo=demo%2Fdemo-app&path=assets%2Fwide.png'
    await renderInstalled({ heroImage: 'assets/hero.png' }, { heroImageDetail: R_WIDE })
    const primary = await waitFor(() => {
      const el = imgBySrc(R_WIDE)
      expect(el).not.toBeNull()
      return el!
    })
    // Ratio keyed on the PRIMARY (a detail banner).
    expect(heroBox()!.className).toContain('aspect-[25/6]')
    fireEvent.error(primary)
    // Same tier has no local candidate; the Browse-tier local art is
    // borrowed rather than showing nothing, and the ratio does not swing.
    await waitFor(() => expect(imgBySrc(LOCAL_HERO)).not.toBeNull())
    expect(heroBox()!.className).toContain('aspect-[25/6]')
  })

  it('prefers the detail-tier local art when the failed primary is a detail banner and both tiers exist locally', async () => {
    const R_WIDE = '/api/apps/blob?repo=demo%2Fdemo-app&path=assets%2Fwide.png'
    await renderInstalled(
      { heroImage: 'assets/hero.png', heroImageDetail: 'assets/wide.png' },
      { heroImageDetail: R_WIDE },
    )
    const primary = await waitFor(() => {
      const el = imgBySrc(R_WIDE)
      expect(el).not.toBeNull()
      return el!
    })
    fireEvent.error(primary)
    // Detail primary -> detail-tier local art, not the Browse hero.
    await waitFor(() => expect(imgBySrc(LOCAL_WIDE)).not.toBeNull())
    expect(imgBySrc(LOCAL_HERO)).toBeNull()
  })

  it('a theme flip re-arms both latches after the terminal state', async () => {
    getApp.mockResolvedValue(installedApp({ heroImage: 'assets/hero.png', heroImageDark: 'assets/hero-dark.png' }))
    listRegistry.mockResolvedValue({
      apps: [registryRow({ heroImage: R_HERO, heroImageDark: R_HERO_DARK })],
      serverPlatform: { os: 'linux', arch: 'x86_64' },
    })
    const { rerender } = render(detailUi())
    const primary = await waitFor(() => {
      const el = imgBySrc(R_HERO)
      expect(el).not.toBeNull()
      return el!
    })
    fireEvent.error(primary)
    await waitFor(() => expect(imgBySrc(LOCAL_HERO)).not.toBeNull())
    fireEvent.error(imgBySrc(LOCAL_HERO)!)
    await waitFor(() => expect(heroBox()).toBeNull())

    // Theme flip: the resolved URL changes, so the banner deserves a fresh
    // attempt — a stale latch here would be a sticky failure.
    theme.current = 'dark'
    rerender(detailUi())
    const darkPrimary = await waitFor(() => {
      const el = imgBySrc(R_HERO_DARK)
      expect(el).not.toBeNull()
      return el!
    })
    fireEvent.error(darkPrimary)
    await waitFor(() => expect(imgBySrc(LOCAL_HERO_DARK)).not.toBeNull())
  })

  it('not-installed page: hero stays hide-on-error with no fallback attempt', async () => {
    getApp.mockRejectedValue(new Error('not installed'))
    listRegistry.mockResolvedValue({
      apps: [registryRow({ heroImage: R_HERO, screenshots: [R_S1] })],
      serverPlatform: { os: 'linux', arch: 'x86_64' },
    })
    render(detailUi())
    const primary = await waitFor(() => {
      const el = imgBySrc(R_HERO)
      expect(el).not.toBeNull()
      return el!
    })
    fireEvent.error(primary)
    // No local bytes exist: terminal on the first error, no second attempt.
    await waitFor(() => expect(heroBox()).toBeNull())
    expect(document.querySelectorAll('img[src^="/apps/"]').length).toBe(0)
  })
})

describe('AppDetailPage screenshots — per-thumbnail local fallback, index-aligned (#6864)', () => {
  const MANIFEST_SHOTS = { screenshots: ['shots/a.png', 'https://evil.example/x.png', 'shots/c.png'] }
  const ROW_SHOTS = { screenshots: [R_S1, R_S2, R_S3] }

  it('a failed thumbnail swaps to ITS OWN local art; its neighbours are untouched', async () => {
    await renderInstalled(MANIFEST_SHOTS, ROW_SHOTS)
    const s3 = await waitFor(() => {
      const el = imgBySrc(R_S3)
      expect(el).not.toBeNull()
      return el!
    })
    fireEvent.error(s3)
    // Thumbnail 3 pairs with the manifest's THIRD entry even though the
    // second is refused — the aligned list keeps a placeholder at index 1.
    await waitFor(() => expect(imgBySrc(LOCAL_C)).not.toBeNull())
    expect(imgBySrc(R_S3)).toBeNull()
    // Per-index latches: the other thumbnails still show their primaries.
    expect(imgBySrc(R_S1)).not.toBeNull()
    expect(imgBySrc(R_S2)).not.toBeNull()
  })

  it('a refused middle entry hides its own thumbnail rather than borrowing a neighbour\'s art', async () => {
    await renderInstalled(MANIFEST_SHOTS, ROW_SHOTS)
    const s2 = await waitFor(() => {
      const el = imgBySrc(R_S2)
      expect(el).not.toBeNull()
      return el!
    })
    fireEvent.error(s2)
    // The aligned placeholder at index 1 is '' — nothing to try. A filtered
    // fallback list would put LOCAL_C here: a silently WRONG image, worse
    // than the hidden one this fix addresses.
    await waitFor(() => expect(imgBySrc(R_S2)!.style.display).toBe('none'))
    expect(imgBySrc(LOCAL_C)).toBeNull()
  })

  it('a swapped thumbnail whose local art also fails returns to the hidden terminal state', async () => {
    await renderInstalled(MANIFEST_SHOTS, ROW_SHOTS)
    const s1 = await waitFor(() => {
      const el = imgBySrc(R_S1)
      expect(el).not.toBeNull()
      return el!
    })
    fireEvent.error(s1)
    await waitFor(() => expect(imgBySrc(LOCAL_A)).not.toBeNull())
    fireEvent.error(imgBySrc(LOCAL_A)!)
    await waitFor(() => {
      const el = document.querySelectorAll('.h-40')[0] as HTMLImageElement
      expect(el.style.display).toBe('none')
    })
  })

  it('a theme flip swaps to the dark list and re-arms the latches; the dark fallback family is used', async () => {
    getApp.mockResolvedValue(installedApp({ screenshots: ['shots/a.png'], screenshotsDark: ['shots/a-dark.png'] }))
    listRegistry.mockResolvedValue({
      apps: [registryRow({ screenshots: [R_S1], screenshotsDark: [R_SD1] })],
      serverPlatform: { os: 'linux', arch: 'x86_64' },
    })
    const { rerender } = render(detailUi())
    const s1 = await waitFor(() => {
      const el = imgBySrc(R_S1)
      expect(el).not.toBeNull()
      return el!
    })
    fireEvent.error(s1)
    await waitFor(() => expect(imgBySrc(LOCAL_A)).not.toBeNull())

    theme.current = 'dark'
    rerender(detailUi())
    const d1 = await waitFor(() => {
      const el = imgBySrc(R_SD1)
      expect(el).not.toBeNull()
      return el!
    })
    fireEvent.error(d1)
    // Same theme family on both sides of the pair: dark primary, dark local.
    await waitFor(() => expect(imgBySrc(LOCAL_AD)).not.toBeNull())
    expect(imgBySrc(LOCAL_A)).toBeNull()
  })

  it('no registry list: the local primaries keep hide-on-error as the terminal state (no misaligned retry)', async () => {
    await renderInstalled(MANIFEST_SHOTS, {})
    // Primary list is the FILTERED local one: [LOCAL_A, LOCAL_C].
    const a = await waitFor(() => {
      const el = imgBySrc(LOCAL_C)
      expect(el).not.toBeNull()
      return el!
    })
    fireEvent.error(a)
    await waitFor(() => expect(imgBySrc(LOCAL_C)!.style.display).toBe('none'))
    // No cross-index borrow: LOCAL_A still shows its own primary only once.
    expect(document.querySelectorAll(`img[src="${LOCAL_A}"]`).length).toBe(1)
  })
})

describe('ScreenshotGallery — component contract', () => {
  it('stays default-inert with no fallbacks prop: error hides the thumbnail, exactly as today', () => {
    render(<MemoryRouter><ScreenshotGallery screenshots={[R_S1]} /></MemoryRouter>)
    const img = imgBySrc(R_S1)!
    fireEvent.error(img)
    expect(imgBySrc(R_S1)!.style.display).toBe('none')
  })

  it('skips a fallback identical to the failed primary', () => {
    render(<MemoryRouter><ScreenshotGallery screenshots={[LOCAL_A]} fallbacks={[LOCAL_A]} /></MemoryRouter>)
    fireEvent.error(imgBySrc(LOCAL_A)!)
    expect(imgBySrc(LOCAL_A)!.style.display).toBe('none')
  })
})

describe('HeroBanner — component contract', () => {
  it('a fallback URL that changes independently re-arms the fallback latch alone', async () => {
    const NEXT = '/apps/demo-app/art/assets/hero-v2.png'
    const { rerender } = render(<HeroBanner src={R_HERO} fallbackSrc={LOCAL_HERO} isDetail={false} />)
    fireEvent.error(imgBySrc(R_HERO)!)
    expect(imgBySrc(LOCAL_HERO)).not.toBeNull()
    fireEvent.error(imgBySrc(LOCAL_HERO)!)
    expect(heroBox()).toBeNull()
    // An install completing under a mounted page rewrites the local
    // candidate: the fallback latch must clear while the primary's stays.
    rerender(<HeroBanner src={R_HERO} fallbackSrc={NEXT} isDetail={false} />)
    await waitFor(() => expect(imgBySrc(NEXT)).not.toBeNull())
    expect(imgBySrc(R_HERO)).toBeNull()
  })

  it('renders nothing when there is no primary at all', () => {
    render(<HeroBanner src="" fallbackSrc={LOCAL_HERO} isDetail={false} />)
    expect(heroBox()).toBeNull()
  })
})
