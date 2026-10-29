/**
 * Pure helpers for the flat-lane "Group by kind" option (issue #25861).
 *
 * `sessionKind` must read the SAME signals the rest of the sidebar already uses
 * -- `crew_mode_root` for Crew Mode, `isCrewmateChat` for a crewmate -- and
 * `groupFlatByKind` must be a VIEW-only re-bucketing: it keeps the leading
 * pinned block in place and preserves the input order within each section, so
 * nothing about a session's stored order changes.
 */
import { describe, it, expect } from 'vitest'
import { sessionKind, groupFlatByKind, KIND_ORDER, KIND_LABEL_KEY, kindGroupingActiveForLane, orderKeysByKindScoped } from '../pages/chat-sidebar/kindGroups'
import type { Slot } from '../pages/chat-sidebar/types'

const slot = (key: string, extra: Partial<Slot> = {}): Slot => ({ key, ...extra }) as Slot

describe('sessionKind -- reuses the sidebar\'s own kind signals', () => {
  it('a crew_mode_root is Crew Mode', () => {
    expect(sessionKind(slot('a', { crew_mode_root: true }), [])).toBe('crew')
  })

  it('a slot stamped agent_kind=member is a crewmate, with no roster lookup', () => {
    expect(sessionKind(slot('b', { agent_kind: 'member' }), [])).toBe('crewmate')
  })

  it('a kind-less slot whose agent the roster lists as a crewmate is a crewmate', () => {
    const agents = [{ name: 'Astra', selection_kind: 'member' }]
    expect(sessionKind(slot('c', { agent: 'Astra' }), agents)).toBe('crewmate')
  })

  it('everything else is Normal', () => {
    expect(sessionKind(slot('d'), [])).toBe('normal')
    expect(sessionKind(slot('e', { agent_kind: 'template' }), [])).toBe('normal')
  })

  it('Crew Mode wins over a crewmate signal (it is the first section)', () => {
    expect(sessionKind(slot('f', { crew_mode_root: true, agent_kind: 'member' }), [])).toBe('crew')
  })
})

describe('groupFlatByKind -- view-only re-bucketing', () => {
  const kindOf = (s: Slot) => sessionKind(s, [])

  it('orders the unpinned tail Crew Mode -> Normal -> From crewmates', () => {
    const list = [
      slot('n1'),
      slot('c1', { crew_mode_root: true }),
      slot('m1', { agent_kind: 'member' }),
      slot('n2'),
      slot('c2', { crew_mode_root: true }),
    ]
    const out = groupFlatByKind(list, () => false, kindOf)
    expect(out.map(s => s.key)).toEqual(['c1', 'c2', 'n1', 'n2', 'm1'])
  })

  it('keeps the leading pinned block first and untouched', () => {
    const isPinned = (s: Slot) => s.key === 'p1' || s.key === 'p2'
    const list = [
      slot('p1', { agent_kind: 'member' }),
      slot('p2'),
      slot('n1'),
      slot('c1', { crew_mode_root: true }),
    ]
    const out = groupFlatByKind(list, isPinned, kindOf)
    // Pinned first, in input order; then the unpinned tail grouped by kind.
    expect(out.map(s => s.key)).toEqual(['p1', 'p2', 'c1', 'n1'])
  })

  it('preserves input order WITHIN each section (the active sort is unchanged)', () => {
    const list = [slot('n3'), slot('n1'), slot('n2')]
    const out = groupFlatByKind(list, () => false, kindOf)
    expect(out.map(s => s.key)).toEqual(['n3', 'n1', 'n2'])
  })

  it('drops no rows and never mutates the input', () => {
    const list = [slot('a', { crew_mode_root: true }), slot('b'), slot('c', { agent_kind: 'member' })]
    const copy = [...list]
    const out = groupFlatByKind(list, () => false, kindOf)
    expect(out).toHaveLength(list.length)
    expect(list).toEqual(copy) // input untouched
  })

  it('an empty kind contributes no rows (so the lane renders no header for it)', () => {
    const list = [slot('n1'), slot('n2')] // no crew, no crewmate
    const out = groupFlatByKind(list, () => false, kindOf)
    expect(out.map(s => s.key)).toEqual(['n1', 'n2'])
  })
})

describe('kind table', () => {
  it('has a label key for every kind in KIND_ORDER', () => {
    for (const k of KIND_ORDER) expect(typeof KIND_LABEL_KEY[k]).toBe('string')
    expect(KIND_ORDER).toEqual(['crew', 'normal', 'crewmate'])
  })
})


describe('kindGroupingActiveForLane -- the one grouping-active rule', () => {
  it('is active only for the flat and tree lanes, and only when the option is on', () => {
    expect(kindGroupingActiveForLane(true, 'flat')).toBe(true)
    expect(kindGroupingActiveForLane(true, 'tree')).toBe(true)
    // Never the board (tag columns, no single list) or the conductor lane
    // (nests by lineage): these must leave the manual order ungrouped so a
    // Manual seed there is the plain order, as with the option off.
    expect(kindGroupingActiveForLane(true, 'board')).toBe(false)
    expect(kindGroupingActiveForLane(true, 'board-flat')).toBe(false)
    expect(kindGroupingActiveForLane(true, 'conductor')).toBe(false)
    // Off everywhere when the option is off.
    for (const lane of ['flat', 'tree', 'board', 'board-flat', 'conductor'] as const) {
      expect(kindGroupingActiveForLane(false, lane)).toBe(false)
    }
  })
})

describe('orderKeysByKindScoped -- bucket only grouped keys, in place', () => {
  const kind = (k: string): 'crew' | 'normal' | 'crewmate' =>
    k.startsWith('c') ? 'crew' : k.startsWith('m') ? 'crewmate' : 'normal'

  it('with every key grouped, reduces to a full kind bucketing', () => {
    const out = orderKeysByKindScoped(['n1', 'c1', 'm1', 'n2'], kind, () => true)
    expect(out).toEqual(['c1', 'n1', 'n2', 'm1'])
  })

  it('leaves ungrouped keys exactly where they are, bucketing only the grouped ones', () => {
    // f1/f2 are "foldered" (ungrouped) and must not move; only the grouped keys
    // (c1, n1) are reordered among the stream slots they occupied.
    const grouped = new Set(['c1', 'n1'])
    const out = orderKeysByKindScoped(['n1', 'f1', 'c1', 'f2'], kind, k => grouped.has(k))
    // Positions 0 and 2 held grouped keys -> fill with kind order [c1, n1];
    // f1 (pos 1) and f2 (pos 3) stay put.
    expect(out).toEqual(['c1', 'f1', 'n1', 'f2'])
  })

  it('with no grouped keys, returns the input order unchanged', () => {
    const out = orderKeysByKindScoped(['f1', 'f2', 'f3'], kind, () => false)
    expect(out).toEqual(['f1', 'f2', 'f3'])
  })
})
