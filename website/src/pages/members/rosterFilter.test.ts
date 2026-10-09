/**
 * The roster filter model is pure — members in, rows out — so every dimension
 * is pinned here without a DOM: search, star, origin, the OR'd status set, the
 * sort, the per-row counts, and the storage parsers' junk handling.
 */
import { describe, it, expect } from 'vitest'

import {
  countByFilter, hasNoCrewmates, listedByDefault, matchesStatus, narrowRoster, parseSort, parseStatusFilters, queryNarrows,
  rosterPopulation, rosterShows, sortRoster,
  type MemberSignals, type RosterQuery,
} from './rosterFilter'

const EMPTY_QUERY: RosterQuery = { search: '', starredOnly: false, source: 'all', status: new Set(), sort: 'recent', defaultAgent: '' }

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

/** A roster as a real host serves it. `dashboard_created` is source kirocrew
 *  AND a member id; `has_dm_message` is the DM thread holding a message. */
const NO = { dashboard_created: false, has_dm_message: false }
const MIXED = [
  { name: 'default', ...NO, source: 'builtin' },
  // Dashboard-created, greeting never landed: listed.
  { name: 'radar', display_name: 'Issue Radar', ...NO, dashboard_created: true, source: 'kirocrew', starred: true },
  // Dashboard-created AND chatted: listed.
  { name: 'oncall', dashboard_created: true, has_dm_message: true, source: 'kirocrew' },
  // Legacy kirocrew row: no member id, no message -> hidden.
  { name: 'legacy-aim', ...NO, source: 'kirocrew' },
  // Sync-generated, never chatted -> hidden.
  { name: 'pkg-tool', ...NO, source: 'package' },
  // An app's own stamp, never chatted -> hidden.
  { name: 'app-bot', ...NO, source: 'radar-app' },
  // An app's own stamp, chatted with -> listed.
  { name: 'app-used', ...NO, has_dm_message: true, source: 'radar-app' },
  // An older gateway carries neither field -> listed.
  { name: 'older-gateway', source: 'package' },
]
const byName = (n: string) => MIXED.find((m) => m.name === n)!
const WITH_DEFAULT: RosterQuery = { ...EMPTY_QUERY, defaultAgent: 'default' }
const LISTED = ['default', 'radar', 'oncall', 'app-used', 'older-gateway']

describe('listedByDefault / rosterShows', () => {
  it('lists a dashboard-created row with no message, and any row whose DM thread holds one', () => {
    expect(listedByDefault(byName('radar'), 'default')).toBe(true)
    expect(listedByDefault(byName('oncall'), 'default')).toBe(true)
    expect(listedByDefault(byName('app-used'), 'default')).toBe(true)
    expect(listedByDefault(byName('default'), 'default')).toBe(true)
    expect(listedByDefault(byName('older-gateway'), 'default')).toBe(true)
  })
  it('hides app-stamped, sync and legacy-kirocrew rows with no message', () => {
    expect(listedByDefault(byName('app-bot'), 'default')).toBe(false)
    expect(listedByDefault(byName('pkg-tool'), 'default')).toBe(false)
    expect(listedByDefault(byName('legacy-aim'), 'default')).toBe(false)
    // The default crew is exempt by NAME only.
    expect(listedByDefault(byName('default'), '')).toBe(false)
  })
  it('a starred row is listed like a chatted one, so starring a row reached through the search keeps it', () => {
    expect(listedByDefault(byName('app-bot'), 'default')).toBe(false)
    expect(listedByDefault({ ...byName('app-bot'), starred: true }, 'default')).toBe(true)
  })
  it('a FAILED default-crew lookup (null) lists every row, so the default is never hidden by a read error', () => {
    for (const n of ['app-bot', 'pkg-tool', 'legacy-aim', 'default']) expect(listedByDefault(byName(n), null)).toBe(true)
  })
  it('a live preview counts as a message, so a just-chatted row stays listed before a refetch', () => {
    expect(listedByDefault({ ...byName('app-bot'), last_message: 'hi' }, 'default')).toBe(true)
    expect(listedByDefault({ ...byName('app-bot'), last_message: '  ' }, 'default')).toBe(false)
  })
  it('a typed search decides alone: it reaches hidden rows and skips listed ones it misses', () => {
    expect(rosterShows(byName('legacy-aim'), { search: 'aim', defaultAgent: 'default' })).toBe(true)
    expect(rosterShows(byName('pkg-tool'), { search: ' PKG ', defaultAgent: 'default' })).toBe(true)
    expect(rosterShows(byName('app-bot'), { search: 'bot', defaultAgent: 'default' })).toBe(true)
    expect(rosterShows(byName('radar'), { search: 'pkg', defaultAgent: 'default' })).toBe(false)
    expect(rosterShows(byName('radar'), { search: 'issue radar', defaultAgent: 'default' })).toBe(true)
  })
})

describe('rosterPopulation', () => {
  it('is the default-listed rows with no search', () => {
    expect(names(rosterPopulation(MIXED, WITH_DEFAULT))).toEqual(LISTED)
  })
  it('a search only ADDS the hidden rows it reaches; it never shrinks the population', () => {
    expect(names(rosterPopulation(MIXED, { ...WITH_DEFAULT, search: 'pkg' }))).toEqual([
      'default', 'radar', 'oncall', 'pkg-tool', 'app-used', 'older-gateway',
    ])
    expect(names(rosterPopulation(MIXED, { ...WITH_DEFAULT, search: 'zzz' }))).toEqual(LISTED)
  })
})

describe('narrowRoster hides unlisted rows', () => {
  it('drops them with no search, whatever the other filters say', () => {
    expect(names(narrowRoster(MIXED, WITH_DEFAULT, () => IDLE))).toEqual(LISTED)
    // `source: package` alone would keep pkg-tool and app-bot; the hide rule wins.
    expect(names(narrowRoster(MIXED, { ...WITH_DEFAULT, source: 'package' }, () => IDLE))).toEqual(['app-used', 'older-gateway'])
  })
  it('lets the search reach them, still AND-ed with the other filters', () => {
    expect(names(narrowRoster(MIXED, { ...WITH_DEFAULT, search: 'pkg' }, () => IDLE))).toEqual(['pkg-tool'])
    expect(names(narrowRoster(MIXED, { ...WITH_DEFAULT, search: 'pkg', starredOnly: true }, () => IDLE))).toEqual([])
  })
  it('keeps every row of a roster from a gateway that sends neither field', () => {
    expect(names(narrowRoster(ROSTER, EMPTY_QUERY, signalsOf))).toHaveLength(5)
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

describe('hasNoCrewmates', () => {
  it('treats a default-only roster as empty; the built-in Captain member is a crewmate', () => {
    expect(hasNoCrewmates([])).toBe(true)
    expect(hasNoCrewmates([{ name: 'default' }])).toBe(true)
    expect(hasNoCrewmates([{ name: 'default' }, { name: 'kirocrew-captain' }])).toBe(false)
    expect(hasNoCrewmates([{ name: 'default' }, { name: 'Radar' }])).toBe(false)
    expect(hasNoCrewmates(undefined)).toBe(false)
  })
})
