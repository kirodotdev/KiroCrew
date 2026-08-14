import { describe, it, expect } from 'vitest'
import * as path from 'path'
import { fileURLToPath } from 'url'
import { extractAll } from '../../../../scripts/settingsExtract'
import { SETTINGS_REGISTRY } from '../settingsRegistry.gen'

/**
 * Anti-stale guard for the settings registry.
 *
 * Runs the extractor in-memory over real source files and asserts the result
 * matches the checked-in generated registry. If this test fails:
 *
 *   run `npm run gen:settings` and commit the updated registry.
 */

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)
const SETTINGS_DIR = path.resolve(__dirname, '../../../pages/settings')

// Valid tabs from SettingsPage.tsx (fork: KiroACP-only + de-Amazoned, so no
// provider/secretary/sync/tasks tabs).
const VALID_TABS = new Set([
  'overview', 'chat', 'voice', 'display', 'browser', 'skills', 'computer-use',
  'instances', 'security', 'notifications', 'channels', 'developer', 'about',
  'privacy',
])

describe('settingsRegistry.gen.ts — anti-stale guard', () => {
  it('checked-in registry matches live extraction (run `npm run gen:settings` if this fails)', () => {
    const { entries } = extractAll(SETTINGS_DIR)
    expect(entries).toEqual(SETTINGS_REGISTRY)
  })

  it('registry has at least 40 entries (minimum floor)', () => {
    expect(SETTINGS_REGISTRY.length).toBeGreaterThanOrEqual(40)
  })

  it('all registry entries have valid tab values', () => {
    for (const entry of SETTINGS_REGISTRY) {
      expect(VALID_TABS.has(entry.tab)).toBe(true)
    }
  })

  it('all registry entries have non-empty id and label', () => {
    for (const entry of SETTINGS_REGISTRY) {
      expect(entry.id.length).toBeGreaterThan(0)
      expect(entry.label.length).toBeGreaterThan(0)
    }
  })

  it('no duplicate ids', () => {
    const ids = SETTINGS_REGISTRY.map(e => e.id)
    expect(new Set(ids).size).toBe(ids.length)
  })
})
