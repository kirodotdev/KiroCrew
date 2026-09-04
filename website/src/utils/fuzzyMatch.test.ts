import { describe, it, expect } from 'vitest'
import {
  fuzzyMatch,
  compareByScoreThenName,
  makeScoreThenNameComparator,
  type FuzzyMatchResult,
} from './fuzzyMatch'

// Helper: assert a match happened and narrow the type from `… | null`.
function expectMatch(result: FuzzyMatchResult | null): FuzzyMatchResult {
  expect(result).not.toBeNull()
  return result as FuzzyMatchResult
}

describe('fuzzyMatch — empty / neutral query', () => {
  it('returns a neutral { score: 0, indices: [] } for an empty query', () => {
    expect(fuzzyMatch('', 'cron jobs')).toEqual({ score: 0, indices: [] })
  })

  it('treats a whitespace-only query as empty (providers fall back to recents)', () => {
    expect(fuzzyMatch('   ', 'cron jobs')).toEqual({ score: 0, indices: [] })
    expect(fuzzyMatch('\t', 'anything')).toEqual({ score: 0, indices: [] })
  })

  it('trims surrounding whitespace before matching', () => {
    const trimmed = expectMatch(fuzzyMatch('  cron  ', 'cron'))
    const plain = expectMatch(fuzzyMatch('cron', 'cron'))
    expect(trimmed).toEqual(plain)
  })
})

describe('fuzzyMatch — non-match', () => {
  it('returns null when the query is not a subsequence of the candidate', () => {
    expect(fuzzyMatch('xyz', 'cron')).toBeNull()
    expect(fuzzyMatch('cx', 'cron')).toBeNull()
  })

  it('returns null when only a prefix of the query matches', () => {
    // 'cronx' — 'cron' matches but trailing 'x' is never consumed.
    expect(fuzzyMatch('cronx', 'cron')).toBeNull()
  })

  it('returns null for a non-empty query against an empty candidate', () => {
    expect(fuzzyMatch('a', '')).toBeNull()
  })
})

describe('fuzzyMatch — exact match scores highest', () => {
  it('scores an exact (case-insensitive) match above a prefix match', () => {
    const exact = expectMatch(fuzzyMatch('cron', 'cron'))
    const prefix = expectMatch(fuzzyMatch('cron', 'cron jobs'))
    expect(exact.score).toBeGreaterThan(prefix.score)
  })

  it('scores a prefix match above a scattered subsequence match', () => {
    const prefix = expectMatch(fuzzyMatch('cr', 'cron'))
    const scattered = expectMatch(fuzzyMatch('cr', 'cellar'))
    expect(prefix.score).toBeGreaterThan(scattered.score)
  })

  it('produces a strictly positive score for any genuine match', () => {
    const r = expectMatch(fuzzyMatch('s', 'a-very-long-candidate-string-ending-s'))
    expect(r.score).toBeGreaterThan(0)
  })
})

describe('fuzzyMatch — subsequence indices', () => {
  it('returns ascending indices of the matched characters', () => {
    const r = expectMatch(fuzzyMatch('cj', 'cron jobs'))
    expect(r.indices).toEqual([0, 5])
  })

  it('returns every index for a contiguous full match', () => {
    const r = expectMatch(fuzzyMatch('cron', 'cron'))
    expect(r.indices).toEqual([0, 1, 2, 3])
  })

  it('matches greedily left-to-right across word boundaries', () => {
    // 's','e' -> 'se' in 'session'; 'g' from 'grid'.
    const r = expectMatch(fuzzyMatch('seg', 'session grid'))
    expect(r.indices).toEqual([0, 1, 8])
    // indices are strictly ascending
    for (let i = 1; i < r.indices.length; i++) {
      expect(r.indices[i]).toBeGreaterThan(r.indices[i - 1])
    }
  })
})

describe('fuzzyMatch — case-insensitivity', () => {
  it('matches regardless of query/candidate case', () => {
    const upperQuery = expectMatch(fuzzyMatch('CRON', 'cron'))
    const lowerQuery = expectMatch(fuzzyMatch('cron', 'cron'))
    expect(upperQuery.score).toBe(lowerQuery.score)
    expect(upperQuery.indices).toEqual([0, 1, 2, 3])
  })

  it('matches a lowercase query against an uppercase candidate', () => {
    const r = expectMatch(fuzzyMatch('abc', 'ABC'))
    expect(r.indices).toEqual([0, 1, 2])
  })
})

describe('fuzzyMatch — contiguous-run bonus vs scattered', () => {
  it('ranks a contiguous run above a scattered match of the same query', () => {
    const contiguous = expectMatch(fuzzyMatch('cr', 'cron')) // c,r adjacent
    const scattered = expectMatch(fuzzyMatch('cr', 'cellar')) // c..r far apart
    expect(contiguous.score).toBeGreaterThan(scattered.score)
  })

  it('rewards word-boundary matches over interior-gap matches', () => {
    // 'nj' -> 'new job': boundary 'j' after a space.
    const boundary = expectMatch(fuzzyMatch('nj', 'new job'))
    // 'nj' -> 'ninja': interior 'j' with no boundary/consecutive bonus.
    const interior = expectMatch(fuzzyMatch('nj', 'ninja'))
    expect(boundary.score).toBeGreaterThan(interior.score)
  })
})

describe('compareByScoreThenName — score-desc / name-asc tie-break', () => {
  it('orders by score descending first', () => {
    const sorted = [
      { score: 5, name: 'low' },
      { score: 30, name: 'high' },
      { score: 12, name: 'mid' },
    ].sort(compareByScoreThenName)
    expect(sorted.map((x) => x.name)).toEqual(['high', 'mid', 'low'])
  })

  it('breaks score ties by name ascending (case-insensitive)', () => {
    const sorted = [
      { score: 10, name: 'banana' },
      { score: 10, name: 'Apple' },
      { score: 10, name: 'cherry' },
    ].sort(compareByScoreThenName)
    expect(sorted.map((x) => x.name)).toEqual(['Apple', 'banana', 'cherry'])
  })

  it('returns 0 for equal score and equal name (stable)', () => {
    expect(compareByScoreThenName({ score: 1, name: 'x' }, { score: 1, name: 'x' })).toBe(0)
  })
})

describe('makeScoreThenNameComparator — generic accessors', () => {
  it('sorts arbitrary shapes using supplied score/name accessors', () => {
    interface Row {
      relevance: number
      title: string
    }
    const cmp = makeScoreThenNameComparator<Row>(
      (r) => r.relevance,
      (r) => r.title,
    )
    const sorted = [
      { relevance: 10, title: 'beta' },
      { relevance: 20, title: 'gamma' },
      { relevance: 10, title: 'alpha' },
    ].sort(cmp)
    expect(sorted.map((r) => r.title)).toEqual(['gamma', 'alpha', 'beta'])
  })
})
