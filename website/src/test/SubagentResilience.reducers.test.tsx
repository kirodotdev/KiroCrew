/**
 * Sub-agent turn-resilience reducers: retrying indicator + neutral stopped state.
 *  - sseSubagentRetrying flags a transient-backend retry / cancel auto-continue
 *    (⟳ on the running-card); fresh tool/chunk activity clears it.
 *  - sseSubagentDone with `stopped: true` renders a neutral 'stopped' terminal
 *    state (no error), preserving the user-stop-is-not-a-failure semantics.
 *  - sseSubagentDone clears the stall fields, so a finished card is not
 *    ranked as stalled.
 */
import { describe, it, expect } from 'vitest'
import { createTestStore } from './helpers'
import { setActiveSlot, sseSubagentSpawn, sseSubagentTool, sseSubagentBatchChunks, sseSubagentBatchUpdate, sseSubagentRetrying, sseSubagentSnapshot, sseSubagentStalled, sseSubagentDone } from '../store/chatSlice'

const ID = 'a1b2c3d4'

function spawn(store: ReturnType<typeof createTestStore>) {
  const SLOT = store.getState().chat.activeSlot
  store.dispatch(sseSubagentSpawn({ slot: SLOT, id: ID, task: 'do a thing', agent: 'kirocrew' }))
  return SLOT
}
const sub = (store: ReturnType<typeof createTestStore>) => store.getState().chat.subagents[ID]

describe('subagent turn-resilience reducers', () => {
  it('sseSubagentRetrying sets the retrying flag and clears stalled', () => {
    const store = createTestStore()
    const SLOT = spawn(store)
    store.dispatch(sseSubagentRetrying({ slot: SLOT, id: ID, attempt: 1 }))
    const a = sub(store)
    expect(a.retrying).toBe(true)
    expect(a.stalled).toBe(false)
  })

  it('tool activity clears the retrying flag', () => {
    const store = createTestStore()
    const SLOT = spawn(store)
    store.dispatch(sseSubagentRetrying({ slot: SLOT, id: ID }))
    store.dispatch(sseSubagentTool({ slot: SLOT, id: ID, tool: 'fs_read', tool_count: 1 }))
    expect(sub(store).retrying).toBe(false)
  })

  it('chunk activity clears the retrying flag', () => {
    const store = createTestStore()
    const SLOT = spawn(store)
    store.dispatch(sseSubagentRetrying({ slot: SLOT, id: ID }))
    store.dispatch(sseSubagentBatchChunks({ chunks: [{ slot: SLOT, id: ID, text: 'back alive' }] }))
    expect(sub(store).retrying).toBe(false)
  })

  it('a stall after a retry shows the stall, never both', () => {
    const store = createTestStore()
    // A snapshot without an owning slot is dropped, so give the chat a key.
    store.dispatch(setActiveSlot('chat-a'))
    const SLOT = spawn(store)
    store.dispatch(sseSubagentRetrying({ slot: SLOT, id: ID, attempt: 2 }))
    store.dispatch(sseSubagentStalled({ slot: SLOT, id: ID, stalled: true, idle_secs: 200 }))
    expect(sub(store)).toMatchObject({ stalled: true, retrying: false })

    store.dispatch(sseSubagentRetrying({ slot: SLOT, id: ID, attempt: 3 }))
    store.dispatch(sseSubagentBatchUpdate({ updates: [{ slot: SLOT, id: ID, stalled: true }] }))
    expect(sub(store)).toMatchObject({ stalled: true, retrying: false })

    store.dispatch(sseSubagentRetrying({ slot: SLOT, id: ID, attempt: 4 }))
    store.dispatch(sseSubagentSnapshot({ slot: SLOT, id: ID, task: 'do a thing', agent: 'kirocrew', streaming: '', last_tool: 'shell', started: 1, tool_count: 1, stalled: true }))
    expect(sub(store)).toMatchObject({ stalled: true, retrying: false })
  })

  it('an un-stall frame leaves a live retry in place', () => {
    const store = createTestStore()
    const SLOT = spawn(store)
    store.dispatch(sseSubagentRetrying({ slot: SLOT, id: ID, attempt: 2 }))
    store.dispatch(sseSubagentStalled({ slot: SLOT, id: ID, stalled: false }))
    expect(sub(store)).toMatchObject({ stalled: false, retrying: true })
  })

  it('sseSubagentRetrying guards prototype-pollution ids', () => {
    const store = createTestStore()
    const SLOT = spawn(store)
    store.dispatch(sseSubagentRetrying({ slot: SLOT, id: '__proto__' }))
    // Must not throw and must not pollute Object.prototype.
    expect(({} as Record<string, unknown>).retrying).toBeUndefined()
  })

  it('sseSubagentDone with stopped renders neutral stopped state (no error)', () => {
    const store = createTestStore()
    const SLOT = spawn(store)
    store.dispatch(sseSubagentDone({ slot: SLOT, id: ID, elapsed: 12, error: undefined, stopped: true }))
    const a = sub(store)
    expect(a.status).toBe('stopped')
    expect(a.error).toBeUndefined()
    expect(a.retrying).toBe(false)
  })

  it('sseSubagentDone stopped wins even if a stale error string rides along', () => {
    const store = createTestStore()
    const SLOT = spawn(store)
    store.dispatch(sseSubagentDone({ slot: SLOT, id: ID, elapsed: 5, error: 'Cancelled by user', stopped: true }))
    const a = sub(store)
    expect(a.status).toBe('stopped')
    expect(a.error).toBeUndefined()
  })

  it('sseSubagentDone without stopped keeps error semantics', () => {
    const store = createTestStore()
    const SLOT = spawn(store)
    store.dispatch(sseSubagentDone({ slot: SLOT, id: ID, elapsed: 5, error: 'boom' }))
    const a = sub(store)
    expect(a.status).toBe('error')
    expect(a.error).toBe('boom')
  })

  // ── canonical `outcome` field (single classification source) ──

  it('outcome is the canonical source: outcome=stopped wins over error-derivation', () => {
    const store = createTestStore()
    const SLOT = spawn(store)
    // A payload whose legacy fields would derive 'error' but whose canonical
    // outcome says stopped — outcome MUST win.
    store.dispatch(sseSubagentDone({ slot: SLOT, id: ID, elapsed: 3, error: 'stale', outcome: 'stopped' }))
    const a = sub(store)
    expect(a.status).toBe('stopped')
    expect(a.error).toBeUndefined()
  })

  it('outcome=failed maps to error status', () => {
    const store = createTestStore()
    const SLOT = spawn(store)
    store.dispatch(sseSubagentDone({ slot: SLOT, id: ID, elapsed: 3, error: 'boom', outcome: 'failed' }))
    const a = sub(store)
    expect(a.status).toBe('error')
    expect(a.error).toBe('boom')
  })

  it('outcome=completed maps to done status', () => {
    const store = createTestStore()
    const SLOT = spawn(store)
    store.dispatch(sseSubagentDone({ slot: SLOT, id: ID, elapsed: 3, outcome: 'completed' }))
    const a = sub(store)
    expect(a.status).toBe('done')
  })

  it('legacy payload without outcome falls back to stopped/error derivation', () => {
    const store = createTestStore()
    const SLOT = spawn(store)
    store.dispatch(sseSubagentDone({ slot: SLOT, id: ID, elapsed: 3, stopped: true }))
    expect(sub(store).status).toBe('stopped')
  })

  it('done clears the stall fields', () => {
    const store = createTestStore()
    const SLOT = spawn(store)
    store.dispatch(sseSubagentStalled({ slot: SLOT, id: ID, stalled: true, idle_secs: 240 }))
    store.dispatch(sseSubagentDone({ slot: SLOT, id: ID, elapsed: 300, outcome: 'completed' }))
    const a = sub(store)
    expect(a.status).toBe('done')
    expect(a.stalled).toBe(false)
    expect(a.idleSecs).toBeUndefined()
    expect(a.stalledAt).toBeUndefined()
  })
})
