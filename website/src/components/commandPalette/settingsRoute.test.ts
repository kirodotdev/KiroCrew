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
  it('carries the tab as a path segment and the highlight as a query param', () => {
    expect(settingsRoute(entry({ id: 'browser.x', tab: 'browser' }))).toBe(
      '/settings/browser?highlight=browser.x',
    )
  })

  it('mints the second-level selection as a path segment — legacy keys translated', () => {
    // Without the second level the list-detail panel never mounts, so the
    // highlight resolves against nothing and the row appears to do nothing.
    // The registry's legacy second-level keys (channel/section) become the
    // second PATH segment at this single write path: navigation state lives
    // in segments, only non-navigation params ride the query string.
    expect(settingsRoute(entry({ params: { channel: 'slack' } }))).toBe(
      '/settings/channels/slack?highlight=channels.folder-name',
    )
    expect(settingsRoute(entry({ id: 'security.x', tab: 'security', params: { section: 'rules' } }))).toBe(
      '/settings/security/rules?highlight=security.x',
    )
  })

  it('canonical sub wins over a legacy alias when both are present', () => {
    expect(settingsRoute(entry({ params: { channel: 'discord', sub: 'slack' } }))).toBe(
      '/settings/channels/slack?highlight=channels.folder-name',
    )
  })

  it('encodes segments, non-navigation keys, values and the id', () => {
    const r = settingsRoute(entry({ id: 'a b/c', params: { 'k y': 'v&v' } }))
    expect(r).toBe('/settings/channels?k%20y=v%26v&highlight=a%20b%2Fc')
  })

  it('encodes a sub value as ONE segment — a crafted registry value cannot mint fake depth', () => {
    expect(settingsRoute(entry({ params: { sub: 'a/b' } }))).toBe(
      '/settings/channels/a%2Fb?highlight=channels.folder-name',
    )
    expect(settingsRoute(entry({ tab: 'a/b', params: {} }))).toBe(
      '/settings/a%2Fb?highlight=channels.folder-name',
    )
  })

  it('drops a dot-only segment — URL normalization would resolve it outside /settings', () => {
    // '..' survives encodeURIComponent, and the WHATWG parser resolves even
    // its percent-form as a dot-segment, so the level is dropped instead.
    expect(settingsRoute(entry({ params: { sub: '..' } }))).toBe(
      '/settings/channels?highlight=channels.folder-name',
    )
    expect(settingsRoute(entry({ tab: '..', params: {} }))).toBe('/settings?highlight=channels.folder-name')
  })

  it('omits the second segment when there is no second-level selection', () => {
    expect(settingsRoute(entry())).toBe('/settings/channels?highlight=channels.folder-name')
  })
})
