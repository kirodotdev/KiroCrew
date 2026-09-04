import { describe, it, expect } from 'vitest'

import { SETTINGS_MANUAL } from '../components/commandPalette/settingsManual'
import { SECTION_LABEL_KEY } from '../pages/settings/SecurityPanel'

/**
 * Pins every manual entry's `params.section` to a REAL SecurityPanel rail key.
 *
 * The manual entries hand-carry `section=` strings that SecurityPanel's
 * list-detail rail must recognize to mount anything: a section rename in the
 * panel would otherwise silently turn the deep link into a mounted-nothing
 * no-op — exactly the failure mode the `?section=` params were added to fix.
 * SECTION_LABEL_KEY is keyed by the panel's own SecuritySectionKey union, so
 * this fails at test time instead of shipping a dead link.
 */
describe('settingsManual — security section params', () => {
  const validSections = new Set(Object.keys(SECTION_LABEL_KEY))

  it('every security entry carries a section param the panel can mount', () => {
    const securityEntries = SETTINGS_MANUAL.filter(e => e.tab === 'security')
    expect(securityEntries.length).toBeGreaterThan(0)
    for (const entry of securityEntries) {
      expect(entry.params?.section, `entry ${entry.id} must carry params.section`).toBeTruthy()
      expect(
        validSections.has(entry.params!.section),
        `entry ${entry.id} points at section '${entry.params!.section}', which is not a SecurityPanel rail key (${[...validSections].join(', ')})`,
      ).toBe(true)
    }
  })
})
