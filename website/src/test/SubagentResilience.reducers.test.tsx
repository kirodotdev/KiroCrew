/**
 * Sub-agent turn-resilience reducers: retrying indicator + neutral stopped state.
 *  - sseSubagentRetrying flags a transient-backend retry / cancel auto-continue
 *    (⟳ on the running-card); fresh tool/chunk activity clears it.
 *  - sseSubagentDone with `stopped: true` renders a neutral 'stopped' terminal
 *    state (no error), preserving the user-stop-is-not-a-failure semantics.
 */
import { describe, it, expect } from 'vitest'
import { createTestStore } from './helpers'
import { sseSubagentSpawn, sseSubagentTool, sseSubagentChunk, sseSubagentRetrying, sseSubagentDone } from '../store/chatSlice'

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
    store.dispatch(sseSubagentChunk({ slot: SLOT, id: ID, text: 'back alive' }))
    expect(sub(store).retrying).toBe(false)
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
})
