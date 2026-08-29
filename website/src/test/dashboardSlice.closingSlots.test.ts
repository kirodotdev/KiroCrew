import { describe, it, expect, vi, afterEach } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import dashboardReducer, {
  addSlotOptimistic, removeSlotOptimistic, slotCloseStarted, slotCloseSettled, slotCloseRetireRead, sseSlots, fetchSlots, markSlotUnread, fetchSlotsIfApplied,
} from '../store/dashboardSlice'
import type { ChatSlot } from '../types'

const slot = (key: string): ChatSlot => ({ key, messages: 0, running: false })

/** Dashboard state carrying the given slots, past the first snapshot. */
function seeded(keys: string[]) {
  const initial = dashboardReducer(undefined, { type: '@@INIT' })
  return { ...initial, slots: keys.map(slot), slotsLoaded: true }
}

/** The state a close leaves behind: row gone, tombstone armed. */
function closing(keys: string[], key: string) {
  const started = dashboardReducer(seeded(keys), slotCloseStarted(key))
  return dashboardReducer(started, removeSlotOptimistic(key))
}

const keysOf = (state: { slots: ChatSlot[] }) => state.slots.map(s => s.key)
const refetch = (state: Parameters<typeof dashboardReducer>[0], payload: ChatSlot[]) =>
  dashboardReducer(state, { type: fetchSlots.fulfilled.type, payload })

afterEach(() => { vi.useRealTimers() })

describe('close-in-flight tombstones', () => {
  /** The close flicker: the backend still lists the slot mid-DELETE, so a frame
   *  naming it lands and `applySlots` puts the row back. */
  it('a stale slots frame does not resurrect a slot whose close is in flight', () => {
    const state = closing(['chat-1', 'chat-2'], 'chat-2')
    expect(keysOf(state)).toEqual(['chat-1'])
    expect(keysOf(dashboardReducer(state, sseSlots([slot('chat-1'), slot('chat-2')])))).toEqual(['chat-1'])
  })

  /** Same race on the HTTP surface: an in-flight `GET /api/chat/slots` reply can
   *  predate the close entirely. */
  it('a stale fetchSlots reply does not resurrect a slot whose close is in flight', () => {
    const state = closing(['chat-1', 'chat-2'], 'chat-2')
    expect(keysOf(refetch(state, [slot('chat-1'), slot('chat-2')]))).toEqual(['chat-1'])
  })

  it('withholds only the closing slot, not its peers', () => {
    const state = closing(['chat-1', 'chat-2'], 'chat-2')
    const next = dashboardReducer(state, sseSlots([slot('chat-1'), slot('chat-2'), slot('chat-3')]))
    expect(keysOf(next)).toEqual(['chat-1', 'chat-3'])
  })

  /** A successful close does NOT release its tombstone, because a pre-close GET
   *  can still arrive; the entry survives every frame that still names the key. */
  it('keeps withholding across repeated stale frames', () => {
    let state = closing(['chat-1', 'chat-2'], 'chat-2')
    for (let i = 0; i < 3; i++) {
      state = dashboardReducer(state, sseSlots([slot('chat-1'), slot('chat-2')]))
      expect(keysOf(state)).toEqual(['chat-1'])
    }
    expect(state.closingSlots['chat-2']).toBeDefined()
  })

  /** The reviewer's race, and the reason an omission must NOT retire a tombstone:
   *  an SSE frame omitting the key proves the server popped the slot, but a
   *  `fetchSlots` reply ISSUED BEFORE the close is still in flight carrying it. */
  it('an SSE omission does not let an older fetchSlots reply resurrect the row', () => {
    const state = closing(['chat-1', 'chat-2'], 'chat-2')
    // The server has popped it; this frame is legitimate.
    const omitted = dashboardReducer(state, sseSlots([slot('chat-1')]))
    expect(keysOf(omitted)).toEqual(['chat-1'])
    expect(omitted.closingSlots['chat-2']).toBeDefined()
    // The pre-close GET now lands. It must not put the row back.
    expect(keysOf(refetch(omitted, [slot('chat-1'), slot('chat-2')]))).toEqual(['chat-1'])
  })

  /** Same ordering on the other transport: a stale HTTP reply first, then SSE. */
  it('holds the tombstone across an omission on either transport', () => {
    let state = closing(['chat-1', 'chat-2'], 'chat-2')
    state = refetch(state, [slot('chat-1')])
    expect(state.closingSlots['chat-2']).toBeDefined()
    state = dashboardReducer(state, sseSlots([slot('chat-1'), slot('chat-2')]))
    expect(keysOf(state)).toEqual(['chat-1'])
  })

  /** The give-up path releases explicitly, so the row can come back. */
  it('settling the tombstone lets the row come back', () => {
    const settled = dashboardReducer(closing(['chat-1', 'chat-2'], 'chat-2'), slotCloseSettled('chat-2'))
    expect(keysOf(refetch(settled, [slot('chat-1'), slot('chat-2')]))).toEqual(['chat-1', 'chat-2'])
  })

  /** Reversing that order is what `deleteSlot`'s comment guards against, and it
   *  is worse than the flicker: the row is filtered out of the restoring reply. */
  it('restoring BEFORE settling leaves the row hidden', () => {
    const state = closing(['chat-1', 'chat-2'], 'chat-2')
    expect(keysOf(refetch(state, [slot('chat-1'), slot('chat-2')]))).toEqual(['chat-1'])
  })

  /** `removeSlotOptimistic` alone must not tombstone. A caller removing a slot
   *  whose delete is ALREADY confirmed would otherwise withhold a key the resume
   *  path can legitimately bring back under the same name. */
  it('an optimistic removal on its own creates no tombstone', () => {
    const removed = dashboardReducer(seeded(['chat-1', 'chat-2']), removeSlotOptimistic('chat-2'))
    expect(removed.closingSlots['chat-2']).toBeUndefined()
    expect(keysOf(dashboardReducer(removed, sseSlots([slot('chat-1'), slot('chat-2')])))).toEqual(['chat-1', 'chat-2'])
  })

  /** Resume reuses the closed session's own key, so creating a slot has to clear
   *  any tombstone for it — otherwise the resumed row stays withheld. */
  it('creating a slot clears a tombstone for the same key', () => {
    const state = closing(['chat-1', 'chat-2'], 'chat-2')
    const resumed = dashboardReducer(state, addSlotOptimistic(slot('chat-2')))
    expect(resumed.closingSlots['chat-2']).toBeUndefined()
    expect(keysOf(dashboardReducer(resumed, sseSlots([slot('chat-1'), slot('chat-2')])))).toEqual(['chat-1', 'chat-2'])
  })

  /** Many suites cast a hand-rolled partial `dashboard` state, which carries no
   *  `closingSlots`. Every writer has to tolerate that: `addSlotOptimistic` sits
   *  on the slot-CREATE path, so throwing there breaks unrelated features. */
  it('tolerates a preloaded state with no closingSlots map', () => {
    const partial = { slots: [slot('chat-1')], unreadSlots: [], slotsLoaded: true } as never
    expect(() => dashboardReducer(partial, addSlotOptimistic(slot('chat-2')))).not.toThrow()
    expect(() => dashboardReducer(partial, removeSlotOptimistic('chat-1'))).not.toThrow()
    expect(() => dashboardReducer(partial, slotCloseSettled('chat-1'))).not.toThrow()
    const armed = dashboardReducer(partial, slotCloseStarted('chat-1'))
    expect(armed.closingSlots['chat-1']).toBeDefined()
  })

  /** A slot key is NOT structurally a `chat-<n>-<epoch>`: the resume handler folds
   *  caller-supplied path text (a session key, a filename stem, a notification deep
   *  link) and falls through to a create path, so `__proto__` can reach this reducer.
   *
   *  Writing it with `map[key] = …` would hit the prototype SETTER instead of creating
   *  an own property, so the entry is invisible to `Object.keys` — the tombstone never
   *  withholds, and the stale frame restores the row this PR exists to keep gone. */
  describe('a slot key that names a prototype member is stored as data, not structure', () => {
    const evil = '__proto__'

    it('does not pollute Object.prototype', () => {
      dashboardReducer(seeded(['chat-1', evil]), slotCloseStarted(evil))
      expect(Object.getPrototypeOf({})).toBe(Object.prototype)
      expect(({} as Record<string, unknown>).at).toBeUndefined()
      expect(Object.prototype).not.toHaveProperty('at')
    })

    it('records the tombstone as an OWN, enumerable property', () => {
      const state = dashboardReducer(seeded(['chat-1', evil]), slotCloseStarted(evil))
      expect(Object.prototype.hasOwnProperty.call(state.closingSlots, evil)).toBe(true)
      expect(Object.keys(state.closingSlots)).toContain(evil)
    })

    /** The harm the two assertions above only imply: an unseen tombstone withholds
     *  nothing, so the closed row comes back on the next authoritative frame. */
    it('still withholds the row from a stale frame', () => {
      const state = closing(['chat-1', evil], evil)
      expect(keysOf(state)).toEqual(['chat-1'])
      expect(keysOf(dashboardReducer(state, sseSlots([slot('chat-1'), slot(evil)])))).toEqual(['chat-1'])
    })

    /** And it must still retire normally, or the key would be withheld forever. */
    it('settles like any other key', () => {
      const state = closing(['chat-1', evil], evil)
      const settled = dashboardReducer(state, slotCloseSettled(evil))
      expect(Object.keys(settled.closingSlots)).not.toContain(evil)
      expect(keysOf(refetch(settled, [slot('chat-1'), slot(evil)]))).toEqual(['chat-1', evil])
    })
  })

  /** The sibling site the review named: `ArtifactDetailPage` unbinds an archived
   *  slot post-confirmation, and an in-flight GET issued before that DELETE still
   *  lists it. Removal alone leaves the row resurrectable, so that page pairs
   *  `slotCloseStarted` with the removal and converges on this same withholding. */
  it('withholds a key removed alongside an armed tombstone, as the artifact page does', () => {
    const armed = dashboardReducer(seeded(['chat-1', 'chat-2']), slotCloseStarted('chat-2'))
    const gone = dashboardReducer(armed, removeSlotOptimistic('chat-2'))
    expect(keysOf(gone)).toEqual(['chat-1'])
    expect(keysOf(refetch(gone, [slot('chat-1'), slot('chat-2')]))).toEqual(['chat-1'])
  })

  /** Retirement is keyed to IDENTITY, never to elapsed time: a read ISSUED after
   *  the close supersedes the tombstone, and no amount of waiting does. */
  it('a read issued after the close retires the tombstone; time alone never does', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    const state = closing(['chat-1', 'chat-2'], 'chat-2')

    // Half an hour later an undatable push still proves nothing.
    vi.setSystemTime(new Date('2026-01-01T00:30:00Z'))
    const waited = dashboardReducer(state, sseSlots([slot('chat-1'), slot('chat-2')]))
    expect(keysOf(waited)).toEqual(['chat-1'])
    expect(waited.closingSlots['chat-2']).toBeDefined()

    // The close's OWN read does, and the row follows what it carries. Both halves
    // of the lifecycle: `pending` dates it, `slotCloseRetireRead` identifies it.
    const pending = dashboardReducer(state, { type: fetchSlots.pending.type, meta: { requestId: 'r1' } })
    const issued = dashboardReducer(pending, slotCloseRetireRead({ key: 'chat-2', readId: 'r1' }))
    const dated = dashboardReducer(issued, {
      type: fetchSlots.fulfilled.type,
      payload: [slot('chat-1'), slot('chat-2')],
      meta: { requestId: 'r1' },
    })
    expect(dated.closingSlots['chat-2']).toBeUndefined()
    expect(keysOf(dated)).toEqual(['chat-1', 'chat-2'])
  })
  /** FINDING 1 — a read issued BEFORE the close must never be applied after it.
   *
   *  The hazard is not the tombstone's lifetime: a newer post-close read legitimately
   *  retires the tombstone, and only THEN does the older reply land. Nothing is
   *  withholding the key by that point, so ordering has to be enforced on the reply
   *  itself — a reply serialized before the close cannot speak to membership. */
  it('refuses a slots reply that was issued before the close, however late it lands', () => {
    // A read issued FIRST, while nothing is closing.
    const seeded0 = dashboardReducer(seeded(['chat-1', 'chat-2']), { type: fetchSlots.pending.type, meta: { requestId: 'old' } })
    // Then the close, then a NEWER read that legitimately retires the tombstone.
    const closed = dashboardReducer(seeded0, slotCloseStarted('chat-2'))
    const gone = dashboardReducer(closed, removeSlotOptimistic('chat-2'))
    const issuedNew = dashboardReducer(gone, { type: fetchSlots.pending.type, meta: { requestId: 'new' } })
    const retired = dashboardReducer(issuedNew, {
      type: fetchSlots.fulfilled.type, payload: [slot('chat-1')], meta: { requestId: 'new' },
    })
    // The tombstone is deliberately still held (an older read is outstanding).
    // Now the older reply lands, still naming the closed slot: it must be refused.
    const late = dashboardReducer(retired, {
      type: fetchSlots.fulfilled.type, payload: [slot('chat-1'), slot('chat-2')], meta: { requestId: 'old' },
    })
    expect(keysOf(late)).toEqual(['chat-1'])
  })

  /** A reply issued AFTER the close is still applied — the rule must not refuse
   *  everything, or the row could never legitimately come back. */
  /** OPUS FINDING — a read merely SHARING the close's generation is not proof the
   *  server popped the slot. `close_slot` pops `_slots` only after its nudge-lock
   *  and app-hook awaits, so a poll dispatched after `slotCloseStarted` can outrun
   *  the DELETE and reply STILL LISTING the row. Retiring on the generation alone
   *  would delete the tombstone and flicker the row back — this PR's own defect. */
  it('a concurrent read sharing the close generation neither retires nor resurrects', () => {
    const closed = dashboardReducer(seeded(['chat-1', 'chat-2']), slotCloseStarted('chat-2'))
    // Dispatched AFTER the close, so it carries the same generation, and it is NOT
    // the close's own retirement read: no `slotCloseRetireRead` names it.
    const issued = dashboardReducer(closed, { type: fetchSlots.pending.type, meta: { requestId: 'poll' } })
    const applied = dashboardReducer(issued, {
      type: fetchSlots.fulfilled.type, payload: [slot('chat-1'), slot('chat-2')], meta: { requestId: 'poll' },
    })
    expect(keysOf(applied)).toEqual(['chat-1'])
    expect(applied.closingSlots['chat-2']).toBeDefined()
  })
  /** The close's own read, by contrast, applies its list in full. */
  it('still applies a reply issued after the close', () => {
    const closed = dashboardReducer(seeded(['chat-1', 'chat-2']), slotCloseStarted('chat-2'))
    const pending = dashboardReducer(closed, { type: fetchSlots.pending.type, meta: { requestId: 'r' } })
    const issued = dashboardReducer(pending, slotCloseRetireRead({ key: 'chat-2', readId: 'r' }))
    const applied = dashboardReducer(issued, {
      type: fetchSlots.fulfilled.type, payload: [slot('chat-1'), slot('chat-2')], meta: { requestId: 'r' },
    })
    expect(keysOf(applied)).toEqual(['chat-1', 'chat-2'])
  })
  /** FINDING 1 — an older read must not strand a tombstone permanently.
   *
   *  The post-close reply supersedes the tombstone but cannot retire it while a
   *  pre-close read is outstanding. That older reply is then REFUSED, so nothing
   *  applies its list — and if the supersession was not remembered, no later sweep
   *  ever runs and the key stays hidden for the tab's life. */
  it('retires a superseded tombstone at once, and a later older reply cannot undo it', () => {
    const issuedOld = dashboardReducer(seeded(['chat-1', 'chat-2']), { type: fetchSlots.pending.type, meta: { requestId: 'old' } })
    const closed = dashboardReducer(issuedOld, slotCloseStarted('chat-2'))
    const gone = dashboardReducer(closed, removeSlotOptimistic('chat-2'))
    const pendingNew = dashboardReducer(gone, { type: fetchSlots.pending.type, meta: { requestId: 'new' } })
    const issuedNew = dashboardReducer(pendingNew, slotCloseRetireRead({ key: 'chat-2', readId: 'new' }))
    // The close's own reply retires immediately — waiting on the outstanding older
    // read would withhold the key from this very reply.
    const superseded = dashboardReducer(issuedNew, {
      type: fetchSlots.fulfilled.type, payload: [slot('chat-1')], meta: { requestId: 'new' },
    })
    expect(superseded.closingSlots['chat-2']).toBeUndefined()
    expect(keysOf(superseded)).toEqual(['chat-1'])
    // The older reply lands afterwards with no tombstone left, and is refused on
    // ORDERING alone — which is what made the `olderInFlight` wait redundant.
    const after = dashboardReducer(superseded, {
      type: fetchSlots.fulfilled.type, payload: [slot('chat-1'), slot('chat-2')], meta: { requestId: 'old' },
    })
    expect(keysOf(after)).toEqual(['chat-1'])
  })

  /** FINDING 3 — resuming the key before retirement must not let an earlier reply
   *  delete the live replacement. Clearing the tombstone stops withholding, so the
   *  generation has to advance with it. */
  it('refuses an in-flight omission after the same key is resumed', () => {
    const closed = dashboardReducer(seeded(['chat-1', 'chat-2']), slotCloseStarted('chat-2'))
    const gone = dashboardReducer(closed, removeSlotOptimistic('chat-2'))
    // A read is issued while the key is absent, so its reply will omit it.
    const issued = dashboardReducer(gone, { type: fetchSlots.pending.type, meta: { requestId: 'mid' } })
    // The same key is resumed before that reply lands.
    const resumed = dashboardReducer(issued, addSlotOptimistic(slot('chat-2')))
    expect(keysOf(resumed)).toEqual(['chat-1', 'chat-2'])
    const late = dashboardReducer(resumed, {
      type: fetchSlots.fulfilled.type, payload: [slot('chat-1')], meta: { requestId: 'mid' },
    })
    expect(keysOf(late)).toEqual(['chat-1', 'chat-2'])
  })

  /** A long-delayed reply must not resurrect a closed row. Its record is retained
   *  until it arrives, so the ordering rule can still date and refuse it. */
  it('refuses a long-delayed reply that still carries a closed slot', () => {
    let state = dashboardReducer(seeded(['chat-1', 'chat-2']), { type: fetchSlots.pending.type, meta: { requestId: 'slow' } })
    state = dashboardReducer(state, slotCloseStarted('chat-2'))
    state = dashboardReducer(state, removeSlotOptimistic('chat-2'))
    // Many later reads come and go without disturbing the outstanding record.
    for (let i = 0; i < 12; i++) {
      state = dashboardReducer(state, { type: fetchSlots.pending.type, meta: { requestId: `r${i}` } })
      state = dashboardReducer(state, { type: fetchSlots.fulfilled.type, payload: [slot('chat-1')], meta: { requestId: `r${i}` } })
    }
    expect(state.pendingSlotReads.slow).toBe(0)
    const late = dashboardReducer(state, {
      type: fetchSlots.fulfilled.type, payload: [slot('chat-1'), slot('chat-2')], meta: { requestId: 'slow' },
    })
    expect(keysOf(late)).toEqual(['chat-1'])
  })

  /** With no close at all there is nothing to order against, so even a very old
   *  reply stays authoritative — refusing it would discard a list for free. */
  it('still applies a long-delayed reply when no close ever happened', () => {
    let state = dashboardReducer(seeded(['chat-1']), { type: fetchSlots.pending.type, meta: { requestId: 'slow' } })
    for (let i = 0; i < 12; i++) {
      state = dashboardReducer(state, { type: fetchSlots.pending.type, meta: { requestId: `q${i}` } })
      state = dashboardReducer(state, { type: fetchSlots.fulfilled.type, payload: [slot('chat-1')], meta: { requestId: `q${i}` } })
    }
    expect(state.closeSeq).toBe(0)
    const applied = dashboardReducer(state, {
      type: fetchSlots.fulfilled.type, payload: [slot('chat-1'), slot('chat-7')], meta: { requestId: 'slow' },
    })
    expect(keysOf(applied)).toEqual(['chat-1', 'chat-7'])
  })
  /** A FAILED close must not leave the live session hidden.
   *
   *  The close returns 500, so the row is held pending the retirement read. That read
   *  comes back still listing the slot — the close did not take, so the row is alive
   *  and must reappear. Blocking retirement on an unrelated older read WITHHOLDS the
   *  key from exactly that reply, and the older reply which later clears the tombstone
   *  is itself refused, so nothing is left to restore the row. */
  it('restores the row when the retirement read shows a failed close still listing it', () => {
    // A pre-close read is already in flight.
    let state = dashboardReducer(seeded(['chat-1', 'chat-2']), { type: fetchSlots.pending.type, meta: { requestId: 'old' } })
    state = dashboardReducer(state, slotCloseStarted('chat-2'))
    state = dashboardReducer(state, removeSlotOptimistic('chat-2'))
    expect(keysOf(state)).toEqual(['chat-1'])
    // The close FAILED (500), so the retirement read still lists the slot: it is live.
    state = dashboardReducer(state, { type: fetchSlots.pending.type, meta: { requestId: 'new' } })
    state = dashboardReducer(state, slotCloseRetireRead({ key: 'chat-2', readId: 'new' }))
    const afterNew = dashboardReducer(state, {
      type: fetchSlots.fulfilled.type, payload: [slot('chat-1'), slot('chat-2')], meta: { requestId: 'new' },
    })
    expect(keysOf(afterNew)).toEqual(['chat-1', 'chat-2'])
    // And the older reply landing afterwards must not undo that.
    const afterOld = dashboardReducer(afterNew, {
      type: fetchSlots.fulfilled.type, payload: [slot('chat-1'), slot('chat-2')], meta: { requestId: 'old' },
    })
    expect(keysOf(afterOld)).toEqual(['chat-1', 'chat-2'])
  })

  /** GPT FINDING (blocking) — a read issued BEFORE a session was created omits it,
   *  so applying that membership evicts a live row. No close is involved and no
   *  tombstone exists, so only bumping the generation on the INSERT itself refuses
   *  it. Artifact "New chat" is the reported path. */
  it('refuses a pre-create reply that would evict a newly created session', () => {
    // A read is already in flight when the new chat is created.
    const issued = dashboardReducer(seeded(['chat-1']), { type: fetchSlots.pending.type, meta: { requestId: 'pre' } })
    const created = dashboardReducer(issued, addSlotOptimistic(slot('chat-new')))
    expect(keysOf(created)).toEqual(['chat-1', 'chat-new'])
    // Its reply predates the create, so it omits the new key. Applying that
    // membership would delete a live session.
    const late = dashboardReducer(created, {
      type: fetchSlots.fulfilled.type, payload: [slot('chat-1')], meta: { requestId: 'pre' },
    })
    expect(keysOf(late)).toEqual(['chat-1', 'chat-new'])
  })

  /** GPT FINDING (blocking) — a refused reply must not reconcile either. Its key
   *  set is the very membership just refused, and `reconcileSlots`' unread drain
   *  is UNGATED (only subagent eviction honours `evictStale`), so a slot created
   *  after the read was issued gets its badge dropped from Redux AND localStorage,
   *  which no later event restores. */
  it('a refused reply does not erase the unread state of a slot it cannot know about', () => {
    // A list read is in flight...
    let state = dashboardReducer(seeded(['chat-1', 'chat-2']), { type: fetchSlots.pending.type, meta: { requestId: 'r' } })
    // ...then a close begins, so that reply now predates a close and is refused.
    state = dashboardReducer(state, slotCloseStarted('chat-2'))
    // Meanwhile a NEW inactive slot appears and gains an unread message.
    state = dashboardReducer(state, addSlotOptimistic(slot('chat-new')))
    state = dashboardReducer(state, markSlotUnread('chat-new'))
    expect(state.unreadSlots).toContain('chat-new')
    // The stale reply lands, omitting the slot it was issued before.
    const after = dashboardReducer(state, {
      type: fetchSlots.fulfilled.type, payload: [slot('chat-1'), slot('chat-2')], meta: { requestId: 'r' },
    })
    expect(after.unreadSlots).toContain('chat-new')
  })

  /** GPT FINDING (blocking) — a remote resume that races the close is invisible to
   *  the retiring read. The push carrying the resumed row is WITHHELD while the
   *  tombstone stands and then dropped, and the retiring reply was issued before
   *  the resume, so applying its omission leaves a LIVE session hidden.
   *
   *  No payload field separates that push from a mid-DELETE frame (`created` is
   *  restored on resume and there is no per-instance id), so the row cannot simply
   *  be re-inserted — that would resurrect closed rows and undo this PR. What
   *  settles it is a read issued AFTER retirement, postdating pop and resume both. */
  it('a read issued after retirement restores a session resumed during the close', () => {
    let state = dashboardReducer(seeded(['chat-1', 'chat-2']), slotCloseStarted('chat-2'))
    state = dashboardReducer(state, removeSlotOptimistic('chat-2'))
    // The close's own retirement read is issued and identified.
    state = dashboardReducer(state, { type: fetchSlots.pending.type, meta: { requestId: 'retire' } })
    state = dashboardReducer(state, slotCloseRetireRead({ key: 'chat-2', readId: 'retire' }))
    // A remote client resumes the SAME key; that push is withheld by the tombstone.
    const pushed = dashboardReducer(state, sseSlots([slot('chat-1'), slot('chat-2')]))
    expect(keysOf(pushed)).toEqual(['chat-1'])
    // The retiring reply lands, omitting the key: it was issued before the resume.
    const retired = dashboardReducer(pushed, {
      type: fetchSlots.fulfilled.type, payload: [slot('chat-1')], meta: { requestId: 'retire' },
    })
    expect(retired.closingSlots['chat-2']).toBeUndefined()
    // The confirming read issued after retirement sees the resumed session, and with
    // no tombstone left nothing withholds it.
    const confirming = dashboardReducer(retired, { type: fetchSlots.pending.type, meta: { requestId: 'confirm' } })
    const settled = dashboardReducer(confirming, {
      type: fetchSlots.fulfilled.type, payload: [slot('chat-1'), slot('chat-2')], meta: { requestId: 'confirm' },
    })
    expect(keysOf(settled)).toEqual(['chat-1', 'chat-2'])
  })

  /** GPT FINDING (blocking) — the same eviction, reached by the OTHER ordering. A
   *  server push can insert the new row before the create response arrives, so the
   *  create finds the key already present. Gating the generation bump on our own
   *  insert therefore left `closeSeq` unmoved, the delayed pre-create GET stayed
   *  authoritative, and its omission removed a live session. */
  it('refuses a pre-create reply when a push inserted the new session first', () => {
    // A read is already in flight, listing only the original slot.
    const issued = dashboardReducer(seeded(['chat-1']), { type: fetchSlots.pending.type, meta: { requestId: 'pre' } })
    // The server PUSH lands first and adds the new row.
    const pushed = dashboardReducer(issued, sseSlots([slot('chat-1'), slot('chat-new')]))
    expect(keysOf(pushed)).toEqual(['chat-1', 'chat-new'])
    // Only THEN does the create response arrive — so the key is already present and
    // nothing is inserted here.
    const created = dashboardReducer(pushed, addSlotOptimistic(slot('chat-new')))
    expect(keysOf(created)).toEqual(['chat-1', 'chat-new'])
    // The pre-create reply lands last, omitting the key it was issued before.
    const late = dashboardReducer(created, {
      type: fetchSlots.fulfilled.type, payload: [slot('chat-1')], meta: { requestId: 'pre' },
    })
    expect(keysOf(late)).toEqual(['chat-1', 'chat-new'])
  })

  /** GPT FINDING (blocking) — the dating was ASYMMETRIC. A push that restored a row
   *  advanced the generation, but one that REMOVED an untombstoned row did not — a
   *  close performed in another tab. An older read still passed the dating check and
   *  its inclusion put the deleted row back. Membership change is change either way. */
  it('refuses a read issued before a push that removed a row remotely', () => {
    let s = seeded(['chat-1', 'chat-2'])
    // A read goes out while both rows are live and neither is closing here.
    s = dashboardReducer(s, { type: fetchSlots.pending.type, meta: { requestId: 'old' } })
    expect(s.closingSlots['chat-2']).toBeUndefined()
    // Another tab closes chat-2; we learn it as a removal over SSE.
    s = dashboardReducer(s, sseSlots([slot('chat-1')]))
    expect(keysOf(s)).toEqual(['chat-1'])
    // The older reply still lists it. Applying that membership would resurrect it.
    s = dashboardReducer(s, {
      type: fetchSlots.fulfilled.type,
      payload: [slot('chat-1'), slot('chat-2')],
      meta: { requestId: 'old' },
    })
    expect(keysOf(s)).toEqual(['chat-1'])
  })

  /** GPT + OPUS FINDING (blocking) — the restore-bump read the tombstone map with
   *  bracket access, so a key naming an `Object.prototype` member read a truthy
   *  INHERITED value and was judged already-closing. The bump was skipped and the
   *  erasure race reopened for exactly the keys `_normalize_slot_key` lets through.
   *  `isUnsafeKey` is not enough here: it misses `toString`/`hasOwnProperty`. */
  it.each(['__proto__', 'constructor', 'toString', 'hasOwnProperty'])(
    'dates a restore of the prototype-named key %s', key => {
      let s = seeded(['chat-1'])
      s = dashboardReducer(s, { type: fetchSlots.pending.type, meta: { requestId: 'pre' } })
      s = dashboardReducer(s, sseSlots([slot('chat-1'), slot(key)]))
      expect(keysOf(s)).toContain(key)
      s = dashboardReducer(s, {
        type: fetchSlots.fulfilled.type, payload: [slot('chat-1')], meta: { requestId: 'pre' },
      })
      expect(keysOf(s)).toContain(key)
    },
  )

  /** GPT FINDING (blocking) — `slotsLoaded` was set outside the branch, so a REFUSED
   *  reply still claimed a snapshot had arrived. `sseSlots` drops an empty frame only
   *  while NOT loaded, so a delayed empty startup frame then passed that guard as
   *  authoritative and cleared every row. */
  it('does not mark the list loaded from a reply whose membership was refused', () => {
    const initial = dashboardReducer(undefined, { type: '@@INIT' })
    let s = { ...initial, slots: [slot('chat-1'), slot('chat-2')], slotsLoaded: false }
    // A reconnect read is in flight when a close begins, so its reply is refused.
    s = dashboardReducer(s, { type: fetchSlots.pending.type, meta: { requestId: 'recon' } })
    s = dashboardReducer(s, slotCloseStarted('chat-2'))
    s = dashboardReducer(s, {
      type: fetchSlots.fulfilled.type, payload: [slot('chat-1')], meta: { requestId: 'recon' },
    })
    expect(s.slotsLoaded).toBe(false)
    // The delayed empty startup frame must still be treated as ambiguous, not as
    // "the list genuinely went empty", so no row is cleared.
    s = dashboardReducer(s, sseSlots([]))
    expect(keysOf(s)).toEqual(['chat-1', 'chat-2'])
  })

  /** GPT FINDING (blocking) — the confirming read added to close the earlier
   *  resume race became the hazard itself. It is issued AFTER retirement, so it is
   *  not withheld; if a resume restores the row via SSE while it is in flight, its
   *  own snapshot of absence then erases a live session. A server push that
   *  RESTORES a key must therefore date the list, exactly as a local create does. */
  it('refuses a confirming read that a remote resume overtook', () => {
    let s = seeded(['chat-1', 'chat-2'])
    s = dashboardReducer(s, slotCloseStarted('chat-2'))
    s = dashboardReducer(s, removeSlotOptimistic('chat-2'))
    expect(keysOf(s)).toEqual(['chat-1'])
    // The DELETE resolved: the close's own read retires the tombstone.
    s = dashboardReducer(s, slotCloseRetireRead({ key: 'chat-2', readId: 'retire' }))
    s = dashboardReducer(s, {
      type: fetchSlots.fulfilled.type, payload: [slot('chat-1')], meta: { requestId: 'retire' },
    })
    expect(s.closingSlots['chat-2']).toBeUndefined()
    // The confirming read is issued next, and snapshots absence server-side.
    s = dashboardReducer(s, { type: fetchSlots.pending.type, meta: { requestId: 'confirm' } })
    // THEN another tab resumes the session and the push restores the row.
    s = dashboardReducer(s, sseSlots([slot('chat-1'), slot('chat-2')]))
    expect(keysOf(s)).toEqual(['chat-1', 'chat-2'])
    // The confirming reply lands last, omitting a session that is now live.
    s = dashboardReducer(s, {
      type: fetchSlots.fulfilled.type, payload: [slot('chat-1')], meta: { requestId: 'confirm' },
    })
    expect(keysOf(s)).toEqual(['chat-1', 'chat-2'])
  })

  /** A push listing a slot whose close is still IN FLIGHT is the server not having
   *  popped it yet, not a restore. Dating that would refuse the close's own
   *  retirement read and strand the row hidden. */
  it('does not date a push that merely still lists a closing slot', () => {
    let s = seeded(['chat-1', 'chat-2'])
    s = dashboardReducer(s, slotCloseStarted('chat-2'))
    s = dashboardReducer(s, removeSlotOptimistic('chat-2'))
    const before = s.closeSeq
    s = dashboardReducer(s, sseSlots([slot('chat-1'), slot('chat-2')]))
    expect(s.closeSeq).toBe(before)
    expect(keysOf(s)).toEqual(['chat-1'])
  })

  /** DESIGN Watch, DISCHARGED — the split no longer mirrors the gateway's codes, so
   *  this pins the client half; the wire flag is pinned on the side that sends it. */
  it('keeps no mirrored code list, now the split reads the wire field', async () => {
    const { readFileSync } = await import('node:fs')
    const client = readFileSync(`${process.cwd()}/src/utils/closeOutcome.ts`, 'utf8')
    expect(client).not.toMatch(/CLOSE_ABORT_CODES/)
    for (const code of ['nudge_retire_failed', 'app_close_hook_failed', 'history_save_failed']) {
      expect(client, `${code} is mirrored again`).not.toContain(code)
    }
  })
})

describe('prototype-named slot keys never reach an inherited member', () => {
  const EVIL = ['__proto__', 'constructor', 'toString', 'hasOwnProperty']

  /** A slot key is SERVER-SUPPLIED, so `__proto__` reaches every lookup keyed on it.
   *  Bracket access finds a truthy INHERITED member, and writing through it is global. */
  it('records no retirement read when the key owns no tombstone', () => {
    const polluted: string[] = []
    for (const key of EVIL) {
      // The shared object a bare bracket read would have resolved to, for THIS key.
      const inherited = ({} as Record<string, unknown>)[key] as { retireReadId?: string }
      const state = dashboardReducer(seeded(['chat-1']), slotCloseRetireRead({ key, readId: 'r1' }))
      expect(Object.prototype.hasOwnProperty.call(state.closingSlots, key)).toBe(false)
      if (inherited?.retireReadId !== undefined) polluted.push(key)
      if (inherited) delete inherited.retireReadId
    }
    expect(polluted, 'wrote a retirement onto a shared inherited object').toEqual([])
  })

  it('still records the retirement read on a genuinely closing slot', () => {
    const armed = dashboardReducer(seeded(['chat-1']), slotCloseStarted('chat-1'))
    const state = dashboardReducer(armed, slotCloseRetireRead({ key: 'chat-1', readId: 'r9' }))
    expect(state.closingSlots['chat-1'].retireReadId).toBe('r9')
  })

  it('does not treat a prototype-named create as clearing a real tombstone', () => {
    for (const key of EVIL) {
      const armed = dashboardReducer(seeded(['chat-1']), slotCloseStarted('chat-1'))
      const state = dashboardReducer(armed, addSlotOptimistic(slot(key)))
      expect(Object.prototype.hasOwnProperty.call(state.closingSlots, 'chat-1')).toBe(true)
    }
  })
})

describe('an HTTP read that changes membership dates the list', () => {
  /** Issue a read at the live generation, then land it carrying `payload`. */
  const roundTrip = (state: Parameters<typeof dashboardReducer>[0], id: string, payload: ChatSlot[]) => {
    const issued = dashboardReducer(state, { type: fetchSlots.pending.type, meta: { requestId: id } })
    return dashboardReducer(issued, { type: fetchSlots.fulfilled.type, payload, meta: { requestId: id } })
  }

  /** The close's confirming read: retires the tombstone and leaves the row gone. */
  function confirmed() {
    const armed = dashboardReducer(closing(['chat-1', 'chat-2'], 'chat-2'), slotCloseRetireRead({ key: 'chat-2', readId: 'conf' }))
    const state = roundTrip(armed, 'conf', [slot('chat-1')])
    expect(Object.prototype.hasOwnProperty.call(state.closingSlots, 'chat-2')).toBe(false)
    expect(keysOf(state)).toEqual(['chat-1'])
    return state
  }

  /** `sseSlots` bumps `closeSeq` when a push adds or drops a row. An HTTP reply
   *  that restores one must too, or a read of its own generation undoes it. */
  it('a resumed slot applied over HTTP survives a delayed reply of the same generation', () => {
    const after = confirmed()
    // The delayed reply is ISSUED here, before the resume, so it carries the old
    // generation AND a snapshot that legitimately omits the resumed row.
    const stale = dashboardReducer(after, { type: fetchSlots.pending.type, meta: { requestId: 'stale' } })
    const resumed = roundTrip(stale, 'reconnect', [slot('chat-1'), slot('chat-2')])
    expect(keysOf(resumed), 'the reconnect read did not restore the row').toEqual(['chat-1', 'chat-2'])
    const landed = dashboardReducer(resumed, {
      type: fetchSlots.fulfilled.type, payload: [slot('chat-1')], meta: { requestId: 'stale' },
    })
    expect(keysOf(landed), 'a delayed reply removed a live row').toEqual(['chat-1', 'chat-2'])
  })

  /** CONTROL. The bump is scoped to a membership CHANGE, so ordinary concurrent
   *  polling still applies — a blanket bump would refuse every in-flight read. */
  it('a reply that changes nothing does not date the list', () => {
    const after = confirmed()
    const stale = dashboardReducer(after, { type: fetchSlots.pending.type, meta: { requestId: 'stale' } })
    const same = roundTrip(stale, 'poll', [slot('chat-1')])
    expect(same.closeSeq).toBe(after.closeSeq)
    const landed = dashboardReducer(same, {
      type: fetchSlots.fulfilled.type, payload: [slot('chat-1')], meta: { requestId: 'stale' },
    })
    expect(landed.slotsLoaded, 'a same-membership reply was refused').toBe(true)
  })
})

describe('retiring a tombstone dates the list', () => {
  const issue = (state: Parameters<typeof dashboardReducer>[0], id: string) =>
    dashboardReducer(state, { type: fetchSlots.pending.type, meta: { requestId: id } })
  const land = (state: Parameters<typeof dashboardReducer>[0], id: string, payload: ChatSlot[]) =>
    dashboardReducer(state, { type: fetchSlots.fulfilled.type, payload, meta: { requestId: id } })

  /** The tombstone is the ONLY thing withholding a pre-pop list, so retiring it while a
   *  read of the same generation is in flight lets that read put the closed row back. */
  it('a pre-pop reply of the retirement generation cannot resurrect the closed row', () => {
    const closed = closing(['chat-1', 'chat-2'], 'chat-2')
    // Issued BEFORE the backend popped the slot, so its payload still lists chat-2.
    const polling = issue(closed, 'poll')
    const armed = dashboardReducer(polling, slotCloseRetireRead({ key: 'chat-2', readId: 'conf' }))
    const retired = land(issue(armed, 'conf'), 'conf', [slot('chat-1')])
    expect(Object.prototype.hasOwnProperty.call(retired.closingSlots, 'chat-2')).toBe(false)
    expect(keysOf(retired)).toEqual(['chat-1'])
    const landed = land(retired, 'poll', [slot('chat-1'), slot('chat-2')])
    expect(keysOf(landed), 'a pre-pop reply resurrected a closed session').toEqual(['chat-1'])
  })

  /** CONTROL. The retiring reply must still apply its OWN list — its generation is read
   *  before the bump, so dating the list must not make a close refuse its own proof. */
  it('the retiring reply still applies the list that proves the close', () => {
    const closed = closing(['chat-1', 'chat-2'], 'chat-2')
    const armed = dashboardReducer(closed, slotCloseRetireRead({ key: 'chat-2', readId: 'conf' }))
    const retired = land(issue(armed, 'conf'), 'conf', [slot('chat-1')])
    expect(retired.slotsLoaded, 'the retirement read was refused its own list').toBe(true)
    expect(keysOf(retired)).toEqual(['chat-1'])
  })

  /** RELEASING is the same ordering event as retiring: it too removes the only thing
   *  withholding a pre-pop list. A close whose DELETE succeeded but whose confirming
   *  read failed releases here, so leaving the generation unmoved lets an in-flight
   *  pre-pop reply be re-adopted — the flicker, by the other door. */
  it('a pre-pop reply of the release generation cannot resurrect the closed row', () => {
    const closed = closing(['chat-1', 'chat-2'], 'chat-2')
    // Issued BEFORE the backend popped the slot, so its payload still lists chat-2.
    const polling = issue(closed, 'poll')
    const released = dashboardReducer(polling, slotCloseSettled('chat-2'))
    expect(Object.prototype.hasOwnProperty.call(released.closingSlots, 'chat-2')).toBe(false)
    const landed = land(released, 'poll', [slot('chat-1'), slot('chat-2')])
    expect(keysOf(landed), 'a pre-pop reply resurrected a released session').toEqual(['chat-1'])
  })

  /** CONTROL. Only an actual release is an ordering event — a call naming a slot that
   *  carries no tombstone must not date the list, or innocent reads get refused. */
  it('releasing a slot that was never closing leaves the generation alone', () => {
    const closed = closing(['chat-1', 'chat-2'], 'chat-2')
    const before = closed.closeSeq
    const noop = dashboardReducer(closed, slotCloseSettled('chat-1'))
    expect(noop.closeSeq, 'a no-op release dated the list').toBe(before)
  })
})

describe('slots-membership writers are gated, not merely conventional', () => {
  const source = readFileSync('src/store/dashboardSlice.ts', 'utf8')

  /** Every writer, each with the dating policy that makes it safe. A FOURTH writer has to
   *  add itself here and state its policy, which is the whole point: the ordering machinery
   *  stays correct only while the set is known, and before this nothing gated a new one. */
  const PINNED = [
    // applySlots — dated by `membershipMoved` immediately above the write
    'if (changed) state.slots = merged',
    // addSlotOptimistic — bumps `closeSeq` unconditionally just below the push
    'if (!state.slots.find(s => s.key === key)) state.slots.push(action.payload)',
    // removeSlotOptimistic — deliberately undated, per its own docstring
    'state.slots = state.slots.filter(s => s.key !== action.payload)',
  ]

  it('pins the complete set of slots-membership writes', () => {
    // Matched on ANY receiver, not just `state`: a writer taking the draft as `s` or
    // `draft` is the same hazard, and keying on one name let a mutation control walk past.
    const found = source.split('\n').map(l => l.trim()).filter(l =>
      /\.slots\s*=[^=]/.test(l)
      || /\.slots\.(push|splice|unshift|pop|shift|sort|reverse)\(/.test(l))
    // Positive control: a pattern that matched nothing would make the next assertion vacuous.
    expect(found.length).toBeGreaterThanOrEqual(PINNED.length)
    expect([...found].sort()).toEqual([...PINNED].sort())
  })

  it('keeps the two dated writers dating', () => {
    // Guards the POLICY, not just the set: dropping either bump leaves the writer in the
    // pinned list while silently un-dating it.
    expect(source).toContain('if (membershipMoved(state, next)) state.closeSeq = (state.closeSeq ?? 0) + 1')
    const addBody = source.slice(source.indexOf('addSlotOptimistic(state,'))
    expect(addBody.slice(0, addBody.indexOf('\n    }'))).toContain('state.closeSeq = (state.closeSeq ?? 0) + 1')
  })
})

describe('a resolved slots read reports whether its list applied', () => {
  const issue = (state: Parameters<typeof dashboardReducer>[0], id: string) =>
    dashboardReducer(state, { type: fetchSlots.pending.type, meta: { requestId: id } })
  const resolve = (state: Parameters<typeof dashboardReducer>[0], id: string, payload: ChatSlot[]) =>
    dashboardReducer(state, { type: fetchSlots.fulfilled.type, payload, meta: { requestId: id } })

  it('applies a read that no membership move overtook', () => {
    const state = resolve(issue(seeded(['chat-1']), 'r-ok'), 'r-ok', [slot('chat-1'), slot('chat-2')])
    expect(keysOf(state)).toEqual(['chat-1', 'chat-2'])
  })

  /** The gap this closes: `fulfilled` fires here too, so a caller acting on the payload
   *  alone would act on a list the store threw away. */
  it('refuses a reply that predates a close, so its list never lands', () => {
    const issued = issue(seeded(['chat-1', 'chat-2']), 'r-old')
    const closed = dashboardReducer(issued, slotCloseStarted('chat-2'))
    const state = resolve(closed, 'r-old', [slot('chat-1'), slot('chat-2'), slot('chat-3')])
    // The predating list named a THIRD slot; refusing it means that slot never appears.
    expect(keysOf(state)).toEqual(['chat-1', 'chat-2'])
  })

  /** The verdict must be the REDUCER's, recorded when it decides, not the thunk's guess:
   *  `closeSeq` can advance in the gap, so the reducer refuses lists the guess called applied. */
  it('records the reducer verdict against the read that resolved', () => {
    const ok = resolve(issue(seeded(['chat-1']), 'r-ok'), 'r-ok', [slot('chat-1'), slot('chat-2')])
    expect(ok.lastSlotsRead).toEqual({ readId: 'r-ok', applied: true })
    const issued = issue(seeded(['chat-1', 'chat-2']), 'r-old')
    const closed = dashboardReducer(issued, slotCloseStarted('chat-2'))
    const refused = resolve(closed, 'r-old', [slot('chat-1'), slot('chat-2'), slot('chat-3')])
    expect(refused.lastSlotsRead).toEqual({ readId: 'r-old', applied: false })
  })

  /** ONE channel, ONE entry point: read after the dispatch resolves, so a destructive
   *  consumer cannot act on a pre-reduction guess the reducer went on to refuse. */
  const resolved = { payload: [slot('chat-1')], meta: { requestId: 'r-1' } }
  const withVerdict = (v: { readId?: string; applied: boolean } | null) =>
    (() => ({ dashboard: { lastSlotsRead: v } })) as never

  it('hands back the list only when the reducer applied THIS read', async () => {
    const got = await fetchSlotsIfApplied(
      (() => Promise.resolve(resolved)) as never,
      withVerdict({ readId: 'r-1', applied: true }),
    )
    expect(got).toEqual([slot('chat-1')])
  })

  it('hands back null when refused, when a SIBLING read answered, and when nothing did', async () => {
    const call = (v: { readId?: string; applied: boolean } | null) =>
      fetchSlotsIfApplied((() => Promise.resolve(resolved)) as never, withVerdict(v))
    expect(await call({ readId: 'r-1', applied: false })).toBeNull()
    // A different read's verdict must not answer for this one.
    expect(await call({ readId: 'r-other', applied: true })).toBeNull()
    expect(await call(null)).toBeNull()
  })
})

describe('destructive storage GC never runs on a list that was refused', () => {
  /** A refused `fetchSlots` reply omits a session created mid-read, so GC-ing from it
   *  deletes a LIVE session's localStorage. One caller was missed once; this pins EVERY
   *  caller, so a third one has to carry the guard too. */
  const callers = readdirSync('src', { recursive: true, encoding: 'utf8' })
    .filter(f => /\.tsx?$/.test(f) && !/\.test\.tsx?$/.test(f))
    .map(f => [`src/${f}`, readFileSync(`src/${f}`, 'utf8')] as const)
    // The defining module matches its own name; it is not a caller.
    .filter(([path, src]) => /\bgcOrphanedStorage\(/.test(src) && !path.endsWith('utils/storageGc.ts'))

  it('guards every gcOrphanedStorage caller with the applied-read check', () => {
    // Positive control: zero call sites found would make the loop below assert nothing.
    const sites = callers.flatMap(([path, src]) => {
      const lines = src.split('\n')
      return lines.flatMap((line, i) => (/\bgcOrphanedStorage\(/.test(line)
        // Adjacent, not file-wide: the import line alone satisfies a whole-file match,
        // which let a mutation strip the guard from the call and still pass.
        ? [[`${path}:${i + 1}`, lines.slice(Math.max(0, i - 8), i + 1).join('\n')] as const]
        : []))
    })
    expect(sites.length).toBeGreaterThan(0)
    for (const [where, window] of sites) {
      expect(window, `${where} calls gcOrphanedStorage without going through fetchSlotsIfApplied`)
        .toMatch(/fetchSlotsIfApplied/)
      // Skipping is the only safe answer. Substituting the store's own list is NOT a
      // guard: at boot it is populated BY this read, so it can be emptier than the reply.
      expect(window, `${where} feeds gcOrphanedStorage a fallback list instead of skipping`)
        .not.toMatch(/dashboard\.slots/)
    }
  })

  /** The strongest form of the guarantee now available. A client cannot prove a key is
   *  dead -- localStorage is shared across tabs, so another tab may hold or resume the
   *  session -- so there is no per-key collector left to call from anywhere. */
  it('ships no per-key storage collector at all', () => {
    const prod = readdirSync('src', { recursive: true, encoding: 'utf8' })
      .filter(f => /\.tsx?$/.test(f) && !/\.test\.tsx?$/.test(f))
    const offenders = prod.filter(f => /\bgcSessionStorage\b/.test(readFileSync(`src/${f}`, 'utf8')))
    // Positive control: the same scan DOES find the sweep collector, so an empty offender
    // list is a fact about the per-key collector rather than about a scan reading nothing.
    const sweepers = prod.filter(f => /\bgcOrphanedStorage\b/.test(readFileSync(`src/${f}`, 'utf8')))
    expect(sweepers.length).toBeGreaterThan(0)
    expect(offenders).toEqual([])
  })
})
