import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

// Pins that an older-history request is CANCELLED at the network layer when the
// user switches chat, not merely ignored once it lands.
const TOTAL = 500
const PAGE = 200 // the resume endpoint returns the last 200 rows

type FakeMsg = { role: string; content: string; ts: string }

/** Deterministic history: row N has content `mN` so identity is unambiguous. */
const HISTORY: FakeMsg[] = Array.from({ length: TOTAL }, (_, i) => ({
  role: i % 2 === 0 ? 'user' : 'assistant',
  content: `m${i}`,
  ts: new Date(Date.UTC(2026, 0, 1, 0, 0, i)).toISOString(),
}))

/** Signals handed to each PAGINATED call, in call order. */
let olderSignals: (AbortSignal | undefined)[] = []
/** Releases the Nth paginated call, so a test controls when a page lands. */
let releaseOlder: Array<() => void> = []
/** When set, the paginated call fails outright instead of hanging. */
let failOlderWith: Error | null = null
/** When set, switchSlot's detail fetch is held open, holding the switch window. */
let holdSwitchDetail = false
let releaseSwitch: Array<() => void> = []

vi.mock('../api/client', () => ({
  api: {
    chatSlotDetail: vi.fn((_slot: string, limit?: number, before?: number, signal?: AbortSignal) => {
      const lim = limit ?? PAGE
      const end = before !== undefined ? Math.max(0, Math.min(before, TOTAL)) : TOTAL
      const start = Math.max(0, end - lim)
      const body = { messages: HISTORY.slice(start, end), has_more: start > 0, total: TOTAL, next_before: start }
      // switchSlot's fetch is unpaginated and normally must not block; only the
      // older page is held open, because that is the request under test.
      if (before === undefined) {
        if (!holdSwitchDetail) return Promise.resolve(body)
        return new Promise((resolve) => { releaseSwitch.push(() => resolve(body)) })
      }
      olderSignals.push(signal)
      if (failOlderWith) return Promise.reject(failOlderWith)
      return new Promise((resolve, reject) => {
        releaseOlder.push(() => resolve(body))
        // Mirror fetch(): an aborted request REJECTS with an AbortError.
        signal?.addEventListener('abort', () =>
          reject(new DOMException('Aborted', 'AbortError')))
      })
    }),
    resumeChatSlot: vi.fn(() => Promise.resolve({ ok: true })),
  },
}))

import chatReducer, {
  setActiveSlot,
  resumeFromHistory,
  loadOlderMessages,
  switchSlot,
  refreshSlot,
  clearMessages,
  isSupersededPagingRejection,
} from './chatSlice'
import { api } from '../api/client'

function makeStore() {
  return configureStore({
    reducer: { chat: chatReducer },
    middleware: (getDefault) => getDefault({ serializableCheck: false, immutableCheck: false }),
  })
}

/** Arms the cursor the way resume does: last PAGE of TOTAL, has_more true. */
function resumed(store: ReturnType<typeof makeStore>, key = 'A') {
  store.dispatch(setActiveSlot(key))
  store.dispatch(
    resumeFromHistory.fulfilled(
      { ok: true, key, nextBefore: TOTAL - PAGE, messages: HISTORY.slice(TOTAL - PAGE), hasMore: true, total: TOTAL },
      'req-resume',
      { key, title: key },
    ),
  )
}

/** Lets queued microtasks (thunk settle + reducer) run. */
const flush = () => new Promise<void>((r) => setTimeout(r, 0))

beforeEach(() => {
  olderSignals = []
  releaseOlder = []
  failOlderWith = null
  holdSwitchDetail = false
  releaseSwitch = []
})

describe('older-history fetch is cancelled when the user switches chat', () => {
  it('passes an AbortSignal to the paginated request', async () => {
    const store = makeStore()
    resumed(store)

    const p = store.dispatch(loadOlderMessages())
    await flush()

    // Positive field: the request carries a real signal, so it CAN be cancelled.
    expect(olderSignals[0]).toBeInstanceOf(AbortSignal)

    releaseOlder[0]()
    await p
  })

  it('aborts the in-flight signal when switchSlot is dispatched', async () => {
    const store = makeStore()
    resumed(store)

    const p = store.dispatch(loadOlderMessages())
    await flush()
    expect(olderSignals[0]!.aborted).toBe(false)

    store.dispatch(switchSlot('B'))
    await flush()

    // Positive field: the request the user left behind is actually cancelled.
    expect(olderSignals[0]!.aborted).toBe(true)
    await p
  })

  it('drops a late older page addressed to the chat the user left', async () => {
    const store = makeStore()
    resumed(store)

    // switchSlot.pending moves activeSlot synchronously as it is dispatched, so
    // by the time any older page could land the pane has already changed.
    store.dispatch(setActiveSlot('B'))
    const before = store.getState().chat.messages.map((m) => m.content)

    // Defence in depth behind the abort: a response already decoded when the
    // switch lands must still not enter the pane it was not fetched for.
    store.dispatch(loadOlderMessages.fulfilled(
      { slot: 'A', nextBefore: 0, messages: HISTORY.slice(0, 10), hasMore: false, total: TOTAL },
      'req-older', undefined,
    ))

    const after = store.getState().chat.messages.map((m) => m.content)
    expect(after).toEqual(before)
  })

  it('clears loadingOlder after the abort so the new chat can page', async () => {
    const store = makeStore()
    resumed(store)

    const p = store.dispatch(loadOlderMessages())
    await flush()
    expect(store.getState().chat.loadingOlder).toBe(true)

    store.dispatch(switchSlot('B'))
    // Bounded so an uncancelled fetch fails the ASSERTION rather than timing out.
    await Promise.race([p, new Promise((r) => setTimeout(r, 250))])
    await flush()

    // A stuck flag would silently block paging in the chat the user is now in.
    expect(store.getState().chat.loadingOlder).toBe(false)
  })

  it('leaves a settled fetch alone: a later switch does not abort it', async () => {
    const store = makeStore()
    resumed(store)

    const p = store.dispatch(loadOlderMessages())
    await flush()
    releaseOlder[0]()
    await p
    await flush()

    store.dispatch(switchSlot('B'))
    await flush()

    // The handle is released on settle, so switchSlot has nothing to cancel.
    expect(olderSignals[0]!.aborted).toBe(false)
  })
})

// The pin-jump effect turns any rejection into a "message unavailable" notice,
// so a chat switch would now show it for a pin that is perfectly reachable.
describe('an abort is distinguishable from a real failure', () => {
  /** Dispatches an older-history page, then aborts it via a chat switch. */
  async function rejectionFromSwitch(): Promise<unknown> {
    const store = makeStore()
    resumed(store)
    let caught: unknown = Symbol('nothing thrown')
    const p = store.dispatch(loadOlderMessages()).unwrap().catch((e) => { caught = e })
    await flush()
    store.dispatch(switchSlot('B'))
    await p
    return caught
  }

  it('recognises the rejection a chat switch produces', async () => {
    const err = await rejectionFromSwitch()
    // Without this the pin-jump catch shows a false "message unavailable".
    expect(isSupersededPagingRejection(err)).toBe(true)
  })

  it('does NOT recognise a genuine fetch failure, so real errors still surface', async () => {
    failOlderWith = new Error('upstream exploded')
    const store = makeStore()
    resumed(store)
    let caught: unknown = Symbol('nothing thrown')
    await store.dispatch(loadOlderMessages()).unwrap().catch((e) => { caught = e })

    // Over-broad suppression would silently swallow the unavailable notice.
    expect(isSupersededPagingRejection(caught)).toBe(false)
  })

  it('cannot be detected with instanceof, which is why the guard reads name', async () => {
    const err = await rejectionFromSwitch()
    // unwrap() rethrows RTK's SERIALIZED error, so the `instanceof DOMException`
    // form used elsewhere in this codebase is always false here.
    expect(err instanceof Error).toBe(false)
    expect((err as { name?: string }).name).toBe('AbortError')
  })

  it('is guarded at the pin-jump call site before the unavailable notice', async () => {
    const fs = await import('node:fs')
    const path = await import('node:path')
    const src = fs.readFileSync(path.resolve(__dirname, '../pages/ChatPage.tsx'), 'utf8')

    // The catch must return on an abort BEFORE reaching the notice.
    const guard = src.indexOf('if (isSupersededPagingRejection(err)) return')
    const notice = src.indexOf("setPinNotice(i18nT('pages.chat.pins.message_unavailable'))", guard)
    expect(guard).toBeGreaterThan(-1)
    expect(notice).toBeGreaterThan(guard)
  })

  it('also covers a refused dispatch, which is not an unreachable pin either', () => {
    // A cursor-refused or already-loading dispatch surfaces as ConditionError.
    expect(isSupersededPagingRejection({ name: 'ConditionError', message: 'x' })).toBe(true)
  })
})

// A switch moves activeSlot at once but replaces the cursor only when the detail
// fetch settles, so paging in between reads the new chat at the old chat's offset.
describe('a same-slot switch also invalidates the cursor', () => {
  it('does not page against the cursor the switch is about to replace', async () => {
    holdSwitchDetail = true
    const store = makeStore()
    resumed(store)

    const p = store.dispatch(loadOlderMessages())
    await flush()
    // ChatPage dispatches switchSlot(activeSlot) on mount, so the key is unchanged.
    store.dispatch(switchSlot('A'))
    await p.catch(() => {})
    await flush()

    const s1 = store.getState().chat
    expect(s1.activeSlot).toBe('A')
    expect(s1.loadingOlder).toBe(false)

    const mock = api.chatSlotDetail as unknown as { mock: { calls: unknown[][] } }
    const before = mock.mock.calls.length
    void store.dispatch(loadOlderMessages())
    await flush()
    const paged = mock.mock.calls.slice(before).filter((c) => c[2] !== undefined)
    // The switch is re-fetching this chat and will replace the cursor; paging now
    // would prepend a page the refresh already contains and rewind the cursor.
    expect(paged).toEqual([])
  })
})

describe('the cursor cannot be used across a switch', () => {
  it('does not fetch the new chat at the old chat\'s offset', async () => {
    holdSwitchDetail = true
    const store = makeStore()
    resumed(store)
    expect(store.getState().chat.slotOldestIndex).toBe(TOTAL - PAGE)

    const p = store.dispatch(loadOlderMessages())
    await flush()
    store.dispatch(switchSlot('B'))
    await p.catch(() => {})
    await flush()

    // activeSlot moved and the lock is released, but the cursor value is still the
    // old chat's -- unkeyed, so it cannot be read as the new chat's.
    const s1 = store.getState().chat
    expect(s1.activeSlot).toBe('B')
    expect(s1.loadingOlder).toBe(false)
    expect(s1.slotOldestIndex).toBe(TOTAL - PAGE)
    expect(s1.slotCursorKey).toBeNull()

    const mock = api.chatSlotDetail as unknown as { mock: { calls: unknown[][] } }
    const before = mock.mock.calls.length
    void store.dispatch(loadOlderMessages())
    await flush()
    const paged = mock.mock.calls.slice(before).filter((c) => c[2] !== undefined)
    // A call here would be slot B read at slot A's offset.
    expect(paged).toEqual([])
  })

  it('pages normally again once the switch installs the new cursor', async () => {
    const store = makeStore()
    resumed(store)
    store.dispatch(switchSlot('B'))
    await flush()
    await flush()

    // switchSlot.fulfilled re-keys the cursor, so paging is allowed again.
    expect(store.getState().chat.slotCursorKey).toBe('B')
    const p = store.dispatch(loadOlderMessages())
    await flush()
    releaseOlder[0]()
    const res = await p
    expect(res.meta.requestStatus).toBe('fulfilled')
  })
})

describe('a background refresh must not re-validate a cursor a pending switch invalidated', () => {
  /** The payload refreshSlot.fulfilled receives, at an offset of its own. */
  const refreshPayload = (key: string, nextBefore: number) => ({
    key,
    nextBefore,
    messages: HISTORY.slice(nextBefore, nextBefore + PAGE),
    running: false,
    stopping: false,
    hasMore: true,
    total: TOTAL,
    queue: [],
    context: undefined,
  })

  /** Arms the window: a same-slot switch in flight, then a refresh landing inside it.
   *  refreshSlot's own `activeSlot !== key` guard passes, the key being unchanged. */
  async function refreshInsidePendingSwitch(store: ReturnType<typeof makeStore>) {
    holdSwitchDetail = true
    resumed(store)
    store.dispatch(switchSlot('A'))
    await flush()
    store.dispatch(refreshSlot.fulfilled(refreshPayload('A', 40), 'req-refresh', 'A'))
    await flush()
  }

  it('leaves the cursor invalidated: the pending switch still owns it', async () => {
    const store = makeStore()
    await refreshInsidePendingSwitch(store)
    expect(store.getState().chat.slotCursorKey).toBeNull()
  })

  it('does not page while a switch is pending, even after a refresh lands', async () => {
    const store = makeStore()
    await refreshInsidePendingSwitch(store)

    const mock = api.chatSlotDetail as unknown as { mock: { calls: unknown[][] } }
    const before = mock.mock.calls.length
    void store.dispatch(loadOlderMessages())
    await flush()
    const paged = mock.mock.calls.slice(before).filter((c) => c[2] !== undefined)
    // A call here pages against the refresh's offset and then rewinds the cursor
    // the switch is about to install.
    expect(paged).toEqual([])
  })

  it('still re-keys when no switch is pending, so a refresh does not dead-end paging', async () => {
    const store = makeStore()
    resumed(store)

    store.dispatch(refreshSlot.fulfilled(refreshPayload('A', 40), 'req-refresh', 'A'))
    await flush()

    // No switch is racing, so the refresh owns the cursor it installed.
    expect(store.getState().chat.slotCursorKey).toBe('A')
    expect(store.getState().chat.slotOldestIndex).toBe(40)

    const mock = api.chatSlotDetail as unknown as { mock: { calls: unknown[][] } }
    const before = mock.mock.calls.length
    void store.dispatch(loadOlderMessages())
    await flush()
    const paged = mock.mock.calls.slice(before).filter((c) => c[2] !== undefined)
    expect(paged).toEqual([['A', 100, 40, expect.anything()]])
  })

  it('a superseded settle does not release the claim a newer switch holds', async () => {
    holdSwitchDetail = true
    const store = makeStore()
    resumed(store)

    store.dispatch(switchSlot('A'))
    await flush()
    store.dispatch(switchSlot('A'))
    await flush()

    // Release the FIRST switch's fetch. Its settle must leave the second switch's
    // claim standing, or the second switch's window silently reopens.
    releaseSwitch[0]()
    await flush()
    await flush()
    expect(store.getState().chat.slotSwitchRequestId).not.toBeNull()

    releaseSwitch[1]()
    await flush()
    await flush()
    expect(store.getState().chat.slotSwitchRequestId).toBeNull()
  })
})

describe('clearing a transcript gives the cursor a definite value', () => {
  it('keys the empty cursor to this pane, so a waiting pin jump can resolve', () => {
    const store = makeStore()
    resumed(store)
    store.dispatch(clearMessages())
    const s = store.getState().chat
    expect(s.slotCursorKey).toBe('A')
    expect(s.slotHasMore).toBe(false)
  })

  it('but a pending switch still owns the cursor across a clear', async () => {
    holdSwitchDetail = true
    const store = makeStore()
    resumed(store)
    store.dispatch(switchSlot('A'))
    await flush()
    store.dispatch(clearMessages())
    expect(store.getState().chat.slotCursorKey).toBeNull()
  })
})

describe('a settle dispatched without thunk meta is tolerated', () => {
  it('does not throw when the action carries no meta', () => {
    const store = makeStore()
    resumed(store)
    // Several suites dispatch the bare action object, which has no `meta` at all.
    expect(() => store.dispatch({
      type: switchSlot.fulfilled.type,
      payload: { key: 'A', messages: [], hasMore: false, total: 0, nextBefore: 0, running: false, stopping: false, queue: [] },
    })).not.toThrow()
    expect(store.getState().chat.slotCursorKey).toBe('A')
  })
})

describe('a slot activated during a switch keeps its own cursor', () => {
  // The switch's settle returns early once activeSlot has moved, so if an
  // activating writer also defers, no cursor is ever installed.
  it('createSlot activating a new slot mid-switch still installs a cursor', () => {
    const store = makeStore()
    resumed(store)
    store.dispatch({
      type: switchSlot.fulfilled.type,
      meta: { requestId: 'r0', arg: 'A' },
      payload: { key: 'A', messages: [], hasMore: true, total: 0, nextBefore: 300, running: false, stopping: false, queue: [] },
    })
    expect(store.getState().chat.slotCursorKey).toBe('A')

    // A same-key switch takes the claim; activeSlot is unchanged, so createSlot's
    // own switched-away guard does not fire.
    store.dispatch({ type: switchSlot.pending.type, meta: { requestId: 'r1', arg: 'A' } })
    expect(store.getState().chat.slotCursorKey).toBeNull()

    store.dispatch({
      type: 'chat/createSlot/fulfilled',
      meta: { requestId: 'c1', activate: true, originActiveSlot: 'A' },
      payload: { key: 'NEW' },
    })
    expect(store.getState().chat.activeSlot).toBe('NEW')

    // The switch settles but bails: activeSlot is NEW, not A.
    store.dispatch({
      type: switchSlot.fulfilled.type,
      meta: { requestId: 'r1', arg: 'A' },
      payload: { key: 'A', messages: [], hasMore: true, total: 0, nextBefore: 900, running: false, stopping: false, queue: [] },
    })

    const s = store.getState().chat
    expect(s.activeSlot).toBe('NEW')
    expect(s.slotCursorKey).toBe('NEW')
  })
})
