import { describe, it, expect, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import type { ChatSlot } from '../types'

import * as apiModule from '../api/client'
import dashboardReducer, {
  sseSlots,
  fetchSlots,
  slotCloseStarted,
  slotCloseRetireRead,
} from '../store/dashboardSlice'

const slot = (key: string): ChatSlot => ({ key, messages: 0, running: false, created: '2026-01-01T00:00:00Z' })

const store = () => configureStore({ reducer: { dashboard: dashboardReducer } })
const keys = (s: ReturnType<ReturnType<typeof store>['getState']>): string[] =>
  (s.dashboard.slots ?? []).map(r => r.key)

/** The retiring post-DELETE GET, as the reducer sees it: payload without the closed
 *  slot, its own `readId`, and the server's stamp for that snapshot. */
const retiringGet = (rows: ChatSlot[], readId: string, generation: number) =>
  fetchSlots.fulfilled(rows, readId, undefined, { appliedProvisional: true, generation })

/** GPT 5.6 FINDING (blocking, security-fenced) in this slice -- once the close's
 *  own GET retired the tombstone, a WebSocket frame serialized BEFORE the pop but
 *  delivered after it was applied with no ordering data, so the closed row came back. */
describe('a slots snapshot older than the one applied cannot resurrect a closed row', () => {
  it('drops a pre-pop push delivered after the retiring GET', () => {
    const s = store()
    s.dispatch(sseSlots({ slots: [slot('a'), slot('b')], generation: 1 }))
    expect(keys(s.getState())).toEqual(['a', 'b'])

    s.dispatch(slotCloseStarted('b'))
    s.dispatch(slotCloseRetireRead({ key: 'b', readId: 'r1' }))
    // The close's own read: 'b' is gone and the tombstone retires on it.
    s.dispatch(retiringGet([slot('a')], 'r1', 2))
    expect(keys(s.getState())).toEqual(['a'])
    expect(s.getState().dashboard.closingSlots.b).toBeUndefined()

    // THE DEFECT: this frame was serialized before the pop (generation 1) and still
    // lists 'b'. With no tombstone left, nothing but the stamp can refuse it.
    s.dispatch(sseSlots({ slots: [slot('a'), slot('b')], generation: 1 }))
    expect(keys(s.getState())).toEqual(['a'])
  })

  it('still applies a NEWER push that legitimately lists the key again', () => {
    const s = store()
    s.dispatch(sseSlots({ slots: [slot('a'), slot('b')], generation: 1 }))
    s.dispatch(slotCloseStarted('b'))
    s.dispatch(slotCloseRetireRead({ key: 'b', readId: 'r1' }))
    s.dispatch(retiringGet([slot('a')], 'r1', 2))
    expect(keys(s.getState())).toEqual(['a'])

    // A key is reusable, so a LATER snapshot naming it is a real new session, not the
    // corpse of the closed one. Refusing this would hide a live row for good.
    s.dispatch(sseSlots({ slots: [slot('a'), slot('b')], generation: 3 }))
    expect(keys(s.getState())).toEqual(['a', 'b'])
  })

  it('applies an UNSTAMPED frame, so an older gateway still drives the sidebar', () => {
    const s = store()
    s.dispatch(sseSlots([slot('a'), slot('b')]))
    expect(keys(s.getState())).toEqual(['a', 'b'])
    expect(s.getState().dashboard.lastSlotsGeneration).toBe(0)
  })

  it('refuses a GET whose snapshot predates one already applied', () => {
    const s = store()
    s.dispatch(sseSlots({ slots: [slot('a'), slot('b')], generation: 5 }))
    // Left before generation 5 and would evict 'b'; refused WHOLE, so the list stands.
    s.dispatch(retiringGet([slot('a')], 'r9', 4))
    expect(keys(s.getState())).toEqual(['a', 'b'])
    expect(s.getState().dashboard.lastSlotsRead).toEqual({ readId: 'r9', applied: false })
  })
})

/** GPT 5.6 + Design Review (blocking) -- a gateway restart resets the server counter while a
 *  still-loaded tab keeps its high value, so a generation-only comparison rejected every
 *  snapshot the new process sent, with no recovery short of a reload. */
describe('a snapshot from a NEW gateway process is never stale', () => {
  it('accepts a LOWER generation once the epoch changes, and rebases onto it', () => {
    const s = store()
    s.dispatch(sseSlots({ slots: [slot('a'), slot('b')], generation: 40, epoch: 'boot-1' }))
    expect(s.getState().dashboard.lastSlotsGeneration).toBe(40)

    // The restarted gateway counts from 1 again. Under a generation-only rule 1 <= 40 would
    // drop this frame and every later one.
    s.dispatch(sseSlots({ slots: [slot('c')], generation: 1, epoch: 'boot-2' }))
    expect(keys(s.getState())).toEqual(['c'])
    expect(s.getState().dashboard.lastSlotsGeneration).toBe(1)
    expect(s.getState().dashboard.lastSlotsEpoch).toBe('boot-2')
  })

  it('still refuses a stale frame from within the SAME epoch', () => {
    const s = store()
    s.dispatch(sseSlots({ slots: [slot('a')], generation: 7, epoch: 'boot-2' }))
    // Same process, lower count: the ordering guarantee still applies and this is dropped.
    s.dispatch(sseSlots({ slots: [slot('a'), slot('b')], generation: 6, epoch: 'boot-2' }))
    expect(keys(s.getState())).toEqual(['a'])
  })

  it('recovers on the GET path too, not only the socket', () => {
    const s = store()
    s.dispatch(sseSlots({ slots: [slot('a'), slot('b')], generation: 40, epoch: 'boot-1' }))
    s.dispatch(fetchSlots.fulfilled([slot('z')], 'r1', undefined, {
      appliedProvisional: true, generation: 2, epoch: 'boot-2',
    }))
    expect(keys(s.getState())).toEqual(['z'])
    expect(s.getState().dashboard.lastSlotsRead).toEqual({ readId: 'r1', applied: true })
  })
})

/** FIRST-PRINCIPLES FINDING -- two channels answer "did this read apply": the thunk's
 *  `appliedProvisional` and the reducer's `lastSlotsRead.applied`. The thunk's copy weighed
 *  only `closeSeq`, so once the wire began dating snapshots a STALE reply was still
 *  announced as applied, and `chatSlice`'s residue prune ran on a read the reducer refused.
 *  Both now evaluate staleness through the same helper, so they cannot disagree about it. */
describe('the provisional applied flag agrees with the refusal the reducer makes', () => {
  it('reports a stale reply as NOT applied', async () => {
    const s = store()
    // Establish a baseline the next reply will be older than.
    s.dispatch(sseSlots({ slots: [slot('a')], generation: 9, epoch: 'boot-1' }))

    const spy = vi
      .spyOn(apiModule.api, 'chatSlots')
      .mockResolvedValue({ slots: [slot('a'), slot('b')], generation: 4, epoch: 'boot-1' })
    try {
      const action = await s.dispatch(fetchSlots() as never)
      expect(spy).toHaveBeenCalled()
      expect((action as { meta: { appliedProvisional: boolean } }).meta.appliedProvisional).toBe(
        false,
      )
      // And the authoritative channel must say the same thing about the same reply.
      expect(s.getState().dashboard.lastSlotsRead?.applied).toBe(false)
    } finally {
      spy.mockRestore()
    }
  })

  it('reports a NEWER reply as applied, so the gate is not simply always false', async () => {
    const s = store()
    s.dispatch(sseSlots({ slots: [slot('a')], generation: 2, epoch: 'boot-1' }))
    const spy = vi
      .spyOn(apiModule.api, 'chatSlots')
      .mockResolvedValue({ slots: [slot('a'), slot('b')], generation: 7, epoch: 'boot-1' })
    try {
      const action = await s.dispatch(fetchSlots() as never)
      expect((action as { meta: { appliedProvisional: boolean } }).meta.appliedProvisional).toBe(
        true,
      )
      expect(s.getState().dashboard.lastSlotsRead?.applied).toBe(true)
    } finally {
      spy.mockRestore()
    }
  })
})

/** retired gateway snapshots overwrote
 *  current membership. A differing epoch means "not comparable", NOT stale, deliberately: the
 *  counter restarts at 0 in a new process, so treating a differing epoch as stale would refuse
 *  every snapshot a restarted gateway sends with no recovery until reload. That left a window:
 *  once a snapshot from a NEW epoch was accepted, a reply still in flight from the OLD one was
 *  still "not comparable", so it applied and restored rows the new gateway no longer lists.
 *
 *  The fix distinguishes an epoch never seen (accept -- this is the recovery path) from one
 *  SUPERSEDED by an accepted replacement (refuse -- this client has moved past that process). */
describe('a snapshot from a retired gateway epoch cannot restore stale membership', () => {
  const frame = (rows: ChatSlot[], generation: number, epoch: string) =>
    sseSlots({ slots: rows, generation, epoch })

  it('refuses a delayed reply from the epoch a newer snapshot superseded', () => {
    const s = store()
    s.dispatch(frame([slot('a'), slot('b')], 10, 'boot-1'))
    expect(keys(s.getState())).toEqual(['a', 'b'])

    // The gateway restarts. Its counter starts low, and a differing epoch is not comparable,
    // so this MUST still apply -- that is the recovery path the docstring protects.
    s.dispatch(frame([slot('a')], 1, 'boot-2'))
    expect(keys(s.getState())).toEqual(['a'])
    expect(s.getState().dashboard.retiredSlotsEpochs).toContain('boot-1')

    // The old gateway's in-flight reply lands late, still carrying the retired epoch.
    s.dispatch(frame([slot('a'), slot('b')], 11, 'boot-1'))

    expect(keys(s.getState())).toEqual(['a'])
  })

  it('still accepts an epoch it has never seen, so a restart recovers', () => {
    const s = store()
    s.dispatch(frame([slot('a'), slot('b')], 40, 'boot-1'))
    // Lower generation, unseen epoch: not comparable, and not retired.
    s.dispatch(frame([slot('a'), slot('b'), slot('c')], 2, 'boot-9'))
    expect(keys(s.getState())).toEqual(['a', 'b', 'c'])
  })

  it('retires only on a CHANGE, so a same-epoch sequence never refuses itself', () => {
    const s = store()
    s.dispatch(frame([slot('a')], 1, 'boot-1'))
    s.dispatch(frame([slot('a'), slot('b')], 2, 'boot-1'))
    s.dispatch(frame([slot('a'), slot('b'), slot('c')], 3, 'boot-1'))
    expect(keys(s.getState())).toEqual(['a', 'b', 'c'])
    expect(s.getState().dashboard.retiredSlotsEpochs).toEqual([])
  })

  it('bounds the retired set rather than growing it without limit', () => {
    const s = store()
    for (let i = 1; i <= 14; i++) s.dispatch(frame([slot('a')], i, `boot-${i}`))
    const retired = s.getState().dashboard.retiredSlotsEpochs
    expect(retired.length).toBeLessThanOrEqual(8)
    // Most recent first, so the epochs a late reply could plausibly carry are the kept ones.
    expect(retired[0]).toBe('boot-13')
  })

  it('refuses a WS frame older than an applied HTTP snapshot (baseline lives here now)', () => {
    const s = store()
    s.dispatch(frame([slot('a'), slot('b')], 10, 'boot-1'))
    s.dispatch(
      fetchSlots.fulfilled([slot('a'), slot('b')], 'r-http', undefined, {
        appliedProvisional: true,
        generation: 12,
        epoch: 'boot-1',
      }),
    )
    s.dispatch(frame([slot('a')], 11, 'boot-1'))
    expect(keys(s.getState())).toEqual(['a', 'b'])
  })
})

/** Retiring in order only ever covers an
 *  epoch that BECAME the baseline. An intermediate epoch B -- overtaken in flight by a
 *  C snapshot, so never adopted -- is therefore neither retired nor equal to the live
 *  epoch, and the "not comparable" branch accepted it. Accepting it rebased onto B and
 *  retired the LIVE epoch C, after which every real C frame was refused and membership
 *  froze until a reload. The read now carries the epoch it was ISSUED under. */
describe('an epoch this client never adopted cannot retire the one it did', () => {
  it('refuses a reply from an unadopted epoch and leaves the live epoch driving', () => {
    const s = store()
    s.dispatch(sseSlots({ slots: [slot('a')], generation: 1, epoch: 'A' }))
    expect(s.getState().dashboard.lastSlotsEpoch).toBe('A')

    // Leaves while A is live; its reply will be overtaken in flight.
    s.dispatch(fetchSlots.pending('r1', undefined))

    // C is adopted while that read travels. SAME membership deliberately: a move would
    // bump closeSeq and refuse the reply on the OTHER axis, making this control vacuous.
    s.dispatch(sseSlots({ slots: [slot('a')], generation: 1, epoch: 'C' }))
    expect(s.getState().dashboard.lastSlotsEpoch).toBe('C')

    // B never became the baseline, so it is in neither the retired set nor the live slot.
    s.dispatch(fetchSlots.fulfilled([slot('a'), slot('ghost')], 'r1', undefined, {
      appliedProvisional: false, generation: 9, epoch: 'B',
    }))
    expect(keys(s.getState())).toEqual(['a'])
    expect(s.getState().dashboard.lastSlotsEpoch).toBe('C')
    expect(s.getState().dashboard.retiredSlotsEpochs).not.toContain('C')

    // The consequence the fence is about: C must still drive the sidebar afterwards.
    s.dispatch(sseSlots({ slots: [slot('a'), slot('d')], generation: 2, epoch: 'C' }))
    expect(keys(s.getState())).toEqual(['a', 'd'])
  })

  it('still adopts a new epoch when the live one has not moved since the read was issued', () => {
    const s = store()
    s.dispatch(sseSlots({ slots: [slot('a')], generation: 7, epoch: 'A' }))
    s.dispatch(fetchSlots.pending('r1', undefined))
    /** Nothing was adopted in between, so this is a plain restart and must be recovery.
     *  Guards the opposite over-fix: making ANY differing epoch stale would strand the
     *  client on a dead epoch until a reload, which is what acceptance exists to avoid. */
    s.dispatch(fetchSlots.fulfilled([slot('z')], 'r1', undefined, {
      appliedProvisional: true, generation: 1, epoch: 'B',
    }))
    expect(keys(s.getState())).toEqual(['z'])
    expect(s.getState().dashboard.lastSlotsEpoch).toBe('B')
  })

  it('records the issue epoch on the read, so the rule has something to compare', () => {
    const s = store()
    s.dispatch(sseSlots({ slots: [slot('a')], generation: 1, epoch: 'A' }))
    s.dispatch(fetchSlots.pending('r9', undefined))
    expect(s.getState().dashboard.pendingSlotReads.r9).toEqual({ seq: 1, epoch: 'A' })
  })
})
