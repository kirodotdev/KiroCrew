/** `workDelta.ts`: the TypeScript twin of `fold_delta.apply_work`, the frame
 *  reader, and the base rule on the cached work board. The fixtures are the same
 *  file the Python side runs (`test/test_fold_delta_receivers.py`). */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { QueryClient, QueryObserver } from '@tanstack/react-query'
import cases from '../../test/fixtures/work-delta-cases.json'
import {
  applySlotFoldFrame,
  applyWorkDelta,
  readSlotFoldFrame,
  resetWorkDeltaBases,
  type SlotFoldOutcome,
} from './workDelta'
import { seedFoldedProjection } from './slotProjection'

type Case = { name: string; before: unknown; ops: unknown; after: unknown }

const KEY = ['command-center', 'board-a', 'work'] as const
const reports = (cases.cases as Case[]).find(c => c.name === 'one item reports')!
const appends = (cases.cases as Case[]).find(c => c.name === 'append an item')!

function frame(revision: number, base: number, ops: unknown = reports.ops, v = 1) {
  return readSlotFoldFrame({ v, slot: 'dashboard:board-a', fold: 'work', kind: 'delta', revision, base_revision: base, ops })!
}

describe('applyWorkDelta (shared fixtures)', () => {
  for (const c of cases.cases as Case[]) {
    it(c.name, () => {
      expect(applyWorkDelta(c.before, c.ops)).toEqual(c.after)
    })
  }

  it('never mutates the held value', () => {
    const before = structuredClone(reports.before)
    applyWorkDelta(before, reports.ops)
    expect(before).toEqual(reports.before)
  })
})

describe('readSlotFoldFrame', () => {
  it('reads a delta in the tab key form', () => {
    expect(frame(6, 5)).toMatchObject({ slot: 'board-a', kind: 'delta', revision: 6, baseRevision: 5 })
  })

  it('reads an unknown version, a missing base or bad ops as snapshot_needed', () => {
    expect(frame(6, 5, reports.ops, 2).kind).toBe('snapshot_needed')
    expect(frame(6, 0).kind).toBe('snapshot_needed')
    expect(frame(6, 5, 'nope').kind).toBe('snapshot_needed')
    expect(readSlotFoldFrame({ slot: 'board-a', fold: 'work', kind: 'snapshot_needed', revision: 3 })?.kind)
      .toBe('snapshot_needed')
  })

  it('needs a board', () => {
    expect(readSlotFoldFrame({ fold: 'work' })).toBeNull()
  })
})

describe('applySlotFoldFrame', () => {
  let qc: QueryClient
  beforeEach(() => {
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    resetWorkDeltaBases(qc)
  })

  function hold(value: unknown, published: number) {
    qc.setQueryData(KEY, { session_id: 'board-a', name: 'work', seq: 3, value, published_revision: published })
  }

  it('applies a delta on its base and moves the base', () => {
    hold(reports.before, 5)
    expect(applySlotFoldFrame(qc, frame(6, 5))).toBe('applied')
    expect(qc.getQueryData(KEY)).toEqual({ session_id: 'board-a', name: 'work', seq: 3, value: reports.after, published_revision: 6 })
  })

  it('drops a duplicate at or below the base', () => {
    hold(reports.after, 6)
    expect(applySlotFoldFrame(qc, frame(6, 5))).toBe('stale')
    expect(applySlotFoldFrame(qc, frame(4, 3))).toBe('stale')
  })

  it('asks for a snapshot on a gap, a snapshot_needed, no base, or ops that do not fit', () => {
    hold(reports.before, 5)
    expect(applySlotFoldFrame(qc, frame(9, 8))).toBe('snapshot')
    expect(applySlotFoldFrame(qc, frame(9, 5, reports.ops, 2))).toBe('snapshot')
    const hole = (cases.cases as Case[]).find(c => c.name.startsWith('hole: order'))!
    expect(applySlotFoldFrame(qc, frame(9, 5, hole.ops))).toBe('snapshot')
    expect(qc.getQueryData<{ published_revision: number }>(KEY)?.published_revision).toBe(5)
    qc.setQueryData(KEY, { value: reports.before })
    expect(applySlotFoldFrame(qc, frame(6, 5))).toBe('snapshot')
  })

  it('a new socket forgets every base', () => {
    hold(reports.before, 5)
    resetWorkDeltaBases(qc)
    expect(qc.getQueryData(KEY)).toEqual({ session_id: 'board-a', name: 'work', seq: 3, value: reports.before })
    expect(applySlotFoldFrame(qc, frame(6, 5))).toBe('snapshot')
  })

  it('holds frames while the board is read, then replays them on the read by the base rule', async () => {
    let resolve!: (v: unknown) => void
    const observer = new QueryObserver(qc, {
      queryKey: KEY,
      queryFn: () => new Promise(r => { resolve = r }),
    })
    const stop = observer.subscribe(() => {})
    const replayed: [number, SlotFoldOutcome][] = []
    const onReplay = (f: { revision: number }, o: SlotFoldOutcome) => { replayed.push([f.revision, o]) }
    expect(applySlotFoldFrame(qc, frame(5, 4, appends.ops), onReplay)).toBe('held')
    expect(applySlotFoldFrame(qc, frame(6, 5), onReplay)).toBe('held')
    // The read's value is the board at revision 5 (appends.after == reports.before).
    resolve({ value: appends.after, published_revision: 5 })
    await vi.waitFor(() => { expect(replayed).toHaveLength(2) })
    expect(replayed).toEqual([[5, 'stale'], [6, 'applied']])
    expect(qc.getQueryData<{ value: unknown }>(KEY)?.value).toEqual(reports.after)
    stop()
  })

  it('a whole-value frame never leaves a delta base under a value it did not label', () => {
    // A plain `slot_projection` frame older than the REST snapshot keeps the snapshot.
    hold(reports.before, 20)
    const older = { slot: 'board-a', fold: 'work', revision: 15, value: appends.before }
    expect(seedFoldedProjection(qc, 'board-a', older)).toBe(true)
    expect(qc.getQueryData(KEY)).toEqual({ session_id: 'board-a', name: 'work', seq: 3, value: reports.before, published_revision: 20 })
    // A newer one replaces the value and drops the base, so the next delta pulls a snapshot.
    const newer = { slot: 'board-a', fold: 'work', revision: 25, value: appends.after }
    expect(seedFoldedProjection(qc, 'board-a', newer)).toBe(true)
    const seeded = qc.getQueryData<Record<string, unknown>>(KEY)
    expect(seeded?.value).toEqual(appends.after)
    expect(seeded).not.toHaveProperty('published_revision')
    expect(applySlotFoldFrame(qc, frame(30, 20))).toBe('snapshot')
  })

  it('ignores a fold this tab does not cache by delta', () => {
    const other = readSlotFoldFrame({ v: 1, slot: 'board-a', fold: 'ledger', kind: 'delta', revision: 2, base_revision: 1, ops: {} })!
    expect(applySlotFoldFrame(qc, other)).toBe('ignored')
  })
})
