import { describe, it, expect } from 'vitest'
import reducer, { recordSendAttempt, clearMessages, clearSlotCache, setActiveSlot } from '../store/chatSlice'
import { sseSlots } from '../store/dashboardSlice'
import './mockApiClient'

/**
 * Attempt-time prompt recall (`attemptedSends`).
 *
 * ↑/↓ recall was derived purely from `messages`, so it could only offer prompts
 * that reached the transcript. Every way a send is lost also erases the recall
 * entry, and the composer is cleared before any of them can be known — leaving
 * the user's own text nowhere in the UI. These pin the store half: recording is
 * keyed on SUBMISSION, bounded per slot, and dies with its slot.
 */

const SLOT = 'chat-1-1700000000'
const OTHER = 'chat-2-1700000001'
const init = () => reducer(undefined, { type: '@@INIT' })

describe('recordSendAttempt', () => {
  it('retains a submitted prompt under its slot', () => {
    const s = reducer(init(), recordSendAttempt({ slot: SLOT, text: 'first' }))
    expect(s.attemptedSends[SLOT]).toEqual(['first'])
  })

  it('keeps order oldest to newest, matching the transcript half', () => {
    let s = init()
    for (const text of ['one', 'two', 'three']) s = reducer(s, recordSendAttempt({ slot: SLOT, text }))
    expect(s.attemptedSends[SLOT]).toEqual(['one', 'two', 'three'])
  })

  it('collapses a consecutive duplicate', () => {
    let s = reducer(init(), recordSendAttempt({ slot: SLOT, text: 'same' }))
    s = reducer(s, recordSendAttempt({ slot: SLOT, text: 'same' }))
    expect(s.attemptedSends[SLOT]).toEqual(['same'])
  })

  it('retains a repeat that is not consecutive, as a shell does', () => {
    let s = init()
    for (const text of ['a', 'b', 'a']) s = reducer(s, recordSendAttempt({ slot: SLOT, text }))
    expect(s.attemptedSends[SLOT]).toEqual(['a', 'b', 'a'])
  })

  it('does not let one slot see another slot prompts', () => {
    let s = reducer(init(), recordSendAttempt({ slot: SLOT, text: 'mine' }))
    s = reducer(s, recordSendAttempt({ slot: OTHER, text: 'theirs' }))
    expect(s.attemptedSends[SLOT]).toEqual(['mine'])
    expect(s.attemptedSends[OTHER]).toEqual(['theirs'])
  })

  it('bounds retention and drops the oldest first', () => {
    let s = init()
    for (let i = 0; i < 60; i++) s = reducer(s, recordSendAttempt({ slot: SLOT, text: `p${i}` }))
    const list = s.attemptedSends[SLOT]
    expect(list).toHaveLength(50)
    expect(list[0]).toBe('p10')
    expect(list[list.length - 1]).toBe('p59')
  })

  it('ignores an empty prompt and an absent slot', () => {
    let s = reducer(init(), recordSendAttempt({ slot: SLOT, text: '' }))
    s = reducer(s, recordSendAttempt({ slot: '', text: 'orphan' }))
    expect(s.attemptedSends).toEqual({})
  })

  it('refuses a prototype-polluting slot key', () => {
    const s = reducer(init(), recordSendAttempt({ slot: '__proto__', text: 'hostile' }))
    expect(s.attemptedSends).toEqual({})
    expect(({} as Record<string, unknown>).hostile).toBeUndefined()
  })

  it('drops a slot entry when an authoritative list no longer carries the slot', () => {
    let s = reducer(init(), recordSendAttempt({ slot: SLOT, text: 'stale' }))
    expect(s.attemptedSends[SLOT]).toEqual(['stale'])
    s = reducer(s, sseSlots([{ key: OTHER }] as never))
    expect(s.attemptedSends[SLOT]).toBeUndefined()
  })
})

/**
 * A cleared conversation must not come back through recall.
 *
 * `/clear` empties the transcript but does NOT delete the slot, so it never
 * reaches the `slotKeyedMaps` eviction that retires a slot's state. Recall
 * reads attempts even when `messages` is empty, so an attempt surviving the
 * clear would put discarded text back in the composer on the next ↑. Both
 * clear reducers evict it, exactly as they already evict `thinkingOrphans`.
 */
describe('clearing a conversation retires its recorded attempts', () => {
  it('clearMessages drops the attempts of the slot being viewed', () => {
    let s = reducer(init(), setActiveSlot(SLOT))
    s = reducer(s, recordSendAttempt({ slot: SLOT, text: 'discarded' }))
    expect(s.attemptedSends[SLOT]).toEqual(['discarded'])
    s = reducer(s, clearMessages())
    expect(s.attemptedSends[SLOT]).toBeUndefined()
  })

  it('clearMessages leaves an unrelated slot recoverable', () => {
    let s = reducer(init(), setActiveSlot(SLOT))
    s = reducer(s, recordSendAttempt({ slot: SLOT, text: 'discarded' }))
    s = reducer(s, recordSendAttempt({ slot: OTHER, text: 'untouched' }))
    s = reducer(s, clearMessages())
    expect(s.attemptedSends[SLOT]).toBeUndefined()
    expect(s.attemptedSends[OTHER]).toEqual(['untouched'])
  })

  it('clearSlotCache drops the attempts of a slot cleared in the background', () => {
    let s = reducer(init(), setActiveSlot(SLOT))
    s = reducer(s, recordSendAttempt({ slot: OTHER, text: 'discarded' }))
    s = reducer(s, clearSlotCache(OTHER))
    expect(s.attemptedSends[OTHER]).toBeUndefined()
  })

  it('clearSlotCache leaves the viewed slot recoverable', () => {
    let s = reducer(init(), setActiveSlot(SLOT))
    s = reducer(s, recordSendAttempt({ slot: SLOT, text: 'untouched' }))
    s = reducer(s, recordSendAttempt({ slot: OTHER, text: 'discarded' }))
    s = reducer(s, clearSlotCache(OTHER))
    expect(s.attemptedSends[SLOT]).toEqual(['untouched'])
    expect(s.attemptedSends[OTHER]).toBeUndefined()
  })
})
