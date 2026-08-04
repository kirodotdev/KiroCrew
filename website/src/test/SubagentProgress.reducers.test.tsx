/**
 * Sub-agent progress reducers: the running-card is driven by
 * chat.subagents. These verify the progress fields land:
 *  - sseSubagentTool records toolCount (fired on EVENT_TOOL_CALL too, so a
 *    simple/auto-approved task shows live tool activity) and clears stalled.
 *  - sseSubagentStalled toggles the idle/stalled flag surfaced by the reaper.
 */
import { describe, it, expect } from 'vitest'
import { createTestStore } from './helpers'
import { sseSubagentSpawn, sseSubagentTool, sseSubagentStalled } from '../store/chatSlice'

const ID = 'a1b2c3d4'

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
})
