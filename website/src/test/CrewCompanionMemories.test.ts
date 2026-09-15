/**
 * Crew Companion page: guards against a string rendering as its RAW KEY.
 *
 * This is not a hypothetical. `memoryRows` passed i18next a locale-FORMATTED
 * count (`v.toLocaleString()`, a string) as `count`. Plural selection needs a
 * number, so resolution silently failed and the Memories list rendered
 * `apps.crewCompanion.memories.row_reminders` on screen. Every gate was green at
 * the time -- types, catalog parity, the i18n ratchet -- because none of them
 * evaluates a call site against the catalog. Only looking at the page caught it.
 *
 * So the assertion here is deliberately about OUTPUT, not about keys existing: no
 * produced string may still look like a key, at any count, including the plural
 * boundaries 0 / 1 / 2 that select different catalog forms.
 */
import { describe, it, expect } from 'vitest'
import { memoryRows } from '../apps/crew-companion/memories'
import type { CompanionStats } from '../apps/crew-companion/types'

/** A resolved string never still contains its own key path. */
const looksLikeAKey = (s: string) => /apps\.crewCompanion\./.test(s)

const stats = (over: Partial<CompanionStats> = {}): CompanionStats => ({
  companionSeconds: 227_700,
  streak: 3,
  breathingSessions: 4,
  remindersCreated: 7,
  latestActiveTime: '23:59',
  earliestActiveTime: '00:00',
  firstLaunch: new Date(Date.now() - 8 * 86_400_000).toISOString(),
  ...over,
} as CompanionStats)

describe('Crew Companion memories rows', () => {
  // 0 and 1 are the interesting values: they select _other and _one, and 0 is
  // what was on screen when the bug was found.
  for (const count of [0, 1, 2, 1000]) {
    it(`resolves every string with ${count} reminders and breathing sessions`, () => {
      const rows = memoryRows(
        stats({ remindersCreated: count, breathingSessions: count }),
        'Kiro',
      )
      const unresolved = rows.map((r) => r.text).filter(looksLikeAKey)
      expect(unresolved, `unresolved i18n keys: ${unresolved.join(', ')}`).toEqual([])
    })
  }

  it('renders singular and plural differently at 1 vs 2', () => {
    const one = memoryRows(stats({ remindersCreated: 1 }), 'Kiro')
      .map((r) => r.text).join('|')
    const two = memoryRows(stats({ remindersCreated: 2 }), 'Kiro')
      .map((r) => r.text).join('|')
    // If `count` were a string again, both would be the raw key and so equal.
    expect(one).not.toEqual(two)
  })
})
