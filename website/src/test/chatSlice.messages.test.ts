/**
 * chatSlice: how a delivered message keeps changing -- the send-receipt marks,
 * _segment handling, patches by timestamp, context usage, side-conversation
 * results, model reasoning chunks, and how a batched streaming chunk skips the
 * missed-chunk marker. The first delivery of a chat message frame is in
 * chatSlice.test.ts.
 */
import { describe, it, expect } from 'vitest'
import type { ChatMessage } from '../types'
import type { RootState } from '../store'
import reducer, {
  setActiveSlot,
  appendMessage,
  appendSlotMessage,
  sseChatMessage,
  sseThinkingChunk,
  refreshSlot,
  sseContextUsage,
  sseSideResult,
  sideClose,
  confirmOptimisticSend,
  markSendUnconfirmed,
  selectTrailingSendUnconfirmed,
} from '../store/chatSlice'
import './mockApiClient'

/* #4131: the ONLY confirmation the dashboard composer ever receives is its own
 * HTTP response. `DashboardState.append` suppresses the `chat_message` user echo
 * for every dashboard send by design (`broadcast_user=False`, because the
 * composer already rendered the bubble), so a surface that waits for the echo
 * waits forever — which is why this reducer exists and why the 30s wall-clock
 * sweep it replaced flagged every message the user sent, not just lost ones. */
describe('confirmOptimisticSend — the send response retires the pending state', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const withSlot = { ...initial, activeSlot: 'slot-1' }

  it('retires the pending flag on the matching send and KEEPS its sendId', () => {
    let state = reducer(withSlot, appendMessage({
      role: 'user', content: 'ship it', cls: '', ts: '2026-08-16T10:00:00.000Z',
      meta: { sendId: 's-confirm-1' },
    }))
    expect(state.messages[0].meta?.optimistic).toBe(true)

    state = reducer(state, confirmOptimisticSend({ slot: 'slot-1', sendId: 's-confirm-1' }))

    expect(state.messages[0].meta?.optimistic).toBeUndefined()
    // sendId SURVIVES: a channel-linked slot can still deliver a late echo, and
    // reconcileOptimisticEcho needs the id to update this row in place rather
    // than push a duplicate bubble.
    expect(state.messages[0].meta?.sendId).toBe('s-confirm-1')
  })

  it('stamps the receipt mid on the confirmed bubble so it becomes pinnable this turn', () => {
    let state = reducer(withSlot, appendMessage({
      role: 'user', content: 'ship it', cls: '', ts: '2026-08-16T10:00:00.000Z',
      meta: { sendId: 's-mid-1' },
    }))
    // The optimistic bubble is born with NO mid (the pin control is gated on it).
    expect(state.messages[0].meta?.mid).toBeUndefined()

    state = reducer(state, confirmOptimisticSend({ slot: 'slot-1', sendId: 's-mid-1', mid: 'm-server-42' }))

    expect(state.messages[0].meta?.mid).toBe('m-server-42')
    expect(state.messages[0].meta?.optimistic).toBeUndefined()
  })

  it('leaves the bubble without a mid when the receipt carried none (queued/steer send)', () => {
    let state = reducer(withSlot, appendMessage({
      role: 'user', content: 'ship it', cls: '', ts: '2026-08-16T10:00:00.000Z',
      meta: { sendId: 's-nomid' },
    }))

    state = reducer(state, confirmOptimisticSend({ slot: 'slot-1', sendId: 's-nomid' }))

    expect(state.messages[0].meta?.mid).toBeUndefined()
    expect(state.messages[0].meta?.optimistic).toBeUndefined()
  })

  it('never overwrites a mid a refresh already reconciled (identity is stable once assigned)', () => {
    let state = reducer(withSlot, appendMessage({
      role: 'user', content: 'ship it', cls: '', ts: '2026-08-16T10:00:00.000Z',
      meta: { sendId: 's-existing', mid: 'm-already-here' },
    }))

    state = reducer(state, confirmOptimisticSend({ slot: 'slot-1', sendId: 's-existing', mid: 'm-late-different' }))

    expect(state.messages[0].meta?.mid).toBe('m-already-here')
    expect(state.messages[0].meta?.optimistic).toBeUndefined()
  })

  it('confirms only the matching send, leaving a sibling in-flight bubble pending', () => {
    let state = reducer(withSlot, appendMessage({ role: 'user', content: 'first', cls: '', ts: '2026-08-16T10:00:00.000Z', meta: { sendId: 's-a' } }))
    state = reducer(state, appendMessage({ role: 'user', content: 'second', cls: '', ts: '2026-08-16T10:00:01.000Z', meta: { sendId: 's-b' } }))

    state = reducer(state, confirmOptimisticSend({ slot: 'slot-1', sendId: 's-b' }))

    expect(state.messages[0].meta?.optimistic).toBe(true)   // 's-a' still in flight
    expect(state.messages[1].meta?.optimistic).toBeUndefined()
  })

  it('confirms a background slot bubble in slotMessages (split-view pane)', () => {
    let state = reducer(withSlot, appendSlotMessage({
      slot: 'pane-9',
      message: { role: 'user', content: 'pane send', cls: '', ts: '2026-08-16T10:00:00.000Z', meta: { sendId: 's-pane' } } as ChatMessage,
    }))
    expect(state.slotMessages['pane-9'][0].meta?.optimistic).toBe(true)

    state = reducer(state, confirmOptimisticSend({ slot: 'pane-9', sendId: 's-pane' }))

    expect(state.slotMessages['pane-9'][0].meta?.optimistic).toBeUndefined()
  })

  it('is a no-op for an unknown sendId (a busy-slot send appended no bubble)', () => {
    let state = reducer(withSlot, appendMessage({ role: 'user', content: 'mine', cls: '', ts: '2026-08-16T10:00:00.000Z', meta: { sendId: 's-mine' } }))

    state = reducer(state, confirmOptimisticSend({ slot: 'slot-1', sendId: 's-someone-else' }))

    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].meta?.optimistic).toBe(true)
  })
})

/* The transport deadline fired with no receipt and no echo: the bubble is still
 * `optimistic`, and nothing will clear that until a late echo does. The mark is
 * what the row's pending line is drawn from -- the flag alone also survives a
 * refused or connection-failed send and a queued receipt, none of which is a
 * wait -- and it falls with the flag on both confirmation doors. */
describe('markSendUnconfirmed — the deadline mark on a bubble whose receipt never came', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const withSlot = { ...initial, activeSlot: 'slot-1' }
  const bubble = (sendId: string, content = 'did this arrive?') =>
    appendMessage({ role: 'user', content, cls: '', ts: '2026-09-28T10:00:00.000Z', meta: { sendId } })

  it('marks the matching optimistic bubble and keeps its flag and sendId', () => {
    let state = reducer(withSlot, bubble('s-late'))
    state = reducer(state, markSendUnconfirmed({ slot: 'slot-1', sendId: 's-late' }))
    expect(state.messages[0].meta).toMatchObject({ optimistic: true, sendId: 's-late', deliveryUnconfirmed: true })
  })

  it('marks only the matching send, leaving a sibling bubble unmarked', () => {
    let state = reducer(withSlot, bubble('s-a', 'first'))
    state = reducer(state, bubble('s-b', 'second'))
    state = reducer(state, markSendUnconfirmed({ slot: 'slot-1', sendId: 's-b' }))
    expect(state.messages[0].meta?.deliveryUnconfirmed).toBeUndefined()
    expect(state.messages[1].meta?.deliveryUnconfirmed).toBe(true)
  })

  it('leaves a row an echo or receipt already confirmed alone', () => {
    let state = reducer(withSlot, bubble('s-confirmed'))
    state = reducer(state, confirmOptimisticSend({ slot: 'slot-1', sendId: 's-confirmed', mid: 'm-1' }))
    state = reducer(state, markSendUnconfirmed({ slot: 'slot-1', sendId: 's-confirmed' }))
    expect(state.messages[0].meta?.deliveryUnconfirmed).toBeUndefined()
    expect(state.messages[0].meta?.optimistic).toBeUndefined()
  })

  it('is a no-op for an unknown sendId', () => {
    let state = reducer(withSlot, bubble('s-mine'))
    state = reducer(state, markSendUnconfirmed({ slot: 'slot-1', sendId: 's-someone-else' }))
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].meta?.deliveryUnconfirmed).toBeUndefined()
  })

  it('marks a background slot bubble in slotMessages (the user switched sessions inside the deadline)', () => {
    let state = reducer(withSlot, appendSlotMessage({
      slot: 'pane-9',
      message: { role: 'user', content: 'pane send', cls: '', ts: '2026-09-28T10:00:00.000Z', meta: { sendId: 's-pane' } } as ChatMessage,
    }))
    state = reducer(state, markSendUnconfirmed({ slot: 'pane-9', sendId: 's-pane' }))
    expect(state.slotMessages['pane-9'][0].meta?.deliveryUnconfirmed).toBe(true)
  })

  it('falls with the flag when the receipt arrives after all', () => {
    let state = reducer(withSlot, bubble('s-late-receipt'))
    state = reducer(state, markSendUnconfirmed({ slot: 'slot-1', sendId: 's-late-receipt' }))
    state = reducer(state, confirmOptimisticSend({ slot: 'slot-1', sendId: 's-late-receipt', mid: 'm-2' }))
    expect(state.messages[0].meta?.deliveryUnconfirmed).toBeUndefined()
    expect(state.messages[0].meta?.optimistic).toBeUndefined()
    expect(state.messages[0].meta?.mid).toBe('m-2')
  })

  it('falls with the flag when a correlated echo lands', () => {
    let state = reducer(withSlot, bubble('s-late-echo'))
    state = reducer(state, markSendUnconfirmed({ slot: 'slot-1', sendId: 's-late-echo' }))
    state = reducer(state, sseChatMessage({
      slot: 'slot-1', role: 'user', content: 'did this arrive?', cls: '', ts: '2026-09-28T10:00:05.000Z',
      meta: { sendId: 's-late-echo', mid: 'm-3' },
    }))
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].meta?.deliveryUnconfirmed).toBeUndefined()
    expect(state.messages[0].meta?.optimistic).toBeUndefined()
    expect(state.messages[0].meta?.mid).toBe('m-3')
  })
})

/* The footer's running indicator reads this: while the newest send is still
 * unconfirmed the transcript already says "pending" on the bubble and in the
 * WARN notice under it, so "Thinking…" beneath both would claim the agent is
 * working on a message nothing proves it received. The two confirmation doors
 * (`confirmOptimisticSend`, the correlated echo) clear the mark, and the
 * selector follows the mark, never `optimistic` alone. */
describe('selectTrailingSendUnconfirmed — the newest send is a bubble whose receipt never came', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const withSlot = { ...initial, activeSlot: 'slot-1' }
  const wrap = (chat: typeof initial) => ({ chat }) as unknown as RootState
  const bubble = (sendId: string, content = 'did this arrive?') =>
    appendMessage({ role: 'user', content, cls: '', ts: '2026-09-28T10:00:00.000Z', meta: { sendId } })
  /** The WARN row the `response-late` arm posts directly under the bubble. */
  const notice = () => appendMessage({ role: 'notice', content: '\u26A0\uFE0F Delivery not confirmed', cls: '' })

  it('is true while the trailing user row carries the deadline mark, looking past the notice under it', () => {
    let state = reducer(withSlot, bubble('s-late'))
    state = reducer(state, markSendUnconfirmed({ slot: 'slot-1', sendId: 's-late' }))
    state = reducer(state, notice())
    expect(selectTrailingSendUnconfirmed(wrap(state))).toBe(true)
  })

  it('is false once the receipt confirms the send after all', () => {
    let state = reducer(withSlot, bubble('s-late-receipt'))
    state = reducer(state, markSendUnconfirmed({ slot: 'slot-1', sendId: 's-late-receipt' }))
    state = reducer(state, notice())
    expect(selectTrailingSendUnconfirmed(wrap(state))).toBe(true)
    state = reducer(state, confirmOptimisticSend({ slot: 'slot-1', sendId: 's-late-receipt', mid: 'm-2' }))
    expect(selectTrailingSendUnconfirmed(wrap(state))).toBe(false)
  })

  it('is false once a correlated echo clears the mark', () => {
    let state = reducer(withSlot, bubble('s-late-echo'))
    state = reducer(state, markSendUnconfirmed({ slot: 'slot-1', sendId: 's-late-echo' }))
    state = reducer(state, notice())
    state = reducer(state, sseChatMessage({
      slot: 'slot-1', role: 'user', content: 'did this arrive?', cls: '', ts: '2026-09-28T10:00:05.000Z',
      meta: { sendId: 's-late-echo', mid: 'm-3' },
    }))
    expect(selectTrailingSendUnconfirmed(wrap(state))).toBe(false)
  })

  it('is false for a confirmed send that was never marked', () => {
    let state = reducer(withSlot, bubble('s-ok'))
    state = reducer(state, confirmOptimisticSend({ slot: 'slot-1', sendId: 's-ok', mid: 'm-1' }))
    expect(selectTrailingSendUnconfirmed(wrap(state))).toBe(false)
  })

  it('is false for an optimistic bubble the deadline has not reached (the flag alone does not decide)', () => {
    const state = reducer(withSlot, bubble('s-in-flight'))
    expect(state.messages[0].meta?.optimistic).toBe(true)
    expect(selectTrailingSendUnconfirmed(wrap(state))).toBe(false)
  })

  it('is false when an assistant row trails the marked bubble: the agent is visibly working', () => {
    let state = reducer(withSlot, bubble('s-late'))
    state = reducer(state, markSendUnconfirmed({ slot: 'slot-1', sendId: 's-late' }))
    state = reducer(state, notice())
    state = reducer(state, appendMessage({ role: 'assistant', content: 'on it', cls: '', ts: '2026-09-28T10:00:09.000Z' }))
    expect(selectTrailingSendUnconfirmed(wrap(state))).toBe(false)
  })

  it('is false on an empty transcript', () => {
    expect(selectTrailingSendUnconfirmed(wrap(withSlot))).toBe(false)
  })
})

describe('sseChatMessage — _segment handling', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const withSlot = { ...initial, activeSlot: 'slot-1' }

  it('_segment converts streaming → assistant, preserves content and rawText', () => {
    // Req 2.1, 5.2: streaming message finalized to assistant with rawText preserved
    let state = reducer(withSlot, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'analysis text' }))
    expect(state.messages[0].role).toBe('streaming')

    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: '_segment', content: '' }))
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].role).toBe('assistant')
    expect(state.messages[0].content).toBe('analysis text')
    expect(state.messages[0].rawText).toBe('analysis text')
  })

  it('_segment with no streaming message is a no-op', () => {
    // Req 2.2: no streaming message → no change
    const state = reducer(withSlot, sseChatMessage({ slot: 'slot-1', role: '_segment', content: '' }))
    expect(state.messages).toHaveLength(0)
  })

  it('_segment does not reset lastChunkSeq', () => {
    // Req 7.2: lastChunkSeq preserved across segment boundaries
    let state = reducer(withSlot, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'text', seq: 5 }))
    expect(state.lastChunkSeq).toBe(5)

    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: '_segment', content: '' }))
    expect(state.lastChunkSeq).toBe(5)
    // slotState and slotRunning also unchanged
    expect(state.slotState).toBe('streaming')
  })

  it('chunk after _segment creates new streaming message', () => {
    // Req 2.3: new streaming message after segment boundary
    let state = reducer(withSlot, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'before tool' }))
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: '_segment', content: '' }))
    expect(state.messages[0].role).toBe('assistant')

    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'after tool' }))
    expect(state.messages).toHaveLength(2)
    expect(state.messages[0].role).toBe('assistant')
    expect(state.messages[0].content).toBe('before tool')
    expect(state.messages[1].role).toBe('streaming')
    expect(state.messages[1].content).toBe('after tool')
  })

  it('tool insertion after _segment places tool after finalized assistant', () => {
    // Req 3.1: tool card inserted after the finalized assistant message
    let state = reducer(withSlot, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'reasoning' }))
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: '_segment', content: '' }))
    // Now: [assistant]
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'tool', content: '🔧 read_file' }))
    // Now: [assistant, tool]
    expect(state.messages).toHaveLength(2)
    expect(state.messages[0].role).toBe('assistant')
    expect(state.messages[1].role).toBe('tool')
    expect(state.messages[1].content).toBe('🔧 read_file')
  })

  it('_done after segmented stream converts final streaming → assistant', () => {
    // Req 4.1: final streaming message finalized on _done after a segment
    let state = reducer(withSlot, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'part 1' }))
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: '_segment', content: '' }))
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'tool', content: '🔧 bash' }))
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'part 2' }))
    // Now: [assistant, tool, streaming]
    expect(state.messages).toHaveLength(3)
    expect(state.messages[2].role).toBe('streaming')

    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: '_done', content: '' }))
    // Now: [assistant, tool, assistant]
    expect(state.messages).toHaveLength(3)
    expect(state.messages[0].role).toBe('assistant')
    expect(state.messages[0].content).toBe('part 1')
    expect(state.messages[1].role).toBe('tool')
    expect(state.messages[2].role).toBe('assistant')
    expect(state.messages[2].content).toBe('part 2')
    expect(state.slotRunning).toBe(false)
    expect(state.slotState).toBe('idle')
  })

  it('tool-free stream produces single assistant message (regression)', () => {
    // Req 8.2: no segments → single assistant message, identical to pre-feature behavior
    let state = reducer(withSlot, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'hello ' }))
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'world' }))
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: '_done', content: '' }))
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].role).toBe('assistant')
    expect(state.messages[0].content).toBe('hello world')
  })
})

describe('sseChatMessagePatchByTs', () => {
  const initial = reducer(undefined, { type: '@@INIT' })

  // Build a slot state with one mcp_oauth banner already appended.
  function withMcpOauthBanner(activeSlot: string | null = 'slot-1') {
    const ts = '2026-05-28T01:00:00.000Z'
    const banner = {
      role: 'mcp_oauth',
      content: '🔐 linear requires authentication.',
      cls: 'msg msg-info',
      ts,
      meta: { server_name: 'linear', oauth_url: 'https://mcp.linear.app/authorize' },
    }
    return {
      state: {
        ...initial,
        activeSlot,
        messages: activeSlot === 'slot-1' ? [banner] as ChatMessage[] : [],
        slotMessages: { 'slot-1': [banner] as ChatMessage[] },
      },
      ts,
    }
  }

  it('patches the active slot messages array (success transition)', () => {
    const { state, ts } = withMcpOauthBanner('slot-1')
    const out = reducer(state, {
      type: 'chat/sseChatMessagePatchByTs',
      payload: {
        slot: 'slot-1',
        ts,
        meta: { server_name: 'linear', completed: true },
        content: '🔓 linear authenticated.',
      },
    })
    expect(out.messages[0].content).toBe('🔓 linear authenticated.')
    expect(out.messages[0].meta).toMatchObject({ completed: true, server_name: 'linear' })
    // slotMessages cache also updated.
    expect(out.slotMessages['slot-1'][0].meta).toMatchObject({ completed: true })
  })

  it('patches a slot the user is NOT currently viewing (slotMessages cache only)', () => {
    // The active slot is "other", but the update is for "slot-1". We still
    // patch slotMessages so the user sees the right state after switching back.
    const { state, ts } = withMcpOauthBanner('other')
    const out = reducer(state, {
      type: 'chat/sseChatMessagePatchByTs',
      payload: {
        slot: 'slot-1',
        ts,
        meta: { server_name: 'linear', completed: true },
        content: '🔓 linear authenticated.',
      },
    })
    // Active messages array (= 'other') is untouched.
    expect(out.messages).toEqual([])
    // Cached messages for slot-1 reflect the patched state.
    expect(out.slotMessages['slot-1'][0].meta).toMatchObject({ completed: true })
    expect(out.slotMessages['slot-1'][0].content).toBe('🔓 linear authenticated.')
  })

  it('merges meta — keeps existing keys, adds new ones', () => {
    const { state, ts } = withMcpOauthBanner('slot-1')
    const out = reducer(state, {
      type: 'chat/sseChatMessagePatchByTs',
      payload: {
        slot: 'slot-1',
        ts,
        meta: { failed: true, error: 'dns failed' },
        content: '🚫 linear authentication failed.',
      },
    })
    // server_name preserved; failed + error added.
    expect(out.messages[0].meta).toMatchObject({
      server_name: 'linear',
      failed: true,
      error: 'dns failed',
    })
  })

  it('no-op when ts does not match any message', () => {
    const { state } = withMcpOauthBanner('slot-1')
    const out = reducer(state, {
      type: 'chat/sseChatMessagePatchByTs',
      payload: {
        slot: 'slot-1',
        ts: '2099-01-01T00:00:00.000Z',
        meta: { completed: true },
      },
    })
    // Original banner unchanged.
    expect(out.messages[0].meta).toEqual({
      server_name: 'linear',
      oauth_url: 'https://mcp.linear.app/authorize',
    })
  })

  it('no-op when ts is empty', () => {
    const { state } = withMcpOauthBanner('slot-1')
    const out = reducer(state, {
      type: 'chat/sseChatMessagePatchByTs',
      payload: { slot: 'slot-1', ts: '', meta: { completed: true } },
    })
    expect(out.messages[0].meta?.completed).toBeUndefined()
  })

  // Two restored rows can carry the SAME ts (which is why rows also carry
  // meta.mid), and a ts-keyed lookup resolves the first match. Retiring two
  // superseded OAuth banners would then patch one row twice and leave the other
  // rendering a dead Authorize link (issue #7580).
  describe('row identity', () => {
    /** Two banners deliberately sharing one ts, each with its own mid. */
    function withCollidingBanners() {
      const ts = '2026-05-28T01:00:00.000Z'
      const rows = ['m-first', 'm-second'].map(mid => ({
        role: 'mcp_oauth',
        content: '🔐 linear requires authentication.',
        cls: 'msg msg-info',
        ts,
        meta: { mid, server_name: 'linear', oauth_url: 'https://mcp.linear.app/authorize' },
      }))
      return {
        state: {
          ...initial,
          activeSlot: 'slot-1',
          messages: rows as ChatMessage[],
          slotMessages: { 'slot-1': rows as ChatMessage[] },
        },
        ts,
      }
    }

    it('patches the row named by mid, not the first row sharing its ts', () => {
      const { state, ts } = withCollidingBanners()
      const out = reducer(state, {
        type: 'chat/sseChatMessagePatchByTs',
        payload: {
          slot: 'slot-1',
          ts,
          mid: 'm-second',
          meta: { superseded: true, oauth_url: '' },
          content: 'retired',
        },
      })
      expect(out.messages[1].meta).toMatchObject({ superseded: true, oauth_url: '' })
      expect(out.messages[1].content).toBe('retired')
      // The colliding sibling must be left alone.
      expect(out.messages[0].meta?.superseded).toBeUndefined()
      expect(out.messages[0].meta?.oauth_url).toBe('https://mcp.linear.app/authorize')
    })

    it('retires BOTH colliding rows when each is named by its own mid', () => {
      const { state, ts } = withCollidingBanners()
      const patch = (s: typeof state, mid: string) =>
        reducer(s, {
          type: 'chat/sseChatMessagePatchByTs',
          payload: { slot: 'slot-1', ts, mid, meta: { superseded: true, oauth_url: '' } },
        })
      const out = patch(patch(state, 'm-first'), 'm-second')
      expect(out.messages.map(m => m.meta?.superseded)).toEqual([true, true])
      expect(out.messages.every(m => !m.meta?.oauth_url)).toBe(true)
    })

    it('falls back to ts when the server sends no mid (legacy rows)', () => {
      const { state, ts } = withMcpOauthBanner('slot-1')
      const out = reducer(state, {
        type: 'chat/sseChatMessagePatchByTs',
        payload: { slot: 'slot-1', ts, meta: { completed: true } },
      })
      expect(out.messages[0].meta?.completed).toBe(true)
    })

    it('is a no-op when a mid names no row, rather than patching by ts instead', () => {
      const { state, ts } = withCollidingBanners()
      const out = reducer(state, {
        type: 'chat/sseChatMessagePatchByTs',
        payload: { slot: 'slot-1', ts, mid: 'm-absent', meta: { superseded: true } },
      })
      expect(out.messages.every(m => m.meta?.superseded === undefined)).toBe(true)
    })

    it('patches by mid even when the payload carries no ts at all', () => {
      const { state } = withCollidingBanners()
      const out = reducer(state, {
        type: 'chat/sseChatMessagePatchByTs',
        payload: { slot: 'slot-1', ts: '', mid: 'm-second', meta: { superseded: true } },
      })
      expect(out.messages[1].meta?.superseded).toBe(true)
      expect(out.messages[0].meta?.superseded).toBeUndefined()
    })
  })

  it('no-op when slot is empty', () => {
    const { state, ts } = withMcpOauthBanner('slot-1')
    const out = reducer(state, {
      type: 'chat/sseChatMessagePatchByTs',
      payload: { slot: '', ts, meta: { completed: true } },
    })
    expect(out.messages[0].meta?.completed).toBeUndefined()
  })

  it('content-only update leaves meta untouched', () => {
    const { state, ts } = withMcpOauthBanner('slot-1')
    const out = reducer(state, {
      type: 'chat/sseChatMessagePatchByTs',
      payload: { slot: 'slot-1', ts, content: 'changed' },
    })
    expect(out.messages[0].content).toBe('changed')
    // meta preserved as-is.
    expect(out.messages[0].meta).toEqual({
      server_name: 'linear',
      oauth_url: 'https://mcp.linear.app/authorize',
    })
  })
})

describe('sseContextUsage reducer', () => {
  const initial = reducer(undefined, { type: '@@INIT' })

  it('stores pct and token counts when window is known', () => {
    const state = reducer(initial, sseContextUsage({ slot: 's1', pct: 44, used_tokens: 88000, window_tokens: 200000 }))
    expect(state.slotContextPct['s1']).toBe(44)
    expect(state.slotContextTokens['s1']).toEqual({ used: 88000, window: 200000 })
  })

  it('stores pct only and leaves tokens untouched when window is 0/absent', () => {
    const state = reducer(initial, sseContextUsage({ slot: 's1', pct: 9 }))
    expect(state.slotContextPct['s1']).toBe(9)
    expect(state.slotContextTokens['s1']).toBeUndefined()
    const zero = reducer(initial, sseContextUsage({ slot: 's1', pct: 9, used_tokens: 5, window_tokens: 0 }))
    expect(zero.slotContextTokens['s1']).toBeUndefined()
  })

  it('falls back to used:0 when used_tokens omitted but window present', () => {
    const state = reducer(initial, sseContextUsage({ slot: 's1', pct: 10, window_tokens: 200000 }))
    expect(state.slotContextTokens['s1']).toEqual({ used: 0, window: 200000 })
  })

  it('reset with a window replaces the stored entry (live model switch)', () => {
    const seeded = reducer(initial, sseContextUsage({ slot: 's1', pct: 10, used_tokens: 100000, window_tokens: 1000000 }))
    const state = reducer(seeded, sseContextUsage({ slot: 's1', pct: 36.8, used_tokens: 100000, window_tokens: 272000, reset: true }))
    expect(state.slotContextTokens['s1']).toEqual({ used: 100000, window: 272000 })
    expect(state.slotContextPct['s1']).toBe(36.8)
  })

  it('reset without a window deletes the stored entry (session reset / compaction)', () => {
    // Deleting re-enables the model-derived fallback for the slot's NEW model;
    // without reset the stale old-model entry short-circuits it until the next turn.
    //
    // #1645: this is the frontend half of the "225K used / 0%" bug. After
    // /compact the backend zeroes `used` but keeps the window, so it emits a
    // reset frame here. Honouring it deletes the pre-compaction token entry so
    // the ring can never show a stale count beside the freshly-reset 0%.
    const seeded = reducer(initial, sseContextUsage({ slot: 's1', pct: 10, used_tokens: 225000, window_tokens: 1000000 }))
    const state = reducer(seeded, sseContextUsage({ slot: 's1', pct: 0, reset: true }))
    expect(state.slotContextTokens['s1']).toBeUndefined()
    expect(state.slotContextPct['s1']).toBe(0)
  })

  it('pct-only event WITHOUT reset still leaves stored tokens untouched', () => {
    const seeded = reducer(initial, sseContextUsage({ slot: 's1', pct: 10, used_tokens: 100000, window_tokens: 1000000 }))
    const state = reducer(seeded, sseContextUsage({ slot: 's1', pct: 12 }))
    expect(state.slotContextTokens['s1']).toEqual({ used: 100000, window: 1000000 })
  })
})

describe('sseSideResult — side conversation reducer', () => {
  const initial = reducer(undefined, { type: '@@INIT' })

  it('assistant chunks accumulate as deltas under same run_id', () => {
    let state = reducer(initial, sseSideResult({ slot: 'slot-1', run_id: 'r1', role: 'user', content: 'hi' }))
    state = reducer(state, sseSideResult({ slot: 'slot-1', run_id: 'r1', role: 'assistant', content: 'Hello' }))
    state = reducer(state, sseSideResult({ slot: 'slot-1', run_id: 'r1', role: 'assistant', content: ' world' }))
    expect(state.slotSide['slot-1'].messages).toHaveLength(2)
    expect(state.slotSide['slot-1'].messages[1].content).toBe('Hello world')
    expect(state.slotSide['slot-1'].lastRunId).toBe('r1')
  })

  it('replaces a non-prefix streamed tail with the complete final answer', () => {
    let state = reducer(initial, sseSideResult({ slot: 'slot-1', run_id: 'r1', role: 'assistant', content: 'tail' }))
    state = reducer(state, sseSideResult({
      slot: 'slot-1', run_id: 'r1', role: 'assistant', content: 'head tail', final: true, message_id: 'a1',
    }))
    expect(state.slotSide['slot-1'].messages).toHaveLength(1)
    expect(state.slotSide['slot-1'].messages[0]).toMatchObject({ content: 'head tail', id: 'a1' })
  })

  it('new run_id starts a fresh assistant message', () => {
    let state = reducer(initial, sseSideResult({ slot: 'slot-1', run_id: 'r1', role: 'assistant', content: 'first' }))
    state = reducer(state, sseSideResult({ slot: 'slot-1', run_id: 'r2', role: 'user', content: 'q2' }))
    state = reducer(state, sseSideResult({ slot: 'slot-1', run_id: 'r2', role: 'assistant', content: 'second' }))
    expect(state.slotSide['slot-1'].messages).toHaveLength(3)
    expect(state.slotSide['slot-1'].lastRunId).toBe('r2')
  })

  it('sideClose drops per-slot side state', () => {
    let state = reducer(initial, sseSideResult({ slot: 'slot-1', run_id: 'r1', role: 'user', content: 'q' }))
    state = reducer(state, sseSideResult({ slot: 'slot-2', run_id: 'r2', role: 'user', content: 'q2' }))
    state = reducer(state, sideClose('slot-1'))
    expect(state.slotSide['slot-1']).toBeUndefined()
    expect(state.slotSide['slot-2']).toBeDefined()
  })
})

describe('sseThinkingChunk (model reasoning)', () => {
  const base = reducer(undefined, { type: '@@INIT' })
  const active = reducer(base, setActiveSlot('chat-1'))

  it('creates a content-bearing thinking message', () => {
    const state = reducer(active, sseThinkingChunk({ slot: 'chat-1', content: 'Let me think' }))
    const thinking = state.messages.filter(m => m.role === 'thinking')
    expect(thinking).toHaveLength(1)
    expect(thinking[0].content).toBe('Let me think')
  })

  it('accumulates into a single thinking message within a turn', () => {
    let state = reducer(active, sseThinkingChunk({ slot: 'chat-1', content: 'Step 1. ' }))
    state = reducer(state, sseThinkingChunk({ slot: 'chat-1', content: 'Step 2.' }))
    const thinking = state.messages.filter(m => m.role === 'thinking')
    expect(thinking).toHaveLength(1)
    expect(thinking[0].content).toBe('Step 1. Step 2.')
  })

  it('ignores chunks for a non-active slot', () => {
    const state = reducer(active, sseThinkingChunk({ slot: 'other', content: 'nope' }))
    expect(state.messages).toHaveLength(0)
  })

  it('ignores empty content', () => {
    const state = reducer(active, sseThinkingChunk({ slot: 'chat-1', content: '' }))
    expect(state.messages).toHaveLength(0)
  })

  it('starts a fresh reasoning block in a new turn (after a user message)', () => {
    let state = reducer(active, sseThinkingChunk({ slot: 'chat-1', content: 'first turn reasoning' }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'user', content: 'follow up', ts: '' }))
    state = reducer(state, sseThinkingChunk({ slot: 'chat-1', content: 'second turn reasoning' }))
    const thinking = state.messages.filter(m => m.role === 'thinking')
    expect(thinking).toHaveLength(2)
    expect(thinking[1].content).toBe('second turn reasoning')
  })

  it('chat_chunk preserves a content-bearing reasoning block', () => {
    let state = reducer(active, sseThinkingChunk({ slot: 'chat-1', content: 'reasoning text' }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'answer', seq: 0 }))
    const thinking = state.messages.filter(m => m.role === 'thinking')
    const streaming = state.messages.filter(m => m.role === 'streaming')
    expect(thinking).toHaveLength(1)
    expect(thinking[0].content).toBe('reasoning text')
    expect(streaming).toHaveLength(1)
  })

  it('chat_chunk still drops an empty thinking placeholder', () => {
    let state = reducer(active, appendMessage({ role: 'thinking', content: '', cls: '' }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'answer', seq: 0 }))
    expect(state.messages.filter(m => m.role === 'thinking')).toHaveLength(0)
  })

  it('opens a NEW block for each reasoning burst across tool calls', () => {
    let state = reducer(active, sseThinkingChunk({ slot: 'chat-1', content: 'burst one' }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'tool', content: '🔧 grep', meta: { tool_call_id: 't1' } }))
    state = reducer(state, sseThinkingChunk({ slot: 'chat-1', content: 'burst two' }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'tool', content: '🔧 fs_read', meta: { tool_call_id: 't2' } }))
    state = reducer(state, sseThinkingChunk({ slot: 'chat-1', content: 'burst three' }))
    const thinking = state.messages.filter(m => m.role === 'thinking')
    expect(thinking.map(m => m.content)).toEqual(['burst one', 'burst two', 'burst three'])
    // emission order preserved: each burst sits next to the step it explains
    expect(state.messages.map(m => m.role)).toEqual(['thinking', 'tool', 'thinking', 'tool', 'thinking'])
  })

  it('splices a post-tool burst ABOVE the turn\u2019s still-open streaming row', () => {
    let state = reducer(active, sseThinkingChunk({ slot: 'chat-1', content: 'burst one' }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'partial answer', seq: 0 }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'tool', content: '🔧 grep', meta: { tool_call_id: 't1' } }))
    state = reducer(state, sseThinkingChunk({ slot: 'chat-1', content: 'burst two' }))
    expect(state.messages.map(m => m.role)).toEqual(['thinking', 'tool', 'thinking', 'streaming'])
    // the turn's text keeps accumulating into that same row, below both blocks
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: ' plus more', seq: 1 }))
    expect(state.messages.filter(m => m.role === 'streaming')).toHaveLength(1)
    expect(state.messages.filter(m => m.role === 'thinking')).toHaveLength(2)
  })

  it('keeps one burst when an out-of-band row lands below the open answer', () => {
    // An approval row / queued bubble / stop event is pushed BELOW the turn's
    // open streaming row, so the array tail is not the end of the turn.
    // Measuring from `length` would split this one burst in two and drop the
    // second half beneath the answer it explains.
    let state = reducer(active, sseThinkingChunk({ slot: 'chat-1', content: 'first ' }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'answer so far', seq: 0 }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'error', content: '⟳ Connection lost' }))
    state = reducer(state, sseThinkingChunk({ slot: 'chat-1', content: 'second' }))
    const thinking = state.messages.filter(m => m.role === 'thinking')
    expect(thinking).toHaveLength(1)
    expect(thinking[0].content).toBe('first second')
    expect(state.messages.map(m => m.role)).toEqual(['thinking', 'streaming', 'error'])
  })

  it('closes the burst when an approval pushes the tool call below the open answer', () => {
    // The `tool` branch steps back over a trailing `streaming` row but not over
    // an approval row, so an approval-gated call lands BELOW the text. The rows
    // are then out of emission order and "above the open text row" is no longer
    // "after the last tool" — the post-tool burst must still not be folded into
    // the pre-tool block.
    let state = reducer(active, sseThinkingChunk({ slot: 'chat-1', content: 'why the tool' }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'pre-tool text', seq: 0 }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'permission', content: 'approve?' }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'tool', content: '🔧 shell', meta: { tool_call_id: 't1' } }))
    state = reducer(state, sseThinkingChunk({ slot: 'chat-1', content: 'what the tool returned' }))

    const thinking = state.messages.filter(m => m.role === 'thinking')
    expect(thinking.map(m => m.content)).toEqual(['why the tool', 'what the tool returned'])
    // the new burst lands after the tool it followed, not merged into the first
    expect(state.messages[state.messages.length - 1].role).toBe('thinking')
  })

  it('keeps one burst when a queued bubble interrupts it before any text', () => {
    // The user types while the model is still reasoning, so a `queued` row is
    // appended before the first chunk. It interrupts the burst without ending
    // it — the reasoning that follows is the same burst, not a second block.
    let state = reducer(active, sseThinkingChunk({ slot: 'chat-1', content: 'still ' }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'queued', content: 'and also check X' }))
    state = reducer(state, sseThinkingChunk({ slot: 'chat-1', content: 'thinking' }))
    const thinking = state.messages.filter(m => m.role === 'thinking')
    expect(thinking).toHaveLength(1)
    expect(thinking[0].content).toBe('still thinking')
  })
})

describe('thinking survives refreshSlot (client-only reasoning)', () => {
  const base = reducer(undefined, { type: '@@INIT' })

  const refreshPayload = (key: string, messages: { role: string; content: string; cls?: string; ts?: string; meta?: Record<string, unknown> }[]) => ({
    key, messages, running: false, hasMore: false, total: messages.length, stopping: false,
  })

  it('re-inserts the reasoning block before its assistant after refresh', () => {
    let state = reducer(base, setActiveSlot('chat-1'))
    // model reasons, then answers
    state = reducer(state, sseThinkingChunk({ slot: 'chat-1', content: 'because X then Y' }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'The answer', seq: 0 }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: '_done', content: '' }))
    expect(state.messages.filter(m => m.role === 'thinking')).toHaveLength(1)

    // server refresh carries only the persisted user/assistant (no thinking)
    state = reducer(state, refreshSlot.fulfilled(
      refreshPayload('chat-1', [{ role: 'assistant', content: 'The answer', cls: 'msg msg-a' }]),
      'req', 'chat-1',
    ))

    const thinking = state.messages.filter(m => m.role === 'thinking')
    expect(thinking).toHaveLength(1)
    expect(thinking[0].content).toBe('because X then Y')
    // anchored directly before its assistant
    const ti = state.messages.findIndex(m => m.role === 'thinking')
    const ai = state.messages.findIndex(m => m.role === 'assistant')
    expect(ti).toBeGreaterThanOrEqual(0)
    expect(ti).toBe(ai - 1)
  })

  it('does not duplicate the reasoning block across successive refreshes', () => {
    let state = reducer(base, setActiveSlot('chat-1'))
    state = reducer(state, sseThinkingChunk({ slot: 'chat-1', content: 'reasoning' }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'Answer', seq: 0 }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: '_done', content: '' }))
    const payload = refreshPayload('chat-1', [{ role: 'assistant', content: 'Answer', cls: 'msg msg-a' }])
    state = reducer(state, refreshSlot.fulfilled(payload, 'r1', 'chat-1'))
    state = reducer(state, refreshSlot.fulfilled(payload, 'r2', 'chat-1'))
    expect(state.messages.filter(m => m.role === 'thinking')).toHaveLength(1)
  })

  it('rebuilds the burst order of a multi-tool turn from tool ids (#4218)', () => {
    // Reasoning is client-only, so the refresh rebuilds the array from server
    // history that has never seen a thinking row. The position is recoverable
    // anyway: each burst is followed by its own tool row, whose server-minted
    // `tool_call_id` persists into history, so the post-refresh order must equal
    // the live order exactly. Anchoring on answer CONTENT instead cannot do this
    // — a reason-then-tool turn flushes no text at the tool boundary, so history
    // holds one assistant row for every burst and all but one were parked below
    // the answer.
    let state = reducer(base, setActiveSlot('chat-1'))
    state = reducer(state, sseThinkingChunk({ slot: 'chat-1', content: 'why t1' }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'tool', content: '🔧 grep', meta: { tool_call_id: 't1' } }))
    state = reducer(state, sseThinkingChunk({ slot: 'chat-1', content: 'why t2' }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'The answer', seq: 0 }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: '_done', content: '' }))
    // live: each burst above the step it explains
    expect(state.messages.map(m => m.role)).toEqual(['thinking', 'tool', 'thinking', 'assistant'])

    const payload = refreshPayload('chat-1', [
      { role: 'tool', content: '🔧 grep', cls: '', meta: { tool_call_id: 't1' } },
      { role: 'assistant', content: 'The answer', cls: 'msg msg-a' },
    ])
    state = reducer(state, refreshSlot.fulfilled(payload, 'r1', 'chat-1'))

    expect(state.messages.filter(m => m.role === 'thinking').map(m => m.content))
      .toEqual(['why t1', 'why t2'])
    expect(state.messages.map(m => m.role)).toEqual(['thinking', 'tool', 'thinking', 'assistant'])

    // Idempotent: a second refresh must not re-park or duplicate a block.
    state = reducer(state, refreshSlot.fulfilled(payload, 'r2', 'chat-1'))
    expect(state.messages.map(m => m.role)).toEqual(['thinking', 'tool', 'thinking', 'assistant'])
  })

  it('keeps every burst in place as the tool count grows (#4218)', () => {
    // Severity scaled with burst count before the fix: only one block could
    // anchor, so a turn that reasons before each of 10 calls left 9 stacked
    // below the answer. Drive the count that was reported in the wild.
    const N = 10
    let state = reducer(base, setActiveSlot('chat-1'))
    for (let n = 1; n <= N; n++) {
      state = reducer(state, sseThinkingChunk({ slot: 'chat-1', content: `why t${n}` }))
      state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'tool', content: '🔧 grep', meta: { tool_call_id: `t${n}` } }))
    }
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'Done', seq: 0 }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: '_done', content: '' }))
    const live = state.messages.map(m => m.role)

    const history = []
    for (let n = 1; n <= N; n++) {
      history.push({ role: 'tool', content: '🔧 grep', cls: '', meta: { tool_call_id: `t${n}` } })
    }
    history.push({ role: 'assistant', content: 'Done', cls: 'msg msg-a' })
    state = reducer(state, refreshSlot.fulfilled(refreshPayload('chat-1', history), 'r1', 'chat-1'))

    expect(state.messages.map(m => m.role)).toEqual(live)
    expect(state.messages.filter(m => m.role === 'thinking').map(m => m.content))
      .toEqual(Array.from({ length: N }, (_, i) => `why t${i + 1}`))
    // No tail stack: nothing reasoning-shaped after the answer.
    expect(state.messages[state.messages.length - 1].role).toBe('assistant')
  })

  it('anchors on the earlier row of an auto-approved tool pair (#4218)', () => {
    // An auto-approved call persists TWO tool rows sharing one tool_call_id
    // (🔧 pre-approval + ✅ post-approval). The block reasoned its way TO the
    // call, so it belongs above the first of the pair, not between them.
    let state = reducer(base, setActiveSlot('chat-1'))
    state = reducer(state, sseThinkingChunk({ slot: 'chat-1', content: 'why t1' }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'tool', content: '🔧 grep', meta: { tool_call_id: 't1' } }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'The answer', seq: 0 }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: '_done', content: '' }))

    state = reducer(state, refreshSlot.fulfilled(
      refreshPayload('chat-1', [
        { role: 'tool', content: '🔧 grep', cls: '', meta: { tool_call_id: 't1' } },
        { role: 'tool', content: '✅ grep', cls: '', meta: { tool_call_id: 't1' } },
        { role: 'assistant', content: 'The answer', cls: 'msg msg-a' },
      ]),
      'r1', 'chat-1',
    ))

    expect(state.messages.map(m => m.role)).toEqual(['thinking', 'tool', 'tool', 'assistant'])
    expect(state.messages.filter(m => m.role === 'thinking')).toHaveLength(1)
  })
})

describe('streaming chunk coalescing (batched flag)', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const active = reducer(initial, setActiveSlot('chat-1'))

  it('batched chunk appends content without inserting a missed-chunk marker on a seq jump', () => {
    // The useWebSocket flush buffer owns gap detection across the chunks it
    // merges, so the reducer must NOT re-derive a gap from the batch seq.
    let state = reducer(active, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'Hello ', seq: 1, batched: true }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'world', seq: 5, batched: true }))
    const streaming = state.messages.find(m => m.role === 'streaming')
    expect(streaming?.content).toBe('Hello world')
    expect(streaming?.content).not.toContain('chunk(s) missed')
    expect(state.lastChunkSeq).toBe(5)
  })

  it('non-batched chunk still inserts a missed-chunk marker on a seq jump (behavior unchanged)', () => {
    let state = reducer(active, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'Hello ', seq: 1 }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'world', seq: 5 }))
    const streaming = state.messages.find(m => m.role === 'streaming')
    expect(streaming?.content).toContain('chunk(s) missed')
  })
})
