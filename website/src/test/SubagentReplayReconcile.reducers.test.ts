/**
 * A reconnect replay reconciles into the sub-agent cards the tab holds.
 *
 * Every (re)connect sends `subscribe_subagents`, and the gateway replays every
 * running and finished run, then `subagent_replay_done`. A replay that repeats
 * what a card shows keeps the card object, so the card and every whole-map
 * reader stay as they were; a card the replay does not name is dropped when it
 * ends; a pending spawn approval, which nothing replays, survives.
 */
import { describe, it, expect } from 'vitest'
import { createTestStore } from './helpers'
import type { SubagentActivity } from '../types'
import {
  setActiveSlot,
  sseSubagentSpawn,
  sseSubagentTool,
  sseSubagentDone,
  sseSubagentSnapshot,
  sseSubagentPending,
  sseSubagentQueued,
  sseSubagentRetrying,
  clearSubagentsForSnapshot,
  subagentReplayDone,
} from '../store/chatSlice'

const ACTIVE = 'chat-active'
const OTHER = 'chat-other'
const STARTED = 1_790_000_000

type Store = ReturnType<typeof createTestStore>

/** The card's tool calls, newest last: the tool rows of its live transcript,
 *  read as `{tool, id}` so a row's `seq`, input and output stay out of the
 *  comparison. */
const calls = (card: SubagentActivity | undefined) =>
  card?.transcript?.flatMap(r => (r.kind === 'tool' ? [r.id ? { tool: r.tool, id: r.id } : { tool: r.tool }] : []))

const snapshot = (id: string, slot = ACTIVE, extra: Record<string, unknown> = {}) => sseSubagentSnapshot({
  id, slot, task: `task ${id}`, agent: 'kirocrew', streaming: '', last_tool: 'shell', started: STARTED, tool_count: 2, ...extra,
})

/** The view after a first replay: two running cards, one finished card. */
function replayed(): Store {
  const s = createTestStore()
  s.dispatch(setActiveSlot(ACTIVE))
  s.dispatch(clearSubagentsForSnapshot())
  s.dispatch(snapshot('run-a'))
  s.dispatch(snapshot('run-b', OTHER))
  s.dispatch(sseSubagentDone({ slot: ACTIVE, id: 'done-c', elapsed: 4, task: 'task done-c', agent: 'kirocrew', outcome: 'completed' }))
  s.dispatch(subagentReplayDone())
  return s
}

/** The same replay again, as a reconnect sends it. */
function replayAgain(s: Store) {
  s.dispatch(clearSubagentsForSnapshot())
  s.dispatch(snapshot('run-a'))
  s.dispatch(snapshot('run-b', OTHER))
  s.dispatch(sseSubagentDone({ slot: ACTIVE, id: 'done-c', elapsed: 4, task: 'task done-c', agent: 'kirocrew', outcome: 'completed' }))
  s.dispatch(subagentReplayDone())
}

describe('reconnect replay reconcile', () => {
  it('keeps every map and card object when the replay repeats them', () => {
    const s = replayed()
    const before = s.getState().chat
    replayAgain(s)
    const after = s.getState().chat
    expect(after.subagents).toBe(before.subagents)
    expect(after.subagents['run-a']).toBe(before.subagents['run-a'])
    expect(after.subagents['done-c']).toBe(before.subagents['done-c'])
    expect(after.slotActivity[OTHER].subagents).toBe(before.slotActivity[OTHER].subagents)
    expect(after.subagentQueued).toBe(before.subagentQueued)
    expect(after.subagentQueuedReason).toBe(before.subagentQueuedReason)
  })

  it('keeps a running card and its tool-call timeline across the reconnect', () => {
    const s = replayed()
    s.dispatch(sseSubagentTool({ slot: ACTIVE, id: 'run-a', tool: 'fs_read', tool_count: 3 }))
    s.dispatch(sseSubagentTool({ slot: ACTIVE, id: 'run-a', tool: 'fs_write', tool_count: 4 }))
    const timeline = calls(s.getState().chat.subagents['run-a'])?.map(c => c.tool)
    s.dispatch(clearSubagentsForSnapshot())
    // The card stays on screen while the replay is in flight.
    expect(s.getState().chat.subagents['run-a']).toBeDefined()
    s.dispatch(snapshot('run-a', ACTIVE, { last_tool: 'fs_write', tool_count: 4 }))
    s.dispatch(subagentReplayDone())
    const card = s.getState().chat.subagents['run-a']
    expect(card.lastTool).toBe('fs_write')
    expect(calls(card)?.map(c => c.tool)).toEqual(timeline)
  })

  it('appends a tool that started while the socket was down, so it is the current row', () => {
    const s = replayed()
    s.dispatch(sseSubagentTool({ slot: ACTIVE, id: 'run-a', tool: 'fs_read', tool_count: 3 }))
    s.dispatch(clearSubagentsForSnapshot())
    s.dispatch(snapshot('run-a', ACTIVE, { last_tool: 'shell', tool_count: 4 }))
    s.dispatch(subagentReplayDone())
    const tools = calls(s.getState().chat.subagents['run-a'])?.map(c => c.tool)
    expect(tools?.slice(-2)).toEqual(['fs_read', 'shell'])
    // Replaying the same snapshot again adds nothing.
    const held = s.getState().chat.subagents['run-a']
    s.dispatch(clearSubagentsForSnapshot())
    s.dispatch(snapshot('run-a', ACTIVE, { last_tool: 'shell', tool_count: 4 }))
    s.dispatch(subagentReplayDone())
    expect(s.getState().chat.subagents['run-a']).toBe(held)
  })

  it('appends a changed tool whose count did not move, such as a pending permission request', () => {
    const s = replayed()
    s.dispatch(sseSubagentTool({ slot: ACTIVE, id: 'run-a', tool: 'fs_read', tool_count: 3, tool_call_id: 'tc-1' }))
    s.dispatch(clearSubagentsForSnapshot())
    s.dispatch(snapshot('run-a', ACTIVE, { last_tool: 'invoke_sub_agent', tool_count: 3, last_tool_call_id: 'tc-2' }))
    s.dispatch(subagentReplayDone())
    const tools = calls(s.getState().chat.subagents['run-a'])?.map(c => c.tool)
    expect(tools?.at(-1)).toBe('invoke_sub_agent')
  })

  it('updates the newest row title when replay refines the same call id', () => {
    const s = createTestStore()
    s.dispatch(setActiveSlot(ACTIVE))
    s.dispatch(snapshot('run-a', ACTIVE, { last_tool: 'shell', last_tool_call_id: 'tc-1' }))
    const held = s.getState().chat.subagents['run-a']
    expect(calls(held)).toEqual([{ tool: 'shell', id: 'tc-1' }])

    s.dispatch(clearSubagentsForSnapshot())
    s.dispatch(snapshot('run-a', ACTIVE, { last_tool: 'fs_read', last_tool_call_id: 'tc-1' }))
    s.dispatch(subagentReplayDone())
    const reconciled = calls(s.getState().chat.subagents['run-a'])
    expect(reconciled).toHaveLength(1)
    expect(reconciled?.at(-1)).toEqual({ tool: 'fs_read', id: 'tc-1' })
    // The held card's rows were copied, not rewritten in place.
    expect(calls(held)).toEqual([{ tool: 'shell', id: 'tc-1' }])
  })

  it('seeds a replay-created card with its call id and ignores an approval count bump', () => {
    const s = createTestStore()
    s.dispatch(setActiveSlot(ACTIVE))
    s.dispatch(snapshot('run-a', ACTIVE, { last_tool_call_id: 'tc-1' }))
    const held = s.getState().chat.subagents['run-a'].transcript
    expect(calls(s.getState().chat.subagents['run-a'])).toEqual([{ tool: 'shell', id: 'tc-1' }])

    s.dispatch(clearSubagentsForSnapshot())
    s.dispatch(snapshot('run-a', ACTIVE, { tool_count: 3, last_tool_call_id: 'tc-1' }))
    s.dispatch(subagentReplayDone())
    expect(s.getState().chat.subagents['run-a'].transcript).toBe(held)
  })

  it('an id-bearing replay claims the id-less row an older replay seeded for the same call', () => {
    // The transcript's rule for an id-less seeded row (`recordTranscriptTool`):
    // the first id-bearing frame with its title is that call, not a second one.
    const s = replayed()
    const held = s.getState().chat.subagents['run-a'].transcript
    expect(calls(s.getState().chat.subagents['run-a'])).toEqual([{ tool: 'shell' }])

    s.dispatch(clearSubagentsForSnapshot())
    s.dispatch(snapshot('run-a', ACTIVE, { last_tool_call_id: 'tc-1' }))
    s.dispatch(subagentReplayDone())
    const reconciled = s.getState().chat.subagents['run-a']
    expect(reconciled.transcript).not.toBe(held)
    expect(calls(reconciled)).toEqual([{ tool: 'shell', id: 'tc-1' }])

    s.dispatch(sseSubagentTool({ slot: ACTIVE, id: 'run-a', tool: 'shell', tool_count: 2, tool_call_id: 'tc-1' }))
    expect(calls(s.getState().chat.subagents['run-a'])).toEqual([{ tool: 'shell', id: 'tc-1' }])
  })

  it('adds no row when an approval moved the count under the same call id', () => {
    const s = replayed()
    s.dispatch(sseSubagentTool({ slot: ACTIVE, id: 'run-a', tool: 'shell', tool_count: 3, tool_call_id: 'tc-1' }))
    const held = s.getState().chat.subagents['run-a'].transcript
    s.dispatch(clearSubagentsForSnapshot())
    // Approving a child's request bumps tool_count and sends no frame.
    s.dispatch(snapshot('run-a', ACTIVE, { last_tool: 'shell', tool_count: 4, last_tool_call_id: 'tc-1' }))
    s.dispatch(subagentReplayDone())
    const card = s.getState().chat.subagents['run-a']
    expect(card.transcript).toBe(held)
    expect(card.toolCount).toBe(4)
  })

  it('appends a same-title call with a new id that ran while the socket was down', () => {
    const s = replayed()
    s.dispatch(sseSubagentTool({ slot: ACTIVE, id: 'run-a', tool: 'shell', tool_count: 3, tool_call_id: 'tc-1' }))
    const before = calls(s.getState().chat.subagents['run-a'])?.length ?? 0
    s.dispatch(clearSubagentsForSnapshot())
    s.dispatch(snapshot('run-a', ACTIVE, { last_tool: 'shell', tool_count: 4, last_tool_call_id: 'tc-2' }))
    s.dispatch(subagentReplayDone())
    const reconciled = calls(s.getState().chat.subagents['run-a'])
    expect(reconciled?.length).toBe(before + 1)
    expect(reconciled?.at(-1)).toEqual({ tool: 'shell', id: 'tc-2' })
  })

  it('keeps the newer live call when a replay snapshot collected before it lands after it', () => {
    const s = replayed()
    s.dispatch(sseSubagentTool({ slot: ACTIVE, id: 'run-a', tool: 'fs_read', tool_count: 3, tool_call_id: 'tc-1' }))
    s.dispatch(clearSubagentsForSnapshot())
    // A live call-2 frame overtakes the replay's already-collected call-1 snapshot.
    s.dispatch(sseSubagentTool({ slot: ACTIVE, id: 'run-a', tool: 'fs_write', tool_count: 4, tool_call_id: 'tc-2' }))
    const held = s.getState().chat.subagents['run-a']
    s.dispatch(snapshot('run-a', ACTIVE, { last_tool: 'fs_read', tool_count: 3, last_tool_call_id: 'tc-1' }))
    s.dispatch(subagentReplayDone())
    const card = s.getState().chat.subagents['run-a']
    expect(card).toBe(held)
    expect(calls(card)?.slice(-2)).toEqual([{ tool: 'fs_read', id: 'tc-1' }, { tool: 'fs_write', id: 'tc-2' }])
    expect(card.lastTool).toBe('fs_write')
    expect(card.toolCount).toBe(4)
  })

  it('hydrates a live-created card from a stale snapshot and keeps its newer tool state', () => {
    const s = createTestStore()
    s.dispatch(setActiveSlot(ACTIVE))
    s.dispatch(clearSubagentsForSnapshot())
    // A new tab: the live frame at count 3 overtakes the snapshot collected at count 2.
    s.dispatch(sseSubagentTool({ slot: ACTIVE, id: 'run-new', tool: 'fs_write', tool_count: 3, tool_call_id: 'tc-3' }))
    expect(s.getState().chat.subagents['run-new'].startedAtAssumed).toBe(true)
    s.dispatch(snapshot('run-new', ACTIVE, { agent: 'worker', label: 'Worker', last_tool: 'shell', tool_count: 2, last_tool_call_id: 'tc-2' }))
    s.dispatch(subagentReplayDone())
    const card = s.getState().chat.subagents['run-new']
    expect(card.task).toBe('task run-new')
    expect(card.agent).toBe('worker')
    expect(card.label).toBe('Worker')
    expect(card.startedAt).toBe(STARTED * 1000)
    expect(card.startedAtAssumed).toBeUndefined()
    expect(card.lastTool).toBe('fs_write')
    expect(card.toolCount).toBe(3)
    expect(calls(card)).toEqual([{ tool: 'fs_write', id: 'tc-3' }])
  })

  it('keeps the newer live call when an id-less stale snapshot carries a lower count', () => {
    const s = replayed()
    s.dispatch(clearSubagentsForSnapshot())
    s.dispatch(sseSubagentTool({ slot: ACTIVE, id: 'run-a', tool: 'fs_write', tool_count: 3 }))
    const held = s.getState().chat.subagents['run-a']
    s.dispatch(snapshot('run-a', ACTIVE, { last_tool: 'shell', tool_count: 2 }))
    s.dispatch(subagentReplayDone())
    expect(s.getState().chat.subagents['run-a']).toBe(held)
  })

  it('applies a current snapshot whose call id also sits earlier in the timeline', () => {
    const s = replayed()
    // Parallel calls, then permission frames that repeat each id at an unmoved count.
    s.dispatch(sseSubagentTool({ slot: ACTIVE, id: 'run-a', tool: 'shell', tool_count: 3, tool_call_id: 'c1' }))
    s.dispatch(sseSubagentTool({ slot: ACTIVE, id: 'run-a', tool: 'fs_read', tool_count: 4, tool_call_id: 'c2' }))
    s.dispatch(sseSubagentTool({ slot: ACTIVE, id: 'run-a', tool: 'shell', tool_count: 4, tool_call_id: 'c1' }))
    s.dispatch(sseSubagentTool({ slot: ACTIVE, id: 'run-a', tool: 'fs_read', tool_count: 4, tool_call_id: 'c2' }))
    s.dispatch(clearSubagentsForSnapshot())
    s.dispatch(snapshot('run-a', ACTIVE, { last_tool: 'fs_read', tool_count: 4, last_tool_call_id: 'c2', streaming: 'new text', stalled: true, idle_secs: 30 }))
    s.dispatch(subagentReplayDone())
    const card = s.getState().chat.subagents['run-a']
    expect(card.streaming).toBe('new text')
    expect(card.stalled).toBe(true)
  })

  it('replaces a card whose replay differs', () => {
    const s = replayed()
    const before = s.getState().chat.subagents['run-a']
    s.dispatch(clearSubagentsForSnapshot())
    s.dispatch(snapshot('run-a', ACTIVE, { streaming: 'new text' }))
    s.dispatch(subagentReplayDone())
    const after = s.getState().chat.subagents['run-a']
    expect(after).not.toBe(before)
    expect(after.streaming).toBe('new text')
  })

  it('drops the cards the replay does not name, in every slot, and keeps pending approvals', () => {
    const s = replayed()
    s.dispatch(sseSubagentPending({ slot: ACTIVE, id: 'spawn:p-1', task: 'awaiting', approval_id: 'ap-1' }))
    s.dispatch(clearSubagentsForSnapshot())
    s.dispatch(snapshot('run-a'))
    s.dispatch(subagentReplayDone())
    const chat = s.getState().chat
    expect(Object.keys(chat.subagents).sort()).toEqual(['run-a', 'spawn:p-1'])
    expect(chat.slotActivity[OTHER].subagents).toEqual({})
  })

  it('keeps the cards on screen until the replay ends', () => {
    const s = replayed()
    s.dispatch(clearSubagentsForSnapshot())
    expect(Object.keys(s.getState().chat.subagents).sort()).toEqual(['done-c', 'run-a'])
    expect(Object.keys(s.getState().chat.slotActivity[OTHER].subagents)).toEqual(['run-b'])
  })

  it('treats a replay-done marker outside a replay as a no-op', () => {
    const s = replayed()
    s.dispatch(sseSubagentSpawn({ slot: ACTIVE, id: 'live-d', task: 'new', agent: 'kirocrew' }))
    const before = s.getState().chat
    s.dispatch(subagentReplayDone())
    expect(s.getState().chat.subagents).toBe(before.subagents)
  })

  it('clears a retry flag the replay cannot confirm, and keeps one set during the replay', () => {
    const s = replayed()
    s.dispatch(sseSubagentRetrying({ slot: ACTIVE, id: 'run-a' }))
    expect(s.getState().chat.subagents['run-a'].retrying).toBe(true)
    // The old socket missed the recovery; the replay says nothing about retries.
    s.dispatch(clearSubagentsForSnapshot())
    s.dispatch(snapshot('run-a'))
    expect(s.getState().chat.subagents['run-a'].retrying).toBeFalsy()
    // A retry frame on the new connection still lands.
    s.dispatch(sseSubagentRetrying({ slot: OTHER, id: 'run-b' }))
    s.dispatch(snapshot('run-b', OTHER))
    s.dispatch(subagentReplayDone())
    expect(s.getState().chat.slotActivity[OTHER].subagents['run-b'].retrying).toBe(true)
  })

  it('still resets queued counts a reconnect may have left stale', () => {
    const s = replayed()
    s.dispatch(sseSubagentQueued({ slot: ACTIVE, queued: 3 }))
    s.dispatch(clearSubagentsForSnapshot())
    expect(s.getState().chat.subagentQueued).toEqual({})
  })

  it('keeps the receipt instant of a stall the replay repeats', () => {
    const s = createTestStore()
    s.dispatch(setActiveSlot(ACTIVE))
    s.dispatch(snapshot('run-s', ACTIVE, { stalled: true, idle_secs: 90 }))
    const first = s.getState().chat.subagents['run-s']
    s.dispatch(clearSubagentsForSnapshot())
    s.dispatch(snapshot('run-s', ACTIVE, { stalled: true, idle_secs: 90 }))
    s.dispatch(subagentReplayDone())
    expect(s.getState().chat.subagents['run-s']).toBe(first)
  })
})
