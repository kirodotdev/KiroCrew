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
        chat: { ...chatReducer(undefined, { type: '@@INIT' }), subagentQueued: undefined },
      } as never,
    })
    store.dispatch(setActiveSlot('a'))
    expect(() => store.dispatch(sseSubagentQueued({ slot: 'a', queued: 2 }))).not.toThrow()
    expect(count(store)).toBe(2)
  })
})
