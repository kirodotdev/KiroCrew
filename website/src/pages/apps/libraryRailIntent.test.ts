/**
 * The sidebar menu's request to the Library travels as router state, and the
 * Library acts on it, so the reader must refuse every shape the menu does not
 * write. The Library's handling is pinned in `test/LibraryPage.railIntent.test.tsx`.
 */
import { describe, it, expect } from 'vitest'
import { libraryRailState, readLibraryRailIntent } from './libraryRailIntent'

describe('readLibraryRailIntent', () => {
  it('reads back each request the menu writes', () => {
    for (const action of ['focus', 'disable', 'uninstall'] as const) {
      expect(readLibraryRailIntent(libraryRailState({ app: 'alpha', action }))).toEqual({ app: 'alpha', action })
    }
  })

  it('refuses an unknown action, a missing or empty app name, and state that is not a request', () => {
    expect(readLibraryRailIntent({ railIntent: { app: 'alpha', action: 'enable' } })).toBeNull()
    expect(readLibraryRailIntent({ railIntent: { app: '', action: 'disable' } })).toBeNull()
    expect(readLibraryRailIntent({ railIntent: { action: 'disable' } })).toBeNull()
    expect(readLibraryRailIntent({ autoAction: 'update' })).toBeNull()
    expect(readLibraryRailIntent(null)).toBeNull()
    expect(readLibraryRailIntent('railIntent')).toBeNull()
  })
})
