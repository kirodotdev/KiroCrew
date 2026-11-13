import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

/**
 * The settings panel opts back into focus-refetch with a finite `staleTime`.
 *
 * The dashboard's query client defaults every query to `staleTime: Infinity`
 * (`src/test/bootRequests.test.ts` pins that default and the core surfaces that opt
 * out of it). The panel builds a replace-all PUT of the rotation settings from its
 * query cache, so a cache that never goes stale would write a stale copy over a
 * change made in another tab.
 */
describe('Ops Mission Control focus-refetch opt-in', () => {
  it('SettingsPanel sets a finite staleTime (replace-all PUT of rotation settings)', () => {
    const src = readFileSync(resolve(__dirname, 'SettingsPanel.tsx'), 'utf8')
    expect(src).toMatch(/staleTime:\s*\d+/)
  })
})
