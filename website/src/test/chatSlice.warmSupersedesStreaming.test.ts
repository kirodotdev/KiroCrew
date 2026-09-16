/** A warm's page supersedes the pane's client-minted `streaming` row.
 *
 *  The first chunk of a background reply mints a `streaming` row with no
 *  identity (no `mid` until the final assistant frame lands). The warm merge
 *  rescues identity-less prior rows past the anchor as "newer than the page" —
 *  right for a row the page has not caught up to, wrong for the one row the
 *  page already carries in full: the reply itself, still streaming or final.
 *  Keeping it renders the reply twice. The reconnect warm hits this every time
 *  a socket drops mid-reply: the page's streaming row folds every chunk so far,
 *  and the client's copy holds only the chunks it saw before the drop.
 */

import { describe, expect, it } from 'vitest'
import chatReducer, { warmSlotCache } from '../store/chatSlice'
import type { ChatMessage } from '../types'

const SLOT = 'member-oncall'
const init = () => chatReducer(undefined, { type: '@@INIT' })
const row = (role: string, content: string, mid?: string): ChatMessage =>
  ({ role, content, cls: '', ts: '2026-01-01T00:00:00Z', ...(mid ? { meta: { mid } } : {}) })
const streamingRow = (content: string): ChatMessage =>
  ({ role: 'streaming', content, cls: 'msg msg-a', rawText: content, meta: { clientTs: 'c1' } })

const warm = (messages: ChatMessage[], running: boolean, warmSeq = 1) => ({
  type: warmSlotCache.fulfilled.type,
  payload: { key: SLOT, messages, queue: [], hasMore: false, total: messages.length, running, warmSeq, boundedRead: false },
})

const seed = (prior: ChatMessage[], runState: 'streaming' | 'idle' = 'streaming') => ({
  ...init(),
  activeSlot: 'chat-active',
  slotMessages: { [SLOT]: prior },
  slotRun: { [SLOT]: { state: runState, lastChunkSeq: 5 } },
})

describe('warmSlotCache.fulfilled supersedes the client streaming row', () => {
  it('mid-turn: the page streaming row replaces the partial client copy (no double text)', () => {
    const prior = [row('user', 'go', 'u1'), streamingRow('c0 c1 c2 c3 c4 c5 ')]
    const page = [row('user', 'go', 'u1'), { ...streamingRow('c0 c1 c2 c3 c4 c5 c6 c7 c8 c9 '), seq: 9 } as ChatMessage]
    const next = chatReducer(seed(prior), warm(page, true))
    const rows = next.slotMessages[SLOT]
    expect(rows.map(m => m.role)).toEqual(['user', 'streaming'])
    expect(rows[1].content).toBe('c0 c1 c2 c3 c4 c5 c6 c7 c8 c9 ')
    // The replay floor is raised to the page's newest chunk so a racing live
    // chunk at or below it is not applied a second time.
    expect(next.slotRun[SLOT].lastChunkSeq).toBe(9)
    expect(next.slotRun[SLOT].state).toBe('streaming')
  })

  it('turn ended offline: the final assistant row replaces the stale client copy', () => {
    const prior = [row('user', 'go', 'u1'), streamingRow('c0 c1 c2 ')]
    const page = [row('user', 'go', 'u1'), row('assistant', 'c0 c1 c2 c3 c4 c5 c6 c7 c8 c9 ', 'a1')]
    const next = chatReducer(seed(prior), warm(page, false))
    const rows = next.slotMessages[SLOT]
    expect(rows.map(m => m.role)).toEqual(['user', 'assistant'])
    expect(rows.some(m => m.role === 'streaming')).toBe(false)
    expect(next.slotRun[SLOT].state).toBe('idle')
  })

  it('turn ended offline, slot already idled by the slots snapshot: the client-finalized copy goes too', () => {
    // The reconnect fetchSlots can settle the pane before the warm fulfils:
    // syncSlotRunningFromServer finalizes the streaming row to `assistant`
    // with no mid. It is the same reply as the page's final row.
    const finalized: ChatMessage = { ...streamingRow('c0 c1 c2 '), role: 'assistant' }
    const prior = [row('user', 'go', 'u1'), finalized]
    const page = [row('user', 'go', 'u1'), row('assistant', 'c0 c1 c2 c3 c4 c5 c6 c7 c8 c9 ', 'a1')]
    const next = chatReducer(seed(prior, 'idle'), warm(page, false))
    const rows = next.slotMessages[SLOT]
    expect(rows.map(m => m.meta?.mid ?? m.role)).toEqual(['u1', 'a1'])
  })

  it('keeps the client copy when the page has not caught up to the reply yet', () => {
    // The server may not have flushed the first chunk when the page was cut:
    // no reply row past the anchor means the client row is the only copy —
    // decline, not guess.
    const prior = [row('user', 'go', 'u1'), streamingRow('c0 ')]
    const page = [row('user', 'go', 'u1')]
    const next = chatReducer(seed(prior), warm(page, true))
    expect(next.slotMessages[SLOT].map(m => m.role)).toEqual(['user', 'streaming'])
    expect(next.slotMessages[SLOT][1].content).toBe('c0 ')
  })

  it('still rescues identity-bearing rows the page missed', () => {
    // A tool row that landed live after the page was cut has a mid the page
    // lacks: that is a real newer row and survives alongside the superseded
    // streaming copy being dropped.
    const prior = [row('user', 'go', 'u1'), streamingRow('c0 '), row('tool', '🔧 later', 't9')]
    const page = [row('user', 'go', 'u1'), { ...streamingRow('c0 c1 c2 c3 c4 c5 c6 '), seq: 6 } as ChatMessage]
    const next = chatReducer(seed(prior), warm(page, true))
    const rows = next.slotMessages[SLOT]
    expect(rows.map(m => m.meta?.mid ?? m.role)).toEqual(['u1', 'streaming', 't9'])
    expect(rows.filter(m => m.role === 'streaming')).toHaveLength(1)
  })

  it('keeps the client copy when it is NEWER than the page (a chunk raced the fetch)', () => {
    // Client floor 12 > page seq 9: the page cannot vouch for chunks 10..12, so
    // dropping the copy would lose them until the end-of-turn warm. Keep it
    // (the pre-existing behavior) and leave the floor where the live frames put it.
    const prior = [row('user', 'go', 'u1'), streamingRow('c0 c1 c2 c3 c4 c5 c10 c11 c12 ')]
    const page = [row('user', 'go', 'u1'), { ...streamingRow('c0 c1 c2 c3 c4 c5 c6 c7 c8 c9 '), seq: 9 } as ChatMessage]
    const state = { ...seed(prior), slotRun: { [SLOT]: { state: 'streaming' as const, lastChunkSeq: 12 } } }
    const next = chatReducer(state, warm(page, true))
    const rows = next.slotMessages[SLOT]
    expect(rows.filter(m => m.role === 'streaming').map(m => m.content)).toContain('c0 c1 c2 c3 c4 c5 c10 c11 c12 ')
    expect(next.slotRun[SLOT].lastChunkSeq).toBe(12)
  })

  it('keeps the client copy when the page streaming row carries no seq (older gateway)', () => {
    const prior = [row('user', 'go', 'u1'), streamingRow('c0 c1 ')]
    const page = [row('user', 'go', 'u1'), streamingRow('c0 c1 c2 c3 ')]
    const next = chatReducer(seed(prior), warm(page, true))
    expect(next.slotMessages[SLOT].filter(m => m.role === 'streaming')).toHaveLength(2)
  })

  it('keeps a client-finalized copy when the page still says streaming (stale page)', () => {
    // fetchSlots idled the slot (turn over, _done missed) but this page was cut
    // before the end: it is older than the client's copy and no end-of-turn
    // warm will follow. Dropping the copy here would lose the reply for good.
    const finalized: ChatMessage = { ...streamingRow('c0 c1 c2 c3 c4 c5 c6 c7 c8 c9 '), role: 'assistant' }
    const prior = [row('user', 'go', 'u1'), finalized]
    const page = [row('user', 'go', 'u1'), { ...streamingRow('c0 c1 c2 '), seq: 2 } as ChatMessage]
    const next = chatReducer(seed(prior, 'idle'), warm(page, true))
    expect(next.slotMessages[SLOT].some(m => m.role === 'assistant' && m.content.includes('c9'))).toBe(true)
  })

  it('never touches the next turn: a send that landed mid-fetch keeps its live stream', () => {
    // Snapshot for turn A (final), fulfilled after turn B already started
    // streaming. A's stale copy is superseded by A's final row; B's user row
    // and B's identity-less streaming row belong to a turn the page predates
    // and stay whole.
    const copyA: ChatMessage = { ...streamingRow('a0 a1 '), role: 'assistant' }
    const prior = [row('user', 'go A', 'uA'), copyA, row('user', 'go B', 'uB'), streamingRow('b0 b1 b2 ')]
    const page = [row('user', 'go A', 'uA'), row('assistant', 'a0 a1 a2 a3 ', 'aA')]
    const next = chatReducer(seed(prior), warm(page, false))
    const rows = next.slotMessages[SLOT]
    expect(rows.map(m => m.meta?.mid ?? `${m.role}:${m.content}`)).toEqual(['uA', 'aA', 'uB', 'streaming:b0 b1 b2 '])
  })

  it('an optimistic user row (sendId only) is a turn boundary too', () => {
    const prior = [row('user', 'go A', 'uA'), streamingRow('a0 '), { role: 'user', content: 'go B', cls: '', meta: { sendId: 's-b' } } as ChatMessage, streamingRow('b0 ')]
    const page = [row('user', 'go A', 'uA'), row('assistant', 'a0 a1 ', 'aA')]
    const next = chatReducer(seed(prior), warm(page, false))
    const rows = next.slotMessages[SLOT]
    expect(rows.map(m => m.meta?.mid ?? m.meta?.sendId ?? `${m.role}:${m.content}`)).toEqual(['uA', 'aA', 's-b', 'streaming:b0 '])
  })

  it('leaves the active slot alone (switchSlot owns its messages)', () => {
    const state = { ...seed([row('user', 'go', 'u1'), streamingRow('c0 ')]), activeSlot: SLOT }
    const next = chatReducer(state, warm([row('user', 'go', 'u1'), row('assistant', 'full', 'a1')], false))
    expect(next.slotMessages[SLOT].map(m => m.role)).toEqual(['user', 'streaming'])
  })
})
