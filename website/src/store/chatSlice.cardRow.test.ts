import { describe, it, expect } from 'vitest'
import reducer, { sseChatMessage, sseChatMessagePatchByTs } from './chatSlice'
import '../test/mockApiClient'

/**
 * A change card / guide offer arrives as a `card` row in the middle of a turn,
 * right after the tool call that proposed it. It is placed like that tool row
 * (ahead of the still-open streaming message), in the active slot and in a
 * background slot alike, and a later status patch updates the SAME row.
 */

const SLOT = 'chat-card-frame'
const OTHER = 'chat-card-other'
const init = () => ({ ...reducer(undefined, { type: '@@INIT' }), activeSlot: SLOT })

const cardFrame = (slot: string, mid: string) => ({
  slot,
  role: 'card',
  content: 'Shorter replies',
  cls: 'msg msg-card',
  ts: '2026-10-03T10:00:01Z',
  meta: { mid, card: { surface: 'change', id: 'cc_1', slot, kind: 'setting.change', title: 'Shorter replies', status: 'pending' } },
})

describe('a card row frame', () => {
  it('lands ahead of the open streaming message in the active slot', () => {
    let s = init()
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'I will propose it.', seq: 1 }))
    s = reducer(s, sseChatMessage(cardFrame(SLOT, 'm-card')))
    expect(s.messages.map(m => m.role)).toEqual(['card', 'streaming'])
    // The open text row is still the one later chunks extend.
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: ' Done.', seq: 2 }))
    expect(s.messages.map(m => m.role)).toEqual(['card', 'streaming'])
    expect(s.messages[1].content).toBe('I will propose it. Done.')
  })

  it('lands ahead of the open streaming message in a background slot', () => {
    let s = init()
    s = reducer(s, sseChatMessage({ slot: OTHER, role: 'chunk', content: 'text', seq: 1 }))
    s = reducer(s, sseChatMessage(cardFrame(OTHER, 'm-bg')))
    expect(s.slotMessages[OTHER].map(m => m.role)).toEqual(['card', 'streaming'])
  })

  it('is not appended twice when the same row is redelivered', () => {
    let s = init()
    s = reducer(s, sseChatMessage(cardFrame(SLOT, 'm-dup')))
    s = reducer(s, sseChatMessage(cardFrame(SLOT, 'm-dup')))
    expect(s.messages.filter(m => m.role === 'card')).toHaveLength(1)
  })

  it('takes a status patch in place, by its row id', () => {
    let s = init()
    s = reducer(s, sseChatMessage(cardFrame(SLOT, 'm-patch')))
    const card = { surface: 'change', id: 'cc_1', slot: SLOT, kind: 'setting.change', title: 'Shorter replies', status: 'applied' }
    s = reducer(s, sseChatMessagePatchByTs({ slot: SLOT, ts: '2026-10-03T10:00:01Z', mid: 'm-patch', meta: { mid: 'm-patch', card } }))
    expect(s.messages).toHaveLength(1)
    expect((s.messages[0].meta?.card as { status: string }).status).toBe('applied')
  })
})
