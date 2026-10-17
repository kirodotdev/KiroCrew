/**
 * Property tests for Browse tab and Sidebar filter logic.
 *
 * Property 4: Browse filter shows exactly disabled builtins
 * Property 5: Sidebar filter shows only enabled apps with pages
 *
 * Validates: Requirements 3.1, 4.2, 4.4, 5.2, 5.3
 */
import { describe, it, expect } from 'vitest'

// ---------------------------------------------------------------------------
// Pure filter functions extracted from AppsPage logic
// ---------------------------------------------------------------------------

type AppEntry = {
  name: string
  displayName: string
  enabled: boolean
  origin?: string
  lifecycle?: string
  manifest?: {
    description?: string
    author?: string
    tags?: string[]
    hidden?: boolean
    ui?: { pages?: { route: string; label: string; icon: string }[] }
  }
}

/**
 * Browse tab filter: returns all non-hidden builtin apps for discovery.
 * Discover shows enabled builtins too, each carrying its Enabled/Disabled
 * state. This mirrors the logic in AppsPage.tsx Browse tab.
 */
function filterBrowsableBuiltins(apps: AppEntry[]): AppEntry[] {
  return apps.filter(a => a.origin === 'builtin' && !a.manifest?.hidden)
}

/**
 * Sidebar filter: returns apps that should appear in navigation.
 * This mirrors the logic in App.tsx refreshAppNav.
 */
function filterSidebarApps(apps: AppEntry[]): AppEntry[] {
  return apps.filter(a => a.enabled && (a.manifest?.ui?.pages?.length || 0) > 0)
}

// ---------------------------------------------------------------------------
// Property 4: Browse filter shows exactly disabled builtins
// ---------------------------------------------------------------------------

describe('Property 4: Browse filter shows all non-hidden builtins', () => {
  it('includes builtin apps regardless of enabled state', () => {
    const apps: AppEntry[] = [
      { name: 'disabled-builtin', displayName: 'DB', enabled: false, origin: 'builtin' },
      { name: 'enabled-builtin', displayName: 'EB', enabled: true, origin: 'builtin' },
      { name: 'registry-app', displayName: 'RA', enabled: true, origin: 'registry' },
    ]
    const result = filterBrowsableBuiltins(apps)
    expect(result.map(a => a.name)).toEqual(['disabled-builtin', 'enabled-builtin'])
  })

  it('includes enabled builtins', () => {
    const apps: AppEntry[] = [
      { name: 'enabled-builtin', displayName: 'EB', enabled: true, origin: 'builtin' },
    ]
    const result = filterBrowsableBuiltins(apps)
    expect(result.map(a => a.name)).toEqual(['enabled-builtin'])
  })

  it('excludes hidden builtins', () => {
    const apps: AppEntry[] = [
      { name: 'hidden-builtin', displayName: 'HB', enabled: false, origin: 'builtin', manifest: { hidden: true } },
      { name: 'shown-builtin', displayName: 'SB', enabled: true, origin: 'builtin' },
    ]
    const result = filterBrowsableBuiltins(apps)
    expect(result.map(a => a.name)).toEqual(['shown-builtin'])
  })

  it('excludes non-builtin apps', () => {
    const apps: AppEntry[] = [
      { name: 'disabled-registry', displayName: 'DR', enabled: false, origin: 'registry' },
      { name: 'disabled-local', displayName: 'DL', enabled: false, origin: 'local' },
      { name: 'disabled-external', displayName: 'DE', enabled: false, origin: 'external' },
    ]
    const result = filterBrowsableBuiltins(apps)
    expect(result).toHaveLength(0)
  })

  it('returns empty for empty list', () => {
    expect(filterBrowsableBuiltins([])).toHaveLength(0)
  })

  it('handles multiple builtins', () => {
    const apps: AppEntry[] = [
      { name: 'a', displayName: 'A', enabled: false, origin: 'builtin' },
      { name: 'b', displayName: 'B', enabled: false, origin: 'builtin' },
      { name: 'c', displayName: 'C', enabled: true, origin: 'builtin' },
    ]
    const result = filterBrowsableBuiltins(apps)
    expect(result.map(a => a.name)).toEqual(['a', 'b', 'c'])
  })

  it('property: result contains app iff origin=builtin AND not hidden', () => {
    // Exhaustive check over all combinations
    const origins = ['builtin', 'registry', 'local', 'external'] as const
    const enabledStates = [true, false]

    for (const origin of origins) {
      for (const enabled of enabledStates) {
        const apps: AppEntry[] = [{ name: `${origin}-${enabled}`, displayName: 'X', enabled, origin }]
        const result = filterBrowsableBuiltins(apps)
        const shouldInclude = origin === 'builtin'
        expect(result.length === 1).toBe(shouldInclude)
      }
    }
  })
})

// ---------------------------------------------------------------------------
// Property 5: Sidebar filter shows only enabled apps with pages
// ---------------------------------------------------------------------------

describe('Property 5: Sidebar filter shows only enabled apps with pages', () => {
  it('includes enabled app with pages', () => {
    const apps: AppEntry[] = [{
      name: 'with-pages', displayName: 'WP', enabled: true,
      manifest: { ui: { pages: [{ route: '/test', label: 'Test', icon: 'Zap' }] } },
    }]
    const result = filterSidebarApps(apps)
    expect(result.map(a => a.name)).toEqual(['with-pages'])
  })

  it('excludes disabled app with pages', () => {
    const apps: AppEntry[] = [{
      name: 'disabled-pages', displayName: 'DP', enabled: false,
      manifest: { ui: { pages: [{ route: '/test', label: 'Test', icon: 'Zap' }] } },
    }]
    const result = filterSidebarApps(apps)
    expect(result).toHaveLength(0)
  })

  it('excludes enabled app without pages', () => {
    const apps: AppEntry[] = [
      { name: 'no-pages', displayName: 'NP', enabled: true, manifest: { ui: { pages: [] } } },
      { name: 'no-ui', displayName: 'NU', enabled: true, manifest: {} },
      { name: 'null-manifest', displayName: 'NM', enabled: true },
    ]
    const result = filterSidebarApps(apps)
    expect(result).toHaveLength(0)
  })

  it('property: result contains app iff enabled=true AND has at least one page', () => {
    const cases: AppEntry[] = [
      { name: 'enabled-pages', displayName: 'EP', enabled: true, manifest: { ui: { pages: [{ route: '/a', label: 'A', icon: 'X' }] } } },
      { name: 'enabled-no-pages', displayName: 'ENP', enabled: true, manifest: { ui: { pages: [] } } },
      { name: 'disabled-pages', displayName: 'DP', enabled: false, manifest: { ui: { pages: [{ route: '/b', label: 'B', icon: 'Y' }] } } },
      { name: 'disabled-no-pages', displayName: 'DNP', enabled: false, manifest: { ui: { pages: [] } } },
    ]

    const result = filterSidebarApps(cases)
    expect(result.map(a => a.name)).toEqual(['enabled-pages'])
  })

  it('handles mixed origins correctly', () => {
    const apps: AppEntry[] = [
      { name: 'builtin-enabled', displayName: 'BE', enabled: true, origin: 'builtin', manifest: { ui: { pages: [{ route: '/x', label: 'X', icon: 'Z' }] } } },
      { name: 'builtin-disabled', displayName: 'BD', enabled: false, origin: 'builtin', manifest: { ui: { pages: [{ route: '/y', label: 'Y', icon: 'Z' }] } } },
      { name: 'registry-enabled', displayName: 'RE', enabled: true, origin: 'registry', manifest: { ui: { pages: [{ route: '/z', label: 'Z', icon: 'Z' }] } } },
    ]
    const result = filterSidebarApps(apps)
    expect(result.map(a => a.name)).toEqual(['builtin-enabled', 'registry-enabled'])
  })
})
