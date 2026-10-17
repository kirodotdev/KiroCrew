/**
 * An incremental sub-agent frame must not vanish when the store holds no entry
 * for the agent it names.
 *
 * Between one `subagent_spawn` and one `subagent_done`, the incremental frames
 * (tool, streaming text, stalled, retrying) are the only evidence the panel
 * gets. When the container is missing they have nowhere to land, and the gap is
 * reachable in ordinary use rather than only in theory:
 * `clearSubagentsForSnapshot` keeps only `pending` entries across a reconnect,
 * so an agent already running at that moment loses its entry while every frame
 * it has left is an incremental one. These pin that such a frame creates the
 * card it needs, and that the prototype-pollution guard still refuses.
 */
import { describe, it, expect } from 'vitest'
import { createTestStore } from './helpers'
import {
  setActiveSlot,
  sseSubagentSpawn,
  sseSubagentTool,
  sseSubagentStalled,
  sseSubagentRetrying,
  sseSubagentBatchChunks,
  sseSubagentBatchUpdate,
  sseSubagentDone,
  sseSubagentSnapshot,
  clearSubagentsForSnapshot,
  markSubagentApproving,
} from '../store/chatSlice'

const ID = 'never-spawned-here'
const SLOT = 'chat-missed-spawn'

function store() {
  const s = createTestStore()
  s.dispatch(setActiveSlot(SLOT))
  return s
}
const sub = (s: ReturnType<typeof createTestStore>, id = ID) => s.getState().chat.subagents[id]

describe('an incremental frame for an unknown sub-agent', () => {
  it('creates the card a tool frame needs, instead of dropping the frame', () => {
    const s = store()
    expect(sub(s)).toBeUndefined()
    s.dispatch(sseSubagentTool({ slot: SLOT, id: ID, tool: 'fs_read', tool_count: 2 }))
    const a = sub(s)
    expect(a).toBeDefined()
    expect(a.status).toBe('tool')
    expect(a.lastTool).toBe('fs_read')
    expect(a.toolCount).toBe(2)
    expect(a.id).toBe(ID)
  })

  it('creates the card streaming text needs, and keeps the text', () => {
    const s = store()
    s.dispatch(sseSubagentBatchChunks({ chunks: [{ id: ID, slot: SLOT, text: 'partial output' }] }))
    expect(sub(s).streaming).toBe('partial output')
  })

  it('creates the card a stalled frame needs, with the idle span that justifies it', () => {
    const s = store()
    s.dispatch(sseSubagentStalled({ slot: SLOT, id: ID, stalled: true, idle_secs: 42 }))
    expect(sub(s).stalled).toBe(true)
    expect(sub(s).idleSecs).toBe(42)
  })

  it('creates the card a retrying frame needs', () => {
    const s = store()
    s.dispatch(sseSubagentRetrying({ slot: SLOT, id: ID, attempt: 2 }))
    expect(sub(s).retrying).toBe(true)
  })

  it('creates the card a coalesced batch update needs', () => {
    const s = store()
    s.dispatch(sseSubagentBatchUpdate({ updates: [{ id: ID, slot: SLOT, tool: 'shell', tool_count: 7 }] }))
    expect(sub(s).lastTool).toBe('shell')
    expect(sub(s).toolCount).toBe(7)
    expect(sub(s).status).toBe('tool')
  })

  it('leaves task and agent empty for a later frame to fill in', () => {
    const s = store()
    s.dispatch(sseSubagentTool({ slot: SLOT, id: ID, tool: 'fs_read' }))
    expect(sub(s).task).toBe('')
    expect(sub(s).agent).toBe('')
    s.dispatch(sseSubagentDone({ slot: SLOT, id: ID, elapsed: 3, outcome: 'completed', task: 'the real task', agent: 'kirocrew' }))
    expect(sub(s).task).toBe('the real task')
    expect(sub(s).agent).toBe('kirocrew')
  })

  it('makes a running agent visible again after a reconnect discards its entry', () => {
    // The whole point, end to end: spawn, lose the entry the way a reconnect
    // does, then deliver the next incremental frame the agent would send.
    const s = store()
    s.dispatch(sseSubagentSpawn({ slot: SLOT, id: ID, task: 'do a thing', agent: 'kirocrew' }))
    expect(sub(s)).toBeDefined()
    s.dispatch(clearSubagentsForSnapshot())
    expect(sub(s)).toBeUndefined()
    s.dispatch(sseSubagentTool({ slot: SLOT, id: ID, tool: 'fs_write' }))
    expect(sub(s)).toBeDefined()
    expect(sub(s).lastTool).toBe('fs_write')
  })

  it('files a frame for another slot under that slot, not the active one', () => {
    const s = store()
    s.dispatch(sseSubagentTool({ slot: 'chat-somewhere-else', id: ID, tool: 'fs_read' }))
    expect(sub(s)).toBeUndefined()
    expect(s.getState().chat.slotActivity['chat-somewhere-else'].subagents[ID].lastTool).toBe('fs_read')
  })
})

describe('an ownerless frame creates nothing', () => {
  // `slot: ''` reaches the store from a degraded spawn. Creating a bucket for it
  // would mint a session no session owns, and the global activity view would
  // report an owned running agent; nothing later removes it, because a snapshot
  // replay refuses the same input. `sseSubagentSnapshot` fails closed here and so
  // does the upsert.
  for (const reducer of ['tool', 'chunks', 'update'] as const) {
    it(`refuses an empty slot on the ${reducer} frame`, () => {
      const s = store()
      if (reducer === 'tool') {
        s.dispatch(sseSubagentTool({ slot: '', id: ID, tool: 'fs_read' }))
      } else if (reducer === 'chunks') {
        s.dispatch(sseSubagentBatchChunks({ chunks: [{ id: ID, slot: '', text: 'x' }] }))
      } else {
        s.dispatch(sseSubagentBatchUpdate({ updates: [{ id: ID, slot: '', tool: 'fs_read' }] }))
      }
      expect(sub(s)).toBeUndefined()
      expect(Object.prototype.hasOwnProperty.call(s.getState().chat.slotActivity, '')).toBe(false)
    })
  }

  it('leaves no bucket for any falsy-looking slot key', () => {
    const s = store()
    s.dispatch(sseSubagentTool({ slot: '', id: ID, tool: 'fs_read' }))
    expect(Object.keys(s.getState().chat.slotActivity)).not.toContain('')
  })
})

describe('the prototype-pollution guard still refuses', () => {
  for (const hostile of ['__proto__', 'constructor', 'prototype']) {
    it(`creates nothing for a "${hostile}" id`, () => {
      const s = store()
      s.dispatch(sseSubagentTool({ slot: SLOT, id: hostile, tool: 'fs_read' }))
      // Nothing reachable under that key, and Object.prototype is untouched.
      expect(Object.prototype.hasOwnProperty.call(s.getState().chat.subagents, hostile)).toBe(false)
      expect((Object.prototype as unknown as { lastTool?: string }).lastTool).toBeUndefined()
    })

    it(`creates nothing for a "${hostile}" slot`, () => {
      const s = store()
      s.dispatch(sseSubagentTool({ slot: hostile, id: ID, tool: 'fs_read' }))
      expect(Object.prototype.hasOwnProperty.call(s.getState().chat.slotActivity, hostile)).toBe(false)
    })
  }
})

describe('a frame that only decorates a card still requires one', () => {
  it('markSubagentApproving does not invent an agent', () => {
    // The control: not every reducer became creative. An approval toggle has
    // nothing to say about an agent it cannot find.
    const s = store()
    s.dispatch(markSubagentApproving({ id: ID, approving: true }))
    expect(sub(s)).toBeUndefined()
  })
})

describe('a minted entry does not assert a start time it never saw', () => {
  it('flags the assumed start time', () => {
    // The frame carries no start time, so the arrival instant is an assumption.
    // An agent may have been running for minutes before it arrived.
    const s = store()
    s.dispatch(sseSubagentTool({ slot: SLOT, id: ID, tool: 'fs_read' }))
    expect(sub(s).startedAtAssumed).toBe(true)
  })

  it('carries no flag on an entry whose spawn frame was seen', () => {
    const s = store()
    s.dispatch(sseSubagentSpawn({ slot: SLOT, id: ID, task: 'do a thing', agent: 'kirocrew' }))
    expect(sub(s).startedAtAssumed).toBeFalsy()
  })

  it('clears the flag when a snapshot supplies the real start', () => {
    const s = store()
    s.dispatch(sseSubagentTool({ slot: SLOT, id: ID, tool: 'fs_read' }))
    expect(sub(s).startedAtAssumed).toBe(true)
    s.dispatch(sseSubagentSnapshot({
      id: ID, slot: SLOT, task: 'do a thing', agent: 'kirocrew',
      streaming: '', last_tool: 'fs_read', started: 1_700_000_000,
    }))
    expect(sub(s).startedAtAssumed).toBeFalsy()
    expect(sub(s).startedAt).toBe(1_700_000_000 * 1000)
  })

  it('clears the flag when the done frame supplies real timing', () => {
    const s = store()
    s.dispatch(sseSubagentTool({ slot: SLOT, id: ID, tool: 'fs_read' }))
    s.dispatch(sseSubagentDone({ slot: SLOT, id: ID, elapsed: 300, outcome: 'completed' }))
    expect(sub(s).startedAtAssumed).toBeFalsy()
  })

  it('a late spawn frame cannot launder the assumption it reuses', () => {
    // The rebuild path reuses an existing startedAt. A spawn frame carries no
    // start time of its own, so reusing an assumed one keeps it assumed --
    // dropping the flag there would promote a guess to a fact.
    const s = store()
    s.dispatch(sseSubagentTool({ slot: SLOT, id: ID, tool: 'fs_read' }))
    const assumedAt = sub(s).startedAt
    s.dispatch(sseSubagentSpawn({ slot: SLOT, id: ID, task: 'do a thing', agent: 'kirocrew' }))
    expect(sub(s).startedAt).toBe(assumedAt)
    expect(sub(s).startedAtAssumed).toBe(true)
    expect(sub(s).task).toBe('do a thing')
  })
})
