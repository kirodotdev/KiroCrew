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

  it('rides the entry params BEFORE the highlight', () => {
    // Without them the list-detail panel never mounts, so the highlight resolves
    // against nothing and the row appears to do nothing on a narrow viewport.
    expect(settingsRoute(entry({ params: { channel: 'slack' } }))).toBe(
      '/settings?tab=channels&channel=slack&highlight=channels.folder-name',
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
