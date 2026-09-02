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

  it('resolves the repo-relative iconPath an EXTERNAL app actually declares', () => {
    // The manifest contract gives a fetched app `iconPath` and reserves `iconUrl`
    // for a builtin's absolute client-local path. Reading only `iconUrl` here left
    // every external app's rail and command-palette icon as the generic box — on
    // the one surface that renders for every enabled app on every load.
    const t = appNavTarget(
      app({
        name: 'endless-worlds',
        manifest: {
          iconPath: 'assets/icon.webp',
          iconPathDark: './assets/icon-dark.webp',
          ui: { pages: [{ route: '/x' }] },
        },
      }),
    )
    expect(t?.iconUrl).toBe('/apps/endless-worlds/art/assets/icon.webp')
    // The leading `./` is stripped, so a manifest may declare either spelling.
    expect(t?.iconUrlDark).toBe('/apps/endless-worlds/art/assets/icon-dark.webp')
  })

  it('prefers iconPath over iconUrl when a manifest declares both', () => {
    const t = appNavTarget(
      app({
        name: 'both',
        manifest: {
          iconPath: 'assets/from-path.webp',
          iconUrl: 'assets/from-url.webp',
          ui: { pages: [{ route: '/x' }] },
        },
      }),
    )
    expect(t?.iconUrl).toBe('/apps/both/art/assets/from-path.webp')
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

describe('appNavTarget icon resolution', () => {
  /**
   * Both consumers of this target — the left rail and the palette — render for
   * EVERY enabled installed app on every dashboard load, so an unresolved
   * manifest icon leaks the viewer to whatever host the app named without them
   * opening anything.
   */
  it("passes a built-in's absolute icon through unchanged", () => {
    const t = appNavTarget(app({
      manifest: { iconUrl: '/app-assets/demo/icon.svg', ui: { pages: [{ route: '/demo' }] } },
    }))
    expect(t?.iconUrl).toBe('/app-assets/demo/icon.svg')
  })

  it('serves a repo-relative manifest icon from the app own install directory', () => {
    // The rail renders for EVERY enabled app on every dashboard load, so this is
    // the surface that most needs an icon URL with no network behind it: the
    // blob proxy this replaced answers from a git clone gated by an SSRF
    // allowlist, and a cold start could 403 every external app's rail icon at
    // once. `sourceUrl` is now irrelevant here — the app's own name locates the
    // bytes.
    //
    // The field is `iconPath`, not a relative `iconUrl`. That is the one the
    // backend's declared-field set carries, so it is the only one an art-route URL
    // can be built from; a relative `iconUrl` would produce a path the route
    // refuses by construction. `clientLocalArt` covers what `iconUrl` does mean.
    const t = appNavTarget(app({
      sourceUrl: 'https://example.invalid/octocat/demo',
      manifest: { iconPath: 'assets/icon.webp', ui: { pages: [{ route: '/demo' }] } },
    }))
    expect(t?.iconUrl).toBe('/apps/demo/art/assets/icon.webp')
    expect(t?.iconUrl).not.toContain('/api/apps/blob')
  })

  it('does not build an art URL from a RELATIVE iconUrl', () => {
    // It would be a guaranteed 404: `_ART_MANIFEST_FIELDS` on the backend carries
    // `iconPath`, never `iconUrl`, because for a fetched app `iconUrl` is ignored
    // by design so a publisher cannot name a host the client would load.
    const t = appNavTarget(app({
      manifest: { iconUrl: 'assets/icon.webp', ui: { pages: [{ route: '/demo' }] } },
    }))
    expect(t?.iconUrl).toBe('')
  })

  it('still honours a builtin ABSOLUTE iconUrl, which is what the field means', () => {
    const t = appNavTarget(app({
      manifest: { iconUrl: '/app-assets/demo/icon.svg', ui: { pages: [{ route: '/demo' }] } },
    }))
    expect(t?.iconUrl).toBe('/app-assets/demo/icon.svg')
  })

  it('renders no icon for a manifest naming an external host', () => {
    for (const bad of ['https://evil.example/i.png', '//evil.example/i.png', '/\t/evil.example/i.png']) {
      const t = appNavTarget(app({
        manifest: { iconUrl: bad, iconUrlDark: bad, ui: { pages: [{ route: '/demo' }] } },
      }))
      expect(t?.iconUrl).toBe('')
      expect(t?.iconUrlDark).toBe('')
    }
  })
})
