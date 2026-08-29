/**
 * The switchSlot thunk boundary carries the numeric HTTP status (#6199).
 *
 * The classifier tests in `../test/agentSessionResumeMissingSlot.test.ts` feed
 * `isMissingSlotError` its payload directly, and the flow tests there fake
 * `unwrap()` — so neither would notice if `switchSlot` itself stopped producing
 * the payload. This file pins the WIRING: the real thunk, dispatched against a
 * real store, must reject with `{ status, message }` (via `rejectWithValue`,
 * which `unwrap()` throws verbatim) whenever the slot-detail fetch failed with
 * a status attached, and must keep the ordinary serialized-error shape when no
 * status exists, so the prose fallback still has something to read.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

vi.mock('../api/client', () => ({ api: { chatSlotDetail: vi.fn() } }))

import chatReducer, { switchSlot, warmSlotCache, setActiveSlot, setSlotState, setSlotRunning, startLocalTurn, sseChatMessage, clearMessages, clearSlotCache, slotsSnapshotApplied } from './chatSlice'
import { api } from '../api/client'
import { isMissingSlotError } from '../utils/thunkError'

function makeStore() {
  return configureStore({
    reducer: { chat: chatReducer },
    // serializableCheck stays ON deliberately: "the payload can safely enter
    // the store" is part of the contract this file pins, and the check is what
    // would flag a future payload smuggling a Response or Error instance.
    middleware: (getDefault) => getDefault({ immutableCheck: false }),
  })
}

const detail = vi.mocked(api.chatSlotDetail)

/** What the api client throws, shaped structurally (`status` + `message`) the
 *  way `ApiError` carries them. The thunk's catch is deliberately structural
 *  rather than `instanceof ApiError` — mocking `../api/client` wholesale, as
 *  this file and its siblings do, is exactly why (see the comment in
 *  `switchSlot`) — so the real class is not needed to exercise it. */
const apiError = (status: number, message: string) => Object.assign(new Error(message), { status })

/** The value `unwrap()` throws for *key*, or null if the switch succeeded. */
async function rejection(key: string): Promise<unknown> {
  try {
    await makeStore().dispatch(switchSlot(key)).unwrap()
    return null
  } catch (e) {
    return e
  }
}

describe('switchSlot — the rejection carries the numeric status (#6199)', () => {
  beforeEach(() => vi.clearAllMocks())

  it('rejects with { status, message }, and 404 classifies as slot-gone', async () => {
    // The message deliberately carries NO prose hint ("404"/"not found"): the
    // pre-fix classifier answered false here, so this case pins the
    // false-NEGATIVE half of the bug, not just the wiring.
    detail.mockRejectedValue(apiError(404, 'slot unavailable'))
    const e = await rejection('gone')
    expect(e).toEqual({ status: 404, message: 'slot unavailable' })
    expect(isMissingSlotError(e)).toBe(true)
  })

  it('a 500 quoting "not found" is NOT a missing slot, end to end', async () => {
    // The shipped regression: before the status survived the boundary, this
    // rejection matched /not found/i and a live session was replaced.
    detail.mockRejectedValue(apiError(500, 'agent "foo" not found'))
    const e = await rejection('alive')
    expect(e).toEqual({ status: 500, message: 'agent "foo" not found' })
    expect(isMissingSlotError(e)).toBe(false)
  })

  it('a status-less failure keeps the serialized-error shape for the prose fallback', async () => {
    detail.mockRejectedValue(new TypeError('Failed to fetch'))
    const e = await rejection('k')
    // miniSerializeError: a plain object, not an Error, message preserved.
    expect(e).toMatchObject({ message: 'Failed to fetch' })
    expect(e instanceof Error).toBe(false)
    expect(isMissingSlotError(e)).toBe(false)
  })
})

/**
 * switchSlot.rejected must not strand the store on a slot that could not load
 * (#6309). `pending` assigns `activeSlot` synchronously; when the fetch then
 * 404s the target is GONE, so the reducer restores the pre-switch selection —
 * activeSlot, its cached message page, its paging cursor — and leaves the MRU
 * as if no switch happened (in particular the gone key must NOT return to the
 * stack: that is regression 3 of the three that caller-side compensation
 * shipped on #6260). A transient failure keeps the target selected so a retry
 * can succeed. Exercised end to end: real thunk, real store, mocked transport.
 */
describe('switchSlot.rejected — a gone target restores the pre-switch selection (#6309)', () => {
  // Braces matter: `mockReset()` returns the mock (a function), and a hook
  // that RETURNS a function hands vitest a teardown callback — the mock then
  // gets invoked argument-less after each test and its rejected promise fails
  // the test from the outside. Return void instead.
  beforeEach(() => { detail.mockReset() })

  type Page = { messages: Array<{ role: string; content: string; ts: string }>; has_more: boolean; total: number; next_before: number }
  const msg = (role: string, content: string, i: number) =>
    ({ role, content, ts: new Date(Date.UTC(2026, 0, 1, 0, 0, i)).toISOString() })
  const PAGES: Record<string, Page> = {
    C: { messages: [msg('user', 'c0', 0)], has_more: false, total: 1, next_before: 0 },
    // has_more + a non-zero cursor, so the test can tell "restored" from
    // "reset to zero" on every cursor field, not just the key.
    A: { messages: [msg('user', 'hello', 0), msg('assistant', 'hi', 1)], has_more: true, total: 42, next_before: 7 },
  }

  /** Store selecting A with a loaded pane, MRU ['C'], via real switches. */
  async function primedStore() {
    detail.mockImplementation((key: string) => {
      if (key in PAGES) return Promise.resolve(PAGES[key])
      if (key === 'flaky') return Promise.reject(apiError(500, 'gateway hiccup'))
      return Promise.reject(apiError(404, 'slot unavailable'))
    })
    const store = makeStore()
    await store.dispatch(switchSlot('C'))
    await store.dispatch(switchSlot('A'))
    return store
  }

  it('404: restores the prior activeSlot, its exact cached page, and its paging cursor', async () => {
    const store = await primedStore()
    await store.dispatch(switchSlot('gone'))
    const s = store.getState().chat
    expect(s.activeSlot).toBe('A')
    // The page itself, not merely "non-empty": the re-hydrate must hand back
    // A's cached transcript, not some other slot's or a placeholder.
    expect(s.messages.map(m => [m.role, m.content])).toEqual([['user', 'hello'], ['assistant', 'hi']])
    expect(s.slotLoading).toBe(false)
    // The cursor trio is re-keyed to the restored slot with its captured
    // values — `pending` voided the key, and without the restore older-history
    // paging refuses forever on a pane that plainly has more.
    expect(s.slotCursorKey).toBe('A')
    expect(s.slotHasMore).toBe(true)
    expect(s.slotOldestIndex).toBe(7)
    // The claim is released either way.
    expect(s.slotSwitchRequestId).toBeNull()
    expect(s.slotSwitchOrigin).toBeNull()
  })

  it('404: the MRU ends as if no switch happened, and the gone key does not return to it', async () => {
    let goneDeleted = false
    detail.mockImplementation((key: string) => {
      if (key === 'gone') return goneDeleted ? Promise.reject(apiError(404, 'slot unavailable')) : Promise.resolve(PAGES.C)
      return Promise.resolve(PAGES[key] ?? PAGES.C)
    })
    const store = makeStore()
    // Visit gone → C → A, so 'gone' sits on the MRU like any real past slot.
    await store.dispatch(switchSlot('gone'))
    await store.dispatch(switchSlot('C'))
    await store.dispatch(switchSlot('A'))
    expect(store.getState().chat.slotHistory).toEqual(['gone', 'C'])
    goneDeleted = true
    await store.dispatch(switchSlot('gone'))
    const s = store.getState().chat
    expect(s.activeSlot).toBe('A')
    // Exact array: A (restored, active ∉ history) is back out, C is untouched,
    // and the DELETED key stayed stripped rather than being pushed back on —
    // Alt+` must never aim at a session that no longer exists.
    expect(s.slotHistory).toEqual(['C'])
  })

  it('a transient failure keeps the target selected with a cleared pane, so a retry can succeed', async () => {
    const store = await primedStore()
    await store.dispatch(switchSlot('flaky'))
    const s = store.getState().chat
    expect(s.activeSlot).toBe('flaky')
    expect(s.messages).toEqual([])
    expect(s.slotLoading).toBe(false)
  })

  it('a 404 landing after the user already switched on does not yank the selection', async () => {
    let rejectGone!: (e: unknown) => void
    detail.mockImplementation((key: string) => {
      if (key === 'gone') return new Promise((_resolve, reject) => { rejectGone = reject })
      return Promise.resolve(PAGES[key] ?? PAGES.C)
    })
    const store = makeStore()
    await store.dispatch(switchSlot('A'))
    const inflight = store.dispatch(switchSlot('gone')) // held open
    await store.dispatch(switchSlot('C')) // user moved on; C owns the claim now
    rejectGone(apiError(404, 'slot unavailable'))
    await inflight
    const s = store.getState().chat
    expect(s.activeSlot).toBe('C')
    expect(s.messages.map(m => [m.role, m.content])).toEqual([['user', 'c0']])
  })

  it('a same-key 404 (the active slot itself was deleted) falls back to the cleared pane', async () => {
    const store = await primedStore()
    detail.mockImplementation(() => Promise.reject(apiError(404, 'slot unavailable')))
    await store.dispatch(switchSlot('A'))
    const s = store.getState().chat
    // There is nothing earlier to restore to — the origin IS the failed target —
    // so this keeps the pre-#6309 behavior rather than "restoring" to the dead slot.
    expect(s.activeSlot).toBe('A')
    expect(s.messages).toEqual([])
    expect(s.slotLoading).toBe(false)
  })

  it('a rapid A→B→C chain whose C 404s falls back to settled A, not half-loaded B', async () => {
    let releaseB!: (v: unknown) => void
    detail.mockImplementation((key: string) => {
      if (key === 'B') return new Promise((resolve) => { releaseB = resolve })
      if (key === 'gone') return Promise.reject(apiError(404, 'slot unavailable'))
      return Promise.resolve(PAGES[key] ?? PAGES.C)
    })
    const store = makeStore()
    await store.dispatch(switchSlot('A'))
    const inflightB = store.dispatch(switchSlot('B')) // held open: B never settles
    await store.dispatch(switchSlot('gone'))
    const s = store.getState().chat
    // The provisional B view is not a selection worth restoring — pending kept
    // the settled origin, so the fallback is A with its real page and cursor.
    expect(s.activeSlot).toBe('A')
    expect(s.messages.map(m => [m.role, m.content])).toEqual([['user', 'hello'], ['assistant', 'hi']])
    expect(s.slotCursorKey).toBe('A')
    expect(s.slotHasMore).toBe(true)
    // B rides the MRU as a transit (the navigation-stack contract: the MRU
    // records where the user aimed; a jump to it re-fetches). A is back out
    // (active ∉ history) and the gone key never enters.
    expect(s.slotHistory).toEqual(['B'])
    releaseB(PAGES.C); await inflightB // settle the orphan: it must not clobber A
    expect(store.getState().chat.activeSlot).toBe('A')
  })

  it('an origin that never had a valid cursor restores with paging honestly un-keyed', async () => {
    detail.mockImplementation((key: string) =>
      key === 'gone' ? Promise.reject(apiError(404, 'slot unavailable')) : Promise.resolve(PAGES.C))
    const store = makeStore()
    // Selected directly (no switch settled for it), so no cursor describes A.
    store.dispatch(setActiveSlot('A'))
    await store.dispatch(switchSlot('gone'))
    const s = store.getState().chat
    expect(s.activeSlot).toBe('A')
    expect(s.messages).toEqual([])
    // No guessed cursor: paging stays refused until a real fetch re-keys it.
    expect(s.slotCursorKey).toBeNull()
  })

  it('a run that began mid-flight lands on the restored composer', async () => {
    let rejectGone!: (e: unknown) => void
    detail.mockImplementation((key: string) => {
      if (key === 'gone') return new Promise((_resolve, reject) => { rejectGone = reject })
      return Promise.resolve(PAGES[key] ?? PAGES.C)
    })
    const store = makeStore()
    await store.dispatch(switchSlot('A'))
    const inflight = store.dispatch(switchSlot('gone'))
    // A starts streaming while it is non-active: the frame lands in slotRun.
    store.dispatch(sseChatMessage({ slot: 'A', role: 'chunk', content: 'x' }))
    rejectGone(apiError(404, 'slot unavailable'))
    await inflight
    const s = store.getState().chat
    expect(s.activeSlot).toBe('A')
    expect(s.slotRunning).toBe(true)
    expect(s.slotState).toBe('streaming')
  })

  it('a run that ended mid-flight is not resurrected onto the restored composer', async () => {
    let rejectGone!: (e: unknown) => void
    detail.mockImplementation((key: string) => {
      if (key === 'gone') return new Promise((_resolve, reject) => { rejectGone = reject })
      return Promise.resolve(PAGES[key] ?? PAGES.C)
    })
    const store = makeStore()
    await store.dispatch(switchSlot('A'))
    store.dispatch(setSlotState('streaming')) // A is running when the switch starts
    const inflight = store.dispatch(switchSlot('gone'))
    // The turn finishes while A is non-active; pending's slotRun seed is what
    // lets this _done frame overwrite the stale pre-switch mirror.
    store.dispatch(sseChatMessage({ slot: 'A', role: '_done', content: '' }))
    rejectGone(apiError(404, 'slot unavailable'))
    await inflight
    const s = store.getState().chat
    expect(s.activeSlot).toBe('A')
    expect(s.slotRunning).toBe(false)
    expect(s.slotState).toBe('idle')
  })

  it('a cleared pane does not resurrect its pre-clear transcript through the restore', async () => {
    const store = await primedStore()
    // The backend confirmed a clear for the active slot: the cached page is
    // evicted with the live pane, so no stale copy survives to be restored.
    store.dispatch(clearMessages())
    await store.dispatch(switchSlot('gone'))
    const s = store.getState().chat
    expect(s.activeSlot).toBe('A')
    expect(s.messages).toEqual([])
  })

  it('keepTargetOnMissing: a just-created slot stays selected through its own 404', async () => {
    const store = await primedStore()
    await store.dispatch(switchSlot({ key: 'gone', keepTargetOnMissing: true }))
    const s = store.getState().chat
    // The caller vouched the target exists (it just created it): the reducer
    // keeps it selected with the cleared-pane retry state instead of unwinding.
    expect(s.activeSlot).toBe('gone')
    expect(s.messages).toEqual([])
    expect(s.slotLoading).toBe(false)
  })

  it('a provisional switch does not clobber the half-loaded slot\'s live run entry', async () => {
    let releaseB!: (v: unknown) => void
    // Only B's FIRST read is held open -- that is the device that keeps B's switch in
    // flight across the provisional one, which is what this test is about. Later reads
    // of B resolve: a slot mid-turn gets a BOUNDED first read like any other (run state
    // is not an input to the window), so its coverage check can legitimately ask for a
    // second one, and a mock that answers that with another never-resolving promise
    // hangs the thunk on a harness artifact instead of on the behaviour asserted below.
    // `releaseB` is reassigned by each executor, so a second held promise would also
    // strand the first release with nothing left to call it.
    let bReads = 0
    detail.mockImplementation((key: string) => {
      if (key === 'B') {
        bReads += 1
        if (bReads === 1) return new Promise((resolve) => { releaseB = resolve })
        return Promise.resolve(PAGES.B ?? PAGES.C)
      }
      if (key === 'gone') return Promise.reject(apiError(404, 'slot unavailable'))
      return Promise.resolve(PAGES[key] ?? PAGES.C)
    })
    const store = makeStore()
    await store.dispatch(switchSlot('A')) // A settled, idle
    // B is streaming in the background (its frames landed in slotRun).
    store.dispatch(sseChatMessage({ slot: 'B', role: 'chunk', content: 'x' }))
    const inflightB = store.dispatch(switchSlot('B')) // held open
    await store.dispatch(switchSlot('gone')) // provisional pending: no seed
    const s = store.getState().chat
    expect(s.activeSlot).toBe('A')
    // The provisional pending must NOT have copied A's idle mirror into B:
    // B's turn is still live and its entry still says so.
    expect(s.slotRun['B'].state).toBe('streaming')
    releaseB(PAGES.C); await inflightB
  })

  it('a running-but-not-yet-streaming turn survives the restore round trip', async () => {
    let rejectGone!: (e: unknown) => void
    detail.mockImplementation((key: string) => {
      if (key === 'gone') return new Promise((_resolve, reject) => { rejectGone = reject })
      return Promise.resolve(PAGES[key] ?? PAGES.C)
    })
    const store = makeStore()
    await store.dispatch(switchSlot('A'))
    // The queued-turn window: the turn is in flight but no chunk has arrived,
    // so the fine-grained slotState still reads 'idle' while slotRunning is true.
    store.dispatch(setSlotRunning(true))
    const inflight = store.dispatch(switchSlot('gone'))
    rejectGone(apiError(404, 'slot unavailable'))
    await inflight
    const s = store.getState().chat
    expect(s.activeSlot).toBe('A')
    // slotRun never moved mid-flight, so the SNAPSHOT wins -- deriving from
    // the seeded 'idle' alone would have dropped this live turn.
    expect(s.slotRunning).toBe(true)
  })

  it('a queued turn that COMPLETES mid-flight is not resurrected as busy (same-value round trip)', async () => {
    let rejectGone!: (e: unknown) => void
    detail.mockImplementation((key: string) => {
      if (key === 'gone') return new Promise((_resolve, reject) => { rejectGone = reject })
      return Promise.resolve(PAGES[key] ?? PAGES.C)
    })
    const store = makeStore()
    await store.dispatch(switchSlot('A'))
    // Queued-turn window: running true while the fine-grained state is 'idle'.
    store.dispatch(setSlotRunning(true))
    const inflight = store.dispatch(switchSlot('gone'))
    // The queued turn completes while A is non-active. slotRun goes idle→idle
    // (same value), so only the event-time sync can record the completion.
    store.dispatch(sseChatMessage({ slot: 'A', role: '_done', content: '' }))
    rejectGone(apiError(404, 'slot unavailable'))
    await inflight
    const s = store.getState().chat
    expect(s.activeSlot).toBe('A')
    expect(s.slotRunning).toBe(false)
    expect(s.slotState).toBe('idle')
  })

  it('a locally-sent turn that ends mid-flight releases the pending-turn guard on restore', async () => {
    let rejectGone!: (e: unknown) => void
    detail.mockImplementation((key: string) => {
      if (key === 'gone') return new Promise((_resolve, reject) => { rejectGone = reject })
      return Promise.resolve(PAGES[key] ?? PAGES.C)
    })
    const store = makeStore()
    await store.dispatch(switchSlot('A'))
    store.dispatch(startLocalTurn('A')) // local send awaiting server confirmation
    const inflight = store.dispatch(switchSlot('gone'))
    store.dispatch(sseChatMessage({ slot: 'A', role: '_done', content: '' })) // turn ends while non-active
    rejectGone(apiError(404, 'slot unavailable'))
    await inflight
    const s = store.getState().chat
    expect(s.activeSlot).toBe('A')
    expect(s.slotRunning).toBe(false)
    // The guard fell with the turn: leaving it set would hide Continue and
    // make running=false snapshots for A be ignored indefinitely.
    expect(s.pendingTurnSlot).toBeNull()
  })

  it('an UNCONFIRMED local send keeps its pending-turn guard through the restore', async () => {
    let rejectGone!: (e: unknown) => void
    detail.mockImplementation((key: string) => {
      if (key === 'gone') return new Promise((_resolve, reject) => { rejectGone = reject })
      return Promise.resolve(PAGES[key] ?? PAGES.C)
    })
    const store = makeStore()
    await store.dispatch(switchSlot('A'))
    store.dispatch(startLocalTurn('A'))
    const inflight = store.dispatch(switchSlot('gone'))
    rejectGone(apiError(404, 'slot unavailable')) // no _done arrived
    await inflight
    const s = store.getState().chat
    expect(s.activeSlot).toBe('A')
    // The send is still awaiting confirmation: the guard (and running) survive
    // so a stale server snapshot cannot clobber the just-started turn.
    expect(s.slotRunning).toBe(true)
    expect(s.pendingTurnSlot).toBe('A')
  })

  it('a background slot_clear during the switch does not resurrect through the restore', async () => {
    let rejectGone!: (e: unknown) => void
    detail.mockImplementation((key: string) => {
      if (key === 'gone') return new Promise((_resolve, reject) => { rejectGone = reject })
      return Promise.resolve(PAGES[key] ?? PAGES.C)
    })
    const store = makeStore()
    await store.dispatch(switchSlot('A')) // A loaded and cached on the way out
    const inflight = store.dispatch(switchSlot('gone'))
    // The backend confirms a /clear for A while it is NOT the active view:
    // the cached page is evicted, exactly like the active-slot clearMessages.
    store.dispatch(clearSlotCache('A'))
    rejectGone(apiError(404, 'slot unavailable'))
    await inflight
    const s = store.getState().chat
    expect(s.activeSlot).toBe('A')
    expect(s.messages).toEqual([]) // cleared, not the pre-clear transcript
  })

  it('a stale warm fulfillment cannot unlock a mid-turn origin through the restore', async () => {
    let rejectGone!: (e: unknown) => void
    detail.mockImplementation((key: string) => {
      if (key === 'gone') return new Promise((_resolve, reject) => { rejectGone = reject })
      return Promise.resolve(PAGES[key] ?? PAGES.C)
    })
    const store = makeStore()
    await store.dispatch(switchSlot('A'))
    store.dispatch(setSlotRunning(true)) // a turn is in flight on A
    const inflight = store.dispatch(switchSlot('gone'))
    // A stale warm snapshot (running:false, taken before the turn) lands while
    // the switch is in flight. It must NOT mark the origin idle: it is an
    // unordered point-in-time read, not a run event.
    store.dispatch(warmSlotCache.fulfilled(
      { key: 'A', messages: [], queue: [], hasMore: false, total: 0, running: false, stopping: false, nextBefore: 0, warmSeq: 1, context: undefined } as never,
      'warm-req', 'A'))
    rejectGone(apiError(404, 'slot unavailable'))
    await inflight
    const s = store.getState().chat
    expect(s.activeSlot).toBe('A')
    // The composer stays locked: the turn never emitted a _done frame.
    expect(s.slotRunning).toBe(true)
  })

  it('an origin evicted by an authoritative slots snapshot is not restored', async () => {
    let rejectGone!: (e: unknown) => void
    detail.mockImplementation((key: string) => {
      if (key === 'gone') return new Promise((_resolve, reject) => { rejectGone = reject })
      return Promise.resolve(PAGES[key] ?? PAGES.C)
    })
    const store = makeStore()
    await store.dispatch(switchSlot('A'))
    const inflight = store.dispatch(switchSlot('gone'))
    // The authoritative list drops A while the switch is in flight (activeSlot
    // 'gone' is protected by the reconcile, A is not).
    store.dispatch(slotsSnapshotApplied([]) as never)
    rejectGone(apiError(404, 'slot unavailable'))
    await inflight
    const s = store.getState().chat
    // Restoring deleted A would re-create the dead-slot selection this fix
    // exists to unwind — the rejection falls back to the clearing path instead.
    expect(s.activeSlot).toBe('gone')
    expect(s.messages).toEqual([])
    expect(s.slotSwitchOrigin).toBeNull()
  })
})
