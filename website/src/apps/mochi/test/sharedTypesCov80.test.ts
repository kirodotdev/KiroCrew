/**
 * shared/types — the CompanionStats factory, its JSON parser, and the merge.
 *
 * `parseStatsJSON` is the read path for a file on disk that any crash mid-write
 * can leave truncated, and its whole contract is "never throws, always usable":
 * a throw here takes the stats panel down with no way for the user to recover the
 * file. So every corrupted shape it can meet is enumerated.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'

import {
  IPC,
  createDefaultStats,
  mergeStats,
  parseStatsJSON,
  type CompanionStats,
} from '../src/shared/types'

afterEach(() => {
  vi.useRealTimers()
})

describe('createDefaultStats', () => {
  it('starts a companion on day one with everything else at zero', () => {
    const s = createDefaultStats()
    expect(s.streak).toBe(1)
    expect(s.companionSeconds).toBe(0)
    expect(s.messages).toEqual({ sent: 0, received: 0 })
    expect(s.moods).toEqual({})
    expect(s.busiestDay).toEqual({ date: '', messages: 0 })
  })

  it('dates lastActiveDate as today, zero-padded', () => {
    vi.useFakeTimers()
    // A single-digit month AND day: the padding is the reason todayStr exists,
    // and '2026-8-3' would never match a stored 'YYYY-MM-DD' again.
    vi.setSystemTime(new Date('2026-08-03T10:00:00Z'))
    expect(createDefaultStats().lastActiveDate).toBe('2026-08-03')
  })

  it('hands back a fresh object each time, never a shared reference', () => {
    const a = createDefaultStats()
    const b = createDefaultStats()
    a.moods.happy = 1
    expect(b.moods).toEqual({})
    expect(a.messages).not.toBe(b.messages)
  })
})

describe('parseStatsJSON', () => {
  it('fills missing fields from the defaults', () => {
    const s = parseStatsJSON(JSON.stringify({ streak: 9, peeks: 4 }))
    expect(s.streak).toBe(9)
    expect(s.peeks).toBe(4)
    expect(s.messages).toEqual({ sent: 0, received: 0 })
  })

  it('returns defaults for unparseable JSON instead of throwing', () => {
    expect(parseStatsJSON('{ truncated').streak).toBe(1)
    expect(parseStatsJSON('').streak).toBe(1)
  })

  it('returns defaults for JSON that is not an object', () => {
    expect(parseStatsJSON('null').streak).toBe(1)
    expect(parseStatsJSON('42').streak).toBe(1)
    // An array parses fine and would then merge key-by-key into nonsense.
    expect(parseStatsJSON('[1,2]').streak).toBe(1)
  })
})

describe('mergeStats', () => {
  const base = createDefaultStats()

  it('takes each provided override and leaves the rest alone', () => {
    const out = mergeStats(base, { walkSteps: 12, latestActiveTime: '02:10' })
    expect(out.walkSteps).toBe(12)
    expect(out.latestActiveTime).toBe('02:10')
    expect(out.screenshots).toBe(base.screenshots)
  })

  it('keeps a zero override — 0 is a value, not "unset"', () => {
    const seeded: CompanionStats = { ...base, walkSteps: 50, streak: 7 }
    const out = mergeStats(seeded, { walkSteps: 0 })
    expect(out.walkSteps).toBe(0)
    expect(out.streak).toBe(7)
  })

  it('merges the two nested objects one level deep', () => {
    const seeded: CompanionStats = {
      ...base,
      messages: { sent: 5, received: 5 },
      busiestDay: { date: '2026-07-28', messages: 9 },
    }
    const out = mergeStats(seeded, {
      messages: { sent: 8 } as CompanionStats['messages'],
      busiestDay: { messages: 11 } as CompanionStats['busiestDay'],
    })
    expect(out.messages).toEqual({ sent: 8, received: 5 })
    expect(out.busiestDay).toEqual({ date: '2026-07-28', messages: 11 })
  })
})

describe('IPC channel names', () => {
  it('namespaces every channel and keeps them unique', () => {
    const values = Object.values(IPC)
    expect(new Set(values).size).toBe(values.length)
    expect(values.every((v) => v.includes(':'))).toBe(true)
    expect(IPC.APPROVAL_RESPOND).toBe('approval:respond')
  })
})
