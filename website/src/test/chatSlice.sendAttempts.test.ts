/**
 * The per-slot record of submitted prompts that ↑ recall reads when a prompt's
 * bubble has left the transcript: what it keeps, how long, and when it goes.
 */
import { describe, it, expect } from 'vitest'
import chatReducer, { appendMessage, clearMessages, clearSlotCache, recordSendAttempt, selectSendAttempts, truncateAfterIndex } from '../store/chatSlice'
import { sseSlots } from '../store/dashboardSlice'
import type { RootState } from '../store'
import './mockApiClient'

/** The slice's per-slot retention bound (`SEND_ATTEMPTS_PER_SLOT_MAX`). */
const SEND_ATTEMPTS_PER_SLOT_MAX = 50
const init = () => chatReducer(undefined, { type: '@@INIT' })
const record = (slot: string, text: string, sendId: string) => recordSendAttempt({ slot, text, sendId })
const asRoot = (chat: ReturnType<typeof init>) => ({ chat }) as unknown as RootState

describe('recordSendAttempt', () => {
  it('keeps each slot’s prompts apart, oldest first', () => {
    let s = init()
    s = chatReducer(s, record('a', 'one', 's1'))
    s = chatReducer(s, record('b', 'other', 's2'))
    s = chatReducer(s, record('a', 'two', 's3'))
    expect(selectSendAttempts(asRoot(s), 'a')).toEqual([{ text: 'one', sendId: 's1' }, { text: 'two', sendId: 's3' }])
    expect(selectSendAttempts(asRoot(s), 'b')).toEqual([{ text: 'other', sendId: 's2' }])
  })

  it('keeps only the newest entries past the per-slot cap', () => {
    let s = init()
    for (let i = 0; i < SEND_ATTEMPTS_PER_SLOT_MAX + 5; i++) s = chatReducer(s, record('a', `p${i}`, `s${i}`))
    const kept = selectSendAttempts(asRoot(s), 'a')
    expect(kept).toHaveLength(SEND_ATTEMPTS_PER_SLOT_MAX)
    expect(kept[0].text).toBe('p5')
    expect(kept[kept.length - 1].text).toBe(`p${SEND_ATTEMPTS_PER_SLOT_MAX + 4}`)
  })

  it('ignores an empty prompt, a missing id, and a key that would reach Object.prototype', () => {
    let s = init()
    s = chatReducer(s, record('a', '', 's1'))
    s = chatReducer(s, record('a', 'x', ''))
    s = chatReducer(s, record('__proto__', 'x', 's2'))
    expect(s.sendAttempts ?? {}).toEqual({})
    expect(Object.prototype).not.toHaveProperty('push')
  })

  it('answers a stable empty list for a slot with no record', () => {
    const s = init()
    expect(selectSendAttempts(asRoot(s), 'a')).toBe(selectSendAttempts(asRoot(s), 'b'))
    expect(selectSendAttempts(asRoot(s), null)).toEqual([])
  })
})

describe('a slot’s record ends with the slot', () => {
  it('a clear of the active conversation forgets its prompts only', () => {
    let s = { ...init(), activeSlot: 'a' }
    s = chatReducer(s, record('a', 'one', 's1'))
    s = chatReducer(s, record('b', 'two', 's2'))
    s = chatReducer(s, clearMessages())
    expect(selectSendAttempts(asRoot(s), 'a')).toEqual([])
    expect(selectSendAttempts(asRoot(s), 'b')).toHaveLength(1)
  })

  it('an edit-and-resend truncation forgets the prompts it removed, and keeps the rest', () => {
    let s = { ...init(), activeSlot: 'a' }
    s = chatReducer(s, appendMessage({ role: 'user', content: 'X', cls: '', meta: { sendId: 'sx' } }))
    s = chatReducer(s, appendMessage({ role: 'user', content: 'Y', cls: '', meta: { sendId: 'sy' } }))
    s = chatReducer(s, record('a', 'lost', 'sl'))
    s = chatReducer(s, record('a', 'X', 'sx'))
    s = chatReducer(s, record('a', 'Y', 'sy'))
    s = chatReducer(s, truncateAfterIndex(0))
    expect(selectSendAttempts(asRoot(s), 'a')).toEqual([{ text: 'lost', sendId: 'sl' }])
  })

  it('a confirmed clear of a background slot forgets its prompts', () => {
    let s = chatReducer(init(), record('b', 'two', 's2'))
    s = chatReducer(s, clearSlotCache('b'))
    expect(selectSendAttempts(asRoot(s), 'b')).toEqual([])
  })

  it('a slot gone from the authoritative list takes its record with it', () => {
    let s = { ...init(), activeSlot: 'a' }
    s = chatReducer(s, record('a', 'one', 's1'))
    s = chatReducer(s, record('gone', 'two', 's2'))
    s = chatReducer(s, sseSlots([{ key: 'a', messages: 0, running: false }]))
    expect(selectSendAttempts(asRoot(s), 'a')).toHaveLength(1)
    expect(s.sendAttempts?.gone).toBeUndefined()
  })
})
