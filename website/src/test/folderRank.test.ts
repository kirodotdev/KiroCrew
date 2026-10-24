import { describe, it, expect } from 'vitest'
import type { ChatFolder } from '../types'
import rankFixture from '../../../test/fixtures/chat_folder_rank.json'
import { MAX_RANK_LEN, RANK_DIGITS, planPosition, rankBetween, spreadRanks, validRank } from '../utils/folderRank'
import { bySidebarOrder } from '../utils/folderTree'

describe('the shared rank fixture (test/fixtures/chat_folder_rank.json)', () => {
  // The pytest side asserts the same cases through folder_rank.py, so the
  // optimistic draw here and the rank the gateway stores cannot disagree.
  for (const c of rankFixture.between_cases) {
    it(`rankBetween(${c.lo}, ${c.hi})`, () => {
      expect(rankBetween(c.lo, c.hi)).toBe(c.expected)
    })
  }
  for (const c of rankFixture.spread_cases) {
    it(`spreadRanks(${c.count})`, () => {
      expect(spreadRanks(c.count)).toEqual(c.expected)
    })
  }
  for (const c of rankFixture.sort_cases) {
    it(c.name, () => {
      const rows = c.rows.map(r => ({ ...r, parent_id: '' })) as unknown as ChatFolder[]
      expect([...rows].sort(bySidebarOrder).map(r => r.id)).toEqual(c.expected)
    })
  }
})

describe('rankBetween', () => {
  it('keeps random inserts strictly ordered and valid', () => {
    let seed = 20260929
    const rand = (n: number) => {
      seed = (seed * 1103515245 + 12345) % 2 ** 31
      return seed % n
    }
    for (let round = 0; round < 200; round++) {
      const keys = spreadRanks(rand(6)).sort()
      for (let step = 0; step < 40; step++) {
        const i = rand(keys.length + 1)
        const lo = i > 0 ? keys[i - 1] : null
        const hi = i < keys.length ? keys[i] : null
        const key = rankBetween(lo, hi)
        if (key === null) break
        expect(validRank(key)).toBe(key)
        if (lo !== null) expect(lo < key).toBe(true)
        if (hi !== null) expect(key < hi).toBe(true)
        keys.splice(i, 0, key)
      }
    }
  })

  it('refuses a bad interval', () => {
    expect(rankBetween('b', 'a')).toBeNull()
    expect(rankBetween('a', 'a')).toBeNull()
    expect(rankBetween('a0', null)).toBeNull()
  })

  it('refuses rather than growing a key past the cap', () => {
    let lo = 'V'
    for (let i = 0; i < 10_000; i++) {
      const key = rankBetween(lo, 'W')
      if (key === null) return
      expect(key.length).toBeLessThanOrEqual(MAX_RANK_LEN)
      lo = key
    }
    throw new Error('the gap never filled')
  })
})

describe('validRank', () => {
  it('uses the same 62-digit alphabet as the gateway', () => {
    expect(RANK_DIGITS).toHaveLength(62)
    expect([...RANK_DIGITS].sort().join('')).toBe(RANK_DIGITS)
    expect(RANK_DIGITS.slice(0, 11)).toBe('0123456789A')
  })

  it.each([null, undefined, '', 5, true, ['a'], 'a0', 'a b', 'é', 'x'.repeat(MAX_RANK_LEN + 1)])(
    'rejects %j',
    (value) => {
      expect(validRank(value)).toBeNull()
    },
  )
})

describe('planPosition', () => {
  const f = (id: string, rank?: string, order = 0) => ({ id, name: id, order, rank }) as ChatFolder

  it('is one rank between ranked neighbours', () => {
    const { rank, respread } = planPosition([f('a', 'F'), f('b', 'V')], 1)
    expect(respread.size).toBe(0)
    expect('F' < rank && rank < 'V').toBe(true)
  })

  it('spreads a section with an unranked neighbour, keeping its order', () => {
    const { rank, respread } = planPosition([f('a', undefined, 0), f('b', undefined, 1)], 1)
    const a = respread.get('a') as string
    const b = respread.get('b') as string
    expect(a < rank && rank < b).toBe(true)
  })

  it('spreads when any row elsewhere in the section is unranked or tied', () => {
    expect(planPosition([f('a', 'F'), f('b', 'V'), f('c', undefined, 3)], 0).respread.size).toBeGreaterThan(0)
    expect(planPosition([f('a', 'F'), f('b', 'V'), f('c', 'V')], 0).respread.size).toBeGreaterThan(0)
  })
})
