/**
 * The Crew board's banding rules.
 *
 * These are the decisions a reviewer cannot check by reading the page: which band
 * claims a row when two flags are set, and that the caller's cached array is left
 * alone. Both are pinned here rather than through a render, because a render would
 * cost a DOM and prove less about the rule itself.
 */

import { describe, it, expect } from 'vitest'

import {
  artifactEntries,
  partitionBoardRows,
  rowKindLabelKey,
} from '../pages/crewBoardRows'
import type { WorkBoardItem } from '../api/crewBoard'

/** A minimal row. Only the fields a band rule reads are interesting; the rest are
 *  filled with the store's own defaults so a test never asserts against a shape
 *  the server cannot produce. */
function item(over: Partial<WorkBoardItem> = {}): WorkBoardItem {
  return {
    schema: 1,
    item_id: 'it_00000001',
    title: 'an item',
    acceptance: {},
    state: 'open',
    verdict: null,
    decision: '',
    round: 1,
    fails: 0,
    status: 'progress',
    summary: '',
    artifacts: {},
    pr: null,
    last_report_at: null,
    created_at: '2026-09-22T09:00:00+00:00',
    closed_at: null,
    orphaned: false,
    stale: false,
    acceptance_concrete: true,
    outstanding: false,
    terminal: false,
    alive: 'running',
    events: [],
    ...over,
  }
}

describe('partitionBoardRows', () => {
  it('lifts an outstanding item into the decision band', () => {
    const rows = [item({ item_id: 'a' }), item({ item_id: 'b', status: 'question', outstanding: true })]
    const bands = partitionBoardRows(rows)
    expect(bands.ruling.map((r) => r.item_id)).toEqual(['b'])
    expect(bands.working.map((r) => r.item_id)).toEqual(['a'])
    expect(bands.finished).toEqual([])
  })

  it('collapses a terminal item into the finished band', () => {
    const bands = partitionBoardRows([item({ item_id: 'z', state: 'accepted', terminal: true })])
    expect(bands.finished.map((r) => r.item_id)).toEqual(['z'])
    expect(bands.ruling).toEqual([])
    expect(bands.working).toEqual([])
  })

  it('keeps a CLOSED question out of the decision band', () => {
    // The whole point of the band is that every row in it is actionable. A
    // conductor can answer a question BY closing the item, and that item still
    // carries `status: "question"` forever — so terminal has to win, or the band
    // fills with finished work and stops being read.
    const bands = partitionBoardRows([
      item({ item_id: 'closed-q', status: 'question', outstanding: true, terminal: true, state: 'rejected' }),
    ])
    expect(bands.ruling).toEqual([])
    expect(bands.finished.map((r) => r.item_id)).toEqual(['closed-q'])
  })

  it('preserves input order within each band', () => {
    const bands = partitionBoardRows([
      item({ item_id: '1' }),
      item({ item_id: '2', outstanding: true }),
      item({ item_id: '3' }),
      item({ item_id: '4', outstanding: true }),
    ])
    expect(bands.ruling.map((r) => r.item_id)).toEqual(['2', '4'])
    expect(bands.working.map((r) => r.item_id)).toEqual(['1', '3'])
  })

  it('does not mutate the caller array, which is React Query cache data', () => {
    const rows = [item({ item_id: 'a', terminal: true }), item({ item_id: 'b' })]
    const before = rows.map((r) => r.item_id)
    partitionBoardRows(rows)
    expect(rows.map((r) => r.item_id)).toEqual(before)
    expect(rows).toHaveLength(2)
  })

  it('returns three empty bands for no items', () => {
    expect(partitionBoardRows([])).toEqual({ ruling: [], working: [], finished: [] })
  })
})

describe('rowKindLabelKey', () => {
  it('ranks orphaned above outstanding and stale', () => {
    const key = rowKindLabelKey(item({ orphaned: true, outstanding: true, stale: true }))
    expect(key).toBe('pages.crewBoard.kind_orphaned')
  })

  it('ranks outstanding above stale', () => {
    expect(rowKindLabelKey(item({ outstanding: true, stale: true }))).toBe('pages.crewBoard.kind_ruling')
  })

  it('names a stale row', () => {
    expect(rowKindLabelKey(item({ stale: true }))).toBe('pages.crewBoard.kind_stale')
  })

  it('returns null for a terminal row so the caller renders its state token', () => {
    // A closed item's kind IS its state, and that word arrives from the store as
    // data. Translating it would put a different word on the page from the one
    // every tool and log reports for the same item.
    expect(rowKindLabelKey(item({ terminal: true, state: 'accepted' }))).toBeNull()
  })

  it('names an ordinary working row', () => {
    expect(rowKindLabelKey(item())).toBe('pages.crewBoard.kind_working')
  })
})

describe('artifactEntries', () => {
  it('sorts artifacts by key for a stable row', () => {
    const entries = artifactEntries(item({ artifacts: { worktree: '/w', branch: 'b', commit: 'c' } }))
    expect(entries.map(([k]) => k)).toEqual(['branch', 'commit', 'worktree'])
  })

  it('drops an empty value rather than rendering a labelled blank', () => {
    expect(artifactEntries(item({ artifacts: { branch: '', commit: 'c' } }))).toEqual([['commit', 'c']])
  })

  it('skips an artifacts pr when the pr field already carries one', () => {
    // Two sources for the same fact can disagree, and a row showing "PR #12"
    // beside "pr 13" is worse than showing one of them.
    const entries = artifactEntries(item({ pr: 12, artifacts: { pr: '13', branch: 'b' } }))
    expect(entries).toEqual([['branch', 'b']])
  })

  it('keeps an artifacts pr when the pr field is empty', () => {
    const entries = artifactEntries(item({ pr: null, artifacts: { pr: '13' } }))
    expect(entries).toEqual([['pr', '13']])
  })

  it('tolerates a row with no artifacts map at all', () => {
    const bare = { ...item(), artifacts: undefined as unknown as Record<string, string> }
    expect(artifactEntries(bare)).toEqual([])
  })
})
