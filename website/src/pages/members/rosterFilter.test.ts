/**
 * The roster filter model is pure — members in, rows out — so every dimension
 * is pinned here without a DOM: search, star, origin, the OR'd status set, the
 * sort, the per-row counts, and the storage parsers' junk handling.
 */
import { describe, it, expect } from 'vitest'

import {
  countByFilter, listedByDefault, matchesStatus, narrowRoster, parseSort, parseStatusFilters, queryNarrows,
  chatRecency, rosterPopulation, rosterShows, sortRoster,
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

/** A roster as a real host serves it. `has_dm_message` is the DM thread holding
 *  a message, whoever sent it; `dashboard_created` is source kirocrew AND a
 *  member id, and no longer lists a row by itself. */
const NO = { dashboard_created: false, has_dm_message: false }
const MIXED = [
  // The built-in default crewmate: a row like any other, no message -> hidden.
  { name: 'default', ...NO, source: 'builtin' },
  // Dashboard-created, greeting never landed, but starred: listed.
  { name: 'radar', display_name: 'Issue Radar', ...NO, dashboard_created: true, source: 'kirocrew', starred: true },
  // Dashboard-created AND a message in the thread: listed.
  { name: 'oncall', dashboard_created: true, has_dm_message: true, source: 'kirocrew' },
  // Dashboard-created, no message, not starred -> hidden (an empty thread).
  { name: 'fresh', ...NO, dashboard_created: true, source: 'kirocrew' },
  // Legacy kirocrew row: no member id, no message -> hidden.
  { name: 'legacy-aim', ...NO, source: 'kirocrew' },
  // Sync-generated, empty thread -> hidden.
  { name: 'pkg-tool', ...NO, source: 'package' },
  // An app's own stamp, empty thread -> hidden.
  { name: 'app-bot', ...NO, source: 'radar-app' },
  // An app's own stamp whose thread holds a message (the app wrote it) -> listed.
  { name: 'app-used', ...NO, has_dm_message: true, source: 'radar-app' },
  // An older gateway carries neither field -> listed.
  { name: 'older-gateway', source: 'package' },
]
const byName = (n: string) => MIXED.find((m) => m.name === n)!
const LISTED = ['radar', 'oncall', 'app-used', 'older-gateway']

describe('listedByDefault / rosterShows', () => {
  it('lists a row whose DM thread holds a message, whoever sent it, and a starred one', () => {
    expect(listedByDefault(byName('oncall'))).toBe(true)
    expect(listedByDefault(byName('app-used'))).toBe(true)
    expect(listedByDefault(byName('radar'))).toBe(true)
    expect(listedByDefault(byName('older-gateway'))).toBe(true)
  })
  it('hides an empty thread, whatever made the row: default, dashboard-created, app, sync, legacy', () => {
    for (const n of ['default', 'fresh', 'app-bot', 'pkg-tool', 'legacy-aim']) expect(listedByDefault(byName(n))).toBe(false)
  })
  it('lists Mate, the first crewmate, from the start; a fresh roster shows Mate and not the default crewmate', () => {
    const fresh = [
      { name: 'default', ...NO, source: 'builtin' },
      { name: 'mate', ...NO, source: 'builtin' },
    ]
    expect(names(rosterPopulation(fresh, EMPTY_QUERY))).toEqual(['mate'])
  })
  it('a starred row is listed like one with a message, so starring a row reached through the search keeps it', () => {
    expect(listedByDefault(byName('app-bot'))).toBe(false)
    expect(listedByDefault({ ...byName('app-bot'), starred: true })).toBe(true)
  })
  it('the search also reads the agent template, so a row is reachable by what it RUNS', () => {
    // The folded roster in the header chip has always offered this term; it
    // lives here now so the column's search reaches the same rows.
    expect(rosterShows({ name: 'oncall', kiro_agent: 'kirocrew-oncall' }, { search: 'kirocrew-onc', defaultAgent: 'default' })).toBe(true)
    expect(rosterShows({ name: 'oncall', kiro_agent: 'kirocrew-oncall' }, { search: 'radar', defaultAgent: 'default' })).toBe(false)
  })
  it('a typed search decides alone: it reaches hidden rows and skips listed ones it misses', () => {
    expect(rosterShows(byName('legacy-aim'), { search: 'aim' })).toBe(true)
    expect(rosterShows(byName('pkg-tool'), { search: ' PKG ' })).toBe(true)
    expect(rosterShows(byName('app-bot'), { search: 'bot' })).toBe(true)
    expect(rosterShows(byName('radar'), { search: 'pkg' })).toBe(false)
    expect(rosterShows(byName('radar'), { search: 'issue radar' })).toBe(true)
  })
  it('the open row stays listed while open, even with an empty thread', () => {
    expect(rosterShows(byName('fresh'), { search: '', chosen: 'fresh' })).toBe(true)
  })
})

describe('rosterPopulation', () => {
  it('is the default-listed rows with no search', () => {
    expect(names(rosterPopulation(MIXED, EMPTY_QUERY))).toEqual(LISTED)
  })
  it('a search only ADDS the hidden rows it reaches; it never shrinks the population', () => {
    expect(names(rosterPopulation(MIXED, { ...EMPTY_QUERY, search: 'pkg' }))).toEqual([
      'radar', 'oncall', 'pkg-tool', 'app-used', 'older-gateway',
    ])
    expect(names(rosterPopulation(MIXED, { ...EMPTY_QUERY, search: 'zzz' }))).toEqual(LISTED)
  })
})

describe('narrowRoster hides unlisted rows', () => {
  it('drops them with no search, whatever the other filters say', () => {
    expect(names(narrowRoster(MIXED, EMPTY_QUERY, () => IDLE))).toEqual(LISTED)
    // `source: package` alone would keep pkg-tool and app-bot; the hide rule wins.
    expect(names(narrowRoster(MIXED, { ...EMPTY_QUERY, source: 'package' }, () => IDLE))).toEqual(['app-used', 'older-gateway'])
  })
  it('lets the search reach them, still AND-ed with the other filters', () => {
    expect(names(narrowRoster(MIXED, { ...EMPTY_QUERY, search: 'pkg' }, () => IDLE))).toEqual(['pkg-tool'])
    expect(names(narrowRoster(MIXED, { ...EMPTY_QUERY, search: 'pkg', starredOnly: true }, () => IDLE))).toEqual([])
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

describe('Recent order: the newest message in each thread, whoever sent it', () => {
  const CHAT = [
    // A scheduled run wrote the newest message: it is the newest conversation.
    { name: 'bg-only', source: 'kirocrew', dashboard_created: true, has_dm_message: true, last_message: 'patrol note', last_active_ts: 900 },
    { name: 'default', source: 'builtin', has_dm_message: true, last_active_ts: 50 },
    { name: 'app-bot', source: 'radar-app', has_dm_message: false, last_active_ts: 800 },
    { name: 'old-chat', source: 'package', has_dm_message: true, last_active_ts: 5 },
    { name: 'new-chat', source: 'kirocrew', has_dm_message: true, last_active_ts: 300 },
  ]
  it('lists every thread that holds a message, and no empty one', () => {
    expect(CHAT.filter((m) => listedByDefault(m)).map((m) => m.name)).toEqual(['bg-only', 'default', 'old-chat', 'new-chat'])
  })
  it('orders by the thread\'s newest message, like a messages app', () => {
    expect(sortRoster(CHAT, 'recent').map((m) => m.name)).toEqual(['bg-only', 'app-bot', 'new-chat', 'default', 'old-chat'])
    expect(chatRecency({ name: 'x', last_active_ts: 7 })).toBe(7)
    expect(chatRecency({ name: 'x' })).toBe(0)
  })
})
