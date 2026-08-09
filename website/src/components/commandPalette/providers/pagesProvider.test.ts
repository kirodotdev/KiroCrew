import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { NavigateFunction } from 'react-router-dom'
import type { Result } from '../types'

/**
 * Unit tests for the pure {@link createPagesProvider} factory
 * (Search Everywhere). The page candidate list is the union of the
 * surface registry (mocked here) and the hardcoded non-rail EXTRA_PAGES, so we
 * stub `getBuiltinSurfaces` and pass a spy `navigate`.
 */

const { getBuiltinSurfaces } = vi.hoisted(() => ({
  getBuiltinSurfaces: vi.fn(() => [
    { navId: 'home', label: 'Home', route: '/', icon: null },
    { navId: 'crs', label: 'Code Reviews', route: '/crs', icon: null },
    // Same route as the EXTRA_PAGES 'Logs' entry — registry must win on dedup.
    { navId: 'logs-surface', label: 'Logs Surface', route: '/logs', icon: null },
  ]),
}))

// `surfaceLabel` is mocked alongside the surface list because pagesProvider
// resolves the display title through it (the registry's `label` is a frozen
// English fallback beside a `labelKey`). Mirroring the real resolver's
// fallback order keeps these fixtures asserting on their own `label` values.
//
// The provider reads `getAdvertisedSurfaces()` — the list a consumer may SHOW —
// so that is what the fixture stands in for. Its real implementation drops
// preview-gated surfaces; the gate itself is covered in
// `test/previewSurfaces.test.tsx` against the real registry and real
// localStorage. None of the fixtures below is gated.
vi.mock('../../../surfaces/registry', () => ({
  getAdvertisedSurfaces: getBuiltinSurfaces,
  surfaceLabel: (s: { label: string; labelKey?: string }) => s.label,
}))

import { createPagesProvider } from './pagesProvider'

function navigate(): { nav: NavigateFunction; spy: ReturnType<typeof vi.fn> } {
  const spy = vi.fn()
  return { nav: spy as unknown as NavigateFunction, spy }
}

/** Normalize the (possibly-sync) provider search to an awaited array. */
async function run(p: ReturnType<typeof createPagesProvider>, q: string): Promise<Result[]> {
  return Promise.resolve(p.search(q))
}

beforeEach(() => {
  getBuiltinSurfaces.mockClear()
})

describe('createPagesProvider — identity', () => {
  it('exposes the pages provider id, label, and an icon node', () => {
    const { nav } = navigate()
    const p = createPagesProvider(nav)
    expect(p.id).toBe('pages')
    expect(p.label).toBe('Pages')
    expect(p.icon).toBeTruthy()
  })
})

describe('createPagesProvider — registry + extras', () => {
  it('matches a registry surface and navigates to its route on Enter', async () => {
    const { nav, spy } = navigate()
    const p = createPagesProvider(nav)

    const arr = await run(p, 'code')
    const hit = arr.find((r) => r.title === 'Code Reviews')
    expect(hit).toBeDefined()
    expect(hit!.providerId).toBe('pages')
    expect(hit!.subtitle).toBe('/crs')
    hit!.onActivate()
    expect(spy).toHaveBeenCalledWith('/crs')
  })

  it('includes routed-but-not-in-rail EXTRA_PAGES (e.g. Hooks)', async () => {
    const { nav, spy } = navigate()
    const p = createPagesProvider(nav)

    const arr = await run(p, 'hooks')
    const hit = arr.find((r) => r.title === 'Hooks')
    expect(hit).toBeDefined()
    hit!.onActivate()
    expect(spy).toHaveBeenCalledWith('/hooks')
  })

  it('dedupes by route with the registry winning over EXTRA_PAGES', async () => {
    const { nav } = navigate()
    const p = createPagesProvider(nav)

    const arr = await run(p, 'logs')
    const onLogsRoute = arr.filter((r) => r.subtitle === '/logs')
    expect(onLogsRoute).toHaveLength(1)
    // Registry surface label wins; the EXTRA 'Logs' entry is dropped.
    expect(onLogsRoute[0].title).toBe('Logs Surface')
  })

  it('returns no results when the query matches nothing', async () => {
    const { nav } = navigate()
    const p = createPagesProvider(nav)
    expect(await run(p, 'zzzzqqqq')).toEqual([])
  })

  it('re-reads the registry on every search (newly registered surfaces appear)', async () => {
    const { nav } = navigate()
    const p = createPagesProvider(nav)
    await run(p, 'home')
    await run(p, 'home')
    // Called fresh per search — proves no cached snapshot of the rail.
    expect(getBuiltinSurfaces).toHaveBeenCalledTimes(2)
  })

  it('sorts results by score descending', async () => {
    const { nav } = navigate()
    const p = createPagesProvider(nav)
    // 'o' matches Home, Code Reviews, Logs Surface, Developer, Hooks, ...
    const arr = await run(p, 'o')
    expect(arr.length).toBeGreaterThan(1)
    for (let i = 1; i < arr.length; i++) {
      expect(arr[i - 1].score).toBeGreaterThanOrEqual(arr[i].score)
    }
  })
})
