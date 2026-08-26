import { settingsRoute } from './settingsRoute'
import type { SettingEntry } from './settingsTypes'

/**
 * The deep link is shared by every reader of the settings registry, so its shape is
 * asserted once here rather than in each caller.
 */

function entry(over: Partial<SettingEntry> = {}): SettingEntry {
  return {
    id: 'channels.folder-name',
    label: 'Folder name',
    tab: 'channels',
    type: 'input',
    occurrence: 1,
    ...over,
  } as SettingEntry
}

describe('settingsRoute', () => {
  it('carries the tab and the highlight', () => {
    expect(settingsRoute(entry({ id: 'browser.x', tab: 'browser' }))).toBe(
      '/settings?tab=browser&highlight=browser.x',
    )
  })

  it('rides the entry params BEFORE the highlight — legacy keys translated to sub', () => {
    // Without them the list-detail panel never mounts, so the highlight resolves
    // against nothing and the row appears to do nothing on a narrow viewport.
    // The registry's legacy second-level keys (channel/section) are rewritten
    // to the canonical `sub` at this single write path: an alias-bearing link
    // renders BOTH the navigation shell's back bar and the SubNav's on a phone.
    expect(settingsRoute(entry({ params: { channel: 'slack' } }))).toBe(
      '/settings?tab=channels&sub=slack&highlight=channels.folder-name',
    )
    expect(settingsRoute(entry({ id: 'security.x', tab: 'security', params: { section: 'rules' } }))).toBe(
      '/settings?tab=security&sub=rules&highlight=security.x',
    )
  })

  it('encodes keys, values and the id', () => {
    const r = settingsRoute(entry({ id: 'a b/c', params: { 'k y': 'v&v' } }))
    expect(r).toBe('/settings?tab=channels&k%20y=v%26v&highlight=a%20b%2Fc')
  })

  it('omits the params segment when there are none', () => {
    expect(settingsRoute(entry())).not.toContain('&channel')
  })
})
