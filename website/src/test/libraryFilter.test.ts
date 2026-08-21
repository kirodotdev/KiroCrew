import { describe, it, expect } from 'vitest'

import { keepInLibrary } from '../pages/AppsPage'
import type { InstalledApp } from '../components/appstore/types'

/**
 * Library tab filter -- the page's own `keepInLibrary` predicate, imported.
 *
 * A disabled builtin is hidden -- the wheel ships many default-off and listing
 * them all buries the apps a reader uses -- EXCEPT one that replaces a host
 * surface, which must stay listed so it can be switched back on.
 */
type LibraryEntry = Pick<InstalledApp, 'origin' | 'enabled' | 'manifest'> & { name: string }

const overlayApp = (enabled: boolean): LibraryEntry => ({
  name: 'command-bar',
  enabled,
  origin: 'builtin',
  manifest: { ui: { overlays: [{ id: 'command-bar', replaces: 'quick-search' }] } },
} as LibraryEntry)

const plainBuiltin = (enabled: boolean): LibraryEntry => ({
  name: 'papyrus',
  enabled,
  origin: 'builtin',
} as LibraryEntry)

describe('Library filter keeps a host-surface app switchable both ways', () => {
  it('lists a disabled app that replaces a host surface', () => {
    // The case that makes the switch two-way: its own copy tells the reader to
    // disable it to get the old surface back, and Discover carries no row for a
    // builtin, so hiding it here would make disabling irreversible in the UI.
    expect(keepInLibrary(overlayApp(false))).toBe(true)
  })

  it('still hides an ordinary disabled builtin', () => {
    // Unchanged for every app that does not claim a host surface.
    expect(keepInLibrary(plainBuiltin(false))).toBe(false)
  })

  it('lists both when enabled', () => {
    expect([overlayApp(true), plainBuiltin(true)].filter(keepInLibrary).length).toBe(2)
  })
})
