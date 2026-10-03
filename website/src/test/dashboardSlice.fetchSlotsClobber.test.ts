import { describe, it, expect, vi } from 'vitest'
import reducer, {
  sseSlots,
  sseConnected,
  sseSlotTitle,
  sseSlotColor,
  sseTodoUpdate,
  sseMcpReportUpdate,
  touchSlotActivity,
  updateSlot,
  updateSlotFolder,
  updateSlotPin,
  patchSlotLink,
  patchSlotSourceLinks,
  fetchSlots,
  addSlotOptimistic,
} from '../store/dashboardSlice'
import type { ChatSlot } from '../types'

vi.mock('../api/client', () => ({
  api: { chatSlots: vi.fn(), chatMode: vi.fn() },
}))

/**
 * The `/api/chat/slots` round-trip window (issue #11149).
 *
 * `applySlots` is a whole-list positional replace: a row survives only while it
 * is `jsonEqual` to the incoming one, so any single-slot write that lands after
 * the server serialized its reply and before `fetchSlots.fulfilled` runs used to
 * be silently overwritten by the older server row.
 *
 * These cases drive the window directly — `pending`, then the write, then
 * `fulfilled` carrying the PRE-write row — for every single-slot writer in the
 * slice, so the guarantee is stated per writer rather than per caller. The
 * caller list is deliberately absent: the fix is in the reducer, so no caller
 * can opt out of it and no caller has to opt in.
 */

const mk = (key: string, over: Partial<ChatSlot> = {}): ChatSlot => ({
  key,
  title: key,
  messages: 1,
  running: false,
  pending_approval: false,
  waiting_for_input: false,
  last_activity_ts: undefined,
  ...over,
})

/** Fresh objects, as the real payload is parsed from the wire. */
const wire = (...slots: ChatSlot[]): ChatSlot[] => slots.map(s => JSON.parse(JSON.stringify(s)) as ChatSlot)

const started = (requestId: string) => ({
  type: fetchSlots.pending.type,
  meta: { requestId, requestStatus: 'pending' },
})
const reply = (slots: ChatSlot[], requestId: string) => ({
  type: fetchSlots.fulfilled.type,
  payload: wire(...slots),
  meta: { requestId, requestStatus: 'fulfilled' },
})

/** A store that already holds a live snapshot, so `slotsLoaded` is true and the
 *  reply is no longer the only list there is. */
const loaded = (...slots: ChatSlot[]) =>
  reducer(reducer(undefined, { type: '@@INIT' }), sseSlots(wire(...slots)))

const rowOf = (state: ReturnType<typeof loaded>, key: string) => state.slots.find(s => s.key === key)

/** Stamps are indexed through a `k:` prefix (see `stampKey`), so no slot key can
 *  collide with `Object.prototype`. Read them the way the slice writes them. */
const stampOf = (state: ReturnType<typeof loaded>, key: string) => state.slotWrittenAt[`k:${key}`]

/** The membership stamp (`slotMemberAt`), read the same prefixed way. */
const stampOfMember = (state: ReturnType<typeof loaded>, key: string) => state.slotMemberAt[`k:${key}`]

describe('a fetchSlots reply cannot clobber a write that raced it', () => {
  /** Each case: seed row, the write that lands mid-flight, and what must survive. */
  const cases: {
    writer: string
    seed: ChatSlot
    write: (state: ReturnType<typeof loaded>) => ReturnType<typeof loaded>
    survives: (row: ChatSlot | undefined) => void
  }[] = [
    {
      writer: 'sseSlotTitle',
      seed: mk('a', { title: 'old' }),
      write: s => reducer(s, sseSlotTitle({ key: 'a', title: 'renamed' })),
      survives: row => expect(row?.title).toBe('renamed'),
    },
    {
      writer: 'sseTodoUpdate',
      seed: mk('a'),
      write: s => reducer(s, sseTodoUpdate({ slot: 'a', todo: { items: [{ text: 'ship', status: 'in_progress' }] } as never })),
      survives: row => expect(row?.todo).toBeTruthy(),
    },
    {
      writer: 'sseMcpReportUpdate',
      seed: mk('a'),
      write: s => reducer(s, sseMcpReportUpdate({ slot: 'a', mcp_report: { servers: [] } as never })),
      survives: row => expect(row?.mcp_report).toBeTruthy(),
    },
    {
      writer: 'touchSlotActivity',
      seed: mk('a', { last_ts: '2026-01-01T00:00:00Z' }),
      write: s => reducer(s, touchSlotActivity({ key: 'a', ts: '2026-06-01T00:00:00Z', settled: true })),
      survives: row => expect(row?.last_ts).toBe('2026-06-01T00:00:00Z'),
    },
    {
      writer: 'updateSlot',
      seed: mk('a', { messages: 1 }),
      write: s => reducer(s, updateSlot({ key: 'a', messages: 42 })),
      survives: row => expect(row?.messages).toBe(42),
    },
    {
      writer: 'updateSlotFolder',
      seed: mk('a'),
      write: s => reducer(s, updateSlotFolder({ key: 'a', folderId: 'f-1' })),
      survives: row => expect(row?.folder_id).toBe('f-1'),
    },
    {
      writer: 'updateSlotPin',
      seed: mk('a', { pinned: false }),
      write: s => reducer(s, updateSlotPin({ key: 'a', pinned: true })),
      survives: row => expect(row?.pinned).toBe(true),
    },
    {
      writer: 'sseSlotColor',
      seed: mk('a', { color_index: 1 } as Partial<ChatSlot>),
      write: s => reducer(s, sseSlotColor({ key: 'a', color_index: 7 })),
      survives: row => expect(row?.color_index).toBe(7),
    },
    {
      writer: 'patchSlotLink',
      seed: mk('a', { links: [{ channel: 'slack', direction: 'origin', paused: false }] } as Partial<ChatSlot>),
      write: s => reducer(s, patchSlotLink({ key: 'a', channel: 'slack', patch: { paused: true } })),
      survives: row => expect(row?.links?.[0].paused).toBe(true),
    },
    {
      writer: 'patchSlotSourceLinks',
      seed: mk('a', { source_links: [{ url: 'https://example.test/pr/1', state: 'open' }] } as Partial<ChatSlot>),
      write: s => reducer(s, patchSlotSourceLinks({ url: 'https://example.test/pr/1', state: 'merged' })),
      survives: row => expect(row?.source_links?.[0].state).toBe('merged'),
    },
  ]

  for (const c of cases) {
    it(`${c.writer}: the pre-write server row does not win`, () => {
      const base = loaded(c.seed)
      // The request leaves. Everything from here on is newer than its reply.
      const inFlight = reducer(base, started('r1'))
      const written = c.write(inFlight)
      // The reply lands carrying the row as it was BEFORE the write.
      const settled = reducer(written, reply([c.seed], 'r1'))
      c.survives(rowOf(settled, 'a'))
    })
  }

  it('holds only the raced key: the same reply still applies to every other row', () => {
    const base = loaded(mk('a', { title: 'old-a' }), mk('b', { title: 'old-b' }))
    const inFlight = reducer(base, started('r1'))
    const written = reducer(inFlight, sseSlotTitle({ key: 'a', title: 'renamed-a' }))
    // The reply predates the rename of `a` but carries a genuine change to `b`.
    const settled = reducer(written, reply([mk('a', { title: 'old-a' }), mk('b', { title: 'server-b' })], 'r1'))
    expect(rowOf(settled, 'a')?.title).toBe('renamed-a')
    expect(rowOf(settled, 'b')?.title).toBe('server-b')
  })

  it('holds only against the replies the write outran', () => {
    const base = loaded(mk('a', { title: 'old' }))
    const written = reducer(reducer(base, started('r1')), sseSlotTitle({ key: 'a', title: 'renamed' }))
    // A request dispatched AFTER the write can see it, so its reply is newer and
    // is authoritative — including a server-side rename back.
    const later = reducer(written, started('r2'))
    const settled = reducer(later, reply([mk('a', { title: 'old' })], 'r2'))
    expect(rowOf(settled, 'a')?.title).toBe('old')
  })

  it('applies a reply unfiltered before the first snapshot', () => {
    // Cold boot: nothing on screen can be newer than the only list there is.
    // Note this holds WITHOUT gating on `slotsLoaded` — `patchSlotRow` can only
    // stamp a row it found, and an empty list has none.
    const cold = reducer(undefined, { type: '@@INIT' })
    const written = reducer(reducer(cold, started('r1')), sseSlotTitle({ key: 'a', title: 'renamed' }))
    expect(written.slotWrittenAt).toEqual({})
    const settled = reducer(written, reply([mk('a', { title: 'server' })], 'r1'))
    expect(rowOf(settled, 'a')?.title).toBe('server')
  })

  it('still holds across a reconnect, which clears slotsLoaded but keeps the rows', () => {
    // The reconnect path is the whole reason this is not gated on `slotsLoaded`:
    // `sseConnected` clears the flag while leaving `slots` populated, and
    // `useWebSocket` dispatches it and then `fetchSlots()` while the WS replay
    // backlog writes into those same rows. Gating would switch the guard off
    // exactly here, on the caller the issue rates highest-risk.
    const base = loaded(mk('a', { title: 'old' }))
    const reconnected = reducer(base, sseConnected())
    expect(reconnected.slotsLoaded).toBe(false)
    expect(reconnected.slots).toHaveLength(1)
    const inFlight = reducer(reconnected, started('r1'))
    const written = reducer(inFlight, sseSlotTitle({ key: 'a', title: 'renamed' }))
    const settled = reducer(written, reply([mk('a', { title: 'old' })], 'r1'))
    expect(rowOf(settled, 'a')?.title).toBe('renamed')
  })

  it('protects a slot whose key would collide with Object.prototype', () => {
    // Stamps are indexed through a prefix, so there is no key the guard skips.
    // A bail-out on such a key would leave exactly this row unprotected while
    // every comment in the slice says the clobber is unrepresentable.
    for (const key of ['__proto__', 'constructor', 'prototype']) {
      const base = loaded(mk(key, { title: 'old' }))
      const written = reducer(reducer(base, started('r1')), sseSlotTitle({ key, title: 'renamed' }))
      const settled = reducer(written, reply([mk(key, { title: 'old' })], 'r1'))
      expect(settled.slots.find(s => s.key === key)?.title, key).toBe('renamed')
    }
  })

  it('keeps the stamp record free of bare slot keys', () => {
    const written = reducer(loaded(mk('a')), sseSlotTitle({ key: 'a', title: 'renamed' }))
    expect(Object.keys(written.slotWrittenAt)).toEqual(['k:a'])
    // Nothing was written through Object.prototype on the way.
    expect(Object.prototype.hasOwnProperty.call(written.slotWrittenAt, 'a')).toBe(false)
  })

  it('does not hold on a write both recency guards rejected', () => {
    // `touchSlotActivity` with an OLDER ts changes nothing — provided BOTH
    // watermarks are already ahead of it, since an unset `last_turn_ts` is a
    // real write. A bump that moved nothing must not withhold the reply's row.
    const base = loaded(mk('a', { title: 'old', last_ts: '2026-06-01T00:00:00Z', last_turn_ts: '2026-06-01T00:00:00Z' }))
    const inFlight = reducer(base, started('r1'))
    const noop = reducer(inFlight, touchSlotActivity({ key: 'a', ts: '2026-01-01T00:00:00Z', settled: true }))
    const settled = reducer(noop, reply([mk('a', { title: 'server', last_ts: '2026-06-01T00:00:00Z', last_turn_ts: '2026-06-01T00:00:00Z' })], 'r1'))
    expect(rowOf(settled, 'a')?.title).toBe('server')
  })

  it('does not hold every row on screen for a source-link delta that matched one', () => {
    const base = loaded(
      mk('a', { source_links: [{ url: 'https://example.test/pr/1', state: 'open' }] } as Partial<ChatSlot>),
      mk('b', { title: 'old-b' }),
    )
    const inFlight = reducer(base, started('r1'))
    const written = reducer(inFlight, patchSlotSourceLinks({ url: 'https://example.test/pr/1', state: 'merged' }))
    const settled = reducer(written, reply([
      mk('a', { source_links: [{ url: 'https://example.test/pr/1', state: 'open' }] } as Partial<ChatSlot>),
      mk('b', { title: 'server-b' }),
    ], 'r1'))
    expect(rowOf(settled, 'a')?.source_links?.[0].state).toBe('merged')
    expect(rowOf(settled, 'b')?.title).toBe('server-b')
  })

  it('releases the request mark when the reply settles', () => {
    const base = loaded(mk('a'))
    const settled = reducer(reducer(base, started('r1')), reply([mk('a')], 'r1'))
    expect(settled.slotFetchWriteMark.r1).toBeUndefined()
  })

  it('releases the request mark when the request fails', () => {
    const base = loaded(mk('a'))
    const inFlight = reducer(base, started('r1'))
    const failed = reducer(inFlight, { type: fetchSlots.rejected.type, meta: { requestId: 'r1', requestStatus: 'rejected' } })
    expect(failed.slotFetchWriteMark.r1).toBeUndefined()
  })

  it('prunes a write stamp once an authoritative frame drops the key', () => {
    const base = loaded(mk('a'), mk('b'))
    const written = reducer(base, sseSlotTitle({ key: 'a', title: 'renamed' }))
    expect(stampOf(written, 'a')).toBeGreaterThan(0)
    // A live frame is authoritative on membership, so `a` is gone for good.
    const dropped = reducer(written, sseSlots(wire(mk('b'))))
    expect(stampOf(dropped, 'a')).toBeUndefined()
  })

  it('keeps a stamp when a stale reply merely omits a key it never saw', () => {
    // An HTTP reply can predate a slot's creation. Eviction is withheld there,
    // and so is pruning — otherwise the reply would strip the protection of the
    // very row it is too old to describe.
    const base = loaded(mk('a'), mk('b'))
    const written = reducer(base, sseSlotTitle({ key: 'a', title: 'renamed' }))
    const settled = reducer(reducer(written, started('r1')), reply([mk('b')], 'r1'))
    expect(stampOf(settled, 'a')).toBeGreaterThan(0)
  })
})

/**
 * Membership half of the same window (issue #14831).
 *
 * A chat created with the Sessions-panel New chat button joins the list on
 * `chat/createSlot/fulfilled`. If a `GET /api/chat/slots` was already in flight
 * when the create landed, the server serialized that reply before the new slot
 * existed, so the reply OMITS the new key. `applySlots` is a whole-list
 * positional replace, so the omitting reply drops the freshly created row — and
 * the recency guard, which only substitutes rows the reply CONTAINS, cannot
 * re-add a key the reply left out. The row stays gone until the next
 * authoritative list, which is why a full reload was the only cure.
 *
 * The create is a single-slot membership write, so it must outrank a reply that
 * predates it exactly as a content write does — and the guard must RE-ADD the
 * omitted row, not merely refrain from substituting it.
 */
const createdSlot = (slot: ChatSlot) => ({
  type: 'chat/createSlot/fulfilled',
  payload: wire(slot)[0],
  meta: { requestStatus: 'fulfilled' },
})

describe('a fetchSlots reply cannot drop a slot whose creation raced it', () => {
  it('re-adds a mid-flight created slot the reply omits', () => {
    const base = loaded(mk('a'))
    // The list fetch leaves while only `a` exists.
    const inFlight = reducer(base, started('r1'))
    // The New chat button creates `b`; it joins the list.
    const created = reducer(inFlight, createdSlot(mk('b', { title: 'new chat' })))
    expect(rowOf(created, 'b')).toBeTruthy()
    // The reply the server serialized before `b` existed carries only `a`.
    const settled = reducer(created, reply([mk('a')], 'r1'))
    expect(rowOf(settled, 'b')?.title).toBe('new chat')
    expect(rowOf(settled, 'a')).toBeTruthy()
  })

  it('stamps the create so it participates in the recency guard', () => {
    const inFlight = reducer(loaded(mk('a')), started('r1'))
    const created = reducer(inFlight, createdSlot(mk('b')))
    expect(stampOfMember(created, 'b')).toBeGreaterThan(0)
  })

  it('does not re-add a slot an authoritative live frame removed', () => {
    // Only a reply that OUTRAN the create resurrects it. A live `slots` frame
    // dispatched after the create is authoritative on membership: if it drops
    // the key, the key is gone, and its stamp is pruned so no later reply
    // revives it.
    const inFlight = reducer(loaded(mk('a')), started('r1'))
    const created = reducer(inFlight, createdSlot(mk('b')))
    const live = reducer(created, sseSlots(wire(mk('a'))))
    expect(rowOf(live, 'b')).toBeUndefined()
    expect(stampOfMember(live, 'b')).toBeUndefined()
    // A later reply that also omits `b` leaves it gone — the create no longer
    // outranks anything.
    const settled = reducer(reducer(live, started('r2')), reply([mk('a')], 'r2'))
    expect(rowOf(settled, 'b')).toBeUndefined()
  })

  it('lets a reply dispatched after the create omit it authoritatively', () => {
    // A request that leaves AFTER the create can see it, so a reply that omits
    // the key means it was deleted server-side; the row must not be resurrected.
    const created = reducer(loaded(mk('a')), createdSlot(mk('b')))
    const later = reducer(created, started('r1'))
    const settled = reducer(later, reply([mk('a')], 'r1'))
    expect(rowOf(settled, 'b')).toBeUndefined()
  })

  it('re-adds the omitted creation without disturbing the reply order of the rest', () => {
    const base = loaded(mk('a'), mk('c'))
    const inFlight = reducer(base, started('r1'))
    const created = reducer(inFlight, createdSlot(mk('b', { title: 'new' })))
    // Reply carries a genuine rename of `c` but predates `b`.
    const settled = reducer(created, reply([mk('a'), mk('c', { title: 'server-c' })], 'r1'))
    expect(rowOf(settled, 'a')).toBeTruthy()
    expect(rowOf(settled, 'c')?.title).toBe('server-c')
    expect(rowOf(settled, 'b')?.title).toBe('new')
  })

  it('does not resurrect a content-only write when the reply omits its key', () => {
    // GPT F1: a rename stamps the row for CONTENT recency, but a reply that
    // omits the key means another client deleted it in the window. A content
    // write must keep its row only while the reply still lists it (substitution)
    // — it must NOT re-add a key the reply dropped, or a deleted chat returns as
    // a ghost row.
    const base = loaded(mk('a'), mk('b', { title: 'old' }))
    const inFlight = reducer(base, started('r1'))
    const renamed = reducer(inFlight, sseSlotTitle({ key: 'b', title: 'renamed' }))
    // The reply omits `b` entirely (a foreign delete serialized after it).
    const settled = reducer(renamed, reply([mk('a')], 'r1'))
    expect(rowOf(settled, 'b')).toBeUndefined()
    expect(rowOf(settled, 'a')).toBeTruthy()
  })

  it('still substitutes a content write for a key the reply DOES list', () => {
    // The content guard is unchanged where the reply lists the key: the
    // on-screen row wins over the pre-write server row.
    const base = loaded(mk('a', { title: 'old' }))
    const inFlight = reducer(base, started('r1'))
    const renamed = reducer(inFlight, sseSlotTitle({ key: 'a', title: 'renamed' }))
    const settled = reducer(renamed, reply([mk('a', { title: 'old' })], 'r1'))
    expect(rowOf(settled, 'a')?.title).toBe('renamed')
  })

  it('re-adds a mid-flight addSlotOptimistic (resume/fork/companion) the reply omits', () => {
    // Every add site routes through addSlotOptimistic, so the one stamp there
    // covers resume, fork, companion-chat and Papyrus — not just the New chat
    // button's createSlot path.
    const base = loaded(mk('a'))
    const inFlight = reducer(base, started('r1'))
    const added = reducer(inFlight, addSlotOptimistic(mk('b', { title: 'resumed' })))
    const settled = reducer(added, reply([mk('a')], 'r1'))
    expect(rowOf(settled, 'b')?.title).toBe('resumed')
  })

  it('does not re-add a mid-flight creation on a reply dispatched after it (GPT F1)', () => {
    // GPT F1: a foreign client deletes `b` after the create and no WS removal
    // frame reaches this tab. The FIRST in-flight reply (serialized before the
    // create) still re-adds it — the #14831 cure — but a LATER fetch dispatched
    // after the create carries a mark at or above the membership stamp, so
    // `membersOutranking` never re-adds `b` for it: the row drops as the
    // authoritative foreign delete it is, no resurrection while the WS is down.
    const base = loaded(mk('a'))
    const inFlight = reducer(base, started('r1'))
    const created = reducer(inFlight, createdSlot(mk('b', { title: 'new chat' })))
    const firstReply = reducer(created, reply([mk('a')], 'r1'))
    expect(rowOf(firstReply, 'b')?.title).toBe('new chat') // re-added by the pre-add reply
    // A later fetch leaves AFTER the create and its reply also omits `b`.
    const second = reducer(reducer(firstReply, started('r2')), reply([mk('a')], 'r2'))
    expect(rowOf(second, 'b')).toBeUndefined() // not re-added: r2's mark covers the add
  })

  it('keeps the new row across TWO overlapping pre-create fetches', () => {
    // Two `fetchSlots` are in flight at once — a routine state: slotFetchesInFlight
    // is a list, the thunk has no dedupe, and reconnect/replay dispatch sites
    // overlap. Both r1 and r2 are dispatched BEFORE the create, so both carry a
    // mark below the membership stamp and both replies re-add `b` when they omit
    // it. Neither drops the new row.
    const base = loaded(mk('a'))
    const twoInFlight = reducer(reducer(base, started('r1')), started('r2'))
    const created = reducer(twoInFlight, createdSlot(mk('b', { title: 'new chat' })))
    const afterR1 = reducer(created, reply([mk('a')], 'r1'))
    expect(rowOf(afterR1, 'b')?.title).toBe('new chat') // r1 re-adds
    const afterR2 = reducer(afterR1, reply([mk('a')], 'r2'))
    expect(rowOf(afterR2, 'b')?.title).toBe('new chat') // r2 keeps it, not dropped
  })

  it('still drops a foreign-deleted row once no pre-add fetch remains, after overlap', () => {
    // After both pre-create replies settle, a THIRD later fetch dispatched after
    // the create carries a mark at or above the stamp, so its omitting reply (an
    // authoritative foreign delete) drops `b` — the overlap never becomes a ghost.
    const base = loaded(mk('a'))
    const twoInFlight = reducer(reducer(base, started('r1')), started('r2'))
    const created = reducer(twoInFlight, createdSlot(mk('b')))
    const afterR1 = reducer(created, reply([mk('a')], 'r1'))
    const afterR2 = reducer(afterR1, reply([mk('a')], 'r2'))
    const third = reducer(reducer(afterR2, started('r3')), reply([mk('a')], 'r3'))
    expect(rowOf(third, 'b')).toBeUndefined()
  })

  it('keeps the full server row when a reply genuinely lists the new key (Opus)', () => {
    // Opus advisory: the membership stamp must NOT also stamp CONTENT, or a
    // reply that genuinely lists the new key (its handler ran after the server
    // created the slot) is sent down the substitution branch and the thin
    // optimistic row wins — dropping server-only fields the reply carried.
    const base = loaded(mk('a'))
    const inFlight = reducer(base, started('r1'))
    const added = reducer(inFlight, addSlotOptimistic(mk('b', { title: 'thin' })))
    // The reply DOES list `b`, with the fuller server title.
    const settled = reducer(added, reply([mk('a'), mk('b', { title: 'server authoritative' })], 'r1'))
    expect(rowOf(settled, 'b')?.title).toBe('server authoritative')
  })

  it('bumps the write counter on a membership add so other races still order', () => {
    // stampSlotMember still advances slotWriteSeq, so a content write that lands
    // after an add is correctly newer than a reply dispatched before either.
    const base = loaded(mk('a'))
    const inFlight = reducer(base, started('r1'))
    const added = reducer(inFlight, addSlotOptimistic(mk('b')))
    const renamed = reducer(added, sseSlotTitle({ key: 'a', title: 'renamed' }))
    const settled = reducer(renamed, reply([mk('a', { title: 'old' }), mk('b')], 'r1'))
    expect(rowOf(settled, 'a')?.title).toBe('renamed')
  })
})
