import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// Bare-factory mock, matching the rest of the corpus.
const themesFn = vi.fn()
const themeDetailFn = vi.fn()
const themeBootFn = vi.fn()
const updateThemeConfigFn = vi.fn()
const deleteThemeFn = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    themes: () => themesFn(),
    themeDetail: (slug: string) => themeDetailFn(slug),
    themeBoot: () => themeBootFn(),
    updateThemeConfig: (body: unknown) => updateThemeConfigFn(body),
    deleteTheme: (slug: string) => deleteThemeFn(slug),
  },
}))

import { ApiError } from '../api/apiError'
import {
  DEFAULT_COLOR_THEME,
  ThemeProvider,
  themeDataAttribute,
  useTheme,
} from '../hooks/useTheme'
import type { CustomThemeData, ThemeAssets } from '../hooks/useTheme'
import {
  MAX_ENTRY_CHARS,
  THEME_DATA_KEY,
  isValidCachedThemeData,
  readCachedThemeData,
  writeCachedThemeData,
} from '../hooks/themeRenderCache'

const wrapper = ({ children }: { children: ReactNode }) => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  )
}

const SLUG = 'pearce-crt'
const OTHER = 'godspeed-mission-control'
const KEY = THEME_DATA_KEY
const catalog = {
  themes: [
    { slug: SLUG, name: 'Pearce CRT', emoji: '📺', source: 'installed' },
    { slug: OTHER, name: 'Godspeed Mission Control', emoji: '🚀', source: 'installed' },
  ],
}
const detail = {
  slug: SLUG,
  name: 'Pearce CRT',
  emoji: '📺',
  dark: { '--bg': '#000000' },
  light: { '--bg': '#ffffff' },
  level: 1,
  assets: { branding: { botName: 'CRT' }, hasOverrides: false },
}
const otherDetail = { ...detail, slug: OTHER, name: 'Godspeed Mission Control', emoji: '🚀' }
const level2Detail: CustomThemeData = {
  ...detail,
  level: 2,
  assets: {
    branding: { botName: 'CRT' },
    fonts: [{ family: 'CRT', src: 'styles/fonts/crt.woff2' }],
    hasOverrides: true,
    loaderIcons: ['spinner'],
    loaderImages: ['loader/boot.png'],
    overlays: [
      {
        id: 'scanlines',
        position: 'top',
        zIndex: 1,
        pointerEvents: false,
        animation: 'once',
        trigger: 'continuous',
      },
    ],
    topbar: { dark: true, light: true, height: '28px', hideOnMobile: false },
    hasAudio: true,
    audio: {
      triggers: { activate: { src: 'audio/start.ogg', volume: 0.5, maxDuration: 2 } },
      ambient: null,
    },
    hasPersona: true,
    personaInfo: { sha256: 'sha-current', chars: 11, text: 'Current CRT' },
  },
}

/** A promise the test settles by hand, so "still pending" is observable. */
function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

const styleFor = (slug: string) => document.getElementById(`mc-custom-theme-${slug}`)

describe('useTheme: active-theme render cache (themed first paint)', () => {
  beforeEach(() => {
    localStorage.clear()
    document.head.querySelectorAll('style').forEach((n) => n.remove())
    delete document.documentElement.dataset.theme
    window.matchMedia = vi.fn().mockReturnValue({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }) as unknown as typeof window.matchMedia
    themesFn.mockReset()
    themeDetailFn.mockReset()
    themeBootFn.mockReset()
    updateThemeConfigFn.mockReset()
    deleteThemeFn.mockReset()
    deleteThemeFn.mockResolvedValue({})
    themeBootFn.mockResolvedValue({})
    updateThemeConfigFn.mockResolvedValue({})
    themeDetailFn.mockImplementation((slug: string) =>
      Promise.resolve(slug === SLUG ? detail : otherDetail),
    )
    themesFn.mockResolvedValue(catalog)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('persists the active theme after its detail loads', async () => {
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)
    const { result } = renderHook(() => useTheme(), { wrapper })
    await waitFor(() => expect(result.current.customThemeDataMap.get(SLUG)).toEqual(detail))
    await waitFor(() => expect(JSON.parse(localStorage.getItem(KEY) ?? 'null')).toEqual(detail))
  })

  it('cold mount with a valid cache injects the theme CSS before /api/themes resolves', async () => {
    const pendingCatalog = deferred<typeof catalog>()
    let themeAtCatalogRequest: string | undefined
    themesFn.mockImplementation(() => {
      themeAtCatalogRequest = document.documentElement.dataset.theme
      return pendingCatalog.promise
    })
    themeDetailFn.mockReturnValue(new Promise(() => {})) // never resolves
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)
    localStorage.setItem(KEY, JSON.stringify(detail))

    const { result } = renderHook(() => useTheme(), { wrapper })

    // Synchronously after render: the theme's <style> is in the head and the
    // map is seeded, with nothing from the network yet.
    expect(styleFor(SLUG)).not.toBeNull()
    expect(result.current.customThemeDataMap.get(SLUG)).toEqual(detail)
    expect(document.documentElement.dataset.theme).toBe(`custom-${SLUG}-dark`)
    expect(themeAtCatalogRequest).toBe(`custom-${SLUG}-dark`)
    // Branding runs on the first effect pass, off the cache alone.
    await waitFor(() => expect(result.current.brandName).toBe('CRT'))
    // The catalog gate is still closed: self-repair must not run off a cache.
    expect(result.current.customThemes).toEqual([])
    expect(result.current.colorTheme).toBe(`custom-${SLUG}`)
  })

  it('cold mount seeds only Level-1 data from an L2 cache, then publishes the server detail', async () => {
    const pendingDetail = deferred<CustomThemeData>()
    themeDetailFn.mockImplementation((slug: string) =>
      slug === SLUG ? pendingDetail.promise : Promise.resolve(otherDetail),
    )
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)
    localStorage.setItem(KEY, JSON.stringify(level2Detail))

    const { result } = renderHook(() => useTheme(), { wrapper })

    const seeded = result.current.customThemeDataMap.get(SLUG)
    expect(seeded?.level).toBeLessThanOrEqual(1)
    expect(seeded?.assets).toMatchObject({
      branding: level2Detail.assets?.branding,
      fonts: level2Detail.assets?.fonts,
      hasOverrides: true,
      loaderIcons: level2Detail.assets?.loaderIcons,
      loaderImages: level2Detail.assets?.loaderImages,
    })
    expect(seeded?.assets).not.toHaveProperty('overlays')
    expect(seeded?.assets).not.toHaveProperty('topbar')
    expect(seeded?.assets).not.toHaveProperty('audio')
    expect(seeded?.assets).not.toHaveProperty('personaInfo')

    pendingDetail.resolve(level2Detail)
    await waitFor(() => expect(result.current.customThemeDataMap.get(SLUG)).toEqual(level2Detail))
  })

  it('strips persona data from a hand-edited cache entry before seeding', () => {
    themesFn.mockReturnValue(new Promise(() => {}))
    themeDetailFn.mockReturnValue(new Promise(() => {}))
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)
    localStorage.setItem(KEY, JSON.stringify(level2Detail))

    const { result } = renderHook(() => useTheme(), { wrapper })

    const seeded = result.current.customThemeDataMap.get(SLUG)
    expect(seeded?.level).toBeLessThanOrEqual(1)
    expect(seeded?.assets).not.toHaveProperty('personaInfo')
    expect(seeded?.assets).not.toHaveProperty('overlays')
    expect(seeded?.assets).not.toHaveProperty('topbar')
    expect(seeded?.assets).not.toHaveProperty('audio')
  })

  it('cold mount seeds a real installed-theme payload with additive fields', async () => {
    const realShaped = {
      ...detail,
      source: 'installed',
      extraServerField: { revision: 2 },
      assets: {
        branding: { botName: 'CRT' },
        hasOverrides: false,
        fonts: [{ family: 'CRT', src: 'styles/fonts/crt.woff2', format: 'woff2' }],
      },
    }
    expect(isValidCachedThemeData(realShaped, SLUG)).toBe(true)
    themesFn.mockReturnValue(new Promise(() => {}))
    themeDetailFn.mockReturnValue(new Promise(() => {}))
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)
    localStorage.setItem(KEY, JSON.stringify(realShaped))

    const { result } = renderHook(() => useTheme(), { wrapper })

    expect(result.current.customThemeDataMap.get(SLUG)).toEqual(realShaped)
    expect(styleFor(SLUG)).not.toBeNull()
    await waitFor(() => expect(result.current.brandName).toBe('CRT'))
  })

  it('drops a corrupt cache entry without throwing and loads from the network', async () => {
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)
    localStorage.setItem(KEY, '{not json')
    const { result } = renderHook(() => useTheme(), { wrapper })
    expect(result.current.customThemeDataMap.size).toBe(0)
    await waitFor(() => expect(result.current.customThemeDataMap.get(SLUG)).toEqual(detail))
    // The bad entry was replaced by the real payload.
    expect(JSON.parse(localStorage.getItem(KEY) ?? 'null')).toEqual(detail)
  })

  it('drops a shape-invalid cache entry (wrong slug) at mount', () => {
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)
    localStorage.setItem(KEY, JSON.stringify({ ...detail, slug: 'someone-else' }))
    themesFn.mockReturnValue(new Promise(() => {}))
    themeDetailFn.mockReturnValue(new Promise(() => {}))
    const { result } = renderHook(() => useTheme(), { wrapper })
    expect(result.current.customThemeDataMap.size).toBe(0)
    expect(styleFor(SLUG)).toBeNull()
    expect(localStorage.getItem(KEY)).toBeNull()
  })

  it('a cache entry with a mistyped nested asset field cannot crash the mount', () => {
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)
    localStorage.setItem(
      KEY,
      JSON.stringify({ ...detail, assets: { fonts: [{ family: 1, src: 'styles/fonts/x.woff2' }] } }),
    )
    themesFn.mockReturnValue(new Promise(() => {}))
    themeDetailFn.mockReturnValue(new Promise(() => {}))
    const { result } = renderHook(() => useTheme(), { wrapper })
    expect(result.current.customThemeDataMap.size).toBe(0)
    expect(localStorage.getItem(KEY)).toBeNull()
  })

  it('a seed that throws drops the cache and mounts unthemed instead of crash-looping', () => {
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)
    localStorage.setItem(KEY, JSON.stringify(detail))
    themesFn.mockReturnValue(new Promise(() => {}))
    themeDetailFn.mockReturnValue(new Promise(() => {}))
    const spy = vi.spyOn(document.head, 'appendChild').mockImplementationOnce(() => {
      throw new Error('boom')
    })
    try {
      // A throw here would fail the test: the seed must swallow it.
      const { result } = renderHook(() => useTheme(), { wrapper })
      expect(result.current.customThemeDataMap.size).toBe(0)
      expect(localStorage.getItem(KEY)).toBeNull()
      expect(styleFor(SLUG)).toBeNull()
    } finally {
      spy.mockRestore()
    }
  })

  it('fetches the active theme detail in parallel with the catalog, not after it', async () => {
    const pendingCatalog = deferred<typeof catalog>()
    const pendingDetail = deferred<typeof detail>()
    themesFn.mockReturnValue(pendingCatalog.promise)
    themeDetailFn.mockImplementation((slug: string) =>
      slug === SLUG ? pendingDetail.promise : Promise.resolve(otherDetail),
    )
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)

    const { result } = renderHook(() => useTheme(), { wrapper })

    // Both requests are in flight before either has resolved.
    await waitFor(() => expect(themeDetailFn).toHaveBeenCalledWith(SLUG))
    expect(themesFn).toHaveBeenCalledTimes(1)
    expect(themeDetailFn).toHaveBeenCalledTimes(1)

    // The active detail landing alone is enough to inject and merge it, and
    // to bump themeVersion so var-snapshotting consumers re-read.
    const versionBefore = result.current.themeVersion
    pendingDetail.resolve(detail)
    await waitFor(() => expect(result.current.customThemeDataMap.get(SLUG)).toEqual(detail))
    expect(styleFor(SLUG)).not.toBeNull()
    expect(result.current.themeVersion).toBeGreaterThan(versionBefore)
    expect(result.current.customThemes).toEqual([])

    // The catalog then fills in the rest without re-fetching the active slug.
    pendingCatalog.resolve(catalog)
    await waitFor(() => expect(result.current.customThemeDataMap.get(OTHER)).toEqual(otherDetail))
    expect(themeDetailFn.mock.calls.filter(([s]) => s === SLUG)).toHaveLength(1)
    expect(themeDetailFn).toHaveBeenCalledWith(OTHER)
  })

  it('refetches overrides after a same-detail reinstall but not during cold-load reconciliation', async () => {
    const detailWithOverrides = level2Detail
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve(':root { --panel: #123456; }'),
    })
    vi.stubGlobal('fetch', fetchMock)
    themeDetailFn.mockResolvedValue(detailWithOverrides)
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)
    localStorage.setItem(KEY, JSON.stringify(detailWithOverrides))

    const { result } = renderHook(() => useTheme(), { wrapper })

    await waitFor(() => expect(result.current.customThemes).toHaveLength(2))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))

    window.dispatchEvent(new Event('mc-custom-themes-changed'))

    await waitFor(() => expect(themesFn).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
  })

  it('refetches overrides when a reinstall supersedes a pending initial catalog load', async () => {
    const pendingInitialCatalog = deferred<typeof catalog>()
    const detailWithOverrides = level2Detail
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve(':root { --panel: #123456; }'),
    })
    vi.stubGlobal('fetch', fetchMock)
    themesFn.mockReturnValueOnce(pendingInitialCatalog.promise).mockResolvedValue(catalog)
    themeDetailFn.mockImplementation((slug: string) =>
      Promise.resolve(slug === SLUG ? detailWithOverrides : otherDetail),
    )
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)
    localStorage.setItem(KEY, JSON.stringify(detailWithOverrides))

    const { result } = renderHook(() => useTheme(), { wrapper })

    await waitFor(() => expect(themesFn).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const fetchesAfterSeed = fetchMock.mock.calls.length

    window.dispatchEvent(new Event('mc-custom-themes-changed'))

    await waitFor(() => expect(themesFn).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(result.current.customThemes).toHaveLength(2))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(fetchesAfterSeed + 1))

    pendingInitialCatalog.resolve(catalog)
    await act(async () => {
      await pendingInitialCatalog.promise
      await new Promise((resolve) => setTimeout(resolve, 0))
    })
    expect(fetchMock).toHaveBeenCalledTimes(fetchesAfterSeed + 1)
  })

  it('a reload whose detail changed refetches overrides exactly once, not once per pass', async () => {
    const detailWithOverrides = {
      ...detail,
      assets: { ...detail.assets, hasOverrides: true },
    }
    const changedDetail = { ...detailWithOverrides, dark: { '--bg': '#101010' } }
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve(':root { --panel: #123456; }'),
    })
    vi.stubGlobal('fetch', fetchMock)
    themeDetailFn.mockImplementation((slug: string) =>
      Promise.resolve(slug === SLUG ? detailWithOverrides : otherDetail),
    )
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)

    const { result } = renderHook(() => useTheme(), { wrapper })

    await waitFor(() => expect(result.current.customThemes).toHaveLength(2))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const detailCallsBefore = themeDetailFn.mock.calls.length

    // The pack's detail JSON changed on disk: the early fetch applies the new
    // data, then the catalog pass carries the same data and must be skipped.
    // The catalog is held back until the early fetch has been applied so the
    // two passes reach the effect as separate renders, as they do over a real
    // network, instead of collapsing into one batched update.
    const catalogGate = deferred<typeof catalog>()
    themesFn.mockReturnValueOnce(catalogGate.promise)
    themeDetailFn.mockImplementation((slug: string) =>
      Promise.resolve(slug === SLUG ? changedDetail : otherDetail),
    )
    window.dispatchEvent(new Event('mc-custom-themes-changed'))

    await waitFor(() => expect(themesFn).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(result.current.customThemeDataMap.get(SLUG)).toEqual(changedDetail))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))

    catalogGate.resolve(catalog)
    // Early active fetch + the catalog pass's fetch of the other pack.
    await waitFor(() => expect(themeDetailFn.mock.calls.length).toBe(detailCallsBefore + 2))
    await waitFor(() => expect(result.current.customThemeDataMap.get(OTHER)).toEqual(otherDetail))
    // Let any second effect run from the catalog pass settle before asserting.
    await new Promise((r) => setTimeout(r, 50))
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('keeps the newer detail when an older overlapping reload resolves late', async () => {
    const staleEarlyDetail = deferred<CustomThemeData>()
    const changedDetail: CustomThemeData = {
      ...detail,
      dark: { '--bg': '#222222' },
      assets: { ...detail.assets, branding: { botName: 'New CRT' } },
    }
    let activeCalls = 0
    themeDetailFn.mockImplementation((slug: string) => {
      if (slug !== SLUG) return Promise.resolve(otherDetail)
      activeCalls += 1
      return activeCalls === 1 ? staleEarlyDetail.promise : Promise.resolve(changedDetail)
    })
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)

    const { result } = renderHook(() => useTheme(), { wrapper })

    await waitFor(() => expect(themeDetailFn).toHaveBeenCalledWith(SLUG))
    window.dispatchEvent(new Event('mc-custom-themes-changed'))
    await waitFor(() => expect(result.current.customThemeDataMap.get(SLUG)).toEqual(changedDetail))

    staleEarlyDetail.resolve(detail)
    await act(async () => {
      await staleEarlyDetail.promise
      await new Promise((resolve) => setTimeout(resolve, 0))
    })
    expect(result.current.customThemeDataMap.get(SLUG)).toEqual(changedDetail)
  })

  it('keeps a newer catalog and selection when an older catalog resolves late', async () => {
    const newSlug = 'newly-installed'
    const newDetail: CustomThemeData = {
      ...detail,
      slug: newSlug,
      name: 'Newly Installed',
      emoji: '✨',
    }
    const newCatalog = {
      themes: [
        ...catalog.themes,
        { slug: newSlug, name: 'Newly Installed', emoji: '✨', source: 'installed' },
      ],
    }
    const staleCatalog = deferred<typeof catalog>()
    themesFn.mockReturnValueOnce(staleCatalog.promise).mockResolvedValue(newCatalog)
    themeDetailFn.mockImplementation((slug: string) => {
      if (slug === SLUG) return Promise.resolve(detail)
      if (slug === OTHER) return Promise.resolve(otherDetail)
      return Promise.resolve(newDetail)
    })

    const { result } = renderHook(() => useTheme(), { wrapper })

    await waitFor(() => expect(themesFn).toHaveBeenCalledTimes(1))
    window.dispatchEvent(new Event('mc-custom-themes-changed'))
    await waitFor(() =>
      expect(result.current.customThemes.some((theme) => theme.value === `custom-${newSlug}`)).toBe(
        true,
      ),
    )

    act(() => result.current.setColorTheme(`custom-${newSlug}`))
    await waitFor(() => expect(result.current.colorTheme).toBe(`custom-${newSlug}`))
    expect(localStorage.getItem('mc-color-theme')).toBe(`custom-${newSlug}`)

    staleCatalog.resolve(catalog)
    await act(async () => {
      await staleCatalog.promise
      await new Promise((resolve) => setTimeout(resolve, 0))
    })

    expect(result.current.customThemes.some((theme) => theme.value === `custom-${newSlug}`)).toBe(
      true,
    )
    expect(result.current.colorTheme).toBe(`custom-${newSlug}`)
    expect(localStorage.getItem('mc-color-theme')).toBe(`custom-${newSlug}`)
  })

  it('keeps an installed selection when its detail fails and clears the flag after recovery', async () => {
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)
    // The rejection is never surfaced; the flag alone drives the fixed notice.
    themeDetailFn.mockRejectedValue(new ApiError(502, 'detail transport failed'))

    const { result } = renderHook(() => useTheme(), { wrapper })

    await waitFor(() => {
      expect(result.current.installedThemeLoadFailed).toBe(true)
    })
    expect(themeDetailFn.mock.calls.filter(([slug]) => slug === SLUG)).toHaveLength(2)
    expect(result.current.colorTheme).toBe(`custom-${SLUG}`)
    expect(localStorage.getItem('mc-color-theme')).toBe(`custom-${SLUG}`)
    expect(updateThemeConfigFn).not.toHaveBeenCalledWith({ color: 'kiro' })

    themeDetailFn.mockImplementation((slug: string) =>
      Promise.resolve(slug === SLUG ? detail : otherDetail),
    )
    window.dispatchEvent(new Event('mc-custom-themes-changed'))

    await waitFor(() => expect(result.current.installedThemeLoadFailed).toBe(false))
    await waitFor(() => expect(result.current.customThemeDataMap.get(SLUG)).toEqual(detail))
  })

  it('keeps the render cache when the listed pack is merely unloadable', async () => {
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)
    localStorage.setItem(KEY, JSON.stringify(detail))
    themeDetailFn.mockRejectedValue(new ApiError(502, 'detail transport failed'))

    const { result } = renderHook(() => useTheme(), { wrapper })

    await waitFor(() => expect(result.current.customThemes).toHaveLength(2))
    expect(themeDetailFn.mock.calls.filter(([slug]) => slug === SLUG)).toHaveLength(2)
    // Settle the branding/overrides effect run that follows the catalog pass.
    await new Promise((r) => setTimeout(r, 50))

    // The seed is still the detail on screen, so the pack is not unstyled and
    // the derived flag stays false: no notice contradicts the themed screen.
    expect(result.current.installedThemeLoadFailed).toBe(false)
    // The selection is kept AND the one cache entry survives, so the next cold
    // load still paints themed — the whole point of the cache.
    expect(result.current.colorTheme).toBe(`custom-${SLUG}`)
    expect(localStorage.getItem(KEY)).not.toBeNull()
    expect(readCachedThemeData(SLUG)).toEqual(detail)
  })

  it('keeps the cached detail and the themed attribute when both reloads of a listed pack fail', async () => {
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)
    localStorage.setItem(KEY, JSON.stringify(detail))
    // Both the early active-detail fetch and the catalog pass reject.
    themeDetailFn.mockRejectedValue(new ApiError(502, 'detail transport failed'))

    const { result } = renderHook(() => useTheme(), { wrapper })

    await waitFor(() => expect(result.current.customThemes).toHaveLength(2))
    expect(themeDetailFn.mock.calls.filter(([slug]) => slug === SLUG)).toHaveLength(2)
    await new Promise((r) => setTimeout(r, 50))

    // The seeded entry is carried forward, so the themed first paint is not
    // torn down by a transient reload failure; with the detail in the map the
    // derived flag is false, so no notice claims the pack failed to load.
    expect(result.current.customThemeDataMap.get(SLUG)).toEqual(detail)
    expect(result.current.installedThemeLoadFailed).toBe(false)
    expect(document.documentElement.dataset.theme).toBe(
      themeDataAttribute(`custom-${SLUG}`, 'dark'),
    )
    expect(localStorage.getItem(KEY)).not.toBeNull()
  })

  it('paints the default attribute while a kept selection has no CSS, then restores it', async () => {
    // Light mode: the bare `:root` palette is the dark one, so an unmatched
    // custom attribute would leave a light-mode user with no readable surface.
    window.matchMedia = vi.fn().mockReturnValue({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }) as unknown as typeof window.matchMedia
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)
    themeDetailFn.mockRejectedValue(new ApiError(502, 'detail transport failed'))

    const { result } = renderHook(() => useTheme(), { wrapper })

    await waitFor(() => {
      expect(result.current.installedThemeLoadFailed).toBe(true)
    })
    await waitFor(() =>
      expect(document.documentElement.dataset.theme).toBe(
        themeDataAttribute(DEFAULT_COLOR_THEME, 'light'),
      ),
    )
    // The selection itself is untouched: the picker still shows the choice.
    expect(result.current.colorTheme).toBe(`custom-${SLUG}`)
    expect(localStorage.getItem('mc-color-theme')).toBe(`custom-${SLUG}`)

    themeDetailFn.mockImplementation((slug: string) =>
      Promise.resolve(slug === SLUG ? detail : otherDetail),
    )
    window.dispatchEvent(new Event('mc-custom-themes-changed'))

    await waitFor(() => expect(result.current.installedThemeLoadFailed).toBe(false))
    await waitFor(() =>
      expect(document.documentElement.dataset.theme).toBe(
        themeDataAttribute(`custom-${SLUG}`, 'light'),
      ),
    )
  })

  it('a pack whose detail is merely pending keeps its custom attribute', async () => {
    window.matchMedia = vi.fn().mockReturnValue({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }) as unknown as typeof window.matchMedia
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)
    themeDetailFn.mockReturnValue(new Promise(() => {})) // never resolves
    themesFn.mockReturnValue(new Promise(() => {}))

    const { result } = renderHook(() => useTheme(), { wrapper })

    await waitFor(() => expect(themeDetailFn).toHaveBeenCalledWith(SLUG))
    await new Promise((r) => setTimeout(r, 50))
    expect(result.current.installedThemeLoadFailed).toBe(false)
    expect(document.documentElement.dataset.theme).toBe(
      themeDataAttribute(`custom-${SLUG}`, 'light'),
    )
  })

  it('reports a rejected detail for a theme selected while the catalog is pending', async () => {
    const pendingCatalog = deferred<typeof catalog>()
    themesFn.mockReturnValue(pendingCatalog.promise)
    themeDetailFn.mockImplementation((slug: string) =>
      slug === SLUG
        ? Promise.resolve(detail)
        : Promise.reject(new ApiError(502, 'new theme detail failed')),
    )
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)

    const { result } = renderHook(() => useTheme(), { wrapper })

    await waitFor(() => expect(themeDetailFn).toHaveBeenCalledWith(SLUG))
    act(() => result.current.setColorTheme(`custom-${OTHER}`))
    expect(result.current.colorTheme).toBe(`custom-${OTHER}`)

    pendingCatalog.resolve(catalog)

    await waitFor(() => {
      expect(result.current.installedThemeLoadFailed).toBe(true)
    })
    expect(themeDetailFn).toHaveBeenCalledWith(OTHER)
    expect(result.current.colorTheme).toBe(`custom-${OTHER}`)
  })

  it('flags non-API failures the same way as gateway rejections', async () => {
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)
    // What `fetch` itself throws when the network is down.
    themeDetailFn.mockRejectedValue(new TypeError('Failed to fetch'))

    const { result } = renderHook(() => useTheme(), { wrapper })

    await waitFor(() => {
      expect(result.current.installedThemeLoadFailed).toBe(true)
    })
    expect(result.current.colorTheme).toBe(`custom-${SLUG}`)
  })

  it('follows the selection onto a listed pack whose detail failed earlier, and back', async () => {
    // Light mode, so an unmatched custom attribute would leave no readable
    // surface: the switch must paint the default attribute immediately.
    window.matchMedia = vi.fn().mockReturnValue({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }) as unknown as typeof window.matchMedia
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)
    // The catalog lists both packs; only OTHER's detail rejects.
    themeDetailFn.mockImplementation((slug: string) =>
      slug === SLUG
        ? Promise.resolve(detail)
        : Promise.reject(new ApiError(502, 'other detail failed')),
    )

    const { result } = renderHook(() => useTheme(), { wrapper })

    await waitFor(() => expect(result.current.customThemes).toHaveLength(2))
    await waitFor(() => expect(result.current.customThemeDataMap.get(SLUG)).toEqual(detail))
    expect(result.current.installedThemeLoadFailed).toBe(false)
    expect(document.documentElement.dataset.theme).toBe(
      themeDataAttribute(`custom-${SLUG}`, 'light'),
    )

    // No catalog reload runs on a selection change, so the flag has to be
    // derived from the selection: a stored flag was reset here and stayed
    // false, leaving the custom attribute applied with no injected block.
    act(() => result.current.setColorTheme(`custom-${OTHER}`))
    expect(result.current.colorTheme).toBe(`custom-${OTHER}`)
    expect(result.current.installedThemeLoadFailed).toBe(true)
    await waitFor(() =>
      expect(document.documentElement.dataset.theme).toBe(
        themeDataAttribute(DEFAULT_COLOR_THEME, 'light'),
      ),
    )
    expect(themeDetailFn.mock.calls.filter(([slug]) => slug === OTHER)).toHaveLength(1)

    act(() => result.current.setColorTheme(`custom-${SLUG}`))
    expect(result.current.colorTheme).toBe(`custom-${SLUG}`)
    expect(result.current.installedThemeLoadFailed).toBe(false)
    await waitFor(() =>
      expect(document.documentElement.dataset.theme).toBe(
        themeDataAttribute(`custom-${SLUG}`, 'light'),
      ),
    )
  })

  it('removes the cache and self-repairs when the cached slug is gone from the catalog', async () => {
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)
    localStorage.setItem(KEY, JSON.stringify(detail))
    themesFn.mockResolvedValue({ themes: [] })
    themeDetailFn.mockRejectedValue(new Error('404'))

    const { result } = renderHook(() => useTheme(), { wrapper })
    expect(styleFor(SLUG)).not.toBeNull() // painted from the cache first

    await waitFor(() => expect(result.current.colorTheme).toBe('kiro'))
    expect(result.current.installedThemeLoadFailed).toBe(false)
    expect(localStorage.getItem(KEY)).toBeNull()
    expect(styleFor(SLUG)).toBeNull()
  })

  it('keeps the new selection\'s cache when the pack it left is gone from the catalog', async () => {
    // Both packs load on mount. A reload (pack uninstalled elsewhere) is held
    // pending; the user moves onto OTHER meanwhile, so the apply effect writes
    // OTHER's projection to the render cache at once (its detail is already in
    // the map). The catalog then comes back without SLUG: the vanished pack's
    // style block goes, but the cache clear must be keyed on the CURRENT
    // selection, not the slug that was active when the reload began -- the
    // map write that follows carries byte-identical OTHER data, so the apply
    // effect skips and nothing would rewrite the key.
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)
    const { result } = renderHook(() => useTheme(), { wrapper })
    await waitFor(() => expect(result.current.customThemeDataMap.has(OTHER)).toBe(true))
    expect(readCachedThemeData(SLUG)).toEqual(detail)

    const pendingCatalog = deferred<typeof catalog>()
    themesFn.mockReturnValue(pendingCatalog.promise)
    themeDetailFn.mockImplementation((slug: string) =>
      slug === OTHER ? Promise.resolve(otherDetail) : Promise.reject(new Error('404')),
    )
    window.dispatchEvent(new Event('mc-custom-themes-changed'))
    await waitFor(() => expect(themesFn).toHaveBeenCalledTimes(2))

    act(() => result.current.setColorTheme(`custom-${OTHER}`))
    expect(result.current.colorTheme).toBe(`custom-${OTHER}`)
    expect(readCachedThemeData(OTHER)).toEqual(otherDetail)

    await act(async () => {
      pendingCatalog.resolve({ themes: catalog.themes.filter((t) => t.slug === OTHER) })
    })
    await waitFor(() => expect(result.current.customThemes).toHaveLength(1))
    await waitFor(() => expect(styleFor(SLUG)).toBeNull())

    expect(readCachedThemeData(OTHER)).toEqual(otherDetail)
    expect(localStorage.getItem(KEY)).not.toBeNull()
    expect(result.current.customThemeDataMap.has(OTHER)).toBe(true)
    expect(result.current.colorTheme).toBe(`custom-${OTHER}`)
    expect(styleFor(OTHER)).not.toBeNull()
  })

  it('a built-in selection empties the cache without enumerating storage', async () => {
    const keySpy = vi.spyOn(Storage.prototype, 'key')
    localStorage.setItem('mc-color-theme', 'kiro')
    localStorage.setItem(KEY, JSON.stringify(detail))
    renderHook(() => useTheme(), { wrapper })
    await waitFor(() => expect(localStorage.getItem(KEY)).toBeNull())
    expect(keySpy).not.toHaveBeenCalled()
  })

  it('deleting a pack clears the render cache only when that pack is the active one', async () => {
    localStorage.setItem('mc-color-theme', `custom-${SLUG}`)
    const { result } = renderHook(() => useTheme(), { wrapper })
    await waitFor(() => expect(readCachedThemeData(SLUG)).toEqual(detail))

    // Deleting the other pack: the active pack's cache is still what the next
    // cold load paints from, so it must survive.
    await act(() => result.current.deleteCustomTheme(OTHER))
    expect(deleteThemeFn).toHaveBeenCalledWith(OTHER)
    expect(readCachedThemeData(SLUG)).toEqual(detail)
    expect(result.current.colorTheme).toBe(`custom-${SLUG}`)

    // Deleting the active pack: the cache would paint a theme that no longer
    // exists, so it goes, and the selection falls back to the default.
    await act(() => result.current.deleteCustomTheme(SLUG))
    expect(deleteThemeFn).toHaveBeenCalledWith(SLUG)
    expect(localStorage.getItem(KEY)).toBeNull()
    await waitFor(() => expect(result.current.colorTheme).toBe(DEFAULT_COLOR_THEME))
  })
})

describe('themeRenderCache helpers', () => {
  beforeEach(() => localStorage.clear())

  it('persists only the Level-1 projection of an L2 detail', () => {
    expect(writeCachedThemeData(level2Detail)).toBe(true)
    const stored = JSON.parse(localStorage.getItem(KEY) ?? 'null') as CustomThemeData

    expect(stored.level).toBeLessThanOrEqual(1)
    expect(stored.assets).toMatchObject({
      branding: level2Detail.assets?.branding,
      fonts: level2Detail.assets?.fonts,
      hasOverrides: true,
      loaderIcons: level2Detail.assets?.loaderIcons,
      loaderImages: level2Detail.assets?.loaderImages,
    })
    expect(stored.assets).not.toHaveProperty('overlays')
    expect(stored.assets).not.toHaveProperty('topbar')
    expect(stored.assets).not.toHaveProperty('audio')
    expect(stored.assets).not.toHaveProperty('hasAudio')
    expect(stored.assets).not.toHaveProperty('personaInfo')
    expect(stored.assets).not.toHaveProperty('hasPersona')
  })

  it('honours the size cap', () => {
    const huge = { ...detail, dark: { '--x': 'y'.repeat(MAX_ENTRY_CHARS) } }
    expect(writeCachedThemeData(huge)).toBe(false)
    expect(localStorage.getItem(KEY)).toBeNull()
    expect(writeCachedThemeData(detail)).toBe(true)
    expect(readCachedThemeData(SLUG)).toEqual(detail)
  })

  it('keeps only the most recently written slug', () => {
    expect(writeCachedThemeData(detail)).toBe(true)
    expect(writeCachedThemeData(otherDetail)).toBe(true)
    expect(readCachedThemeData(OTHER)).toEqual(otherDetail)
    expect(readCachedThemeData(SLUG)).toBeNull()
  })

  it('validates every projected field', () => {
    expect(isValidCachedThemeData(detail, SLUG)).toBe(true)
    expect(isValidCachedThemeData(null, SLUG)).toBe(false)
    expect(isValidCachedThemeData([], SLUG)).toBe(false)
    expect(isValidCachedThemeData({ ...detail, name: 1 }, SLUG)).toBe(false)
    expect(isValidCachedThemeData({ ...detail, dark: { '--bg': 3 } }, SLUG)).toBe(false)
    expect(isValidCachedThemeData({ ...detail, light: 'x' }, SLUG)).toBe(false)
    expect(isValidCachedThemeData({ ...detail, level: '1' }, SLUG)).toBe(false)
    expect(isValidCachedThemeData({ ...detail, assets: 'x' }, SLUG)).toBe(false)
    expect(isValidCachedThemeData({ ...detail, assets: { fonts: [1] } }, SLUG)).toBe(false)
    // The Level-1 nested fields the projection keeps: a wrong type anywhere is
    // a rejection, because the seed runs before first paint and a throw there
    // is a crash loop.
    expect(isValidCachedThemeData({ ...detail, assets: { fonts: [{ family: 1, src: 'x' }] } }, SLUG)).toBe(false)
    expect(isValidCachedThemeData({ ...detail, assets: { fonts: [{ family: 'F', src: 's', weight: '400' }] } }, SLUG)).toBe(false)
    expect(isValidCachedThemeData({ ...detail, assets: { loaderImages: ['a', 2] } }, SLUG)).toBe(false)
    expect(isValidCachedThemeData({ ...detail, assets: { branding: { botName: 1 } } }, SLUG)).toBe(false)
    expect(isValidCachedThemeData({ ...detail, assets: { unknownField: true } }, SLUG)).toBe(true)
    expect(isValidCachedThemeData({ ...detail, toString: 'x' }, SLUG)).toBe(true)
    expect(isValidCachedThemeData(JSON.parse(`{"__proto__":{}, ${JSON.stringify(detail).slice(1)}`), SLUG)).toBe(true)
    // A complete, compile-time typed Level 2 descriptor is accepted.
    const fullAssets: ThemeAssets = {
      branding: { botName: 'B', logo: 'branding/logo.svg', favicon: 'branding/f.png', wordmark: 'w.svg' },
      fonts: [{ family: 'F', src: 'styles/fonts/f.woff2', weight: 400, style: 'normal', role: 'sans' }],
      hasOverrides: true,
      loaderIcons: ['spinner'],
      loaderImages: ['loader/a.png'],
      overlays: [{ id: 'fx', position: 'top', zIndex: 1, pointerEvents: false, animation: 'once', trigger: 'continuous' }],
      topbar: { dark: true, light: false, height: '28px', hideOnMobile: true },
      hasAudio: true,
      audio: { triggers: { a: { src: 's', volume: 0.5, maxDuration: 0 } }, ambient: { src: 'b', volume: 0.2, loop: true, fadeIn: 1 } },
      hasPersona: true,
      personaInfo: { sha256: 'abc', chars: 3, text: 'hey' },
    }
    const full: CustomThemeData = {
      ...detail,
      level: 2,
      assets: fullAssets,
    }
    expect(isValidCachedThemeData(full, SLUG)).toBe(true)
    expect(isValidCachedThemeData({ ...detail, assets: undefined, level: undefined }, SLUG)).toBe(true)
  })

  it('accepts mistyped Level-2 asset fields and strips them on read', () => {
    // L2 fields are never persisted by the write path and are dropped by the
    // projection on the read path, so the validator does not inspect them: a
    // hand-edited entry with garbage there still seeds its Level-1 data.
    const mistypedL2 = {
      ...detail,
      assets: {
        ...detail.assets,
        overlays: {},
        topbar: { height: 28 },
        hasAudio: 'yes',
        audio: { triggers: { a: { src: 's' } }, ambient: null },
        hasPersona: 1,
        personaInfo: { sha256: 'x', chars: '1', text: 't' },
      },
    }
    expect(isValidCachedThemeData(mistypedL2, SLUG)).toBe(true)
    localStorage.setItem(KEY, JSON.stringify(mistypedL2))
    const read = readCachedThemeData(SLUG)
    expect(read).toEqual(detail)
    expect(read?.assets).not.toHaveProperty('overlays')
    expect(read?.assets).not.toHaveProperty('topbar')
    expect(read?.assets).not.toHaveProperty('hasAudio')
    expect(read?.assets).not.toHaveProperty('audio')
    expect(read?.assets).not.toHaveProperty('hasPersona')
    expect(read?.assets).not.toHaveProperty('personaInfo')
  })
})
