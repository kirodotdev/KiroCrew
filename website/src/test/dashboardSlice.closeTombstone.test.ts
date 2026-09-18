/**
 * Close tombstone: a session being closed must not flicker back into the
 * sidebar when an authoritative slot list that predates the server-side pop
 * lands while (or just after) the DELETE is in flight (#11224).
 *
 * The thunk is not run here -- these drive the reducer with the exact action
 * sequence `deleteSlot` emits (`pending` -> `removeSlotOptimistic` -> await ->
 * `fulfilled` | `rejected`) interleaved with `sseSlots` / `fetchSlots.fulfilled`
 * frames, which is the only thing the contract is about.
 */
import reducer, {
  sseSlots,
  fetchSlots,
  addSlotOptimistic,
  removeSlotOptimistic,
  releaseCloseHold,
  sseSubagentStatus,
} from '../store/dashboardSlice'
import type { ChatSlot } from '../types'

vi.mock('../api/client', () => ({
  api: { chatSlots: vi.fn(), chatMode: vi.fn() },
}))

const row = (key: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({
  key, title: key, messages: 1, running: false, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined, ...extra,
})
const A = row('chat-a')
const B = row('chat-b')
const C = row('chat-c')

const pending = (key: string, requestId = 'r') => ({ type: 'chat/deleteSlot/pending', meta: { arg: key, requestId, requestStatus: 'pending' } })
const fulfilled = (key: string, requestId = 'r') => ({ type: 'chat/deleteSlot/fulfilled', meta: { arg: key, requestId, requestStatus: 'fulfilled' }, payload: key })
const rejected = (key: string, requestId = 'r') => ({ type: 'chat/deleteSlot/rejected', meta: { arg: key, requestId, requestStatus: 'rejected' }, error: { message: 'save failed' } })
const fetchStarted = (requestId = 'h') => ({ type: fetchSlots.pending.type, meta: { requestId, requestStatus: 'pending' } })
const httpReply = (slots: ChatSlot[], requestId = 'h') => ({ type: fetchSlots.fulfilled.type, payload: slots, meta: { requestId, requestStatus: 'fulfilled' } })

const keys = (s: { slots: ChatSlot[] }) => s.slots.map(x => x.key)

/** A live tab: the first snapshot has landed, so later frames are authoritative. */
function live() {
  return reducer(reducer(undefined, { type: '@@INIT' }), sseSlots([A, B, C]))
}

/** Arm a close of B the way the thunk does, up to (not including) the await. */
function closing(state = live()) {
  state = reducer(state, pending('chat-b'))
  return reducer(state, removeSlotOptimistic('chat-b'))
}

describe('dashboardSlice close tombstone', () => {
  it('starts with no closing keys', () => {
    expect(reducer(undefined, { type: '@@INIT' }).closingSlots).toEqual({})
  })

  it('holds the closing row out of a live frame that still lists it (the flicker)', () => {
    let s = closing()
    expect(keys(s)).toEqual(['chat-a', 'chat-c'])
    // Server has not popped yet: a coalesced push re-serializes the full registry.
    s = reducer(s, sseSlots([A, B, C]))
    expect(keys(s)).toEqual(['chat-a', 'chat-c'])
    // ...and a stale HTTP reply assembled before the click says the same.
    s = reducer(s, httpReply([A, B, C]))
    expect(keys(s)).toEqual(['chat-a', 'chat-c'])
  })

  it('does not delay the rest of the frame: other rows, generation, unread drain', () => {
    let s = closing()
    const gen = s.slotsGeneration
    s = reducer(s, sseSlots([A, B, { ...C, title: 'renamed' }]))
    expect(s.slots.find(x => x.key === 'chat-c')?.title).toBe('renamed')
    expect(s.slotsGeneration).toBe(gen + 1)
  })

  it('does not let an omitting frame release an in-flight hold', () => {
    // The two authoritative writers are unordered with each other: a live
    // frame serialized after the pop can arrive BEFORE an HTTP reply assembled
    // before it. If the omission released the hold, that reply would re-add
    // the row. The close's own lifecycle retires the tombstone instead.
    let s = closing()
    s = reducer(s, sseSlots([A, C]))
    expect(Object.keys(s.closingSlots)).toEqual(['chat-b'])
    s = reducer(s, httpReply([A, B, C]))
    expect(keys(s)).toEqual(['chat-a', 'chat-c'])
  })

  it('keeps holding after the 200 through a post-pop omission followed by a stale reply', () => {
    let s = closing()
    s = reducer(s, fulfilled('chat-b'))
    // Post-pop live frame omits the key...
    s = reducer(s, sseSlots([A, C]))
    // ...then a pre-pop HTTP reply that still lists it lands late.
    s = reducer(s, httpReply([A, B, C]))
    expect(keys(s)).toEqual(['chat-a', 'chat-c'])
    // And a coalesced straggler frame serialized before the pop.
    s = reducer(s, sseSlots([A, B, C]))
    expect(keys(s)).toEqual(['chat-a', 'chat-c'])
  })

  it('retires a confirmed hold after the frame budget so a later same-key session shows', () => {
    let s = closing()
    s = reducer(s, fulfilled('chat-b'))
    for (let i = 0; i < 3; i++) s = reducer(s, sseSlots([A, C]))
    expect(s.closingSlots).toEqual({})
    s = reducer(s, sseSlots([A, C, B]))
    expect(keys(s)).toEqual(['chat-a', 'chat-c', 'chat-b'])
  })

  it('yields to membership after a bounded number of post-confirmation frames', () => {
    let s = closing()
    s = reducer(s, fulfilled('chat-b'))
    for (let i = 0; i < 3; i++) {
      s = reducer(s, sseSlots([A, B, C]))
      expect(keys(s)).toEqual(['chat-a', 'chat-c'])
    }
    // A server that still lists a slot it confirmed closed has kept it alive;
    // hiding it forever would lose the user a session. Show it.
    s = reducer(s, sseSlots([A, B, C]))
    expect(keys(s)).toEqual(['chat-a', 'chat-b', 'chat-c'])
    expect(s.closingSlots).toEqual({})
  })

  it('holds through any number of frames while the request is in flight', () => {
    let s = closing()
    for (let i = 0; i < 20; i++) s = reducer(s, sseSlots([A, B, C]))
    expect(keys(s)).toEqual(['chat-a', 'chat-c'])
  })

  it('stops hiding a session whose DELETE has stalled past the wall-clock cap', () => {
    // `deleteChatSlot` carries no deadline. A close the server never answers
    // must degrade to the pre-fix behaviour (the row visibly comes back), not
    // to a session hidden until reload.
    vi.useFakeTimers()
    try {
      vi.setSystemTime(new Date('2026-09-16T06:00:00Z'))
      let s = closing()
      vi.setSystemTime(new Date('2026-09-16T06:00:29Z'))
      s = reducer(s, sseSlots([A, B, C]))
      expect(keys(s)).toEqual(['chat-a', 'chat-c'])
      vi.setSystemTime(new Date('2026-09-16T06:00:31Z'))
      s = reducer(s, sseSlots([A, B, C]))
      expect(keys(s)).toEqual(['chat-a', 'chat-b', 'chat-c'])
      expect(s.closingSlots).toEqual({})
    } finally {
      vi.useRealTimers()
    }
  })

  it('releases on rejection so the refetch can put the row back', () => {
    let s = closing()
    s = reducer(s, rejected('chat-b'))
    expect(s.closingSlots).toEqual({})
    s = reducer(s, httpReply([A, B, C]))
    expect(keys(s)).toEqual(['chat-a', 'chat-b', 'chat-c'])
  })

  it('lets the failure-path refetch restore the row even when it lands before `rejected`', () => {
    // The thunk's catch dispatches `releaseCloseHold` then `fetchSlots()`, and
    // `rejected` trails an unbounded `await navigation`. The reply can beat it.
    let s = closing()
    s = reducer(s, releaseCloseHold({ key: 'chat-b', requestId: 'r' }))
    s = reducer(s, httpReply([A, B, C]))
    expect(keys(s)).toEqual(['chat-a', 'chat-b', 'chat-c'])
    s = reducer(s, rejected('chat-b'))
    expect(keys(s)).toEqual(['chat-a', 'chat-b', 'chat-c'])
    expect(s.closingSlots).toEqual({})
  })

  it('is superseded by a same-key optimistic add (resume / fork) and by createSlot', () => {
    let s = closing()
    s = reducer(s, addSlotOptimistic(B))
    expect(s.closingSlots).toEqual({})
    expect(keys(s)).toContain('chat-b')

    s = closing()
    s = reducer(s, { type: 'chat/createSlot/fulfilled', payload: B, meta: { requestId: 'c', requestStatus: 'fulfilled', arg: {} } })
    expect(s.closingSlots).toEqual({})
    expect(keys(s)).toContain('chat-b')
  })

  it('outlives a fetch that was in flight at confirmation, however many frames arrive first', () => {
    // A `fetchSlots` started before the close can be answered from a list
    // assembled before the pop and still land after several live frames. The
    // frame budget cannot bound that; the hold waits for the request itself.
    let s = live()
    s = reducer(s, fetchStarted('stale'))
    s = closing(s)
    s = reducer(s, fulfilled('chat-b'))
    for (let i = 0; i < 6; i++) s = reducer(s, sseSlots([A, C]))
    expect(Object.keys(s.closingSlots)).toEqual(['chat-b'])
    s = reducer(s, httpReply([A, B, C], 'stale'))
    expect(keys(s)).toEqual(['chat-a', 'chat-c'])
    // The stale reply was the last thing the hold was waiting for.
    expect(s.closingSlots).toEqual({})
    expect(s.slotFetchesInFlight).toEqual([])
  })

  it('stops waiting for a pre-confirmation fetch that never settles, on the wall clock', () => {
    // `chatSlots()` has no deadline. A snapshotted fetch that stalls must not
    // pin the tombstone until reload — same failure mode as the in-flight cap.
    vi.useFakeTimers()
    try {
      vi.setSystemTime(new Date('2026-09-16T06:00:00Z'))
      let s = live()
      s = reducer(s, fetchStarted('stalled'))
      s = closing(s)
      s = reducer(s, fulfilled('chat-b'))
      vi.setSystemTime(new Date('2026-09-16T06:00:29Z'))
      for (let i = 0; i < 5; i++) s = reducer(s, sseSlots([A, B, C]))
      expect(keys(s)).toEqual(['chat-a', 'chat-c'])
      vi.setSystemTime(new Date('2026-09-16T06:00:31Z'))
      s = reducer(s, sseSlots([A, B, C]))
      expect(keys(s)).toEqual(['chat-a', 'chat-b', 'chat-c'])
      expect(s.closingSlots).toEqual({})
    } finally {
      vi.useRealTimers()
    }
  })

  it('filters only the closed key out of a stalled pre-confirmation reply that lands after the cap', () => {
    // The server confirmed the close, so a reply from a fetch that predates it
    // is known to be pre-pop FOR THAT KEY. Once the cap has released the hold,
    // that reply must not put the row back — but an unrelated change it
    // carries is still the newest thing this transport said, and applies.
    vi.useFakeTimers()
    try {
      vi.setSystemTime(new Date('2026-09-16T06:00:00Z'))
      let s = live()
      s = reducer(s, fetchStarted('stalled'))
      s = closing(s)
      s = reducer(s, fulfilled('chat-b'))
      vi.setSystemTime(new Date('2026-09-16T06:00:31Z'))
      s = reducer(s, sseSlots([A, C]))
      expect(s.closingSlots).toEqual({})
      expect(s.staleSlotFetches).toEqual({ stalled: ['chat-b'] })
      s = reducer(s, httpReply([A, B, { ...C, title: 'renamed' }], 'stalled'))
      expect(keys(s)).toEqual(['chat-a', 'chat-c'])
      expect(s.slots.find(x => x.key === 'chat-c')?.title).toBe('renamed')
      expect(s.staleSlotFetches).toEqual({})
      expect(s.slotFetchesInFlight).toEqual([])
      // A fetch issued afterwards is ordinary.
      s = reducer(s, fetchStarted('later'))
      s = reducer(s, httpReply([A, C, B], 'later'))
      expect(keys(s)).toEqual(['chat-a', 'chat-c', 'chat-b'])
    } finally {
      vi.useRealTimers()
    }
  })

  it('drops the stalled reply even when it is itself the first list after the cap', () => {
    // No frame has expired the hold yet; the stalled reply arrives first. It
    // must not expire the hold and then be applied in the same breath.
    vi.useFakeTimers()
    try {
      vi.setSystemTime(new Date('2026-09-16T06:00:00Z'))
      let s = live()
      s = reducer(s, fetchStarted('stalled'))
      s = closing(s)
      s = reducer(s, fulfilled('chat-b'))
      vi.setSystemTime(new Date('2026-09-16T06:00:31Z'))
      s = reducer(s, httpReply([A, B, { ...C, title: 'renamed' }], 'stalled'))
      expect(keys(s)).toEqual(['chat-a', 'chat-c'])
      expect(s.slots.find(x => x.key === 'chat-c')?.title).toBe('renamed')
      expect(s.closingSlots).toEqual({})
      expect(s.staleSlotFetches).toEqual({})
      expect(s.slotFetchesInFlight).toEqual([])
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps a same-key session recreated meanwhile when the stale reply lands', () => {
    // The key was closed, the hold expired with a fetch still stalled, and the
    // key was then resumed (another client, say) and announced by a live frame.
    // The stale reply's row for that key is pre-pop and untrusted — but the
    // key itself is live again, so the on-screen row must survive the reply.
    vi.useFakeTimers()
    try {
      vi.setSystemTime(new Date('2026-09-16T06:00:00Z'))
      let s = live()
      s = reducer(s, fetchStarted('stalled'))
      s = closing(s)
      s = reducer(s, fulfilled('chat-b'))
      vi.setSystemTime(new Date('2026-09-16T06:00:31Z'))
      s = reducer(s, sseSlots([A, C]))
      expect(s.staleSlotFetches).toEqual({ stalled: ['chat-b'] })
      const recreated = { ...B, title: 'resumed', messages: 9 }
      s = reducer(s, sseSlots([A, C, recreated]))
      const onScreen = s.slots.find(x => x.key === 'chat-b')
      s = reducer(s, httpReply([A, { ...B, title: 'pre-pop' }, C], 'stalled'))
      expect(keys(s)).toEqual(['chat-a', 'chat-b', 'chat-c'])
      expect(s.slots.find(x => x.key === 'chat-b')).toBe(onScreen)
      expect(s.staleSlotFetches).toEqual({})
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not wait for a fetch that started after confirmation', () => {
    // A fetch issued after the 200 is answered from the post-pop registry.
    let s = closing()
    s = reducer(s, fulfilled('chat-b'))
    s = reducer(s, fetchStarted('fresh'))
    for (let i = 0; i < 3; i++) s = reducer(s, sseSlots([A, C]))
    expect(s.closingSlots).toEqual({})
    s = reducer(s, httpReply([A, C], 'fresh'))
    expect(s.slotFetchesInFlight).toEqual([])
  })

  it('lets a same-key retry own the hold; the earlier attempt\'s late `rejected` is ignored', () => {
    // Attempt 1 fails; its `rejected` trails an unbounded peer navigation. The
    // user retries the close before it fires. That stale `rejected` must not
    // release attempt 2's hold, or a frame still listing the key restores it.
    let s = live()
    s = reducer(s, pending('chat-b', 'attempt-1'))
    s = reducer(s, removeSlotOptimistic('chat-b'))
    s = reducer(s, releaseCloseHold({ key: 'chat-b', requestId: 'attempt-1' }))
    s = reducer(s, httpReply([A, B, C], 'recover'))
    expect(keys(s)).toEqual(['chat-a', 'chat-b', 'chat-c'])
    s = reducer(s, pending('chat-b', 'attempt-2'))
    s = reducer(s, removeSlotOptimistic('chat-b'))
    s = reducer(s, rejected('chat-b', 'attempt-1'))
    expect(s.closingSlots['chat-b']?.requestId).toBe('attempt-2')
    s = reducer(s, sseSlots([A, B, C]))
    expect(keys(s)).toEqual(['chat-a', 'chat-c'])
    // And attempt 1's stale `fulfilled` cannot move attempt 2 off the clock.
    s = reducer(s, fulfilled('chat-b', 'attempt-1'))
    expect(s.closingSlots['chat-b']?.inFlightUntil).not.toBeNull()
  })

  it('tracks concurrent closes independently', () => {
    let s = closing()
    s = reducer(s, pending('chat-c'))
    s = reducer(s, removeSlotOptimistic('chat-c'))
    s = reducer(s, sseSlots([A, B, C]))
    expect(keys(s)).toEqual(['chat-a'])
    s = reducer(s, fulfilled('chat-c'))
    for (let i = 0; i < 3; i++) s = reducer(s, sseSlots([A, B]))
    expect(keys(s)).toEqual(['chat-a'])
    // C's confirmed hold has retired; B's in-flight hold has not.
    expect(Object.keys(s.closingSlots)).toEqual(['chat-b'])
  })

  it('does not evict a held slot\'s subagent state before the server confirms', () => {
    // Existing invariant: only an authoritative list that OMITS the key may
    // tear down sub-agent state, because a failed close leaves the slot live.
    let s = live()
    s = reducer(s, sseSubagentStatus({ slot: 'chat-b', running: 1 } as never))
    s = closing(s)
    s = reducer(s, sseSlots([A, B, C]))
    expect(s.subagentRunning['chat-b']).toBeDefined()
    // Teardown follows the frame's membership, not the hold.
    s = reducer(s, sseSlots([A, C]))
    expect(s.subagentRunning['chat-b']).toBeUndefined()
  })

  it('refuses prototype keys instead of throwing inside the pending reducer', () => {
    // Slot keys are server-minted and never look like this, but the record is
    // written by a dynamic property name and must stay behind the same
    // `isUnsafeKey` guard as every other per-slot map in the slice.
    for (const key of ['__proto__', 'constructor', 'prototype']) {
      let s = live()
      expect(() => { s = reducer(s, pending(key)) }).not.toThrow()
      expect(Object.keys(s.closingSlots)).toEqual([])
      expect(() => { s = reducer(s, fulfilled(key)) }).not.toThrow()
      expect(() => { s = reducer(s, rejected(key)) }).not.toThrow()
      expect(() => { s = reducer(s, releaseCloseHold({ key, requestId: 'r' })) }).not.toThrow()
      expect(Object.getPrototypeOf(s.closingSlots)).toBe(Object.prototype)
      expect(keys(s)).toEqual(['chat-a', 'chat-b', 'chat-c'])
    }
  })

  it('keeps row identity for untouched rows while a hold filters the frame', () => {
    let s = closing()
    const a0 = s.slots[0]
    s = reducer(s, sseSlots([A, B, C]))
    expect(s.slots[0]).toBe(a0)
  })
})
