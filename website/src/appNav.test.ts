import { describe, it, expect } from 'vitest'

import { appNavTarget, appNavTargets, isAppNavigable } from './appNav'
import type { AppNavRecord } from './appNav'

/**
 * The left rail and the palette's Apps provider both resolve an app record to a
 * destination through these functions. Every case below is therefore a shared
 * contract: a change that breaks one surface breaks both, which is the point —
 * the alternative (two copies of the derivation) is what lets them drift into
 * sending a user to different places for the same app.
 */

function app(over: Partial<AppNavRecord> = {}): AppNavRecord {
  return {
    name: 'demo',
    enabled: true,
    manifest: { ui: { pages: [{ route: '/demo' }] } },
    ...over,
  }
}

describe('isAppNavigable', () => {
  it('accepts an enabled app with a UI page', () => {
    expect(isAppNavigable(app())).toBe(true)
  })

  it('rejects a disabled app', () => {
    // A disabled app has no rail row, so no surface may offer to open it.
    expect(isAppNavigable(app({ enabled: false }))).toBe(false)
    expect(isAppNavigable(app({ enabled: undefined }))).toBe(false)
  })

  it('rejects an app with no UI pages', () => {
    expect(isAppNavigable(app({ manifest: { ui: { pages: [] } } }))).toBe(false)
    expect(isAppNavigable(app({ manifest: { ui: {} } }))).toBe(false)
    expect(isAppNavigable(app({ manifest: {} }))).toBe(false)
    expect(isAppNavigable(app({ manifest: undefined }))).toBe(false)
  })
})

describe('appNavTarget — routing', () => {
  it('routes an installed app through AppHost', () => {
    const t = appNavTarget(app({ name: 'my-app', origin: 'installed' }))
    expect(t?.route).toBe('/apps/my-app')
    expect(t?.id).toBe('app-my-app')
  })

  it('routes a native builtin to its own registered page route', () => {
    // No `ui.entry` means the surface is compiled in and already registered.
    const t = appNavTarget(
      app({ name: 'dev-fleet', origin: 'builtin', manifest: { ui: { pages: [{ route: '/fleet' }] } } }),
    )
    expect(t?.route).toBe('/fleet')
    expect(t?.id).toBe('dev-fleet')
  })

  it('routes a builtin that ships a dynamic UI bundle through AppHost', () => {
    // `ui.entry` means there is no natively compiled surface to land on, so the
    // page route would 404 — it has to go through AppHost like an installed app.
    const t = appNavTarget(
      app({
        name: 'meetings',
        origin: 'builtin',
        manifest: { ui: { entry: 'index.js', pages: [{ route: '/meetings' }] } },
      }),
    )
    expect(t?.route).toBe('/apps/meetings')
    expect(t?.id).toBe('app-meetings')
  })

  it('sends an orphaned app to the migration page, outranking every other case', () => {
    // Orphaned wins even for a native builtin: the app predates a manifest
    // migration, so its old page route may no longer be served at all.
    const t = appNavTarget(
      app({
        name: 'stale',
        origin: 'builtin',
        orphaned: true,
        manifest: { ui: { pages: [{ route: '/stale' }] } },
      }),
    )
    expect(t?.route).toBe('/apps/migrate/stale')
    expect(t?.orphaned).toBe(true)
  })

  it('returns null for an app with no destination', () => {
    expect(appNavTarget(app({ enabled: false }))).toBeNull()
    expect(appNavTarget(app({ manifest: { ui: { pages: [] } } }))).toBeNull()
  })

  it('uses the FIRST page when an app declares several', () => {
    const t = appNavTarget(
      app({
        origin: 'builtin',
        manifest: { ui: { pages: [{ route: '/first' }, { route: '/second' }] } },
      }),
    )
    expect(t?.route).toBe('/first')
  })
})

describe('appNavTarget — icon inputs', () => {
  it('carries the builtin flag the glyph fallback depends on', () => {
    // The lucide lookup is builtin-only: `iconName` comes from the manifest, so
    // resolving it for an INSTALLED app would render a builtin glyph for any app
    // whose page.icon happens to collide with one.
    expect(appNavTarget(app({ origin: 'builtin' }))?.builtin).toBe(true)
    expect(appNavTarget(app({ origin: 'installed' }))?.builtin).toBe(false)
    expect(appNavTarget(app({ origin: undefined }))?.builtin).toBe(false)
  })

  it('surfaces all three icon sources, defaulting to empty strings', () => {
    const t = appNavTarget(
      app({
        manifest: {
          iconUrl: '/app-assets/demo/icon.svg',
          ui: { pages: [{ route: '/demo', icon: 'Rocket', iconUrl: 'logo.png' }] },
        },
      }),
    )
    expect(t?.iconUrl).toBe('/app-assets/demo/icon.svg')
    expect(t?.iconName).toBe('Rocket')
    expect(t?.pageIconUrl).toBe('logo.png')

    const bare = appNavTarget(app())
    expect(bare?.iconUrl).toBe('')
    expect(bare?.iconName).toBe('')
    expect(bare?.pageIconUrl).toBe('')
  })
})

describe('appNavTargets', () => {
  it('drops non-navigable apps and preserves API order', () => {
    const out = appNavTargets([
      app({ name: 'a', origin: 'installed' }),
      app({ name: 'off', enabled: false }),
      app({ name: 'no-ui', manifest: { ui: { pages: [] } } }),
      app({ name: 'b', origin: 'installed' }),
    ])
    expect(out.map((t) => t.name)).toEqual(['a', 'b'])
  })

  it('returns an empty list for an empty response', () => {
    expect(appNavTargets([])).toEqual([])
  })
})
