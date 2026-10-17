/** The `slot_projection` revision gate: the highest per (slot, fold) wins, and it
 *  decides about FRAMES only.
 *
 *  A slot fold joins several unit files, so its `seq` is the newest unit's own and
 *  does not move for a conductor-side change on a board with a worker bound. The
 *  server mints a monotonic revision instead; these cases pin the only rule the
 *  client needs from it, which is that a lower one is DROPPED rather than applied
 *  on top of a newer value — and dropped means nothing happens, not "re-read".
 *
 *  The ledger holds only what a frame actually delivered here. No case below asks
 *  it about a REST read, because it is never asked: a read is applied whatever
 *  revision it carries. `commandCenterSources.test.tsx` pins that end.
 *
 *  Reached through the `useWebSocket` facade, like every other owner binding: the
 *  accepted-revision ledger is module state, so a second import path to it would
 *  let the reset reach a different Map from the gate. */
import { describe, expect, it, beforeEach } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import {
  resetSlotProjectionRevisions,
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
    // A prior FRAME applied revision 10 (a read caches no revision); a delayed
    // frame at 9 is older than it, so it is dropped and the baseline stays.
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

describe('the ledger records only what a frame delivered', () => {
  it('a revision never delivered here cannot make the frame that delivers it stale', () => {
    // The server may be at revision 90 and this tab at nothing: it has taken no
    // frame and may not have read either. The frame carrying 90 is the first
    // sight of that value, so it is taken.
    expect(kindOf(frame(90, ['first sight']))).toBe('folded')
    // And once taken, the SAME number is a duplicate.
    expect(kindOf(frame(90, ['again']))).toBe('stale')
  })

  it('after a gateway restart the ledger is empty, so the new process numbers from 1 freely', () => {
    expect(kindOf(frame(900))).toBe('folded')
    // The socket re-opens on a restarted gateway, which mints from 1 again.
    resetSlotProjectionRevisions()
    expect(kindOf(frame(1, ['after-restart']))).toBe('folded')
    expect(kindOf(frame(2, ['after-restart']))).toBe('folded')
  })
})
