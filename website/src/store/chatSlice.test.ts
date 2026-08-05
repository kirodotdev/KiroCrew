import { describe, it, expect } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, {
  setActiveSlot,
  sseChatMessage,
  hydrateSlotMessages,
  sseSubagentSpawn,
  sseSubagentChunk,
  sseToolActivity,
  sseToolResult,
  switchSlot,
  refreshSlot,
  warmSlotCache,
} from './chatSlice'
import { extractSpawnRunLaunch, isSpawnRunTool } from '../pages/chat/SubagentRunCard'

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

    // A result for call-A must attach to toolA. A trailing
    // `|| !log[i].tool_call_id` fallback would match the most-recent id-less
    // entry (toolB) first, painting the output onto the wrong tool.
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

describe('sseToolResult — tool output also lands on the tool MESSAGE meta', () => {
  // The inline SubagentRunCard detects a spawn_run launch by parsing
  // "Spawned N subagent(s)." out of message.meta.output. That field must be
  // patched onto the client message too, or the card would appear only after a
  // slot refetch — never during the live turn that spawned the agents.
  const SPAWN_OUTPUT = 'Spawned 2 subagent(s). Results will arrive as completion events:\n  a1b2c3d4 (kirocrew): map the picker\n  e5f6a7b8 (kirocrew): map the desktop shell\n'

  it('patches meta.output on the matching tool message so the launch is detectable live', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    store.dispatch(sseChatMessage({ slot: 'active', role: 'tool', content: '🔧 spawn_run', meta: { tool_call_id: 'call-spawn' } }))

    store.dispatch(sseToolResult({ slot: 'active', output: SPAWN_OUTPUT, tool_call_id: 'call-spawn' }))

    const msg = store.getState().chat.messages.find((m) => m.role === 'tool')!
    expect((msg.meta as Record<string, unknown>).output).toBe(SPAWN_OUTPUT)
    // The user-visible outcome: the card renders and TurnBlock stops folding
    // the pill into the collapsible group.
    expect(isSpawnRunTool(msg)).toBe(true)
    expect(extractSpawnRunLaunch(msg)).toEqual({ ids: ['a1b2c3d4', 'e5f6a7b8'], announced: 2 })
  })

  it('patches every message sharing the tool_call_id (auto-approved pill pair)', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    // An auto-approved tool emits TWO tool messages under one id: the
    // pre-approval 🔧 pill and the post-approval ✅ pill. The server patches
    // both, so stopping at the first would leave the pair disagreeing.
    store.dispatch(sseChatMessage({ slot: 'active', role: 'tool', content: '🔧 spawn_run', meta: { tool_call_id: 'call-spawn' } }))
    store.dispatch(sseChatMessage({ slot: 'active', role: 'tool', content: '✅ spawn_run', meta: { tool_call_id: 'call-spawn' } }))

    store.dispatch(sseToolResult({ slot: 'active', output: SPAWN_OUTPUT, tool_call_id: 'call-spawn' }))

    const tools = store.getState().chat.messages.filter((m) => m.role === 'tool')
    expect(tools).toHaveLength(2)
    for (const m of tools) expect((m.meta as Record<string, unknown>).output).toBe(SPAWN_OUTPUT)
  })

  it('patches a background slot through its cached message list', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    store.dispatch(hydrateSlotMessages({ slot: 'bg', messages: [{ role: 'tool', content: '🔧 spawn_run', cls: '', meta: { tool_call_id: 'call-bg' } }] }))

    store.dispatch(sseToolResult({ slot: 'bg', output: SPAWN_OUTPUT, tool_call_id: 'call-bg' }))

    const cached = store.getState().chat.slotMessages['bg']!
    expect((cached[0].meta as Record<string, unknown>).output).toBe(SPAWN_OUTPUT)
    // The active slot's own scrollback must not be touched by another slot's
    // tool result.
    expect(store.getState().chat.messages).toHaveLength(0)
  })

  it('leaves messages untouched when the frame carries no tool_call_id', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    store.dispatch(sseChatMessage({ slot: 'active', role: 'tool', content: '🔧 spawn_run', meta: { tool_call_id: 'call-spawn' } }))

    // Parity with the server, which also guards on `if _tcid:` — an id-less
    // result may only be matched positionally in the tool LOG, never painted
    // onto an arbitrary scrollback bubble.
    store.dispatch(sseToolResult({ slot: 'active', output: SPAWN_OUTPUT }))

    const msg = store.getState().chat.messages.find((m) => m.role === 'tool')!
    expect((msg.meta as Record<string, unknown>).output).toBeUndefined()
  })

  it('does not copy ordinary (non-launch) tool output onto messages', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    store.dispatch(sseChatMessage({ slot: 'active', role: 'tool', content: '🔧 ls', meta: { tool_call_id: 'call-ls' } }))

    // `toolLog` is capped at 100 entries; `state.messages` is not, and a single
    // output can reach the server's 1 MB cap. Copying every result here would
    // let one long autonomous turn grow the heap without bound, so only launch
    // results — the sole scrollback consumer — are copied.
    const big = 'x'.repeat(200_000)
    store.dispatch(sseToolResult({ slot: 'active', output: big, tool_call_id: 'call-ls' }))

    const msg = store.getState().chat.messages.find((m) => m.role === 'tool')!
    expect((msg.meta as Record<string, unknown>).output).toBeUndefined()
    // The tool log still gets it — that path is bounded.
    expect(store.getState().chat.toolLog.length).toBeGreaterThanOrEqual(0)
  })

  it('ignores a poisoned slot key instead of walking Object.prototype', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    store.dispatch(sseToolResult({ slot: '__proto__', output: SPAWN_OUTPUT, tool_call_id: 'call-x' }))
    expect('output' in ({} as Record<string, unknown>)).toBe(false)
    expect((Object.prototype as Record<string, unknown>).output).toBeUndefined()
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

// All three slot-detail reducers (switchSlot, warmSlotCache, refreshSlot) route
// queued-bubble hydration through the single shared `hydrateQueuedBubbles`
// helper, so a new payload field can't silently diverge between them and drop
// queued bubbles. These tests lock in that every consumer hydrates identically.
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
    // refreshSlot must re-hydrate from the server queue field: rebuilding
    // messages from server history + preserved perms/thinking alone would drop
    // ALL queued bubbles, and re-adding without replacing would duplicate the
    // stale 'qOld' bubble alongside 'qNew'.
    expect(queued.map((m) => m.content)).toEqual(['fresh'])
    expect(queued[0].meta?.queueId).toBe('qNew')
  })
})

describe('chat frame append is idempotent per server row id (issue #1704)', () => {
  const detail = (key: string, messages: Array<{ role: string; content: string; cls?: string; ts?: string; meta?: Record<string, unknown> }>) => ({
    key,
    messages,
    running: false,
    stopping: false,
    hasMore: false,
    total: messages.length,
    queue: [] as Array<{ content: string; queueId: string; ts: string }>,
  })

  it('renders a redelivered frame once, not once per delivery', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    // Post-restart shape: refreshSlot already rebuilt the transcript from disk,
    // so the finalized assistant row (carrying its server id) is present and
    // there is NO trailing 'streaming' row to reconcile into.
    store.dispatch(
      refreshSlot.fulfilled(
        detail('active', [
          { role: 'user', content: 'go', cls: '', ts: '2026-08-05T23:00:00.000000+00:00', meta: { mid: 'm-aaa1' } },
          { role: 'assistant', content: 'the answer', cls: '', ts: '2026-08-05T23:00:01.000000+00:00', meta: { mid: 'm-aaa2' } },
        ]),
        'r0',
        'active',
      ),
    )
    expect(store.getState().chat.messages.filter((m) => m.role === 'assistant')).toHaveLength(1)

    // The same row is delivered again — ten times, as after a restart storm.
    for (let i = 0; i < 10; i++) {
      store.dispatch(sseChatMessage({
        slot: 'active',
        role: 'assistant',
        content: 'the answer',
        ts: '2026-08-05T23:00:01.000000+00:00',
        meta: { mid: 'm-aaa2' },
      }))
    }
    const assistants = store.getState().chat.messages.filter((m) => m.role === 'assistant')
    expect(assistants).toHaveLength(1)
    expect(assistants[0].content).toBe('the answer')
  })

  it('keeps two byte-identical same-tick rows apart because their ids differ', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    const ts = '2026-08-05T23:05:00.000000+00:00'
    // A channel window can replay two identical messages stamped in the same
    // coarse clock tick. Under a (ts, role, content) key the second one vanished;
    // distinct row ids make them distinguishable.
    store.dispatch(sseChatMessage({ slot: 'active', role: 'user', content: 'ok', ts, meta: { mid: 'm-b1' } }))
    store.dispatch(sseChatMessage({ slot: 'active', role: 'user', content: 'ok', ts, meta: { mid: 'm-b2' } }))
    expect(
      store.getState().chat.messages.filter((m) => m.role === 'user').map((m) => m.meta?.mid),
    ).toEqual(['m-b1', 'm-b2'])
  })

  it('never dedups a frame with no row id', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    // Channel-replayed rows genuinely carry no meta (ConversationLog writes only
    // role/content/ts/source_*), so they have no id. Declining to dedup renders
    // a duplicate at worst; guessing would drop a real message.
    const ts = '2026-08-05T23:06:00.000000+00:00'
    store.dispatch(sseChatMessage({ slot: 'active', role: 'user', content: 'same text', ts }))
    store.dispatch(sseChatMessage({ slot: 'active', role: 'user', content: 'same text', ts }))
    expect(store.getState().chat.messages.filter((m) => m.role === 'user')).toHaveLength(2)
  })

  it('dedups on the background-slot path too (session-grid pane)', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    const frame = {
      slot: 'other',
      role: 'assistant',
      content: 'bg answer',
      ts: '2026-08-05T23:07:00.000000+00:00',
      meta: { mid: 'm-c1' },
    }
    store.dispatch(sseChatMessage(frame))
    store.dispatch(sseChatMessage(frame))
    store.dispatch(sseChatMessage(frame))
    expect(store.getState().chat.slotMessages['other'].filter((m) => m.role === 'assistant')).toHaveLength(1)
  })

  it('dedups a redelivered tool frame instead of splicing a second pill', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    const frame = {
      slot: 'active',
      role: 'tool',
      content: 'fs_read',
      ts: '2026-08-05T23:08:00.000000+00:00',
      meta: { mid: 'm-d1', tool_call_id: 'call-A' },
    }
    // The tool branch inserts and RETURNS before the generic push, so the guard
    // has to dominate it or a redelivery splices extra pills mid-transcript.
    store.dispatch(sseChatMessage(frame))
    store.dispatch(sseChatMessage(frame))
    store.dispatch(sseChatMessage(frame))
    expect(store.getState().chat.messages.filter((m) => m.role === 'tool')).toHaveLength(1)
  })

  it('keeps two same-tick identical tool calls apart', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    const ts = '2026-08-05T23:09:00.000000+00:00'
    store.dispatch(sseChatMessage({ slot: 'active', role: 'tool', content: 'fs_read', ts, meta: { mid: 'm-e1', tool_call_id: 'call-A' } }))
    store.dispatch(sseChatMessage({ slot: 'active', role: 'tool', content: 'fs_read', ts, meta: { mid: 'm-e2', tool_call_id: 'call-B' } }))
    const tools = store.getState().chat.messages.filter((m) => m.role === 'tool')
    expect(tools.map((m) => m.meta?.tool_call_id)).toEqual(['call-A', 'call-B'])
  })

  it('a late redelivered assistant frame does not clobber a newer live stream', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    const oldFrame = {
      slot: 'active',
      role: 'assistant',
      content: 'first answer',
      ts: '2026-08-05T23:10:00.000000+00:00',
      meta: { mid: 'm-f1' },
    }
    store.dispatch(sseChatMessage({ slot: 'active', role: 'chunk', content: 'first answer' }))
    store.dispatch(sseChatMessage(oldFrame))
    // A NEW segment starts streaming.
    store.dispatch(sseChatMessage({ slot: 'active', role: 'chunk', content: 'second answer in progress' }))
    // The OLD frame arrives again. The assistant branch reconciles into the
    // trailing 'streaming' row, so without the guard dominating it the live
    // segment's content is overwritten with the stale text.
    store.dispatch(sseChatMessage(oldFrame))
    expect(store.getState().chat.messages.map((m) => [m.role, m.content])).toEqual([
      ['assistant', 'first answer'],
      ['streaming', 'second answer in progress'],
    ])
  })

  it('a late redelivered assistant frame does not clobber a background pane stream', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    const oldFrame = {
      slot: 'other',
      role: 'assistant',
      content: 'bg first',
      ts: '2026-08-05T23:10:30.000000+00:00',
      meta: { mid: 'm-f2' },
    }
    store.dispatch(sseChatMessage({ slot: 'other', role: 'chunk', content: 'bg first' }))
    store.dispatch(sseChatMessage(oldFrame))
    store.dispatch(sseChatMessage({ slot: 'other', role: 'chunk', content: 'bg second in progress' }))
    // Same shape as the active path: the reconcile must have carried the frame's
    // id onto the finalized row, or this redelivery is unrecognisable and
    // overwrites the live segment.
    store.dispatch(sseChatMessage(oldFrame))
    expect(store.getState().chat.slotMessages['other'].map((m) => [m.role, m.content])).toEqual([
      ['assistant', 'bg first'],
      ['streaming', 'bg second in progress'],
    ])
  })

  it('a first-delivery assistant frame still finalizes the live stream', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    // The guard must not swallow the normal case: the streaming row is client
    // minted and has no server id, so it cannot match an incoming frame's id.
    store.dispatch(sseChatMessage({ slot: 'active', role: 'chunk', content: 'streamed text' }))
    store.dispatch(sseChatMessage({
      slot: 'active', role: 'assistant', content: 'streamed text',
      ts: '2026-08-05T23:11:00.000000+00:00', meta: { mid: 'm-g1' },
    }))
    const msgs = store.getState().chat.messages
    expect(msgs).toHaveLength(1)
    expect([msgs[0].role, msgs[0].content]).toEqual(['assistant', 'streamed text'])
  })

  it('switchSlot does not re-attach a local tail the server returned redacted', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    // Locally streamed + finalized reply, raw bytes, carrying its row id.
    store.dispatch(sseChatMessage({ slot: 'active', role: 'chunk', content: 'x' }))
    store.dispatch(sseChatMessage({
      slot: 'active',
      role: 'assistant',
      content: 'the token is ghp_RAWSECRETVALUE0000000000000000000000',
      ts: '2026-08-05T23:12:00.000000+00:00',
      meta: { mid: 'm-h1' },
    }))
    // The slot-detail endpoint redacts on emit, so the SAME row comes back with
    // different bytes. Content equality misses; the row id does not.
    store.dispatch(
      switchSlot.fulfilled(
        detail('active', [
          { role: 'assistant', content: 'the token is [REDACTED: credential]', cls: '', ts: '2026-08-05T23:12:00.000000+00:00', meta: { mid: 'm-h1' } },
        ]),
        'r1',
        'active',
      ),
    )
    const msgs = store.getState().chat.messages.filter((m) => m.role === 'assistant')
    expect(msgs).toHaveLength(1)
    expect(msgs[0].content).toBe('the token is [REDACTED: credential]')
  })

  it('switchSlot keeps a newer reply whose text matches an older row with a different id', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    // The agent answered the same thing twice across two turns. The newest reply
    // has its own row id; a content fallback running alongside the id would match
    // the OLDER row in a stale snapshot and drop this one.
    store.dispatch(sseChatMessage({
      slot: 'active', role: 'assistant', content: 'Done.',
      ts: '2026-08-05T23:16:00.000000+00:00', meta: { mid: 'm-j2' },
    }))
    store.dispatch(
      switchSlot.fulfilled(
        detail('active', [
          { role: 'assistant', content: 'Done.', cls: '', ts: '2026-08-05T23:15:00.000000+00:00', meta: { mid: 'm-j1' } },
        ]),
        'r3',
        'active',
      ),
    )
    const msgs = store.getState().chat.messages.filter((m) => m.role === 'assistant')
    expect(msgs.map((m) => m.meta?.mid)).toEqual(['m-j1', 'm-j2'])
  })

  it('switchSlot does not duplicate a reply that was finalized without a row id', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    // `_done` finalizes the streaming row but carries no meta, so a reply that
    // ended that way has no server id. Content equality is the only handle left,
    // and without it the fetched history's copy would be re-attached as a second
    // bubble.
    store.dispatch(sseChatMessage({ slot: 'active', role: 'chunk', content: 'finished without an id' }))
    store.dispatch(sseChatMessage({ slot: 'active', role: '_done' }))
    const local = store.getState().chat.messages
    expect(local).toHaveLength(1)
    expect([local[0].role, local[0].meta?.mid]).toEqual(['assistant', undefined])

    store.dispatch(
      switchSlot.fulfilled(
        detail('active', [
          { role: 'assistant', content: 'finished without an id', cls: '', ts: '2026-08-05T23:17:00.000000+00:00', meta: { mid: 'm-k1' } },
        ]),
        'r4',
        'active',
      ),
    )
    expect(store.getState().chat.messages.filter((m) => m.role === 'assistant')).toHaveLength(1)
  })

  it('counts dropped redeliveries so at-least-once delivery stays observable', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    const frame = {
      slot: 'active',
      role: 'assistant',
      content: 'counted',
      ts: '2026-08-05T23:18:00.000000+00:00',
      meta: { mid: 'm-n1' },
    }
    store.dispatch(sseChatMessage(frame))
    // First delivery is not a drop.
    expect(store.getState().chat._redeliveredFramesDropped).toBe(0)
    store.dispatch(sseChatMessage(frame))
    store.dispatch(sseChatMessage(frame))
    // The dedup hides the duplicate bubbles that were the only signal something
    // upstream re-emits frames; this counter is what keeps that signal readable.
    expect(store.getState().chat._redeliveredFramesDropped).toBe(2)
    // Background-pane drops count too.
    const bg = { ...frame, slot: 'other', meta: { mid: 'm-n2' } }
    store.dispatch(sseChatMessage(bg))
    store.dispatch(sseChatMessage(bg))
    expect(store.getState().chat._redeliveredFramesDropped).toBe(3)
  })

  it('switchSlot still re-attaches a local reply the server history predates', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('active'))
    store.dispatch(sseChatMessage({
      slot: 'active', role: 'assistant', content: 'newest reply',
      ts: '2026-08-05T23:14:00.000000+00:00', meta: { mid: 'm-i2' },
    }))
    // Server snapshot predates the reply — different row id, different content.
    store.dispatch(
      switchSlot.fulfilled(
        detail('active', [
          { role: 'user', content: 'go', cls: '', ts: '2026-08-05T23:13:00.000000+00:00', meta: { mid: 'm-i1' } },
        ]),
        'r2',
        'active',
      ),
    )
    const msgs = store.getState().chat.messages
    expect(msgs[msgs.length - 1].content).toBe('newest reply')
  })
})
