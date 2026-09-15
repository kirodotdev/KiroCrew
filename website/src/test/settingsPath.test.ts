import { settingsPath } from '../components/settingsPath'

/**
 * settingsPath is the shared write path for every hand-authored Settings deep
 * link (SettingsLink, navigate() call sites, and settingsRoute's registry
 * adapter delegate to it), so its shape is asserted once here.
 */
describe('settingsPath', () => {
  it('builds a bare tab route', () => {
    expect(settingsPath({ tab: 'about' })).toBe('/settings/about')
  })

  it('mints the second-level selection as a path segment', () => {
    expect(settingsPath({ tab: 'security', sub: 'approval' })).toBe('/settings/security/approval')
  })

  it('carries the highlight as a query param', () => {
    expect(settingsPath({ tab: 'chat', highlight: 'chat.default-model' })).toBe(
      '/settings/chat?highlight=chat.default-model',
    )
  })

  it('accepts the key:<configKey> highlight form useSettingHighlight reads', () => {
    expect(settingsPath({ tab: 'privacy', highlight: 'key:telemetry.beacon_enabled' })).toBe(
      '/settings/privacy?highlight=key%3Atelemetry.beacon_enabled',
    )
  })

  it('rides extra params on the query string, before the highlight', () => {
    expect(settingsPath({ tab: 'channels', params: { 'k y': 'v&v' }, highlight: 'a b/c' })).toBe(
      '/settings/channels?k%20y=v%26v&highlight=a%20b%2Fc',
    )
  })

  it('encodes each level as ONE segment — a crafted value cannot mint fake depth', () => {
    expect(settingsPath({ tab: 'a/b' })).toBe('/settings/a%2Fb')
    expect(settingsPath({ tab: 'channels', sub: 'a/b' })).toBe('/settings/channels/a%2Fb')
  })

  it('drops a dot-only level — URL normalization would resolve it outside /settings', () => {
    // '..' survives encodeURIComponent, and the WHATWG parser resolves even
    // its percent-form as a dot-segment, so the level is dropped instead.
    expect(settingsPath({ tab: '..' })).toBe('/settings')
    expect(settingsPath({ tab: 'channels', sub: '..' })).toBe('/settings/channels')
    // A dropped tab also drops the sub: segments are positional, so a sub
    // under the root would be read as a TAB and land somewhere unintended.
    expect(settingsPath({ tab: '..', sub: 'slack', highlight: 'x' })).toBe(
      '/settings?highlight=x',
    )
  })

  it('never emits a protocol or a double slash — always a safe internal route', () => {
    const r = settingsPath({ tab: 'https://evil.example', sub: '//x' })
    expect(r.startsWith('/settings/')).toBe(true)
    expect(r).not.toContain('://')
    expect(r.startsWith('//')).toBe(false)
  })
})
