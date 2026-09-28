/**
 * `selectSubagentActivityCount` — the cross-page "agents are working" count
 * shown in the expanded Sessions rail. It must sum STARTED agents across every slot
 * (active map + slotActivity for background slots, without double-counting the
 * aliased active slot) PLUS accepted-but-queued agents, which have no per-agent
 * entry at all and were therefore invisible everywhere outside the composer chip.
 */
import { describe, it, expect } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, {
  setActiveSlot,
  switchSlot,
  sseSubagentSpawn,
  sseSubagentSnapshot,
  sseSubagentDone,
  sseSubagentQueued,
  selectSubagentActivityCount,
} from './chatSlice'
import dashboardReducer from './dashboardSlice'
import notificationsReducer from './notificationsSlice'
import type { RootState } from './index'

function makeStore() {
  return configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
  })
}

const count = (store: ReturnType<typeof makeStore>) =>
  selectSubagentActivityCount(store.getState() as unknown as RootState)

describe('selectSubagentActivityCount', () => {
  it('is zero with nothing in flight', () => {
    expect(count(makeStore())).toBe(0)
  })

  it('counts started agents in the active slot', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('a'))
    store.dispatch(sseSubagentSpawn({ slot: 'a', id: 'x1', task: 't', agent: 'kirocrew' }))
    store.dispatch(sseSubagentSpawn({ slot: 'a', id: 'x2', task: 't', agent: 'kirocrew' }))
    expect(count(store)).toBe(2)
  })

  it('counts agents in background slots too — the whole point of a rail dot', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('a'))
    store.dispatch(sseSubagentSpawn({ slot: 'b', id: 'y1', task: 't', agent: 'kirocrew' }))
    expect(count(store)).toBe(1)
  })

  it('does not double-count the active slot after switchSlot aliases its map', () => {
    // switchSlot aliases the active slot's subagents map into BOTH state.subagents
    // and slotActivity[active].subagents (same object reference).
    const store = makeStore()
    store.dispatch(setActiveSlot('a'))
    store.dispatch(sseSubagentSpawn({ slot: 'a', id: 'x1', task: 't', agent: 'kirocrew' }))
    store.dispatch(switchSlot('a'))
    expect(count(store)).toBe(1)
  })

  it('drops agents once they finish', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('a'))
    store.dispatch(sseSubagentSpawn({ slot: 'a', id: 'x1', task: 't', agent: 'kirocrew' }))
    store.dispatch(sseSubagentDone({ slot: 'a', id: 'x1', elapsed: 3 }))
    expect(count(store)).toBe(0)
  })

  it('counts queued agents that have not started yet', () => {
    // A wave behind the concurrency cap produces subagent_queued and nothing
    // else, so a started-only count reads 0 for the entire ramp.
    const store = makeStore()
    store.dispatch(setActiveSlot('a'))
    store.dispatch(sseSubagentQueued({ slot: 'a', queued: 3 }))
    expect(count(store)).toBe(3)
  })

  it('sums started and queued across slots', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('a'))
    store.dispatch(sseSubagentSpawn({ slot: 'a', id: 'x1', task: 't', agent: 'kirocrew' }))
    store.dispatch(sseSubagentQueued({ slot: 'b', queued: 2 }))
    expect(count(store)).toBe(3)
  })

  it('returns a stable reference across unrelated dispatches (memoized)', () => {
    // The surface registry invokes activity selectors on EVERY dispatch, so an
    // unmemoized derivation would re-run on unrelated state changes.
    const store = makeStore()
    store.dispatch(setActiveSlot('a'))
    store.dispatch(sseSubagentQueued({ slot: 'a', queued: 1 }))
    const first = count(store)
    const before = selectSubagentActivityCount.recomputations()
    store.dispatch({ type: 'unrelated/noop' })
    expect(count(store)).toBe(first)
    // No recomputation: the memoized inputs (activeSlot, subagents,
    // slotActivity, subagentQueued) are all reference-unchanged.
    expect(selectSubagentActivityCount.recomputations()).toBe(before)
  })
})

describe('sseSubagentQueued on partial preloaded state', () => {
  it('does not throw when subagentQueued is absent from the store', () => {
    // A store built from partial preloaded state has no `subagentQueued` map;
    // indexing it must not throw, or the queue update that the queued-visibility
    // surfaces read is dropped.
    const store = configureStore({
      reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
      preloadedState: {
        chat: { ...chatReducer(undefined, { type: '@@INIT' }), subagentQueued: undefined, subagentQueuedReason: undefined },
      } as never,
    })
    store.dispatch(setActiveSlot('a'))
    expect(() => store.dispatch(sseSubagentQueued({ slot: 'a', queued: 2 }))).not.toThrow()
    expect(count(store)).toBe(2)
  })
})

/**
 * The gate's `reason` rides beside the count. The count alone made every chip
 * say "queued behind the concurrency limit" for a wave the memory guard parked
 * (F20); the label is what lets them say why. An event without one -- an older
 * gateway -- must leave the store exactly as before.
 */
describe('sseSubagentQueued carries the wait reason', () => {
  const reasonFor = (store: ReturnType<typeof makeStore>, slot: string) =>
    (store.getState() as unknown as RootState).chat.subagentQueuedReason?.[slot]

  it('stores a labelled wait beside its count', () => {
    const store = makeStore()
    store.dispatch(sseSubagentQueued({ slot: 'a', queued: 1, reason: 'low_memory', available_gb: 3.2, required_gb: 4.5 }))
    expect(count(store)).toBe(1)
    expect(reasonFor(store, 'a')).toEqual({ reason: 'low_memory', available_gb: 3.2, required_gb: 4.5 })
  })

  it('keeps no reason for a bare count, so an old gateway renders the old text', () => {
    const store = makeStore()
    store.dispatch(sseSubagentQueued({ slot: 'a', queued: 2 }))
    expect(count(store)).toBe(2)
    expect(reasonFor(store, 'a')).toBeUndefined()
  })

  it('replaces a stale reason when a later frame carries none', () => {
    const store = makeStore()
    store.dispatch(sseSubagentQueued({ slot: 'a', queued: 1, reason: 'posture_critical', available_gb: 1.2 }))
    store.dispatch(sseSubagentQueued({ slot: 'a', queued: 1 }))
    expect(reasonFor(store, 'a')).toBeUndefined()
  })

  it('drops the reason with the count at zero', () => {
    const store = makeStore()
    store.dispatch(sseSubagentQueued({ slot: 'a', queued: 1, reason: 'adaptive_cap_zero' }))
    store.dispatch(sseSubagentQueued({ slot: 'a', queued: 0 }))
    expect(count(store)).toBe(0)
    expect(reasonFor(store, 'a')).toBeUndefined()
  })

  it('ignores a kind it cannot render and a non-numeric figure', () => {
    const store = makeStore()
    store.dispatch(sseSubagentQueued({ slot: 'a', queued: 1, reason: 'something_new' }))
    expect(reasonFor(store, 'a')).toBeUndefined()
    store.dispatch(sseSubagentQueued({ slot: 'a', queued: 1, reason: 'low_memory', available_gb: Number.NaN }))
    expect(reasonFor(store, 'a')).toEqual({ reason: 'low_memory' })
  })
})

describe('subagent snapshot ownership', () => {
  const snapshot = (slot: string) => sseSubagentSnapshot({
    slot,
    id: 'orphan-1',
    task: 'read-only audit',
    agent: 'kirocrew',
    streaming: '',
    last_tool: 'WorkspaceSearch',
    started: 1000,
  })

  it('does not attach an ownerless replay snapshot to the active session', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('new-session'))

    store.dispatch(snapshot(''))

    expect(store.getState().chat.subagents).toEqual({})
    expect(store.getState().chat.slotActivity['']).toBeUndefined()
    expect(count(store)).toBe(0)
  })

  it('still routes a snapshot whose owner is the active session', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('owned-session'))

    store.dispatch(snapshot('owned-session'))

    expect(store.getState().chat.subagents['orphan-1']?.task).toBe('read-only audit')
    expect(count(store)).toBe(1)
  })
})
