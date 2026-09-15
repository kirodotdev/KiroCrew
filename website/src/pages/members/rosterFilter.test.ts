/**
 * The roster filter model is pure — members in, rows out — so every dimension
 * is pinned here without a DOM: search, star, origin, the OR'd status set, the
 * sort, the per-row counts, and the storage parsers' junk handling.
 */
import { describe, it, expect } from 'vitest'

import {
  countByFilter, matchesStatus, narrowRoster, parseSort, parseStatusFilters, queryNarrows, sortRoster,
  type MemberSignals, type RosterQuery,
} from './rosterFilter'

const EMPTY_QUERY: RosterQuery = { search: '', starredOnly: false, source: 'all', status: new Set(), sort: 'recent' }

const IDLE: MemberSignals = { running: false, needsYou: false, unread: false, patrolling: false }
const SIGNALS: Record<string, MemberSignals> = {
  conductor: { ...IDLE, running: true, patrolling: true },
  kirocrew: { ...IDLE, needsYou: true },
  'pkg-a': { ...IDLE, unread: true },
  'pkg-b': IDLE,
  'legacy-aim': { ...IDLE, running: true },
}
const signalsOf = (m: { name: string }) => SIGNALS[m.name] ?? IDLE
/** What the page does: sort once per the query's sort, then narrow that order. */
const filterRoster = <M extends (typeof ROSTER)[number]>(members: readonly M[], query: RosterQuery, sig: (m: M) => MemberSignals) =>
  narrowRoster(sortRoster(members, query.sort), query, sig)

const ROSTER = [
  { name: 'pkg-b', source: 'package', starred: false, last_active_ts: 10 },
  { name: 'conductor', source: 'kirocrew', starred: true, last_active_ts: 500 },
  { name: 'legacy-aim', source: 'aim', starred: false },
  { name: 'kirocrew', source: 'builtin', starred: false, last_active_ts: 200 },
  { name: 'pkg-a', source: 'package', starred: true, last_active_ts: 200 },
]
const q = (over: Partial<RosterQuery>): RosterQuery => ({ ...EMPTY_QUERY, ...over })
const names = (rows: { name: string }[]) => rows.map(r => r.name)

describe('sortRoster', () => {
  it('recent: newest activity first, ties and never-talked members alphabetical', () => {
    expect(names(sortRoster(ROSTER, 'recent'))).toEqual(['conductor', 'kirocrew', 'pkg-a', 'pkg-b', 'legacy-aim'])
  })
  it('name: locale-aware alphabetical regardless of activity', () => {
    expect(names(sortRoster(ROSTER, 'name'))).toEqual(['conductor', 'kirocrew', 'legacy-aim', 'pkg-a', 'pkg-b'])
  })
  it('does not mutate its input', () => {
    const before = names(ROSTER)
    sortRoster(ROSTER, 'name')
    expect(names(ROSTER)).toEqual(before)
  })
})

describe('sortRoster + narrowRoster', () => {
  it('no query: every member, in sort order', () => {
    expect(names(filterRoster(ROSTER, EMPTY_QUERY, signalsOf))).toHaveLength(5)
  })
  it('search is case-insensitive, trimmed, and a substring match on the name', () => {
    expect(names(filterRoster(ROSTER, q({ search: '  PKG ' }), signalsOf))).toEqual(['pkg-a', 'pkg-b'])
  })
  it('starred keeps only starred members', () => {
    expect(names(filterRoster(ROSTER, q({ starredOnly: true }), signalsOf))).toEqual(['conductor', 'pkg-a'])
  })
  it('origin buckets: mine / builtin / everything else is package', () => {
    expect(names(filterRoster(ROSTER, q({ source: 'mine' }), signalsOf))).toEqual(['conductor'])
    expect(names(filterRoster(ROSTER, q({ source: 'builtin' }), signalsOf))).toEqual(['kirocrew'])
    expect(names(filterRoster(ROSTER, q({ source: 'package' }), signalsOf))).toEqual(['pkg-a', 'pkg-b', 'legacy-aim'])
  })
  it('one status keeps members in that state', () => {
    expect(names(filterRoster(ROSTER, q({ status: new Set(['working']) }), signalsOf))).toEqual(['conductor', 'legacy-aim'])
    expect(names(filterRoster(ROSTER, q({ status: new Set(['needs_you']) }), signalsOf))).toEqual(['kirocrew'])
    expect(names(filterRoster(ROSTER, q({ status: new Set(['unread']) }), signalsOf))).toEqual(['pkg-a'])
    expect(names(filterRoster(ROSTER, q({ status: new Set(['patrolling']) }), signalsOf))).toEqual(['conductor'])
  })
  it('several statuses OR together, like the sidebar\'s session filters', () => {
    expect(names(filterRoster(ROSTER, q({ status: new Set(['needs_you', 'unread']) }), signalsOf))).toEqual(['kirocrew', 'pkg-a'])
  })
  it('dimensions AND together', () => {
    expect(names(filterRoster(ROSTER, q({ starredOnly: true, status: new Set(['working']) }), signalsOf))).toEqual(['conductor'])
    expect(names(filterRoster(ROSTER, q({ source: 'package', search: 'a' }), signalsOf))).toEqual(['pkg-a', 'legacy-aim'])
    expect(names(filterRoster(ROSTER, q({ starredOnly: true, source: 'builtin' }), signalsOf))).toEqual([])
  })
  it('sort applies to the filtered rows', () => {
    expect(names(filterRoster(ROSTER, q({ source: 'package', sort: 'name' }), signalsOf))).toEqual(['legacy-aim', 'pkg-a', 'pkg-b'])
  })
})

describe('narrowRoster', () => {
  it('keeps the order it is given — the page hands it the committed display order, never re-sorted', () => {
    // Deliberately NOT recency order: a refetch that advanced a timestamp must
    // not move rows, so the narrowing step has no opinion on order at all.
    const committed = [ROSTER[3], ROSTER[0], ROSTER[1], ROSTER[4], ROSTER[2]]
    expect(narrowRoster(committed, EMPTY_QUERY, signalsOf).map((m) => m.name)).toEqual([
      'kirocrew', 'pkg-b', 'conductor', 'pkg-a', 'legacy-aim',
    ])
    expect(narrowRoster(committed, { ...EMPTY_QUERY, starredOnly: true }, signalsOf).map((m) => m.name)).toEqual([
      'conductor', 'pkg-a',
    ])
  })
})

describe('matchesStatus', () => {
  it('an empty set matches everything, including a fully idle member', () => {
    expect(matchesStatus(IDLE, new Set())).toBe(true)
  })
  it('a non-empty set needs at least one matching signal', () => {
    expect(matchesStatus(IDLE, new Set(['working', 'unread']))).toBe(false)
    expect(matchesStatus({ ...IDLE, unread: true }, new Set(['working', 'unread']))).toBe(true)
  })
})

describe('queryNarrows', () => {
  it('is true for star / origin / status, never for the search alone', () => {
    expect(queryNarrows(EMPTY_QUERY)).toBe(false)
    expect(queryNarrows(q({ search: 'x' }))).toBe(false)
    expect(queryNarrows(q({ starredOnly: true }))).toBe(true)
    expect(queryNarrows(q({ source: 'mine' }))).toBe(true)
    expect(queryNarrows(q({ status: new Set(['unread']) }))).toBe(true)
    expect(queryNarrows(q({ sort: 'name' }))).toBe(false)
  })
})

describe('countByFilter', () => {
  it('counts each filter on its own over the whole roster', () => {
    const c = countByFilter(ROSTER, signalsOf)
    expect(c.starred).toBe(2)
    expect(c.status).toEqual({ working: 2, needs_you: 1, unread: 1, patrolling: 1 })
    expect(c.source).toEqual({ mine: 1, builtin: 1, package: 3 })
  })
})

describe('storage parsers reject junk', () => {
  it('parseStatusFilters', () => {
    expect([...parseStatusFilters(null)]).toEqual([])
    expect([...parseStatusFilters('not json')]).toEqual([])
    expect([...parseStatusFilters('{"a":1}')]).toEqual([])
    expect([...parseStatusFilters('["working","bogus","unread"]')]).toEqual(['working', 'unread'])
  })
  it('parseSort', () => {
    expect(parseSort(null)).toBe('recent')
    expect(parseSort('name')).toBe('name')
    expect(parseSort('date-desc')).toBe('recent')
  })
})
