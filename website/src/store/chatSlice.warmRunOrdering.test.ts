/**
 * `warmSlotCache.fulfilled` -- the run-state write is ORDERED against the live
 * frame writers by a per-slot receipt tick (#5581).
 *
 * The reconnect caller warms every background pane after `ws.onopen`, so every
 * frame the tab missed while the socket was down is older than the warm's
 * snapshot by construction. The only frames that can be NEWER than the
 * snapshot are the ones this tab itself applied between the warm's dispatch
 * and its fulfillment -- and those the client can observe: each `slotRun`
 * state writer bumps the entry's `tick`, the thunk captures the tick at
 * dispatch, and `fulfilled` writes only when the tick is unchanged.
 *
 * These tests hold the fetch open on purpose: the ordering lives entirely
 * inside the dispatch -> fulfillment window.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

vi.mock('../api/client', () => ({ api: { chatSlotDetail: vi.fn(), chatSlots: vi.fn().mockResolvedValue([]) } }))

import chatReducer, { warmSlotCache, switchSlot, sseChatMessage, setActiveSlot, selectSlotStreamState, selectSlotRunEpoch, syncSlotRunningFromServer, settleStopNotRunning, startLocalTurn, endLocalTurn } from './chatSlice'
import type { ChatState } from './chatSlice'
import { api } from '../api/client'

const ACTIVE = 'chat-active'
const BG = 'chat-bg'

function makeStore(preload?: Partial<ChatState>) {
  return configureStore({
    reducer: { chat: chatReducer },
    preloadedState: { chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: ACTIVE, ...preload } },
    middleware: (getDefault) => getDefault({ immutableCheck: false }),
  })
}
type Store = ReturnType<typeof makeStore>

const detail = vi.mocked(api.chatSlotDetail)
const snapshot = (running: boolean) => ({ messages: [], running, has_more: false, total: 0, queue: [] })

/** Hold the FIRST slot-detail read of `BG` open (the warm); the returned
 *  function settles it with the given snapshot so frames -- or a whole
 *  switch round trip -- can be applied in between. Every other read (a
 *  `switchSlot` into either slot, a later warm) resolves at once with
 *  `others`. */
function holdOpen(others = snapshot(false)): (running: boolean) => void {
  let release!: (v: unknown) => void
  let held = false
  detail.mockImplementation((key: string) => {
    if (key === BG && !held) { held = true; return new Promise((resolve) => { release = resolve }) }
    return Promise.resolve(others)
  })
  return (running) => release(snapshot(running))
}

/** Hold the first `n` slot-detail reads of `BG` open (two warms in flight);
 *  `release[i](running)` settles the i-th one, in any order. */
function holdMany(n: number): Array<(running: boolean) => void> {
  const releases: Array<(v: unknown) => void> = []
  const pending: Array<Promise<unknown>> = []
  for (let i = 0; i < n; i++) pending.push(new Promise((resolve) => { releases.push(resolve) }))
  let seen = 0
  detail.mockImplementation((key: string) => {
    if (key === BG && seen < n) return pending[seen++]
    return Promise.resolve(snapshot(false))
  })
  return releases.map((r) => (running: boolean) => r(snapshot(running)))
}

const frame = (store: Store, role: string, extra: Record<string, unknown> = {}) =>
  store.dispatch(sseChatMessage({ slot: BG, role, content: role === 'chunk' ? 'hi' : '', ...extra } as never))

const flush = () => new Promise((r) => setTimeout(r, 0))

const runState = (store: Store) => selectSlotStreamState(store.getState() as never, BG)
const runStateOf = (store: Store, slot: string) => selectSlotStreamState(store.getState() as never, slot)

beforeEach(() => { detail.mockReset() })

describe('warmSlotCache.fulfilled promotes a background pane the server reports running (#5581)', () => {
  it('(a) reconnect warm running:true, no frames since dispatch -> streaming, composer locked', async () => {
    // The turn STARTED while the socket was down: this tab never saw a frame
    // of it, so the entry is absent and the pane reads idle on main.
    const store = makeStore()
    const release = holdOpen()
    const p = store.dispatch(warmSlotCache(BG))
    expect(runState(store)).toBe('idle')
    release(true)
    await p
    expect(runState(store)).toBe('streaming')
    // The FIRST busy signal after idle counts a turn start, like a chunk does.
    expect(selectSlotRunEpoch(store.getState() as never, BG)).toBe(1)
  })

  it('(a2) a leftover idle entry from a previous turn is promoted too (#5569 round 4)', async () => {
    const store = makeStore({ slotRun: { [BG]: { state: 'idle' } } })
    const release = holdOpen()
    const p = store.dispatch(warmSlotCache(BG))
    release(true)
    await p
    expect(runState(store)).toBe('streaming')
  })

  it('(b) dispatch, `_done` frame, then fulfillment running:true -> stays idle (#5569 round 3)', async () => {
    // Snapshot taken mid-turn; the turn ends and its `_done` reduces BEFORE
    // the fulfillment. The snapshot is older than that write and must lose.
    const store = makeStore({ slotRun: { [BG]: { state: 'streaming' } } })
    const release = holdOpen()
    const p = store.dispatch(warmSlotCache(BG))
    frame(store, '_done')
    expect(runState(store)).toBe('idle')
    release(true)
    await p
    expect(runState(store)).toBe('idle')
  })

  it('(c) dispatch, chunk frame, then fulfillment running:false -> stays streaming', async () => {
    // The mirror image of (b): a NEW turn's first chunk lands between dispatch
    // and fulfillment. A snapshot that predates it must not idle it.
    const store = makeStore()
    const release = holdOpen()
    const p = store.dispatch(warmSlotCache(BG))
    frame(store, 'chunk', { seq: 1 })
    expect(runState(store)).toBe('streaming')
    release(false)
    await p
    expect(runState(store)).toBe('streaming')
  })

  it('does not downgrade a busier ordered state (tool_running stays tool_running)', async () => {
    const store = makeStore({ slotRun: { [BG]: { state: 'tool_running' } } })
    const release = holdOpen()
    const p = store.dispatch(warmSlotCache(BG))
    release(true)
    await p
    expect(runState(store)).toBe('tool_running')
  })

  it('a chunk that lands after the promotion is not a second turn start', async () => {
    const store = makeStore()
    const release = holdOpen()
    const p = store.dispatch(warmSlotCache(BG))
    release(true)
    await p
    expect(selectSlotRunEpoch(store.getState() as never, BG)).toBe(1)
    frame(store, 'chunk', { seq: 1 })
    expect(selectSlotRunEpoch(store.getState() as never, BG)).toBe(1)
    expect(runState(store)).toBe('streaming')
  })

  it('(d) dispatch, slot becomes active, its turn ends on screen, slot goes back to the background, then fulfillment running:true -> stays idle', async () => {
    // While a slot is active its frames write the mirror, not the keyed entry,
    // so the entry's tick alone would read "unchanged" across this round trip
    // and a stale snapshot would relock the finished pane. The handoff on
    // leaving the slot is the ordered write that closes it.
    const store = makeStore({ slotRun: { [BG]: { state: 'streaming' } } })
    const release = holdOpen(snapshot(true))
    const p = store.dispatch(warmSlotCache(BG))
    await store.dispatch(switchSlot(BG))
    expect(store.getState().chat.activeSlot).toBe(BG)
    expect(store.getState().chat.slotRunning).toBe(true)
    // The turn ends on screen: the active path idles the mirror.
    frame(store, '_done')
    expect(store.getState().chat.slotRunning).toBe(false)
    await store.dispatch(switchSlot(ACTIVE))
    expect(store.getState().chat.activeSlot).toBe(ACTIVE)
    expect(runState(store)).toBe('idle')
    release(true)
    await p
    expect(runState(store)).toBe('idle')
  })
})

describe('leaving the active slot hands the mirror back to its keyed entry', () => {
  it('a turn that ended on screen leaves the entry idle, with the tick bumped', () => {
    const store = makeStore({ slotRun: { [ACTIVE]: { state: 'streaming', tick: 3 } }, slotRunning: false, slotState: 'idle' })
    store.dispatch(setActiveSlot(BG))
    expect(store.getState().chat.slotRun[ACTIVE]).toMatchObject({ state: 'idle', tick: 4 })
  })

  it('a running mirror that has not streamed yet hands over streaming', () => {
    const store = makeStore({ slotRunning: true, slotState: 'idle' })
    store.dispatch(setActiveSlot(BG))
    expect(store.getState().chat.slotRun[ACTIVE]).toMatchObject({ state: 'streaming', tick: 1 })
  })

  it('an unconfirmed local send parks idle, so a refused POST cannot wedge the background pane busy', () => {
    const store = makeStore()
    store.dispatch(startLocalTurn(ACTIVE))
    expect(store.getState().chat.slotRunning).toBe(true)
    store.dispatch(setActiveSlot(BG))
    expect(store.getState().chat.slotRun[ACTIVE]).toMatchObject({ state: 'idle', tick: 1 })
    // The send comes back refused after the switch: the mirror's inverse write
    // touches nothing background, and the entry is already idle.
    store.dispatch(endLocalTurn(ACTIVE))
    expect(runStateOf(store, ACTIVE)).toBe('idle')
  })

  it('a busier mirror state is carried as is', () => {
    const store = makeStore({ slotRunning: true, slotState: 'tool_running' })
    store.dispatch(setActiveSlot(BG))
    expect(store.getState().chat.slotRun[ACTIVE]?.state).toBe('tool_running')
  })

  it('a provisional switch in and straight out observes nothing and leaves the tick alone, so a pending warm still applies', async () => {
    const store = makeStore()
    const release = holdOpen()
    const p = store.dispatch(warmSlotCache(BG))
    store.dispatch(setActiveSlot(BG))
    store.dispatch(setActiveSlot(ACTIVE))
    expect(store.getState().chat.slotRun[BG]?.tick ?? 0).toBe(0)
    release(true)
    await p
    expect(runState(store)).toBe('streaming')
  })

  it('(f) an idle entry, switched into, whose server snapshot says running and whose turn then ends on screen, hands back idle with the tick bumped -- a pending warm declines', async () => {
    // Same-value round trip on the mirror (idle -> running -> idle) with the
    // entry idle throughout: only the epoch tells it from a slot that saw
    // nothing.
    const store = makeStore({ slotRun: { [BG]: { state: 'idle' } } })
    const release = holdOpen(snapshot(true))
    const p = store.dispatch(warmSlotCache(BG))
    await store.dispatch(switchSlot(BG))
    expect(store.getState().chat.slotRunning).toBe(true)
    frame(store, '_done')
    expect(store.getState().chat.slotRunning).toBe(false)
    await store.dispatch(switchSlot(ACTIVE))
    expect(store.getState().chat.slotRun[BG]).toMatchObject({ state: 'idle', tick: 1 })
    release(true)
    await p
    expect(runState(store)).toBe('idle')
  })

  it('a same-slot move writes nothing', () => {
    const store = makeStore({ slotRunning: true, slotState: 'streaming' })
    store.dispatch(setActiveSlot(ACTIVE))
    expect(store.getState().chat.slotRun[ACTIVE]).toBeUndefined()
  })
})

describe('the existing #5569 idle gate survives the ordering guard', () => {
  it('still idles a pane whose turn ended while the socket was down', async () => {
    const store = makeStore({ slotRun: { [BG]: { state: 'streaming' } } })
    const release = holdOpen()
    const p = store.dispatch(warmSlotCache(BG))
    release(false)
    await p
    expect(runState(store)).toBe('idle')
  })

  it('a slot that became active mid-flight is left to switchSlot', async () => {
    const store = makeStore()
    const release = holdOpen()
    const p = store.dispatch(warmSlotCache(BG))
    store.dispatch(setActiveSlot(BG))
    release(true)
    await p
    await flush()
    expect(store.getState().chat.slotRun[BG]).toBeUndefined()
  })
})

describe('every slotRun state writer bumps the receipt tick', () => {
  const tick = (store: Store) => store.getState().chat.slotRun[BG]?.tick ?? 0

  it('chunk, tool, compacting and _done frames each bump it', () => {
    const store = makeStore()
    frame(store, 'chunk', { seq: 1 })
    expect(tick(store)).toBe(1)
    frame(store, 'tool', { cls: '', meta: { tool_call_id: 't1' } })
    expect(tick(store)).toBe(2)
    frame(store, 'compacting')
    expect(tick(store)).toBe(3)
    frame(store, '_done')
    expect(tick(store)).toBe(4)
  })

  it('the background branch of syncSlotRunningFromServer bumps it, and its idle then outranks a stale warm', async () => {
    const store = makeStore({ slotRun: { [BG]: { state: 'streaming' } } })
    const release = holdOpen()
    const p = store.dispatch(warmSlotCache(BG))
    store.dispatch(syncSlotRunningFromServer({ slot: BG, running: false, stopping: false }))
    expect(runState(store)).toBe('idle')
    expect(tick(store)).toBe(1)
    release(true)
    await p
    expect(runState(store)).toBe('idle')
  })

  it('the background branch of settleStopNotRunning bumps it, and its idle then outranks a stale warm', async () => {
    const store = makeStore({ slotRun: { [BG]: { state: 'streaming' } } })
    const release = holdOpen()
    const p = store.dispatch(warmSlotCache(BG))
    store.dispatch(settleStopNotRunning({ slot: BG }))
    expect(runState(store)).toBe('idle')
    expect(tick(store)).toBe(1)
    release(true)
    await p
    expect(runState(store)).toBe('idle')
  })

  it('a same-value idle write still bumps it (idle over idle is a transition the snapshot must see)', () => {
    // A queued turn that ran and finished between two snapshots leaves the
    // entry idle both times; only the tick tells the reducer a `_done` ran.
    const store = makeStore({ slotRun: { [BG]: { state: 'idle' } } })
    frame(store, '_done')
    expect(runState(store)).toBe('idle')
    expect(tick(store)).toBe(1)
  })

  it('a warm write does NOT consume the tick (a snapshot is not an observed transition)', async () => {
    const store = makeStore({ slotRun: { [BG]: { state: 'streaming' } } })
    const release = holdOpen()
    const p = store.dispatch(warmSlotCache(BG))
    release(false)
    await p
    expect(runState(store)).toBe('idle')
    expect(tick(store)).toBe(0)
    const release2 = holdOpen()
    const p2 = store.dispatch(warmSlotCache(BG))
    release2(true)
    await p2
    expect(runState(store)).toBe('streaming')
    expect(tick(store)).toBe(0)
  })
})

describe('two warms for one slot are ordered by warmSeq, whichever lands first', () => {
  // Both warms dispatch at the same tick (nothing observed in between): the
  // one dispatched later holds the newer snapshot.
  it('older running:false lands first, newer running:true after -> streaming', async () => {
    const store = makeStore()
    const [w1, w2] = holdMany(2)
    const p1 = store.dispatch(warmSlotCache(BG))
    const p2 = store.dispatch(warmSlotCache(BG))
    w1(false); await p1
    expect(runState(store)).toBe('idle')
    w2(true); await p2
    expect(runState(store)).toBe('streaming')
  })

  it('older running:true lands first, newer running:false after -> idle (the turn ended between the snapshots)', async () => {
    const store = makeStore({ slotRun: { [BG]: { state: 'streaming' } } })
    const [w1, w2] = holdMany(2)
    const p1 = store.dispatch(warmSlotCache(BG))
    const p2 = store.dispatch(warmSlotCache(BG))
    w1(true); await p1
    expect(runState(store)).toBe('streaming')
    w2(false); await p2
    expect(runState(store)).toBe('idle')
  })

  it('newer running:false lands first, older running:true after -> the older one declines, stays idle', async () => {
    const store = makeStore({ slotRun: { [BG]: { state: 'streaming' } } })
    const [w1, w2] = holdMany(2)
    const p1 = store.dispatch(warmSlotCache(BG))
    const p2 = store.dispatch(warmSlotCache(BG))
    w2(false); await p2
    expect(runState(store)).toBe('idle')
    w1(true); await p1
    expect(runState(store)).toBe('idle')
  })

  it('newer running:true lands first, older running:false after -> the older one declines, stays streaming', async () => {
    const store = makeStore()
    const [w1, w2] = holdMany(2)
    const p1 = store.dispatch(warmSlotCache(BG))
    const p2 = store.dispatch(warmSlotCache(BG))
    w2(true); await p2
    expect(runState(store)).toBe('streaming')
    w1(false); await p1
    expect(runState(store)).toBe('streaming')
  })

  it('a newer warm that changed nothing still outranks an older one landing after it', async () => {
    const store = makeStore({ slotRun: { [BG]: { state: 'tool_running' } } })
    const [w1, w2] = holdMany(2)
    const p1 = store.dispatch(warmSlotCache(BG))
    const p2 = store.dispatch(warmSlotCache(BG))
    w2(true); await p2
    expect(runState(store)).toBe('tool_running')
    w1(false); await p1
    expect(runState(store)).toBe('tool_running')
  })
})
