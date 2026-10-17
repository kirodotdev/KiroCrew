import { describe, it, expect, vi } from 'vitest'
import { SETTINGS_REGISTRY } from './settingsRegistry.gen'
import { SETTINGS_KEYWORDS } from './settingsKeywords'
import { createSettingsProvider } from './providers/settingsProvider'
import { APP_AUTO_DOWNLOAD_SETTING_ID, GATEWAY_AUTO_UPDATE_SETTING_ID, scoreSettingEntry } from './settingsSearchCore'

it.each(['how long models think', 'think before answering', 'thinking time'])(
  'finds default reasoning effort by the hint phrase "%s"',
  async query => {
    const provider = createSettingsProvider(vi.fn(), {
      decisionsEnabled: true,
      tipsEnabled: true,
      updateSwitches: { app: true, gateway: true },
    })
    const results = await Promise.resolve(provider.search(query))
    expect(results[0]?.id).toBe('settings:chat.default-reasoning-effort')
  },
)

describe('settingsKeywords integrity', () => {
  it('every SETTINGS_KEYWORDS key maps to a real SETTINGS_REGISTRY id', () => {
    const registryIds = new Set(SETTINGS_REGISTRY.map(e => e.id))
    const deadKeys: string[] = []
    for (const key of Object.keys(SETTINGS_KEYWORDS)) {
      if (!registryIds.has(key)) {
        deadKeys.push(key)
      }
    }
    expect(deadKeys, `Dead keyword IDs not in registry: ${deadKeys.join(', ')}`).toEqual([])
  })
})

describe('the About update switches are found by the words people use', () => {
  const hits = (query: string) =>
    SETTINGS_REGISTRY.filter(e => scoreSettingEntry(query, e) !== null).map(e => e.id)

  it.each(['auto update', 'auto-update', 'automatic updates', 'auto update on restart', 'auto-update on restart'])(
    'finds both switches for %j',
    query => {
      expect(hits(query)).toEqual(expect.arrayContaining([GATEWAY_AUTO_UPDATE_SETTING_ID, APP_AUTO_DOWNLOAD_SETTING_ID]))
    },
  )

  it('does not offer either switch for update notifications, which neither controls', () => {
    expect(hits('update notifications')).not.toContain(GATEWAY_AUTO_UPDATE_SETTING_ID)
    expect(hits('update notifications')).not.toContain(APP_AUTO_DOWNLOAD_SETTING_ID)
  })
})
