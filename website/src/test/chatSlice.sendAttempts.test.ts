/**
 * The per-slot record of submitted prompts that ↑ recall reads when a prompt's
 * bubble has left the transcript: what it keeps, how long, and when it goes.
 */
import { describe, it, expect } from 'vitest'
import chatReducer, { appendMessage, clearMessages, clearSlotCache, confirmOptimisticSend, hydrateSlotMessages, recordSendAttempt, replaceMessages, selectSendAttempts, sseChatMessage, switchSlot, truncateAfterIndex, warmSlotCache } from '../store/chatSlice'
import { promptHistoryWithAttempts } from '../components/composerPromptHistory'
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

  it('an edit-and-resend truncation forgets the prompts it removed and every older one', () => {
    let s = { ...init(), activeSlot: 'a' }
    s = chatReducer(s, appendMessage({ role: 'user', content: 'X', cls: '', meta: { sendId: 'sx' } }))
    s = chatReducer(s, appendMessage({ role: 'user', content: 'Y', cls: '', meta: { sendId: 'sy' } }))
    s = chatReducer(s, record('a', 'lost', 'sl'))
    s = chatReducer(s, record('a', 'X', 'sx'))
    s = chatReducer(s, record('a', 'Y', 'sy'))
    s = chatReducer(s, truncateAfterIndex(0))
    // The user moved past `lost` when they sent X, so ↑ must not offer it
    // ahead of the edited prompt — nor after a rollback restores X and Y.
    expect(selectSendAttempts(asRoot(s), 'a')).toEqual([])
    s = chatReducer(s, replaceMessages([
      { role: 'user', content: 'X', cls: '', meta: { sendId: 'sx' } },
      { role: 'user', content: 'Y', cls: '', meta: { sendId: 'sy' } },
    ]))
    expect(selectSendAttempts(asRoot(s), 'a')).toEqual([])
  })

  it('a truncation keeps a record newer than every prompt it removed', () => {
    let s = { ...init(), activeSlot: 'a' }
    s = chatReducer(s, appendMessage({ role: 'user', content: 'X', cls: '', meta: { sendId: 'sx' } }))
    s = chatReducer(s, appendMessage({ role: 'user', content: 'Y', cls: '', meta: { sendId: 'sy' } }))
    s = chatReducer(s, record('a', 'X', 'sx'))
    s = chatReducer(s, record('a', 'Y', 'sy'))
    s = chatReducer(s, record('a', 'later lost', 'sz'))
    s = chatReducer(s, truncateAfterIndex(1))
    expect(selectSendAttempts(asRoot(s), 'a')).toEqual([{ text: 'later lost', sendId: 'sz' }])
  })

  it('a truncation that removes no recorded prompt keeps the record', () => {
    let s = { ...init(), activeSlot: 'a' }
    s = chatReducer(s, appendMessage({ role: 'user', content: 'X', cls: '', meta: { sendId: 'sx' } }))
    s = chatReducer(s, appendMessage({ role: 'assistant', content: 'reply', cls: '' }))
    s = chatReducer(s, record('a', 'lost', 'sl'))
    s = chatReducer(s, record('a', 'X', 'sx'))
    s = chatReducer(s, truncateAfterIndex(1))
    expect(selectSendAttempts(asRoot(s), 'a')).toHaveLength(2)
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

describe('a delivered send leaves the record', () => {
  const user = (content: string, sendId: string, mid?: string) =>
    ({ role: 'user' as const, content, cls: '', meta: mid ? { sendId, mid } : { sendId, optimistic: true } })

  it('its receipt drops it and every older record, and keeps newer ones', () => {
    let s = { ...init(), activeSlot: 'a' }
    s = chatReducer(s, record('a', 'one', 's1'))
    s = chatReducer(s, record('a', 'two', 's2'))
    s = chatReducer(s, record('a', 'three', 's3'))
    s = chatReducer(s, record('b', 'other', 's9'))
    s = chatReducer(s, confirmOptimisticSend({ slot: 'a', sendId: 's2' }))
    expect(selectSendAttempts(asRoot(s), 'a')).toEqual([{ text: 'three', sendId: 's3' }])
    expect(selectSendAttempts(asRoot(s), 'b')).toHaveLength(1)
  })

  it('a receipt for a send with no record changes nothing', () => {
    let s = { ...init(), activeSlot: 'a' }
    s = chatReducer(s, record('a', 'one', 's1'))
    s = chatReducer(s, confirmOptimisticSend({ slot: 'a', sendId: 'other' }))
    expect(selectSendAttempts(asRoot(s), 'a')).toHaveLength(1)
  })

  it('a correlated echo drops it, on the active and a background slot', () => {
    let s = { ...init(), activeSlot: 'a' }
    s = chatReducer(s, record('a', 'one', 's1'))
    s = chatReducer(s, record('b', 'two', 's2'))
    s = chatReducer(s, appendMessage(user('one', 's1')))
    s = chatReducer(s, sseChatMessage({ slot: 'a', role: 'user', content: 'one', meta: { mid: 'm1', sendId: 's1' } }))
    s = chatReducer(s, sseChatMessage({ slot: 'b', role: 'user', content: 'two', meta: { mid: 'm2', sendId: 's2' } }))
    expect(selectSendAttempts(asRoot(s), 'a')).toEqual([])
    expect(selectSendAttempts(asRoot(s), 'b')).toEqual([])
  })

  it('a confirmed prompt does not return on ↑ after another tab rewinds its row away', () => {
    let s = { ...init(), activeSlot: 'a' }
    s = chatReducer(s, record('a', 'original', 's1'))
    s = chatReducer(s, appendMessage(user('original', 's1')))
    s = chatReducer(s, confirmOptimisticSend({ slot: 'a', sendId: 's1' }))
    // Tab B's edit-and-resend replaced the row; this tab's refresh loads it
    // without the original send's id.
    const replaced = [{ role: 'user' as const, content: 'edited', cls: '', meta: { mid: 'm9' } }]
    s = chatReducer(s, replaceMessages(replaced))
    const history = promptHistoryWithAttempts(replaced, selectSendAttempts(asRoot(s), 'a'))
    expect(history.map(h => h.text)).toEqual(['edited'])
  })

  it('a merged queued echo drops every send it names in sendIds', () => {
    let s = { ...init(), activeSlot: 'a' }
    s = chatReducer(s, record('a', 'A', 'sA'))
    s = chatReducer(s, sseChatMessage({ slot: 'a', role: 'user', content: 'A\n\nB', meta: { mid: 'm1', sendId: 'sB', sendIds: ['sA', 'sB'] } }))
    expect(selectSendAttempts(asRoot(s), 'a')).toEqual([])
  })

  it('a merged queued row on screen keeps its sends off the lost list', () => {
    const rows = [{ role: 'user' as const, content: 'A\n\nB', cls: '', meta: { mid: 'm1', sendId: 'sB', sendIds: ['sA', 'sB'] } }]
    const history = promptHistoryWithAttempts(rows, [{ text: 'A', sendId: 'sA' }])
    expect(history.map(h => h.text)).toEqual(['A\n\nB'])
  })

  // Every loader that accepts server history prunes, not only refreshSlot: a
  // send whose receipt and echo were both missed is proven by its persisted row.
  const persisted = [{ role: 'user' as const, content: 'one', cls: '', meta: { mid: 'm1', sendId: 's1' } }]
  const page = { messages: persisted, running: false, hasMore: false, total: 1, queue: [] }

  it('a slot switch that loads the row drops the record', () => {
    let s = { ...init(), activeSlot: 'a' }
    s = chatReducer(s, record('a', 'one', 's1'))
    s = chatReducer(s, { type: switchSlot.fulfilled.type, payload: { key: 'a', ...page }, meta: { requestId: 'r1' } })
    expect(selectSendAttempts(asRoot(s), 'a')).toEqual([])
  })

  it('a background warm that loads the row drops the record', () => {
    let s = { ...init(), activeSlot: 'b' }
    s = chatReducer(s, record('a', 'one', 's1'))
    s = chatReducer(s, { type: warmSlotCache.fulfilled.type, payload: { key: 'a', ...page } })
    expect(selectSendAttempts(asRoot(s), 'a')).toEqual([])
  })

  it('a background hydrate that carries the row drops the record', () => {
    let s = { ...init(), activeSlot: 'b' }
    s = chatReducer(s, record('a', 'one', 's1'))
    s = chatReducer(s, hydrateSlotMessages({ slot: 'a', messages: persisted }))
    expect(selectSendAttempts(asRoot(s), 'a')).toEqual([])
  })

  it('an unconfirmed send whose row vanished is still offered', () => {
    let s = { ...init(), activeSlot: 'a' }
    s = chatReducer(s, record('a', 'lost', 's1'))
    const history = promptHistoryWithAttempts([], selectSendAttempts(asRoot(s), 'a'))
    expect(history.map(h => h.text)).toEqual(['lost'])
  })
})
