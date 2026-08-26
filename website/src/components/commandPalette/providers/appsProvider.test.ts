import { describe, it, expect, vi } from 'vitest'
import type { NavigateFunction } from 'react-router-dom'

import { createAppsProvider } from './appsProvider'
import type { AppsProviderDeps } from './appsProvider'
import type { AppNavRecord } from '../../../appNav'

/**
 * Unit tests for the pure {@link createAppsProvider} factory — a mock fetch plus a
 * stub navigate, no React hooks, React-Query, or router.
 */

function app(over: Partial<AppNavRecord> = {}): AppNavRecord {
  return {
    name: 'demo',
    enabled: true,
    origin: 'installed',
    manifest: { ui: { pages: [{ route: '/demo', label: 'Demo' }] } },
    ...over,
  }
}

function deps(apps: AppNavRecord[] | (() => Promise<AppNavRecord[]>)): {
  d: AppsProviderDeps
  navigate: ReturnType<typeof vi.fn>
  fetchApps: ReturnType<typeof vi.fn>
} {
  const navigate = vi.fn()
  const fetchApps = vi.fn(typeof apps === 'function' ? apps : async () => apps)
  return {
    d: { fetchApps, navigate: navigate as unknown as NavigateFunction },
    navigate,
    fetchApps,
  }
}

describe('createAppsProvider — identity', () => {
  it('exposes the apps provider id, a label, and an icon', () => {
    const p = createAppsProvider(deps([]).d)
    expect(p.id).toBe('apps')
    expect(p.label).toBeTruthy()
    expect(p.icon).toBeTruthy()
  })
})

describe('createAppsProvider — listing', () => {
  it('lists every navigable app on an empty query, so the tab is a launcher menu', async () => {
    const { d } = deps([
      app({ name: 'alpha', manifest: { ui: { pages: [{ route: '/a', label: 'Alpha' }] } } }),
      app({ name: 'beta', manifest: { ui: { pages: [{ route: '/b', label: 'Beta' }] } } }),
    ])
    const results = await createAppsProvider(d).search('')
    expect(results.map((r) => r.title)).toEqual(['Alpha', 'Beta'])
  })

  it('omits apps with no destination', async () => {
    const { d } = deps([
      app({ name: 'on', manifest: { ui: { pages: [{ route: '/on', label: 'On' }] } } }),
      app({ name: 'off', enabled: false }),
    ])
    const results = await createAppsProvider(d).search('')
    expect(results.map((r) => r.title)).toEqual(['On'])
  })

  it('filters by fuzzy match on the label', async () => {
    const { d } = deps([
      app({ name: 'fleet', manifest: { ui: { pages: [{ route: '/f', label: 'Dev Fleet' }] } } }),
      app({ name: 'papyrus', manifest: { ui: { pages: [{ route: '/p', label: 'Papyrus' }] } } }),
    ])
    const results = await createAppsProvider(d).search('fleet')
    expect(results.map((r) => r.title)).toEqual(['Dev Fleet'])
    expect(results[0].indices.length).toBeGreaterThan(0)
  })

  it('returns nothing when no label matches', async () => {
    const { d } = deps([app()])
    expect(await createAppsProvider(d).search('zzzzz')).toEqual([])
  })
})

describe('createAppsProvider — activation', () => {
  it('navigates to the app route on Enter', async () => {
    const { d, navigate } = deps([app({ name: 'my-app' })])
    const results = await createAppsProvider(d).search('')
    results[0].onActivate()
    expect(navigate).toHaveBeenCalledWith('/apps/my-app')
  })

  it('carries a declarative navigate action matching onActivate', async () => {
    const { d } = deps([app({ name: 'my-app' })])
    const results = await createAppsProvider(d).search('')
    expect(results[0].enter).toEqual({ kind: 'navigate', route: '/apps/my-app' })
  })

  it('binds no modifier action — an app is a pure navigation target', async () => {
    const { d } = deps([app()])
    const results = await createAppsProvider(d).search('')
    expect(results[0].onCmdActivate).toBeUndefined()
    expect(results[0].onAltActivate).toBeUndefined()
  })

  it('routes an orphaned app to migration and says so in the subtitle', async () => {
    const { d, navigate } = deps([app({ name: 'stale', orphaned: true })])
    const results = await createAppsProvider(d).search('')
    results[0].onActivate()
    expect(navigate).toHaveBeenCalledWith('/apps/migrate/stale')
    // The rail signals this with a warn tint; the palette says it in words, which
    // reads without relying on colour.
    expect(results[0].subtitle).not.toBe('/apps/migrate/stale')
    expect(results[0].subtitle).toBeTruthy()
  })

  it('shows the route as the subtitle for a healthy app', async () => {
    const { d } = deps([app({ name: 'my-app' })])
    const results = await createAppsProvider(d).search('')
    expect(results[0].subtitle).toBe('/apps/my-app')
  })
})

describe('createAppsProvider — failure handling', () => {
  it('propagates a failed app-list fetch instead of masking it as "no apps"', async () => {
    // "Could not load the app list" and "no apps are installed" must stay
    // distinguishable at this layer. Rendering that difference is a separate,
    // palette-wide concern (#1928 — CommandPalette consumes no `isError`);
    // resolving to [] here would discard the error before that fix can use it.
    const { d } = deps(async () => {
      throw new Error('gateway restarting')
    })
    await expect(createAppsProvider(d).search('fleet')).rejects.toThrow('gateway restarting')
  })

  it('gives every result a stable id keyed by app name', async () => {
    const { d } = deps([app({ name: 'alpha' }), app({ name: 'beta' })])
    const results = await createAppsProvider(d).search('')
    expect(results.map((r) => r.id)).toEqual(['apps:alpha', 'apps:beta'])
    expect(new Set(results.map((r) => r.id)).size).toBe(results.length)
  })
})
