/** The `slot_projection` revision gate: the highest per (slot, fold) wins.
 *
 *  A slot fold joins several unit files, so its `seq` is the newest unit's own and
 *  does not move for a conductor-side change on a board with a worker bound. The
 *  server mints a monotonic revision instead; these cases pin the only rule the
 *  client needs from it, which is that a lower one is DROPPED rather than applied
 *  on top of a newer value — and dropped means nothing happens, not "re-read".
 *
 *  Reached through the `useWebSocket` facade, like every other owner binding: the
 *  accepted-revision ledger is module state, so a second import path to it would
 *  let the reset reach a different Map from the gate. */
import { describe, expect, it, beforeEach, vi } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import {
  baselineOrHeld,
  invalidateBelowFloor,
  recordSlotProjectionFloor,
  resetSlotProjectionRevisions,
  slotProjectionFloor,
  seedFoldedProjection,
  takeFoldedSlotProjection,
} from '../hooks/useWebSocket'

const SLOT = 'dashboard:conductor'
const WORK_KEY = ['command-center', SLOT, 'work']

function frame(revision: number, items: unknown[] = [], fold = 'work') {
  return { slot: SLOT, fold, revision, value: { items } }
}

/** The outcome kind, which is what the router branches on. */
function kindOf(raw: Record<string, unknown>) {
  return takeFoldedSlotProjection(raw).kind
}

beforeEach(() => {
  resetSlotProjectionRevisions()
})

describe('reading a frame', () => {
  it('reads the valued shape', () => {
    expect(takeFoldedSlotProjection(frame(7, ['a']))).toEqual({
      kind: 'folded',
      frame: { slot: 'conductor', fold: 'work', revision: 7, value: { items: ['a'] } },
    })
  })

  it('reads the bare growth signal as bare, so it still prompts a re-read', () => {
    expect(kindOf({ slot: SLOT })).toBe('bare')
  })

  it.each([
    ['no slot', { fold: 'work', revision: 1, value: {} }],
    ['no fold', { slot: SLOT, revision: 1, value: {} }],
    ['a zero revision', { slot: SLOT, fold: 'work', revision: 0, value: {} }],
    ['a negative revision', { slot: SLOT, fold: 'work', revision: -4, value: {} }],
    ['a string revision', { slot: SLOT, fold: 'work', revision: '9', value: {} }],
    ['a NaN revision', { slot: SLOT, fold: 'work', revision: Number.NaN, value: {} }],
    ['an array value', { slot: SLOT, fold: 'work', revision: 1, value: [] }],
    ['a null value', { slot: SLOT, fold: 'work', revision: 1, value: null }],
  ])('reads a frame with %s as bare rather than seeding from it', (_label, raw) => {
    expect(kindOf(raw as Record<string, unknown>)).toBe('bare')
  })
})

describe('the revision gate', () => {
  it('accepts a rising revision', () => {
    expect(kindOf(frame(1))).toBe('folded')
    expect(kindOf(frame(2))).toBe('folded')
    expect(kindOf(frame(9))).toBe('folded')
  })

  it('reports a LOWER revision as stale, which is the whole contract', () => {
    expect(kindOf(frame(5))).toBe('folded')
    expect(kindOf(frame(4))).toBe('stale')
    expect(kindOf(frame(1))).toBe('stale')
  })

  it('reports a REPEAT of the accepted revision as stale', () => {
    // The server mints a fresh revision for every advance and keeps the old one
    // for a cell that folded nothing, so an equal revision is a duplicate
    // delivery and never a new value.
    expect(kindOf(frame(5))).toBe('folded')
    expect(kindOf(frame(5))).toBe('stale')
  })

  it('STALE is distinct from BARE, so a duplicate costs no re-read', () => {
    // Collapsing the two would make every duplicate frame invalidate the board's
    // query -- the request the revision exists to save.
    expect(kindOf(frame(5))).toBe('folded')
    expect(kindOf(frame(5))).not.toBe('bare')
  })

  it('keeps one revision PER FOLD, so one fold cannot mask another', () => {
    expect(kindOf(frame(10, [], 'work'))).toBe('folded')
    // A lower revision on a DIFFERENT fold of the same slot is still new to it.
    expect(kindOf(frame(3, [], 'panel'))).toBe('folded')
    expect(kindOf(frame(3, [], 'panel'))).toBe('stale')
  })

  it('keeps one revision PER SLOT, so one board cannot mask another', () => {
    expect(kindOf({ ...frame(10), slot: 'dashboard:a' })).toBe('folded')
    expect(kindOf({ ...frame(2), slot: 'dashboard:b' })).toBe('folded')
  })

  it('forgets its numbers on reset, because a revision is one process own', () => {
    expect(kindOf(frame(900))).toBe('folded')
    resetSlotProjectionRevisions()
    // A restarted gateway folds cold and numbers from 1 again; without the reset
    // every frame from it would read as stale and the board would never update.
    expect(kindOf(frame(1))).toBe('folded')
  })
})

describe('seeding the cache', () => {
  /** The frame, taken through the gate so the test seeds what the router seeds. */
  function taken(revision: number, items: unknown[], fold = 'work') {
    const read = takeFoldedSlotProjection(frame(revision, items, fold))
    if (read.kind !== 'folded') throw new Error(`expected a folded frame, got ${read.kind}`)
    return read.frame
  }

  it('writes the value under the work board key and keeps the rest of the entry', () => {
    const client = new QueryClient()
    client.setQueryData(WORK_KEY, { name: 'work', seq: 12, value: { items: ['old'] } })

    expect(seedFoldedProjection(client, SLOT, taken(4, ['new']))).toBe(true)

    expect(client.getQueryData(WORK_KEY)).toEqual({
      name: 'work', seq: 12, value: { items: ['new'] }, revision: 4,
    })
  })

  it('seeds a key the tab has never read, which is where a push saves the most', () => {
    const client = new QueryClient()
    seedFoldedProjection(client, SLOT, taken(1, ['first']))
    expect(client.getQueryData(WORK_KEY)).toEqual({ value: { items: ['first'] }, revision: 1 })
  })

  it('never moves a held value backwards: a delayed older frame leaves a newer baseline', () => {
    const client = new QueryClient()
    // A REST read answered at revision 10; it raises no floor, so a frame at 9 passes it.
    client.setQueryData(WORK_KEY, { name: 'work', value: { items: ['rest'] }, revision: 10 })
    expect(seedFoldedProjection(client, SLOT, taken(9, ['delayed']))).toBe(true)
    expect(client.getQueryData(WORK_KEY)).toEqual({ name: 'work', value: { items: ['rest'] }, revision: 10 })
    // An equal revision is the same fold: kept too.
    seedFoldedProjection(client, SLOT, taken(10, ['same']))
    expect(client.getQueryData(WORK_KEY)).toEqual({ name: 'work', value: { items: ['rest'] }, revision: 10 })
    // A newer frame still lands.
    seedFoldedProjection(client, SLOT, taken(11, ['newer']))
    expect(client.getQueryData(WORK_KEY)).toEqual({ name: 'work', value: { items: ['newer'] }, revision: 11 })
  })

  it('reports false for a fold this tab caches under no key of its own', () => {
    const client = new QueryClient()
    expect(seedFoldedProjection(client, SLOT, taken(1, [], 'radar'))).toBe(false)
    expect(client.getQueryData(WORK_KEY)).toBeUndefined()
  })
})

describe('a baseline against the floor', () => {
  it('accepts a baseline AT the floor, which is the read an unseeded push asked for', () => {
    // A frame at revision 5 raised the floor but could not seed (a read was in
    // flight). The follow-up read returns revision 5; it must replace the 4 held.
    expect(kindOf(frame(5, ['new']))).toBe('folded')
    const held = { value: { items: ['old'] }, revision: 4 }
    const response = { value: { items: ['new'] }, revision: 5 }
    expect(baselineOrHeld(SLOT, 'work', response, held)).toBe(response)
  })

  it('keeps the held value against a baseline BELOW the floor', () => {
    expect(kindOf(frame(5, ['new']))).toBe('folded')
    const held = { value: { items: ['new'] }, revision: 5 }
    const response = { value: { items: ['old'] }, revision: 4 }
    expect(baselineOrHeld(SLOT, 'work', response, held)).toBe(held)
  })
})

describe('a cached value below its floor is never served', () => {
  it('refuses a baseline below the floor when the held value is below it too', () => {
    // A read cached revision 4, then a floor of 5 was recorded; a read still in
    // flight resolves at 4. Keeping the held 4 would serve a value already passed.
    recordSlotProjectionFloor({ revisions: { [SLOT]: { work: 5 } } })
    const held = { value: { items: ['old'] }, revision: 4 }
    const response = { value: { items: ['old'] }, revision: 4 }
    expect(() => baselineOrHeld(SLOT, 'work', response, held)).toThrow()
  })

  it('re-reads every cached board below its floor once a floor is recorded, and only those', () => {
    const queryClient = new QueryClient()
    queryClient.setQueryData(['command-center', 'conductor', 'work'], { value: { items: [] }, revision: 4 })
    queryClient.setQueryData(['command-center', 'current', 'work'], { value: { items: [] }, revision: 7 })
    queryClient.setQueryData(['command-center', 'unnumbered', 'work'], { value: { items: [] } })
    queryClient.setQueryData(['command-center', 'conductor', 'artifacts'], [])
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    recordSlotProjectionFloor({
      revisions: { [SLOT]: { work: 5 }, 'dashboard:current': { work: 7 }, 'dashboard:unnumbered': { work: 2 } },
    })
    invalidateBelowFloor(queryClient)
    expect(invalidate.mock.calls.map(c => c[0]?.queryKey)).toEqual([
      ['command-center', 'conductor', 'work'],
      ['command-center', 'unnumbered', 'work'],
    ])
  })
})

describe('the floor after a gateway restart', () => {
  it('REPLACES the floors with the subscribe frame, so a restarted process is not judged by the old one', () => {
    recordSlotProjectionFloor({ revisions: { [SLOT]: { work: 90 } } })
    expect(slotProjectionFloor(SLOT, 'work')).toBe(90)
    // The new process numbers from 1 again; its subscribe frame is the truth.
    recordSlotProjectionFloor({ revisions: { [SLOT]: { work: 3 } } })
    expect(slotProjectionFloor(SLOT, 'work')).toBe(3)
    expect(kindOf(frame(4, ['after-restart']))).toBe('folded')
  })

  it('drops a floor the new process does not name', () => {
    recordSlotProjectionFloor({ revisions: { [SLOT]: { work: 90 } } })
    recordSlotProjectionFloor({ revisions: {} })
    expect(slotProjectionFloor(SLOT, 'work')).toBe(0)
  })
})

describe('a COLD baseline against the floor', () => {
  it('refuses a cold baseline BELOW the floor, keeping nothing, so the query reads again', () => {
    expect(kindOf(frame(5, ['pushed']))).toBe('folded')
    const response = { value: { items: ['older'] }, revision: 4 }
    expect(() => baselineOrHeld(SLOT, 'work', response, undefined)).toThrow()
  })

  it('accepts a cold baseline at or above the floor', () => {
    expect(kindOf(frame(5, ['pushed']))).toBe('folded')
    const at = { value: { items: ['same'] }, revision: 5 }
    const above = { value: { items: ['newer'] }, revision: 6 }
    expect(baselineOrHeld(SLOT, 'work', at, undefined)).toBe(at)
    expect(baselineOrHeld(SLOT, 'work', above, undefined)).toBe(above)
  })
})
