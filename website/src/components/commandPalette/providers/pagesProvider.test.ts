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
    // A promotable sub-item (`pinnable`), which is how Knowledge reaches the
    // palette now that it is a registered surface rather than an EXTRA_PAGES
    // row. It is in `getAdvertisedSurfaces()` whether or not the user has
    // pinned it — only its RAIL row is opt-in — so the palette sees it here
    // exactly as production does.
    { navId: 'capabilities-knowledge', label: 'Knowledge', route: '/capabilities?tab=knowledge', icon: null },
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
import { PREVIEW_WEBHOOKS } from '../../../utils/previewFlags'

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
  // `previewFlags` is deliberately NOT mocked — the gate reads real
  // localStorage, so it has to be cleared or a flag set by one test decides
  // another test's result.
  localStorage.removeItem(PREVIEW_WEBHOOKS)
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

  it('includes routed-but-not-in-rail EXTRA_PAGES (e.g. Developer)', async () => {
    // Was exemplified by Hooks until `capabilities-hooks` became a registered
    // surface: two rows then resolved to the same HooksPage under the identical
    // title "Hooks", which this file's own webhooks comment forbids ("differ in
    // BOTH title and icon"), so the hand-list row went. Developer is the same
    // KIND of entry -- a real route with no rail surface -- so the property this
    // test exists for is unchanged; only the specimen moved.
    const { nav, spy } = navigate()
    const p = createPagesProvider(nav)

    const arr = await run(p, 'developer')
    const hit = arr.find((r) => r.title === 'Developer')
    expect(hit).toBeDefined()
    hit!.onActivate()
    expect(spy).toHaveBeenCalledWith('/developer')
  })

  it('offers Knowledge and lands on the Capabilities knowledge tab', async () => {
    // Knowledge has no rail row unless the user promotes it, so this is the
    // discoverability lifeline for anyone who used to click it in the sidebar.
    // The entry now comes from the REGISTRY (a `pinnable` surface) rather than
    // from EXTRA_PAGES; the guarantee is unchanged, only its owner moved, and
    // THIS TEST IS WHAT PINS THE MOVE -- drop the surface from the fixture above
    // and the count below goes to 0.
    //
    // The count also documents a property it cannot itself falsify: a
    // reintroduced EXTRA_PAGES twin is skipped by `collectPages`' exact-route
    // dedup, verified by mutation, so it can never produce a second row. Read
    // the 1 as "the registry supplies it", not as "a twin would be caught".
    const { nav, spy } = navigate()
    const p = createPagesProvider(nav)

    const arr = await run(p, 'knowledge')
    const hits = arr.filter((r) => r.subtitle === '/capabilities?tab=knowledge')
    expect(hits).toHaveLength(1)
    hits[0].onActivate()
    expect(spy).toHaveBeenCalledWith('/capabilities?tab=knowledge')
  })

  it('hides the preview-gated Webhooks entry until the flag is on', async () => {
    // `hiddenFromNav` moved the surface out of `getAdvertisedSurfaces()`, which
    // is where the preview gate is normally applied — so the EXTRA_PAGES entry
    // has to carry the gate itself, or hiding it from the rail would smuggle an
    // unreleased page back in through ⌘K.
    localStorage.removeItem(PREVIEW_WEBHOOKS)
    const { nav } = navigate()
    const p = createPagesProvider(nav)

    const arr = await run(p, 'webhooks')
    expect(arr.find((r) => r.subtitle === '/webhooks')).toBeUndefined()
  })

  it('keeps the hiddenFromNav Webhooks surface reachable once the flag is on', async () => {
    // Regression: `hiddenFromNav: true` drops the surface from
    // `getBuiltinSurfaces()`, so without an EXTRA_PAGES entry typing "webhooks"
    // returned nothing and Settings was the only way in.
    localStorage.setItem(PREVIEW_WEBHOOKS, '1')
    const { nav, spy } = navigate()
    const p = createPagesProvider(nav)

    const arr = await run(p, 'webhooks')
    const hit = arr.find((r) => r.subtitle === '/webhooks')
    expect(hit).toBeDefined()
    hit!.onActivate()
    expect(spy).toHaveBeenCalledWith('/webhooks')
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
