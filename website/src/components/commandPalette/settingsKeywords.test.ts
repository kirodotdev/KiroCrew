import { describe, it, expect } from 'vitest'
import { SETTINGS_REGISTRY } from './settingsRegistry.gen'
import { SETTINGS_KEYWORDS } from './settingsKeywords'

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
