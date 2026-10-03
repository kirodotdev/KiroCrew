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
  sseSubagentPending,
  markSubagentApprovalGone,
  reconcileSubagentApprovalGone,
  sseSubagentTool,
  isAwaitingSpawnApproval,
  isSpawnApprovalGone,
  isSpawnApprovalRetired,
  selectSlotSubagentsActive,
  selectSubagentActivityCount,
  selectSidebarSubagentCounts,
  selectSidebarApprovalCounts,
  selectComposerBusy,
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

  it.each(['a', 'background'])('retires a gone approval only after the authoritative list says it is absent in %s', slot => {
    const store = makeStore()
    store.dispatch(setActiveSlot('a'))
    store.dispatch(sseSubagentPending({ slot, id: 'p1', task: 'wait', approval_id: 'ap-1' }))

    const root = () => store.getState() as unknown as RootState
    expect(selectSlotSubagentsActive(root(), slot)).toBe(true)
    expect(selectSubagentActivityCount(root())).toBe(1)
    expect(selectSidebarSubagentCounts(root())[slot]).toBe(1)
    expect(selectSidebarApprovalCounts(root())[slot]).toBe(1)
    expect(selectComposerBusy(root(), slot)).toBe(true)

    store.dispatch(markSubagentApprovalGone({ id: 'p1', approval_id: 'ap-1' }))

    // Decision controls disappear immediately, but a stale surface's 404 does
    // not prove no process launched elsewhere. Busy/count/reload readers stay
    // conservative until the authoritative inventory answers.
    expect(selectSidebarApprovalCounts(root())[slot]).toBeUndefined()
    expect(selectSlotSubagentsActive(root(), slot)).toBe(true)
    expect(selectSubagentActivityCount(root())).toBe(1)
    expect(selectSidebarSubagentCounts(root())[slot]).toBe(1)
    expect(selectComposerBusy(root(), slot)).toBe(true)

    store.dispatch(reconcileSubagentApprovalGone({
      slot, id: 'p1', approval_id: 'ap-1', agent: null,
    }))

    const retired = root().chat.subagents.p1 ?? root().chat.slotActivity[slot]?.subagents.p1
    expect(retired).toMatchObject({ status: 'stopped', approvalRetired: true })
    expect(selectSlotSubagentsActive(root(), slot)).toBe(false)
    expect(selectSubagentActivityCount(root())).toBe(0)
    expect(selectSidebarSubagentCounts(root())[slot]).toBeUndefined()
    expect(selectSidebarApprovalCounts(root())[slot]).toBeUndefined()
    expect(selectComposerBusy(root(), slot)).toBe(false)

    store.dispatch(sseSubagentPending({ slot, id: 'p1', task: 'wait again', approval_id: 'ap-2' }))
    expect(selectSlotSubagentsActive(root(), slot)).toBe(true)
    expect(selectSubagentActivityCount(root())).toBe(1)
    expect(selectSidebarSubagentCounts(root())[slot]).toBe(1)
    expect(selectSidebarApprovalCounts(root())[slot]).toBe(1)
    expect(selectComposerBusy(root(), slot)).toBe(true)
  })

  it('promotes a gone approval to running when another surface launched it', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('a'))
    store.dispatch(sseSubagentPending({ slot: 'a', id: 'p1', task: 'wait', approval_id: 'ap-1' }))
    store.dispatch(markSubagentApprovalGone({ id: 'p1', approval_id: 'ap-1' }))

    store.dispatch(reconcileSubagentApprovalGone({
      slot: 'a',
      id: 'p1',
      approval_id: 'ap-1',
      agent: {
        id: 'p1',
        parent: 'dashboard:a',
        task: 'authoritative task',
        agent: 'kirocrew',
        started: 1_000,
        last_tool: '',
        done: false,
      },
    }))

    const root = store.getState() as unknown as RootState
    expect(root.chat.subagents.p1).toMatchObject({
      status: 'running', task: 'authoritative task', approval_id: undefined, approvalGone: undefined,
    })
    expect(selectSlotSubagentsActive(root, 'a')).toBe(true)
    expect(selectSubagentActivityCount(root)).toBe(1)
    expect(selectSidebarSubagentCounts(root).a).toBe(1)
    expect(selectSidebarApprovalCounts(root).a).toBeUndefined()
    expect(selectComposerBusy(root, 'a')).toBe(true)
  })

  it('does not mistake an inventory row still parked on approval for a running process', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('a'))
    store.dispatch(sseSubagentPending({ slot: 'a', id: 'p1', task: 'wait', approval_id: 'ap-1' }))
    store.dispatch(markSubagentApprovalGone({ id: 'p1', approval_id: 'ap-1' }))

    store.dispatch(reconcileSubagentApprovalGone({
      slot: 'a', id: 'p1', approval_id: 'ap-1',
      agent: { id: 'p1', parent: 'dashboard:a', done: false, awaiting_approval: true },
    }))

    const root = store.getState() as unknown as RootState
    expect(root.chat.subagents.p1).toMatchObject({
      status: 'pending', approval_id: 'ap-1', approvalGone: 'ap-1',
    })
    expect(selectSidebarApprovalCounts(root).a).toBeUndefined()
    expect(selectSlotSubagentsActive(root, 'a')).toBe(true)
    expect(selectComposerBusy(root, 'a')).toBe(true)
  })

  it.each([
    // `outcome` alone decides, without the legacy stopped/error pair.
    [{ outcome: 'failed' as const }, 'error'],
    [{ outcome: 'stopped' as const }, 'stopped'],
    [{ outcome: 'completed' as const, error: 'a partial-result note' }, 'done'],
    // A row that predates `outcome` falls back to the legacy pair.
    [{ error: 'boom' }, 'error'],
  ])('adopts a finished row by its outcome (%o), with no wall-clock duration', (row, status) => {
    const store = makeStore()
    store.dispatch(setActiveSlot('a'))
    store.dispatch(sseSubagentPending({ slot: 'a', id: 'p1', task: 'wait', approval_id: 'ap-1' }))
    store.dispatch(markSubagentApprovalGone({ id: 'p1', approval_id: 'ap-1' }))

    store.dispatch(reconcileSubagentApprovalGone({
      slot: 'a', id: 'p1', approval_id: 'ap-1',
      // Registered 25 minutes ago; a finished row carries no elapsed of its own.
      agent: { id: 'p1', task: 'wait', done: true, started: Date.now() / 1000 - 1500, ...row },
    }))

    const card = (store.getState() as unknown as RootState).chat.subagents.p1
    expect(card.status).toBe(status)
    expect(card.elapsed).toBe(0)
  })

  it('does not let a stale reconciliation retire a fresh approval id', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('a'))
    store.dispatch(sseSubagentPending({ slot: 'a', id: 'p1', task: 'first', approval_id: 'ap-1' }))
    store.dispatch(markSubagentApprovalGone({ id: 'p1', approval_id: 'ap-1' }))
    store.dispatch(sseSubagentPending({ slot: 'a', id: 'p1', task: 'fresh', approval_id: 'ap-2' }))

    store.dispatch(reconcileSubagentApprovalGone({
      slot: 'a', id: 'p1', approval_id: 'ap-1', agent: null,
    }))

    const root = store.getState() as unknown as RootState
    expect(root.chat.subagents.p1).toMatchObject({ status: 'pending', task: 'fresh', approval_id: 'ap-2' })
    expect(selectSidebarApprovalCounts(root).a).toBe(1)
    expect(selectComposerBusy(root, 'a')).toBe(true)
  })

  it.each(['a', 'background'])('stops classifying a gone approval once a real spawn and tool frame arrive in %s', slot => {
    // The spawn/tool frames move status without clearing the gone marker, so
    // the classification itself must be scoped to the pending phase.
    const store = makeStore()
    store.dispatch(setActiveSlot('a'))
    store.dispatch(sseSubagentPending({ slot, id: 'p1', task: 'wait', approval_id: 'ap-1' }))
    store.dispatch(markSubagentApprovalGone({ id: 'p1', approval_id: 'ap-1' }))
    const card = () => {
      const root = store.getState() as unknown as RootState
      return (slot === 'a' ? root.chat.subagents.p1 : root.chat.slotActivity[slot]?.subagents.p1)!
    }
    expect(isSpawnApprovalGone(card())).toBe(true)

    store.dispatch(sseSubagentSpawn({ slot, id: 'p1', task: 'launched elsewhere', agent: 'kirocrew' }))
    expect(card()).toMatchObject({ status: 'running', approvalGone: 'ap-1' })
    expect(isSpawnApprovalGone(card())).toBe(false)
    expect(isAwaitingSpawnApproval(card())).toBe(false)

    store.dispatch(sseSubagentTool({ slot, id: 'p1', tool: 'shell' }))
    expect(card()).toMatchObject({ status: 'tool', approvalGone: 'ap-1' })
    expect(isSpawnApprovalGone(card())).toBe(false)
    expect(isSpawnApprovalRetired(card())).toBe(false)

    // A reconciliation answer that was in flight across the spawn is a no-op.
    store.dispatch(reconcileSubagentApprovalGone({ slot, id: 'p1', approval_id: 'ap-1', agent: null }))
    expect(card().status).toBe('tool')
    expect(selectSlotSubagentsActive(store.getState() as unknown as RootState, slot)).toBe(true)
  })

  it('stops classifying a retired approval once a tool frame proves the launch', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('a'))
    store.dispatch(sseSubagentPending({ slot: 'a', id: 'p1', task: 'wait', approval_id: 'ap-1' }))
    store.dispatch(markSubagentApprovalGone({ id: 'p1', approval_id: 'ap-1' }))
    store.dispatch(reconcileSubagentApprovalGone({ slot: 'a', id: 'p1', approval_id: 'ap-1', agent: null }))
    const card = () => (store.getState() as unknown as RootState).chat.subagents.p1!
    expect(isSpawnApprovalRetired(card())).toBe(true)

    store.dispatch(sseSubagentTool({ slot: 'a', id: 'p1', tool: 'shell' }))
    expect(card()).toMatchObject({ status: 'tool', approvalRetired: undefined })
    expect(isSpawnApprovalRetired(card())).toBe(false)
    expect(isSpawnApprovalGone(card())).toBe(false)

    // The launched run is then stopped: it is a stopped run, not a retired card.
    store.dispatch(sseSubagentDone({ slot: 'a', id: 'p1', elapsed: 5, outcome: 'stopped', stopped: true }))
    expect(card().status).toBe('stopped')
    expect(isSpawnApprovalRetired(card())).toBe(false)
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
