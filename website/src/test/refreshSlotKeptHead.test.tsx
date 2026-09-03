/**
 * refreshSlot keeps the paged-in older head — the fix for "the archive is
 * unreachable while the agent is working".
 *
 * The no-limit refresh corpus starts at the rotation boundary: rows behind it
 * (the size-rotated archive head) exist only in the client, paged in one page
 * at a time. `chat_done` fires refreshSlot after EVERY agent turn, and a WS
 * reconnect (phone lock/unlock) fires it too — so on an active session the
 * old wholesale replacement discarded the archived rows the reader had just
 * scrolled to and reset their cursor to the boundary, over and over.
 * switchSlot and warmSlotCache already keep the head via olderHeadAbovePage;
 * this pins refreshSlot to the same contract.
 */
import { describe, it, expect } from 'vitest'

import reducer, { refreshSlot, setActiveSlot } from '../store/chatSlice'
import type { ChatMessage } from '../pages/chat/types'

const msg = (ts: string, content: string): ChatMessage => ({
  role: 'assistant', content, ts, cls: 'msg msg-a', meta: { mid: `mid-${ts}` },
})

function stateWith(messages: ChatMessage[], slot = 's1') {
  let s = reducer(undefined, { type: '@@init' })
  s = reducer(s, setActiveSlot(slot))
  return { ...s, messages, slotHasMore: true, slotOldestIndex: 178, slotCursorKey: slot }
}

describe('refreshSlot.fulfilled keeps the paged-in older head', () => {
  it('rows above the refresh corpus survive, and the cursor stays behind them', () => {
    // The reader paged two archive rows in (ts 001/002); the refresh corpus
    // starts at the boundary row (ts 100).
    const archived = [msg('2026-01-01T00:00:01Z', 'archived one'), msg('2026-01-01T00:00:02Z', 'archived two')]
    const live = [msg('2026-01-01T01:00:00Z', 'boundary row'), msg('2026-01-01T01:00:01Z', 'tail row')]
    const before = stateWith([...archived, ...live])
    const after = reducer(before, {
      type: refreshSlot.fulfilled.type,
      payload: {
        key: 's1', messages: live, queue: [], running: false, stopping: false,
        hasMore: true, nextBefore: 278, total: 1280,
      },
    })
    const contents = after.messages.map(m => m.content)
    expect(contents).toEqual(['archived one', 'archived two', 'boundary row', 'tail row'])
    // Cursor shifted DOWN by the kept head's row count, so the next "load
    // earlier" continues from where the reader actually is — not a dead
    // click back at the boundary.
    expect(after.slotHasMore).toBe(true)
    expect(after.slotOldestIndex).toBe(276)
  })

  it('a refresh with no paged-in head behaves as before', () => {
    const live = [msg('2026-01-01T01:00:00Z', 'boundary row'), msg('2026-01-01T01:00:01Z', 'tail row')]
    const before = stateWith(live)
    const after = reducer(before, {
      type: refreshSlot.fulfilled.type,
      payload: {
        key: 's1', messages: live, queue: [], running: false, stopping: false,
        hasMore: true, nextBefore: 278, total: 1280,
      },
    })
    expect(after.messages.map(m => m.content)).toEqual(['boundary row', 'tail row'])
    expect(after.slotOldestIndex).toBe(278)
  })
})
