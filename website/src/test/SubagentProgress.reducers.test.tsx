/**
 * Sub-agent progress reducers: the running-card is driven by
 * chat.subagents. These verify the progress fields land:
 *  - sseSubagentTool records toolCount (fired on EVENT_TOOL_CALL too, so a
 *    simple/auto-approved task shows live tool activity) and clears stalled.
 *  - sseSubagentStalled toggles the idle/stalled flag surfaced by the reaper,
 *    and retains the `idle_secs` span that justifies it.
 *  - sseSubagentSnapshot (the reconnect replay path) carries that same span, so
 *    a socket blip during an active stall does not degrade the badge.
 */
import { describe, it, expect } from 'vitest'
import { createTestStore } from './helpers'
import { setActiveSlot, sseSubagentSpawn, sseSubagentTool, sseSubagentStalled, sseSubagentSnapshot } from '../store/chatSlice'

const ID = 'a1b2c3d4'
const SNAPSHOT_SLOT = 'chat-subagent-progress'

function snapshotStore() {
  const store = createTestStore()
  store.dispatch(setActiveSlot(SNAPSHOT_SLOT))
  return store
}

function spawn(store: ReturnType<typeof createTestStore>) {
  const SLOT = store.getState().chat.activeSlot
  store.dispatch(sseSubagentSpawn({ slot: SLOT, id: ID, task: 'do a thing', agent: 'kirocrew' }))
  return SLOT
}
const sub = (store: ReturnType<typeof createTestStore>) => store.getState().chat.subagents[ID]

describe('subagent progress reducers', () => {
  it('sseSubagentTool records tool name + toolCount and sets status tool', () => {
    const store = createTestStore()
    const SLOT = spawn(store)
    expect(sub(store).toolCount).toBe(0)
    store.dispatch(sseSubagentTool({ slot: SLOT, id: ID, tool: 'fs_read', turns: 0, tool_count: 3 }))
    const a = sub(store)
    expect(a.status).toBe('tool')
    expect(a.lastTool).toBe('fs_read')
    expect(a.toolCount).toBe(3)
  })

  it('sseSubagentStalled toggles the stalled flag, and tool activity clears it', () => {
    const store = createTestStore()
    const SLOT = spawn(store)
    store.dispatch(sseSubagentStalled({ slot: SLOT, id: ID, stalled: true, idle_secs: 200 }))
    expect(sub(store).stalled).toBe(true)
    // Fresh activity clears the stalled state.
    store.dispatch(sseSubagentTool({ slot: SLOT, id: ID, tool: 'grep', tool_count: 4 }))
    expect(sub(store).stalled).toBe(false)
  })

  it('sseSubagentStalled retains idle_secs so the card can justify the warning', () => {
    const store = createTestStore()
    const SLOT = spawn(store)
    store.dispatch(sseSubagentStalled({ slot: SLOT, id: ID, stalled: true, idle_secs: 196 }))
    // The idle span is the figure that justifies the badge — total elapsed does
    // not. The backend already sent it; this reducer used to drop it.
    expect(sub(store).idleSecs).toBe(196)
  })

  it('clears idleSecs when the agent un-stalls or resumes work', () => {
    const store = createTestStore()
    const SLOT = spawn(store)
    store.dispatch(sseSubagentStalled({ slot: SLOT, id: ID, stalled: true, idle_secs: 196 }))
    // Explicit un-stall frame (backend _touch_activity path).
    store.dispatch(sseSubagentStalled({ slot: SLOT, id: ID, stalled: false }))
    expect(sub(store).stalled).toBe(false)
    expect(sub(store).idleSecs).toBeUndefined()
    // And again via resumed tool activity, so a stale "no activity for 196s"
    // can never outlive the quiet stretch that produced it.
    store.dispatch(sseSubagentStalled({ slot: SLOT, id: ID, stalled: true, idle_secs: 300 }))
    store.dispatch(sseSubagentTool({ slot: SLOT, id: ID, tool: 'grep', tool_count: 5 }))
    expect(sub(store).idleSecs).toBeUndefined()
  })

  // ── Reconnect replay (#3929) ────────────────────────────────────────────
  // The snapshot frame carries `stalled` but used to omit the idle span, so a
  // reconnect during an active stall dropped the row to the plain fallback
  // wording that was only ever meant for a gateway too old to send the field.

  const snap = (over: Record<string, unknown> = {}) => ({
    id: ID, slot: '', task: 'do a thing', agent: 'kirocrew',
    streaming: '', last_tool: 'Running: sleep 600', started: 1000,
    tool_count: 3, ...over,
  })

  it('sseSubagentSnapshot keeps the idle span across a reconnect', () => {
    const store = snapshotStore()
    const SLOT = SNAPSHOT_SLOT
    store.dispatch(sseSubagentSnapshot(snap({ slot: SLOT, stalled: true, idle_secs: 117 })))
    const a = sub(store)
    expect(a.stalled).toBe(true)
    expect(a.idleSecs).toBe(117)
    // stalledAt is the receipt instant the row advances the figure from, the
    // same way the live frame sets it.
    expect(typeof a.stalledAt).toBe('number')
  })

  it('sseSubagentSnapshot leaves a healthy row without an idle span', () => {
    const store = snapshotStore()
    const SLOT = SNAPSHOT_SLOT
    store.dispatch(sseSubagentSnapshot(snap({ slot: SLOT, stalled: false, idle_secs: 117 })))
    const a = sub(store)
    expect(a.stalled).toBe(false)
    expect(a.idleSecs).toBeUndefined()
    expect(a.stalledAt).toBeUndefined()
  })

  it('a gateway that omits idle_secs still reaches the plain fallback', () => {
    // The fallback wording must stay reachable: an older gateway sends
    // `stalled` with no span, and the row should say "no activity" rather than
    // "no activity for undefineds".
    const store = snapshotStore()
    const SLOT = SNAPSHOT_SLOT
    store.dispatch(sseSubagentSnapshot(snap({ slot: SLOT, stalled: true })))
    const a = sub(store)
    expect(a.stalled).toBe(true)
    expect(a.idleSecs).toBeUndefined()
    expect(a.stalledAt).toBeUndefined()
  })
})
