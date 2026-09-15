import { describe, it, expect } from 'vitest'
import reducer, { refreshSlot, sseChatMessage, warmSlotCache } from '../store/chatSlice'
import './mockApiClient'

/**
 * Replayed-chunk idempotency guard.
 *
 * WS delivery is at-least-once: a reconnect replay or a retry re-stream can
 * redeliver a streaming chunk the client already applied. missedChunkMarker
 * only flags FORWARD gaps (curSeq - prevSeq - 1 > 0), so a repeated/backward
 * seq produced NO marker and its content was appended a second time — the
 * silent mid-stream "stutter" (e.g. "So opus-4.8 ISSo opus-4.8 IS...").
 *
 * Both reducer chunk paths (active `sseChatMessage` and background
 * `applyNonActiveFrame`) drop a chunk whose seq <= the last-seen seq on the
 * non-batched path. A batched frame carries each chunk's seq in `parts`; the
 * reducer, the single owner of the replay floor (`lastChunkSeq`, raised by a
 * snapshot's trailing streaming row and by every applied chunk), keeps only
 * the parts above that floor and leaves the slot untouched when none survive.
 * The WS flush buffer (useWebSocket) only batches and guards against a
 * repeated WS delivery; it has no view of the snapshot floor.
 */

const SLOT = 'active-slot'
const OTHER = 'focused-slot'
const init = () => reducer(undefined, { type: '@@INIT' })

describe('replayed-chunk dedup (active path)', () => {
  it('does not double-append a chunk redelivered with the same seq', () => {
    let s = { ...init(), activeSlot: SLOT }
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'So opus-4.8 IS', seq: 5 }))
    // Same seq redelivered (reconnect replay / retry re-stream).
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'So opus-4.8 IS', seq: 5 }))

    const text = s.messages.map(m => m.content).join('')
    expect(text.match(/So opus-4.8 IS/g)?.length).toBe(1)
  })

  it('drops a backward-seq replay but keeps the forward chunk that followed', () => {
    let s = { ...init(), activeSlot: SLOT }
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'A', seq: 5 }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'B', seq: 6 }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'A', seq: 5 })) // stale replay

    expect(s.messages.map(m => m.content).join('')).toBe('AB')
  })

  it('still appends normal forward-progress chunks (guard does not over-drop)', () => {
    let s = { ...init(), activeSlot: SLOT }
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'a', seq: 1 }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'b', seq: 2 }))

    expect(s.messages.map(m => m.content).join('')).toBe('ab')
  })
})

describe('replayed-chunk dedup (background / applyNonActiveFrame path)', () => {
  it('does not double-append a redelivered chunk for a non-active slot', () => {
    let s = { ...init(), activeSlot: OTHER }
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'So opus-4.8 IS', seq: 5 }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'So opus-4.8 IS', seq: 5 }))

    const streamed = (s.slotMessages[SLOT] ?? []).map(m => m.content).join('')
    expect(streamed.match(/So opus-4.8 IS/g)?.length).toBe(1)
  })
})

/** A refresh snapshot for `key` whose trailing streaming row carries `seq`,
 *  the production path that raises a slot's replay floor. */
const snapshotWithFloor = (key: string, seq: number) => refreshSlot.fulfilled({
  key, running: true, hasMore: false, total: 1, queue: [], stopping: false,
  messages: [{ role: 'streaming', content: 'SNAP', cls: 'msg msg-a', seq }],
}, 'r1', key)

describe('batched frame vs snapshot floor (active path)', () => {
  it('a batched frame with every part at or below the floor is a no-op', () => {
    let s = { ...init(), activeSlot: SLOT }
    s = reducer(s, snapshotWithFloor(SLOT, 3))
    expect(s.lastChunkSeq).toBe(3)
    const before = s
    s = reducer(s, sseChatMessage({
      slot: SLOT, role: 'chunk', content: 'ab', seq: 3, batched: true,
      parts: [{ seq: 2, text: 'a' }, { seq: 3, text: 'b' }],
    }))
    expect(s).toBe(before)
    expect(s.messages.filter(m => m.role === 'streaming')).toHaveLength(1)
    expect(s.messages[0].content).toBe('SNAP')
    expect(s.toolLog).toEqual([])
  })

  it('a no-op batched frame leaves run state alone (no streaming bump, no epoch)', () => {
    let s = { ...init(), activeSlot: SLOT }
    s = reducer(s, snapshotWithFloor(SLOT, 4))
    const { slotState, runEpoch } = s
    expect(slotState).toBe('idle')
    s = reducer(s, sseChatMessage({
      slot: SLOT, role: 'chunk', content: 'x', seq: 4, batched: true, parts: [{ seq: 4, text: 'x' }],
    }))
    expect(s.messages.map(m => m.content)).toEqual(['SNAP'])
    expect(s.slotState).toBe(slotState)
    expect(s.runEpoch).toBe(runEpoch)
    expect(s._wsChunkedDuringFetch).toBe(false)
    expect(s.lastChunkSeq).toBe(4)
  })

  it('a partially covered batched frame appends only the uncovered parts and advances the floor', () => {
    let s = { ...init(), activeSlot: SLOT }
    s = reducer(s, snapshotWithFloor(SLOT, 2))
    s = reducer(s, sseChatMessage({
      slot: SLOT, role: 'chunk', content: 'abcd', seq: 4, batched: true,
      parts: [{ seq: 1, text: 'a' }, { seq: 2, text: 'b' }, { seq: 3, text: 'c' }, { seq: 4, text: 'd' }],
    }))
    const streaming = s.messages.filter(m => m.role === 'streaming')
    expect(streaming).toHaveLength(1)
    expect(streaming[0].content).toBe('SNAPcd')
    expect(streaming[0].rawText).toBe('SNAPcd')
    expect(s.lastChunkSeq).toBe(4)
    expect(s.toolLog.at(-1)).toMatchObject({ type: 'reasoning', text: 'cd' })
  })

  it('a part without a seq is kept even when others are covered', () => {
    let s = { ...init(), activeSlot: SLOT }
    s = reducer(s, snapshotWithFloor(SLOT, 2))
    s = reducer(s, sseChatMessage({
      slot: SLOT, role: 'chunk', content: 'a?', seq: 2, batched: true,
      parts: [{ seq: 2, text: 'a' }, { seq: undefined, text: '?' }],
    }))
    expect(s.messages.filter(m => m.role === 'streaming')[0].content).toBe('SNAP?')
  })

  it('a batched frame with no floor applies every part', () => {
    let s = { ...init(), activeSlot: SLOT }
    s = reducer(s, sseChatMessage({
      slot: SLOT, role: 'chunk', content: 'ab', seq: 2, batched: true,
      parts: [{ seq: 1, text: 'a' }, { seq: 2, text: 'b' }],
    }))
    expect(s.messages.filter(m => m.role === 'streaming')[0].content).toBe('ab')
    expect(s.lastChunkSeq).toBe(2)
  })
})

describe('batched frame vs snapshot floor (background / applyNonActiveFrame path)', () => {
  const bgSnapshot = (seq: number) => warmSlotCache.fulfilled({
    key: SLOT, running: true, hasMore: false, total: 1, queue: [], stopping: false,
    messages: [{ role: 'streaming', content: 'SNAP', cls: 'msg msg-a', seq }],
  }, 'w1', SLOT)

  it('a batched frame with every part at or below the floor is a no-op', () => {
    let s = { ...init(), activeSlot: OTHER }
    s = reducer(s, bgSnapshot(3))
    expect(s.slotRun[SLOT]?.lastChunkSeq).toBe(3)
    const before = s
    s = reducer(s, sseChatMessage({
      slot: SLOT, role: 'chunk', content: 'ab', seq: 3, batched: true,
      parts: [{ seq: 2, text: 'a' }, { seq: 3, text: 'b' }],
    }))
    // The slot's rows, run record and epoch are the snapshot's, untouched.
    expect(s.slotMessages[SLOT]).toBe(before.slotMessages[SLOT])
    expect(s.slotRun[SLOT]).toBe(before.slotRun[SLOT])
    expect(s.runEpoch).toBe(before.runEpoch)
    expect((s.slotMessages[SLOT] ?? []).map(m => m.content)).toEqual(['SNAP'])
    expect(s.slotActivity[SLOT]?.toolLog ?? []).toEqual([])
  })

  it('a partially covered batched frame appends only the uncovered parts and advances the floor', () => {
    let s = { ...init(), activeSlot: OTHER }
    s = reducer(s, bgSnapshot(2))
    s = reducer(s, sseChatMessage({
      slot: SLOT, role: 'chunk', content: 'abcd', seq: 4, batched: true,
      parts: [{ seq: 1, text: 'a' }, { seq: 2, text: 'b' }, { seq: 3, text: 'c' }, { seq: 4, text: 'd' }],
    }))
    const rows = s.slotMessages[SLOT] ?? []
    expect(rows.filter(m => m.role === 'streaming')).toHaveLength(1)
    expect(rows[0].content).toBe('SNAPcd')
    expect(s.slotRun[SLOT]?.lastChunkSeq).toBe(4)
    expect(s.slotRun[SLOT]?.state).toBe('streaming')
  })
})
