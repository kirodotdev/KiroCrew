import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import type { ChatSlot } from '../types'

/** Declared inline rather than via `./mockApiClient`, so the hoisted `vi.mock` is
 *  registered before `chatSlice` pulls `../api/client` into the graph — and so
 *  `chatSlots` is stubbed too, which the give-up path's refetch calls. */
const { mockDelete, mockSlots, mockDeleteSession } = vi.hoisted(() => ({
  mockDelete: vi.fn(),
  mockSlots: vi.fn(),
  mockDeleteSession: vi.fn(),
}))
vi.mock('../api/client', () => ({
  api: { deleteChatSlot: mockDelete, chatSlots: mockSlots, deleteSession: mockDeleteSession },
}))

const chatSlice = await import('../store/chatSlice')
const dashboardSlice = await import('../store/dashboardSlice')
const { deleteSlot } = chatSlice
const chatReducer = chatSlice.default
const dashboardReducer = dashboardSlice.default

const CREATED = '2026-01-01T00:00:00Z'
const slot = (key: string, created = CREATED): ChatSlot => ({ key, messages: 0, running: false, created })

/** GPT FINDING (blocking) — the retiring read cannot see a resume that raced the
 *  close, and the push carrying that resumed row was withheld and dropped. So the
 *  close must issue a SECOND read after retirement: it postdates both the pop and
 *  the resume, and is the only frame authoritative for both. One read is the bug. */
describe('close confirms after retiring the tombstone', () => {
  it('issues a second slots read once the retiring read resolves', async () => {
    mockDelete.mockResolvedValue(undefined)
    mockSlots.mockResolvedValue([slot('chat-1')])
    const s = store()
    await s.dispatch(deleteSlot('chat-2') as never)
    // Let the chained confirming read run.
    await new Promise(r => setTimeout(r, 0))
    expect(mockSlots.mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  /** GPT FINDING (blocking) — the confirming read's promise was DROPPED, not
   *  returned, so a failed confirmation never reached the handler. A resume that
   *  raced retirement was withheld and dropped, and the row stayed hidden with
   *  nothing left to restore it. A failure must release and re-read. */
  it('recovers when the confirming read fails', async () => {
    mockDelete.mockResolvedValue(undefined)
    // The retirement read succeeds; the confirming read that follows it fails.
    mockSlots.mockResolvedValueOnce([slot('chat-1')])
      .mockRejectedValueOnce(status(503))
      .mockResolvedValue([slot('chat-1'), slot('chat-2')])
    const s = store()
    await s.dispatch(deleteSlot('chat-2') as never)
    for (let i = 0; i < 6; i++) await new Promise(r => setTimeout(r, 0))
    // Nothing may stay withheld, and a further read must have been issued.
    expect(closingOf(s)['chat-2']).toBeUndefined()
    expect(mockSlots.mock.calls.length).toBeGreaterThanOrEqual(3)
  })

  /** GPT FINDING (blocking) — a REFUSED confirmation RESOLVES, so keying recovery on
   *  rejection alone missed it entirely: a slot creation overtaking the confirm makes the
   *  reducer discard its list while `unwrap()` still resolves, and the resumed row stayed
   *  hidden with nothing left to restore it. Resolution is not confirmation. */
  it('recovers when the confirming read is refused rather than failed', async () => {
    mockDelete.mockResolvedValue(undefined)
    let releaseConfirm: (v: ChatSlot[]) => void = () => {}
    // The retirement read succeeds; the confirming read is held open so a move can
    // overtake it, then answers normally — a resolution the reducer will discard.
    mockSlots.mockResolvedValueOnce([slot('chat-1')])
      .mockReturnValueOnce(new Promise<ChatSlot[]>(r => { releaseConfirm = r }))
      .mockResolvedValue([slot('chat-1'), slot('chat-2')])
    const s = store()
    const verdicts: boolean[] = []
    s.subscribe(() => {
      const v = s.getState().dashboard.lastSlotsRead
      if (v) verdicts.push(v.applied)
    })
    await s.dispatch(deleteSlot('chat-2') as never)
    for (let i = 0; i < 4; i++) await new Promise(r => setTimeout(r, 0))
    s.dispatch(dashboardSlice.addSlotOptimistic(slot('chat-3')) as never)
    releaseConfirm([slot('chat-1')])
    for (let i = 0; i < 8; i++) await new Promise(r => setTimeout(r, 0))
    // The setup has to genuinely produce a refusal, or the read count below proves nothing.
    expect(verdicts).toContain(false)
    expect(mockSlots.mock.calls.length).toBeGreaterThanOrEqual(3)
  })
})

/** A store holding two slots, neither active — so `deleteSlot` takes no
 *  navigation branch and the assertions are about the close alone. */
function store(slots: ChatSlot[] = [slot('chat-1'), slot('chat-2')]) {
  return configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer },
    middleware: g => g({ serializableCheck: false }),
    preloadedState: {
      dashboard: { ...dashboardReducer(undefined, { type: '@@INIT' }), slots, slotsLoaded: true },
    } as never,
  })
}

/** An ApiError-shaped rejection. The production check reads `.status`
 *  structurally, so the shape is all that has to match. */
const status = (code: number) => Object.assign(new Error(`HTTP ${code}`), { status: code })

const closingOf = (s: ReturnType<typeof store>) => s.getState().dashboard.closingSlots
const keysOf = (s: ReturnType<typeof store>) => s.getState().dashboard.slots.map(x => x.key)

beforeEach(() => {
  vi.clearAllMocks()
  mockSlots.mockResolvedValue([slot('chat-1'), slot('chat-2')])
})

describe('deleteSlot close resilience', () => {
  it('the happy path issues exactly one DELETE and clears the tombstone', async () => {
    mockDelete.mockResolvedValue({ ok: true })
    // A close that SUCCEEDED means the server popped the slot, so the dated read
    // that follows must not still be carrying it.
    mockSlots.mockResolvedValue([slot('chat-1')])
    const s = store()
    await s.dispatch(deleteSlot('chat-2')).unwrap()
    expect(mockDelete).toHaveBeenCalledTimes(1)
    expect(keysOf(s)).toEqual(['chat-1'])
    // Retired by the dated read rather than by a clock, so the withholding lasts
    // exactly as long as it can still be needed.
    await vi.waitFor(() => expect(closingOf(s)['chat-2']).toBeUndefined())
  })

  /** The sweep runs on READ, so a remote resume before retirement is filtered
   *  and — if its push was the last frame — nothing would ever re-run the sweep.
   *  A successful close therefore schedules a list read for just after expiry. */
  it('issues the dated read that retires the tombstone, with no timer', async () => {
    mockDelete.mockResolvedValue({ ok: true })
    mockSlots.mockResolvedValue([slot('chat-1')])
    const s = store()
    await s.dispatch(deleteSlot('chat-2')).unwrap()
    // Issued immediately, not owed to a clock — and because it was issued AFTER
    // the close it supersedes the tombstone instead of resurrecting the row.
    expect(mockSlots).toHaveBeenCalled()
    await vi.waitFor(() => expect(closingOf(s)['chat-2']).toBeUndefined())
    expect(keysOf(s)).toEqual(['chat-1'])
  })

  /** A DELETE whose response was lost may have COMPLETED — one attempt only. */
  it('does not retry a failure that carries no status', async () => {
    mockDelete.mockRejectedValue(new Error('network down'))
    const s = store()
    await expect(s.dispatch(deleteSlot('chat-2')).unwrap()).rejects.toBeTruthy()
    // The subject is the DELETE count: one attempt, never a repeat that could close
    // a replacement. What the ROW does is asserted by the unknown-outcome cases.
    expect(mockDelete).toHaveBeenCalledTimes(1)
  })

  it('does not retry a 403 — repeating it cannot change the answer', async () => {
    mockDelete.mockRejectedValue(status(403))
    const s = store()
    await expect(s.dispatch(deleteSlot('chat-2')).unwrap()).rejects.toBeTruthy()
    expect(mockDelete).toHaveBeenCalledTimes(1)
  })

  it('treats a 404 as already closed rather than a failure', async () => {
    mockDelete.mockRejectedValue(status(404))
    // 404 IS success: the slot is already absent, so the read agrees.
    mockSlots.mockResolvedValue([slot('chat-1')])
    const s = store()
    await s.dispatch(deleteSlot('chat-2')).unwrap()
    expect(mockDelete).toHaveBeenCalledTimes(1)
    expect(keysOf(s)).toEqual(['chat-1'])
  })
  /** The notice branches on the status, so it has to survive the thunk boundary:
   *  a thrown error is reduced by `miniSerializeError` to string fields only. */
  it('carries a numeric status across the thunk boundary', async () => {
    mockDelete.mockRejectedValue(status(403))
    const s = store()
    await expect(s.dispatch(deleteSlot('chat-2')).unwrap()).rejects.toMatchObject({ status: 403 })
  })
  /** An INDETERMINATE failure must treat the row exactly as a success does.
   *
   *  The DELETE may have completed with its response lost, so releasing the
   *  tombstone lets a GET issued BEFORE the close land and resurrect a row the
   *  server did remove — the very race this PR exists to close. Hold it hidden and
   *  let the dated post-close read establish the truth, which is also what the
   *  unknown-outcome notice promises the user ("the list will update on its own"). */
  it.each([
    ['no status', new Error('socket hung up')],
    ['a timeout', status(408)],
    ['a rate limit', status(429)],
    ['a 5xx', status(503)],
  ])('holds the row and lets a dated read decide when the outcome is unknown — %s', async (_l, err) => {
    mockDelete.mockRejectedValue(err)
    // The server still lists it, so the dated read is what brings the row back —
    // and it does so on evidence, not after a fixed wait.
    mockSlots.mockResolvedValue([slot('chat-1'), slot('chat-2')])
    const s = store()
    await expect(s.dispatch(deleteSlot('chat-2')).unwrap()).rejects.toBeDefined()
    expect(mockSlots).toHaveBeenCalled()
    await vi.waitFor(() => expect(keysOf(s)).toEqual(['chat-1', 'chat-2']))
  })

  /** A stale pre-close GET landing on that path must NOT bring the row back. */
  it('withholds an undatable server PUSH for as long as the dated read is in flight', async () => {
    mockDelete.mockRejectedValue(status(503))
    // Hold the read open — the window a stale coalesced frame lands in. A push has
    // no issue generation, so it can never retire the tombstone.
    mockSlots.mockReturnValue(new Promise(() => {}))
    const s = store()
    await expect(s.dispatch(deleteSlot('chat-2')).unwrap()).rejects.toBeDefined()
    s.dispatch({ type: 'dashboard/sseSlots', payload: [slot('chat-1'), slot('chat-2')] })
    expect(keysOf(s)).toEqual(['chat-1'])
    expect(closingOf(s)['chat-2']).toBeDefined()
  })

  /** A DETERMINATE refusal is the opposite case: the server answered, the slot is
   *  provably still there, so the row must come back at once. */
  it('releases the tombstone and reads immediately when the close was refused', async () => {
    mockDelete.mockRejectedValue(status(403))
    const s = store()
    await expect(s.dispatch(deleteSlot('chat-2')).unwrap()).rejects.toMatchObject({ status: 403 })
    expect(closingOf(s)['chat-2']).toBeUndefined()
    expect(mockSlots).toHaveBeenCalled()
  })
  /** FINDING 2 — a retirement read that REJECTS must not strand the tombstone.
   *
   *  The tombstone is retired by a dated reply, and a server push carries no date, so
   *  a read that never lands would withhold the key for the tab's lifetime: a session
   *  another client resumes under that key would stay invisible. Releasing is safe
   *  because ordering is enforced on the reply, not by the tombstone — a reply issued
   *  before this close is refused whether or not the key is still withheld. */
  it('releases the tombstone when the retirement read fails, so a resume stays visible', async () => {
    mockDelete.mockResolvedValue({ ok: true })
    mockSlots.mockRejectedValue(new Error('gateway down'))
    const s = store()
    await s.dispatch(deleteSlot('chat-2')).unwrap()
    await vi.waitFor(() => expect(closingOf(s)['chat-2']).toBeUndefined())
    // With nothing withholding it, a session resumed under the same key is visible.
    s.dispatch({ type: 'dashboard/sseSlots', payload: [slot('chat-1'), slot('chat-2')] })
    expect(keysOf(s)).toEqual(['chat-1', 'chat-2'])
  })

  /** GPT FINDING (blocking) — that fallback read was UNOBSERVED, so when it failed too
   *  the live row stayed absent from Redux with no bounded recovery. The withheld resume
   *  frame carries no date and cannot be replayed, so only an authoritative READ restores
   *  it; this drives the row back with no SSE push involved. */
  it('keeps re-reading until one authoritative list lands', async () => {
    mockDelete.mockResolvedValue({ ok: true })
    mockSlots
      .mockRejectedValueOnce(new Error('gateway down'))
      .mockRejectedValueOnce(new Error('still down'))
      .mockResolvedValue([slot('chat-1'), slot('chat-2')])
    const s = store()
    await s.dispatch(deleteSlot('chat-2')).unwrap()
    await vi.waitFor(() => expect(keysOf(s)).toEqual(['chat-1', 'chat-2']), { timeout: 3000 })
    expect(mockSlots.mock.calls.length).toBeGreaterThanOrEqual(3)
  })

  /** GPT FINDING (blocking) — recovery keyed on REJECTION alone, but a refused reply
   *  RESOLVES: the dashboard discards its list, the restored row stays hidden, and nothing
   *  asks again. A read overtaken by a later close must be retried like a failed one. */
  it('retries a reconciliation read that RESOLVED but was refused', async () => {
    mockDelete.mockResolvedValue({ ok: true })
    let releaseHeld!: (v: unknown) => void
    mockSlots
      // The retirement read fails, which is what hands control to the recovery helper.
      .mockRejectedValueOnce(new Error('gateway down'))
      // The helper's own read is held open so a close can overtake it before it resolves.
      .mockImplementationOnce(() => new Promise(r => { releaseHeld = r }))
      .mockResolvedValue([slot('chat-1')])
    const s = store()
    await s.dispatch(deleteSlot('chat-2') as never)
    await vi.waitFor(() => expect(releaseHeld).toBeDefined())
    // A later close advances the generation, so the held reply is refused as predating it.
    s.dispatch(dashboardSlice.slotCloseStarted('chat-1') as never)
    releaseHeld([slot('chat-1')])
    // Without the fix that resolution ends recovery at two reads and the row stays hidden.
    await vi.waitFor(() => expect(mockSlots.mock.calls.length).toBeGreaterThanOrEqual(3), { timeout: 3000 })
  })

  /** GPT FINDING (blocking) — recovery was BOUNDED at three attempts, so a resume whose own
   *  push was withheld stayed hidden permanently once the retries ran out. The withheld frame
   *  carries no date and can never be replayed, so giving up is giving up for the tab's life.
   *  It now retries until a read APPLIES, with capped backoff so an offline tab only slows. */
  it('does not give up after the old attempt cap', async () => {
    mockDelete.mockResolvedValue({ ok: true })
    mockSlots
      // The retirement read absorbs the first rejection, so the OLD cap's real budget was
      // four reads (1 retirement + 3 attempts). Five rejections outlast it.
      .mockRejectedValueOnce(new Error('down 1'))
      .mockRejectedValueOnce(new Error('down 2'))
      .mockRejectedValueOnce(new Error('down 3'))
      .mockRejectedValueOnce(new Error('down 4'))
      .mockRejectedValueOnce(new Error('down 5'))
      .mockResolvedValue([slot('chat-1'), slot('chat-2')])
    const s = store()
    await s.dispatch(deleteSlot('chat-2') as never)
    // A SIXTH read proves recovery outlived the old cap and kept going.
    await vi.waitFor(() => expect(mockSlots.mock.calls.length).toBeGreaterThanOrEqual(6), { timeout: 20000 })
    await vi.waitFor(() => expect(keysOf(s)).toEqual(['chat-1', 'chat-2']), { timeout: 20000 })
  }, 30000)
})

/** GPT FINDING (blocking) — no client-side collector is safe, at close OR on a permanent
 *  delete: localStorage is shared across tabs, so another tab can hold or resume the key
 *  while this one deletes it, and a delete that reports failure in-band still resolves.
 *  These assert REAL storage rather than a mock call, so they hold however it regresses. */
describe('no session delete ever collects storage', () => {
  const KEY = 'vc_heights_chat-9'

  beforeEach(() => { localStorage.clear() })

  it('leaves the storage alone when a permanent delete reports failure in-band', async () => {
    localStorage.setItem(KEY, '{}')
    mockDeleteSession.mockResolvedValue({ ok: false })
    const s = store()
    await s.dispatch(chatSlice.deleteHistorySession('chat-9') as never)
    expect(localStorage.getItem(KEY)).toBe('{}')
  })

  it('leaves the storage alone even when the permanent delete succeeds', async () => {
    localStorage.setItem(KEY, '{}')
    mockDeleteSession.mockResolvedValue({ ok: true })
    const s = store()
    await s.dispatch(chatSlice.deleteHistorySession('chat-9') as never)
    expect(localStorage.getItem(KEY)).toBe('{}')
  })

  it('leaves the storage alone on a close', async () => {
    localStorage.setItem('vc_heights_chat-2', '{}')
    mockDelete.mockResolvedValue(undefined)
    mockSlots.mockResolvedValue([slot('chat-1')])
    const s = store()
    await s.dispatch(deleteSlot('chat-2') as never)
    expect(localStorage.getItem('vc_heights_chat-2')).toBe('{}')
  })
})

/** GPT FINDING (blocking) — `fetchSlots.fulfilled` fires for a REFUSED reply too, and
 *  chatSlice cannot see dashboard state to learn that the list was discarded. It evicted
 *  residue from that stale list, destroying state for a slot created while it travelled.
 *  The verdict now rides on the action, and eviction requires it. */
describe('residue eviction requires the read to have applied', () => {
  const withResidue = () => ({
    ...chatReducer(undefined, { type: '@@INIT' }),
    slotHistory: ['chat-9'],
    slotsSnapshotSeen: false,
  })
  const resolved = (applied: boolean | undefined) => chatReducer(withResidue(), {
    type: dashboardSlice.fetchSlots.fulfilled.type,
    payload: [slot('chat-1')],
    meta: { requestId: 'r-1', ...(applied === undefined ? {} : { appliedProvisional: applied }) },
  })

  it('keeps residue when the reply was refused', () => {
    expect(resolved(false).slotHistory).toContain('chat-9')
  })

  it('evicts residue when the reply applied', () => {
    expect(resolved(true).slotHistory).not.toContain('chat-9')
  })

  it('keeps residue when no verdict rode along, so an unmarked reply cannot evict', () => {
    expect(resolved(undefined).slotHistory).toContain('chat-9')
  })
})

/** The gesture wiring is the HELPER's own obligation now, not a source grep: both
 *  gestures call this, so its behaviour is what makes the notice un-forgettable. */
describe('closeSlotWithNotice carries the close notice for both gestures', () => {
  const dispatchYielding = (unwrap: () => Promise<unknown>) =>
    (() => ({ unwrap })) as unknown as (a: never) => { unwrap: () => Promise<unknown> }

  it('reports a rejected close and stays silent on a resolved one', async () => {
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {})
    chatSlice.closeSlotWithNotice(dispatchYielding(() => Promise.reject({ status: 0 })), 'chat-1')
    await Promise.resolve()
    await Promise.resolve()
    expect(alertSpy).toHaveBeenCalledTimes(1)
    alertSpy.mockClear()
    chatSlice.closeSlotWithNotice(dispatchYielding(() => Promise.resolve('ok')), 'chat-1')
    await Promise.resolve()
    await Promise.resolve()
    expect(alertSpy).not.toHaveBeenCalled()
    alertSpy.mockRestore()
  })

  it('dispatches the close for the key it was given', () => {
    const seen: unknown[] = []
    const dispatch = ((a: unknown) => { seen.push(a); return { unwrap: () => Promise.resolve('ok') } }) as unknown as (a: never) => { unwrap: () => Promise<unknown> }
    chatSlice.closeSlotWithNotice(dispatch, 'chat-7')
    // A thunk, so the KEY is not readable off the action — dispatching exactly one
    // thing is the observable contract, and the notice test above covers the rest.
    expect(seen).toHaveLength(1)
    expect(typeof seen[0]).toBe('function')
  })
})
