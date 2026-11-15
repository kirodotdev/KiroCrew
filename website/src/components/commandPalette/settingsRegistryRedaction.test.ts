/**
 * Settings search lists each Security -> Redaction toggle once. A manual entry in
 * `settingsManual.ts` replaces the extracted one only when its id matches the id
 * the extractor derives from the label, so a label change must move the id too.
 */
import { describe, expect, it } from 'vitest'
import { SETTINGS_REGISTRY } from './settingsRegistry.gen'

describe('Security -> Redaction settings entries', () => {
  const redaction = SETTINGS_REGISTRY.filter(e => (e.labelKey ?? '').includes('.credential_redaction_'))

  it('lists each redaction toggle once', () => {
    const keys = redaction.map(e => e.labelKey)
    expect(keys.length).toBeGreaterThanOrEqual(3)
    expect(new Set(keys).size).toBe(keys.length)
  })

  it('deep-links every redaction toggle to its section', () => {
    for (const entry of redaction) {
      expect(entry.params, entry.id).toEqual({ section: 'redaction' })
    }
  })
})
