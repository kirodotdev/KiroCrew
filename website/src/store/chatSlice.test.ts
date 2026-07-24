import { describe, it, expect } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, {
  setActiveSlot,
  sseSubagentSpawn,
  sseSubagentChunk,
  sseToolActivity,
  sseToolResult,
  switchSlot,
  refreshSlot,
  warmSlotCache,
} from './chatSlice'

function makeStore() {
  return configureStore({
    reducer: { chat: chatReducer },
    // Thunk payloads carry Date objects/etc.; disable the checks for terseness.
    middleware: (getDefault) => getDefault({ serializableCheck: false, immutableCheck: false }),
  })
}

describe('sseSubagentChunk — prototype-pollution guard (bug chatSlice.ts:931)', () => {
  it('ignores a poisoned __proto__ id and does not pollute Object.prototype', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    store.dispatch(sseSubagentSpawn({ slot: 'active', id: 'real', task: 't', agent: 'kirocrew' }))

    // Failure scenario: a subagent_chunk event whose id === '__proto__' would,
    // without the guard, resolve `state.subagents['__proto__']` to
    // Object.prototype and write `streaming` onto it — polluting every object.
    store.dispatch(sseSubagentChunk({ slot: 'active', id: '__proto__', text: 'poison' }))
    store.dispatch(sseSubagentChunk({ slot: 'active', id: 'constructor', text: 'poison' }))
    store.dispatch(sseSubagentChunk({ slot: 'active', id: 'prototype', text: 'poison' }))

    expect('streaming' in ({} as Record<string, unknown>)).toBe(false)
    expect((Object.prototype as Record<string, unknown>).streaming).toBeUndefined()

    // A legitimate chunk still appends to the real subagent.
    store.dispatch(sseSubagentChunk({ slot: 'active', id: 'real', text: 'hello' }))
    expect(store.getState().chat.subagents.real.streaming).toBe('hello')
  })
})

describe('sseToolResult — prefer exact tool_call_id match (bug chatSlice.ts:1213)', () => {
  it('attaches output to the entry with the matching tid, not a later id-less tool', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))

    // Tool A carries a tool_call_id; a later tool has no id (e.g. a legacy
    // activity entry). Order in the log: [A(id=call-A), B(no id)].
    store.dispatch(sseToolActivity({ slot: 'active', tool: 'toolA', kind: 'tool', purpose: '', input_preview: '', tool_call_id: 'call-A' }))
    store.dispatch(sseToolActivity({ slot: 'active', tool: 'toolB', kind: 'tool', purpose: '', input_preview: '' }))

    // Failure scenario: a result for call-A. The old trailing
    // `|| !log[i].tool_call_id` clause matched the most-recent id-less entry
    // (toolB) first, painting the output onto the wrong tool.
    store.dispatch(sseToolResult({ slot: 'active', output: 'RESULT-A', tool_call_id: 'call-A' }))

    const log = store.getState().chat.toolLog
    const a = log.find((e) => e.text === 'toolA')!
    const b = log.find((e) => e.text === 'toolB')!
    expect(a.output).toBe('RESULT-A')
    expect(b.output).toBeUndefined()
  })

  it('falls back to the most-recent id-less tool only when no tid matches', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    store.dispatch(sseToolActivity({ slot: 'active', tool: 'toolNoId', kind: 'tool', purpose: '', input_preview: '' }))

    // tid supplied but no entry carries it → fall back to the id-less tool.
    store.dispatch(sseToolResult({ slot: 'active', output: 'FALLBACK', tool_call_id: 'missing' }))
    const log = store.getState().chat.toolLog
    expect(log.find((e) => e.text === 'toolNoId')!.output).toBe('FALLBACK')
  })

  it('with no tid, attaches to the most-recent tool entry', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    store.dispatch(sseToolActivity({ slot: 'active', tool: 'first', kind: 'tool', purpose: '', input_preview: '' }))
    store.dispatch(sseToolActivity({ slot: 'active', tool: 'second', kind: 'tool', purpose: '', input_preview: '' }))
    store.dispatch(sseToolResult({ slot: 'active', output: 'LAST' }))
    const log = store.getState().chat.toolLog
    expect(log.find((e) => e.text === 'second')!.output).toBe('LAST')
    expect(log.find((e) => e.text === 'first')!.output).toBeUndefined()
  })
})

describe('warmSlotCache.fulfilled — hydrate queued bubbles (bug chatSlice.ts:1655)', () => {
  it('appends d.queue queued bubbles to the warmed cache instead of dropping them', () => {
    const store = makeStore()
    // activeSlot stays null; warm a background slot 'bg'.
    const payload = {
      key: 'bg',
      messages: [{ role: 'user', content: 'hi', cls: '' }],
      running: false,
      stopping: false,
      hasMore: false,
      total: 1,
      queue: [
        { content: 'queued one', queueId: 'q1', ts: '2026-01-01T00:00:00.000Z' },
        { content: 'queued two', queueId: 'q2', ts: '2026-01-01T00:00:01.000Z' },
      ],
    }
    store.dispatch(warmSlotCache.fulfilled(payload, 'req-1', 'bg'))

    const cached = store.getState().chat.slotMessages['bg']
    // Failure scenario: the queued bubbles were dropped, leaving only the 1
    // history message, so switching to 'bg' lost the user's queued input.
    expect(cached).toHaveLength(3)
    const queued = cached.filter((m) => m.role === 'queued')
    expect(queued.map((m) => m.content)).toEqual(['queued one', 'queued two'])
    expect(queued[0].meta?.queueId).toBe('q1')
    expect(queued[1].meta?.queueId).toBe('q2')
  })
})

// The queue-drop bug existed because switchSlot.fulfilled and
// warmSlotCache.fulfilled hand-mirrored the same slot-detail hydration and
// drifted apart. All three slot-detail reducers (switchSlot, warmSlotCache,
// refreshSlot) now route queued-bubble hydration through the single shared
// `hydrateQueuedBubbles` helper, so a new payload field can't silently diverge
// between them. These tests lock in that every consumer hydrates identically.
describe('slot-detail hydration is centralized (shared hydrateQueuedBubbles path)', () => {
  const detail = (key: string, queue: Array<{ content: string; queueId: string; ts: string }>) => ({
    key,
    messages: [{ role: 'user', content: 'hi', cls: '' }],
    running: false,
    stopping: false,
    hasMore: false,
    total: 1,
    queue,
  })

  it('switchSlot.fulfilled appends server queue bubbles and mirrors them into the cache', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    store.dispatch(
      switchSlot.fulfilled(
        detail('active', [
          { content: 'q-one', queueId: 'q1', ts: '2026-01-01T00:00:00.000Z' },
          { content: 'q-two', queueId: 'q2', ts: '2026-01-01T00:00:01.000Z' },
        ]),
        'req-1',
        'active',
      ),
    )
    const msgs = store.getState().chat.messages
    const queued = msgs.filter((m) => m.role === 'queued')
    expect(queued.map((m) => m.content)).toEqual(['q-one', 'q-two'])
    expect(queued.map((m) => m.meta?.queueId)).toEqual(['q1', 'q2'])
    // The per-slot cache is the same hydrated list (used on next switch-back).
    expect(store.getState().chat.slotMessages['active']).toEqual(msgs)
  })

  it('refreshSlot.fulfilled re-hydrates from the server queue field — was dropping them before, now no stale/dupes', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    // Seed one queued bubble via a switch.
    store.dispatch(
      switchSlot.fulfilled(
        detail('active', [{ content: 'stale', queueId: 'qOld', ts: '2026-01-01T00:00:00.000Z' }]),
        'r0',
        'active',
      ),
    )
    expect(store.getState().chat.messages.filter((m) => m.role === 'queued')).toHaveLength(1)

    // A refresh (e.g. on chat_done) reports a different canonical queue set.
    store.dispatch(
      refreshSlot.fulfilled(
        detail('active', [{ content: 'fresh', queueId: 'qNew', ts: '2026-01-01T00:00:02.000Z' }]),
        'r1',
        'active',
      ),
    )
    const queued = store.getState().chat.messages.filter((m) => m.role === 'queued')
    // Failure scenario before the fix: refreshSlot rebuilt messages from server
    // history + preserved perms/thinking and dropped ALL queued bubbles.
    // Regression scenario: the stale 'qOld' bubble duplicated alongside 'qNew'.
    expect(queued.map((m) => m.content)).toEqual(['fresh'])
    expect(queued[0].meta?.queueId).toBe('qNew')
  })
})
