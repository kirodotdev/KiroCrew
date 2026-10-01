import { describe, it, expect, vi } from 'vitest'
import { SETTINGS_REGISTRY } from './settingsRegistry.gen'
import { SETTINGS_KEYWORDS } from './settingsKeywords'
import { createSettingsProvider } from './providers/settingsProvider'

it.each(['how long models think', 'think before answering', 'thinking time'])(
  'finds default reasoning effort by the hint phrase "%s"',
  async query => {
    const provider = createSettingsProvider(vi.fn(), { decisionsEnabled: true, tipsEnabled: true })
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
