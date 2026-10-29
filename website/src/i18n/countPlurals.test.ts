/**
 * Seven count labels pick their English form from the count.
 *
 * Each held one value with a plural noun after `{{count}}`, so a count of 1
 * rendered "1 tools", "1 sessions could not be moved", "Peeked 1 times". They now
 * carry `_one` / `_other` forms (registered in `pluralKeys.json`), and the call
 * sites pass the raw number as `count` (what i18next selects on) and the
 * `fmtNumber` text as `n` (what the reader sees).
 *
 * `integration/setup.ts` has already called `initI18n('en')`.
 */
import { describe, expect, it } from 'vitest'

import { i18nT } from './t'

const CASES: ReadonlyArray<readonly [key: string, one: string, two: string]> = [
  ['pages.sessionBreakdown.tool_count', '1 tool', '2 tools'],
  ['pages.sessionStorage.refused_heading', '1 session could not be moved', '2 sessions could not be moved'],
  ['apps.mochi.stats.drags', 'Dragged 1 time', 'Dragged 2 times'],
  ['apps.mochi.stats.peeks', 'Peeked 1 time', 'Peeked 2 times'],
  ['apps.mochi.stats.screenshots', 'Looked at your screen 1 time', 'Looked at your screen 2 times'],
  ['apps.mochi.stats.walks', '1 step', '2 steps'],
  ['apps.mochi.stats.longest_chat', 'Longest chat: 1 message', 'Longest chat: 2 messages'],
]

describe('English count labels agree with their count', () => {
  for (const [key, one, two] of CASES) {
    it(`${key}: singular for 1, plural for 2`, () => {
      expect(i18nT(key, { count: 1, n: '1' })).toBe(one)
      expect(i18nT(key, { count: 2, n: '2' })).toBe(two)
    })
  }
})
