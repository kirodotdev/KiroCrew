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
import { useEffect } from 'react'
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

function ErrorImageInEarlierEffect({ src, enabled }: { src: string; enabled: boolean }) {
  // This sibling is rendered before the image component, so React runs its
  // passive effect first. It deterministically models another page effect
  // observing a load error before later image bookkeeping effects.
  useEffect(() => {
    if (!enabled) return
    const image = imgBySrc(src)
    expect(image).not.toBeNull()
    fireEvent.error(image!)
  }, [enabled, src])
  return null
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
    // The aligned placeholder at index 1 is '' — nothing to try, so the
    // thumbnail (and its now-pointless button) unmounts. A filtered fallback
    // list would put LOCAL_C here: a silently WRONG image, worse than the
    // hidden one this fix addresses.
    await waitFor(() => expect(imgBySrc(R_S2)).toBeNull())
    expect(imgBySrc(LOCAL_C)).toBeNull()
    // Neighbours untouched.
    expect(imgBySrc(R_S1)).not.toBeNull()
    expect(imgBySrc(R_S3)).not.toBeNull()
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
    // Terminal: the thumbnail unmounts; its neighbours stay.
    await waitFor(() => expect(imgBySrc(LOCAL_A)).toBeNull())
    expect(imgBySrc(R_S1)).toBeNull()
    expect(imgBySrc(R_S2)).not.toBeNull()
    expect(imgBySrc(R_S3)).not.toBeNull()
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
    await waitFor(() => expect(imgBySrc(LOCAL_C)).toBeNull())
    // No cross-index borrow: LOCAL_A still shows its own primary only once.
    expect(document.querySelectorAll(`img[src="${LOCAL_A}"]`).length).toBe(1)
  })
})

describe('ScreenshotGallery — component contract', () => {
  it('keeps an error observed during a new screenshot generation', () => {
    const { rerender } = render(
      <MemoryRouter>
        <ErrorImageInEarlierEffect src={R_S1} enabled={false} />
        <ScreenshotGallery screenshots={[R_S1]} />
      </MemoryRouter>,
    )

    rerender(
      <MemoryRouter>
        <ErrorImageInEarlierEffect src={R_S2} enabled />
        <ScreenshotGallery screenshots={[R_S2]} />
      </MemoryRouter>,
    )

    expect(imgBySrc(R_S2)).toBeNull()
  })

  it('retries a previously failed list when the strip returns to it (A→B→A)', () => {
    // A theme flip swaps the strip to the dark URLs and back. The failure the
    // light generation recorded must not be restored on the way back: the asset
    // may exist now (an install completing, a transient network error), and the
    // pre-fix effect reset retried on every URL change.
    const { rerender } = render(<MemoryRouter><ScreenshotGallery screenshots={[R_S1]} /></MemoryRouter>)
    fireEvent.error(imgBySrc(R_S1)!)
    expect(imgBySrc(R_S1)).toBeNull()

    rerender(<MemoryRouter><ScreenshotGallery screenshots={[R_SD1]} /></MemoryRouter>)
    expect(imgBySrc(R_SD1)).not.toBeNull()

    rerender(<MemoryRouter><ScreenshotGallery screenshots={[R_S1]} /></MemoryRouter>)
    expect(imgBySrc(R_S1)).not.toBeNull()
  })

  it('stays default-inert with no fallbacks prop: error unmounts the thumbnail, no swap attempted', () => {
    render(<MemoryRouter><ScreenshotGallery screenshots={[R_S1, R_S2]} /></MemoryRouter>)
    fireEvent.error(imgBySrc(R_S1)!)
    expect(imgBySrc(R_S1)).toBeNull()
    expect(document.querySelectorAll('img').length).toBe(1)
    expect(imgBySrc(R_S2)).not.toBeNull()
  })

  it('skips a fallback identical to the failed primary', () => {
    render(<MemoryRouter><ScreenshotGallery screenshots={[LOCAL_A, R_S2]} fallbacks={[LOCAL_A, '']} /></MemoryRouter>)
    fireEvent.error(imgBySrc(LOCAL_A)!)
    expect(imgBySrc(LOCAL_A)).toBeNull()
    expect(imgBySrc(R_S2)).not.toBeNull()
  })

  it('the lightbox enlarges the SWAPPED src, not the dead primary (#6886 review finding)', () => {
    render(<MemoryRouter><ScreenshotGallery screenshots={[R_S1]} fallbacks={[LOCAL_A]} /></MemoryRouter>)
    fireEvent.error(imgBySrc(R_S1)!)
    expect(imgBySrc(LOCAL_A)).not.toBeNull()
    fireEvent.click(document.querySelector('button[aria-label="Open screenshot 1"]')!)
    const dialog = document.querySelector('[role="dialog"]')!
    expect(dialog).not.toBeNull()
    const enlarged = dialog.querySelector('img')!
    expect(enlarged.getAttribute('src')).toBe(LOCAL_A)
  })

  it('suppresses the whole section (header included) when every thumbnail is terminal', () => {
    const { container } = render(<MemoryRouter><ScreenshotGallery screenshots={[R_S1]} /></MemoryRouter>)
    fireEvent.error(imgBySrc(R_S1)!)
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).not.toContain('Screenshots')
  })

  it('lightbox navigation skips terminal indices and counts only the visible subset (#6886 UX round 3)', () => {
    render(<MemoryRouter><ScreenshotGallery screenshots={[R_S1, R_S2, R_S3]} /></MemoryRouter>)
    // Middle screenshot dies: its thumbnail unmounts.
    fireEvent.error(imgBySrc(R_S2)!)
    expect(imgBySrc(R_S2)).toBeNull()
    // Open the first; arrow-right must land on the THIRD, not a blank slide.
    fireEvent.click(document.querySelector('button[aria-label="Open screenshot 1"]')!)
    const dialog = () => document.querySelector('[role="dialog"]')!
    expect(dialog().querySelector('img')!.getAttribute('src')).toBe(R_S1)
    fireEvent.keyDown(dialog(), { key: 'ArrowRight' })
    expect(dialog().querySelector('img')!.getAttribute('src')).toBe(R_S3)
    // Counter reflects the visible subset (2 of 2), not the raw length.
    expect(dialog().textContent).toContain('2 / 2')
    // No further next: the chevron is gone and arrow-right is a no-op.
    expect(dialog().querySelector('button[aria-label="Next"]')).toBeNull()
    fireEvent.keyDown(dialog(), { key: 'ArrowRight' })
    expect(dialog().querySelector('img')!.getAttribute('src')).toBe(R_S3)
    // Arrow-left walks back over the gap.
    fireEvent.keyDown(dialog(), { key: 'ArrowLeft' })
    expect(dialog().querySelector('img')!.getAttribute('src')).toBe(R_S1)
    expect(dialog().textContent).toContain('1 / 2')
  })

  it('refuses a cross-origin fallback URL: error goes terminal, no request to a third-party host (#6886 GPT security finding)', () => {
    // A registry-only row's raw spread can deliver attacker-chosen fallback
    // keys. Honouring an absolute URL would leak the viewer's address to
    // that host on load failure — the surface must never render it.
    const EVIL = 'https://evil.example/tracker.png'
    render(<MemoryRouter><ScreenshotGallery screenshots={[R_S1, R_S2]} fallbacks={[EVIL, LOCAL_A]} /></MemoryRouter>)
    fireEvent.error(imgBySrc(R_S1)!)
    expect(imgBySrc(EVIL)).toBeNull()
    expect(imgBySrc(R_S1)).toBeNull()
    // The same-origin sibling still swaps normally.
    fireEvent.error(imgBySrc(R_S2)!)
    expect(imgBySrc(LOCAL_A)).not.toBeNull()
  })

  it('survives malformed runtime shapes: a non-array in either prop never crashes the page', () => {
    // The registry-only branch spreads the raw third-party row into the view
    // model, so `screenshots: {}` or a colliding `screenshotsFallback: {}`
    // reaches this component despite the string[] type (GPT review finding).
    const bad = {} as unknown as string[]
    const { container: c1 } = render(<MemoryRouter><ScreenshotGallery screenshots={bad} /></MemoryRouter>)
    expect(c1.querySelector('img')).toBeNull()
    const { container: c2 } = render(<MemoryRouter><ScreenshotGallery screenshots={[R_S1]} fallbacks={bad} /></MemoryRouter>)
    expect(c2.querySelector(`img[src="${R_S1}"]`)).not.toBeNull()
    // And the malformed fallback is treated as absent: error goes terminal.
    fireEvent.error(c2.querySelector(`img[src="${R_S1}"]`)!)
    expect(c2.querySelector('img')).toBeNull()
  })

  it('page survives a registry-only row carrying a malformed screenshotsFallback key', async () => {
    getApp.mockRejectedValue(new Error('not installed'))
    listRegistry.mockResolvedValue({
      apps: [registryRow({ screenshots: [R_S1], screenshotsFallback: {} })],
      serverPlatform: { os: 'linux', arch: 'x86_64' },
    })
    render(detailUi())
    // The raw-row spread delivers the malformed key; the page must render.
    const s1 = await waitFor(() => {
      const el = imgBySrc(R_S1)
      expect(el).not.toBeNull()
      return el!
    })
    fireEvent.error(s1)
    expect(imgBySrc(R_S1)).toBeNull()
  })
})

describe('HeroBanner — component contract', () => {
  it('keeps an error observed during a new hero generation', () => {
    const { rerender } = render(
      <>
        <ErrorImageInEarlierEffect src={R_HERO} enabled={false} />
        <HeroBanner src={R_HERO} fallbackSrc={LOCAL_HERO} isDetail={false} />
      </>,
    )

    rerender(
      <>
        <ErrorImageInEarlierEffect src={R_HERO_DARK} enabled />
        <HeroBanner src={R_HERO_DARK} isDetail={false} />
      </>,
    )

    expect(imgBySrc(R_HERO_DARK)).toBeNull()
    expect(heroBox()).toBeNull()
  })

  it('retries a previously failed hero when the page returns to it (A→B→A)', () => {
    // Light hero fails → dark theme → back to light. The light failure belongs
    // to the generation that recorded it and must not be restored, or the hero
    // stays hidden without ever retrying.
    const { rerender } = render(<HeroBanner src={R_HERO} isDetail={false} />)
    fireEvent.error(imgBySrc(R_HERO)!)
    expect(heroBox()).toBeNull()

    rerender(<HeroBanner src={R_HERO_DARK} isDetail={false} />)
    expect(imgBySrc(R_HERO_DARK)).not.toBeNull()

    rerender(<HeroBanner src={R_HERO} isDetail={false} />)
    expect(imgBySrc(R_HERO)).not.toBeNull()
  })

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

  it('refuses a cross-origin fallback URL and unmounts instead (#6886 GPT security finding)', () => {
    const EVIL = 'https://evil.example/hero.png'
    render(<HeroBanner src={R_HERO} fallbackSrc={EVIL} isDetail={false} />)
    fireEvent.error(imgBySrc(R_HERO)!)
    expect(imgBySrc(EVIL)).toBeNull()
    expect(heroBox()).toBeNull()
  })
})
