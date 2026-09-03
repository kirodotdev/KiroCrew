/**
 * Opening a chat fetches ONE page, not the slot's whole chained history.
 *
 * The risk pinned here is not the bound but its consequence: a bounded first
 * page can be shorter than the viewport, leaving the top-of-transcript path as
 * the only route to older history. So these tests assert the store lands
 * *pageable* after a bounded open — including a page far too short to overflow
 * any viewport — and that a page-back fetches and prepends. jsdom has no
 * layout, so asserting on scrollability would assert on fiction; the real gate
 * and the real thunk are used instead.
 *
 * refreshSlot is pinned unbounded (it REPLACES a transcript it did not page) and
 * the warm path at PANE_HYDRATE_LIMIT, so THIS file's bound must not leak there.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { createTestStore } from './helpers'
import { switchSlot, refreshSlot, loadOlderMessages, clearMessages, deleteSlot, OLDER_PAGE_LIMIT, OLDER_WALK_PAGE_LIMIT } from '../store/chatSlice'
import { shouldPaginateOlder } from '../pages/chat/pagination'
import { api } from '../api/client'

const SLOT = 'slot-1'

interface Row { role: string; content: string; cls: string; ts: string; meta?: Record<string, unknown> }
const rows = (n: number, prefix: string): Row[] =>
  Array.from({ length: n }, (_, i) => ({
    role: 'assistant', content: `${prefix}${i}`, cls: 'msg msg-a',
    ts: `2026-01-01T00:00:${String(i).padStart(2, '0')}Z`,
  }))

/** A bounded slot-detail response: `next_before` is where the next page starts. */
function page(messages: Row[], hasMore: boolean, nextBefore: number) {
  return { messages, has_more: hasMore, next_before: nextBefore, total: nextBefore + messages.length }
}

/** Open a chat and return the store plus the api spy. */
async function open(detail: ReturnType<typeof vi.spyOn>) {
  const store = createTestStore()
  await store.dispatch(switchSlot(SLOT))
  return { store, detail }
}

afterEach(() => { vi.restoreAllMocks() })

describe('bounded initial fetch leaves older history reachable', () => {
  it('a first page far too short to fill a viewport is still pageable', async () => {
    const detail = vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page(rows(3, 'm'), true, 240) as never)
    const { store } = await open(detail)
    const chat = store.getState().chat

    // Three rows cannot overflow anything, so this is exactly the case where a
    // gate requiring an already-scrollable transcript would deadlock.
    expect(chat.messages).toHaveLength(3)
    expect(shouldPaginateOlder({ loadingOlder: chat.loadingOlder, slotHasMore: chat.slotHasMore })).toBe(true)

    // The click affordance's own mount condition in ChatPage, so the reader has a
    // path that does not depend on an intersection callback firing at all.
    expect(chat.slotHasMore && chat.slotCursorKey === chat.activeSlot).toBe(true)
    expect(chat.slotOldestIndex).toBe(240)
  })

  it('paging back from that short page fetches at the cursor and prepends', async () => {
    const detail = vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page(rows(3, 'm'), true, 240) as never)
    const { store } = await open(detail)

    detail.mockResolvedValue(page(rows(2, 'older'), false, 0) as never)
    await store.dispatch(loadOlderMessages())

    expect(detail).toHaveBeenLastCalledWith(SLOT, OLDER_WALK_PAGE_LIMIT, 240, expect.any(AbortSignal))
    const chat = store.getState().chat
    expect(chat.messages).toHaveLength(5)
    expect(chat.messages[0].content).toBe('older0')
    expect(chat.messages[4].content).toBe('m2')
    // Server reported the start of history, so the affordance retires.
    expect(chat.slotHasMore).toBe(false)
  })

  it('does not offer paging when the bounded page is the whole history', async () => {
    // Negative control: the pageable assertions above must be able to read false.
    const detail = vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page(rows(3, 'm'), false, 0) as never)
    const { store } = await open(detail)
    const chat = store.getState().chat
    expect(shouldPaginateOlder({ loadingOlder: chat.loadingOlder, slotHasMore: chat.slotHasMore })).toBe(false)
    expect(chat.slotHasMore && chat.slotCursorKey === chat.activeSlot).toBe(false)
  })

  it('a long session renders its newest page and can page back', async () => {
    const detail = vi.spyOn(api, 'chatSlotDetail')
      .mockResolvedValue(page(rows(OLDER_PAGE_LIMIT, 'm'), true, 500) as never)
    const { store } = await open(detail)
    const chat = store.getState().chat
    expect(chat.messages).toHaveLength(OLDER_PAGE_LIMIT)
    expect(chat.messages[OLDER_PAGE_LIMIT - 1].content).toBe(`m${OLDER_PAGE_LIMIT - 1}`)
    expect(shouldPaginateOlder({ loadingOlder: chat.loadingOlder, slotHasMore: chat.slotHasMore })).toBe(true)
  })
})

describe('preserved thinking order under a bounded window', () => {
  /* A reasoning block is anchored to the assistant row that follows it. When the
   * open is BOUNDED that anchor can fall outside the fetched page, so it matches
   * nothing and the tail append lands it below the NEWEST answer -- where this
   * store's own note records it sticks and is re-appended on every later refresh.
   * Driven through the real thunk: `switchSlot.pending` restores slotMessages as
   * `existing`, which is exactly the switch-away-and-back path at issue. */
  const A = (content: string, i: number): Row => ({
    role: 'assistant', content, cls: 'msg msg-a', ts: `2026-01-01T00:00:0${i}Z`,
  })
  const THINK = { role: 'thinking', content: 'old reasoning', cls: 'msg msg-think', ts: '2026-01-01T00:00:00Z' }
  /** Retained transcript: the block's anchor is an OLDER answer, so a bounded
   *  page that returns only the newest reply leaves that anchor behind. */
  const CACHED = [THINK, A('OLD ANSWER', 1), A('NEWEST ANSWER', 2)]

  async function reopenStore(serverRows: Row[], hasMore: boolean, cached = CACHED) {
    const base = createTestStore().getState().chat
    const store = createTestStore({ chat: { ...base, slotMessages: { [SLOT]: cached } } } as never)
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page(serverRows, hasMore, 240) as never)
    await store.dispatch(switchSlot(SLOT))
    return store
  }

  async function reopen(serverRows: Row[], hasMore: boolean, cached = CACHED) {
    return (await reopenStore(serverRows, hasMore, cached)).getState().chat.messages
  }

  it('does not strand an off-window anchored block below the newest answer', async () => {
    const m = await reopen([A('NEWEST ANSWER', 2)], true)
    expect(m.map(r => r.role)).not.toContain('thinking')
    expect(m[m.length - 1].content).toBe('NEWEST ANSWER')
  })

  it('keeps an anchored block in position when the window is COMPLETE', async () => {
    const m = await reopen([A('OLD ANSWER', 1), A('NEWEST ANSWER', 2)], false)
    const think = m.findIndex(r => r.role === 'thinking')
    const anchor = m.findIndex(r => r.content === 'OLD ANSWER')
    expect(think).toBeGreaterThanOrEqual(0)
    // Restored before its own anchor, NOT after the newest answer.
    expect(think).toBeLessThan(anchor)
  })

  it('still appends an UNANCHORED block on a bounded window', async () => {
    // No assistant row follows it, so it is the in-flight / confirmed-steer case
    // the tail append exists for; bounding the fetch must not discard it.
    const m = await reopen([A('NEWEST ANSWER', 2)], true, [A('NEWEST ANSWER', 2), THINK])
    expect(m.map(r => r.role)).toContain('thinking')
  })

  it('parks an anchored-but-absent block even when the window is COMPLETE', async () => {
    // #5802 drops a confirmed anchor inside the covered region instead of stacking it
    // at the tail; the sink keeps the never-silently-lost guarantee append used to give.
    const chat = (await reopenStore([A('NEWEST ANSWER', 2)], false)).getState().chat
    expect(chat.messages.map(r => r.role)).not.toContain('thinking')
    expect((chat.thinkingOrphans[SLOT] ?? []).map(o => o.msg.content)).toEqual(['old reasoning'])
  })

  it('PARKS the off-window block rather than discarding it', async () => {
    // Reasoning is client-only, so `messages` was its ONLY copy: skipping the row
    // without keeping it anywhere loses it for good, page-back included.
    const chat = (await reopenStore([A('NEWEST ANSWER', 2)], true)).getState().chat
    expect(chat.messages.map(r => r.role)).not.toContain('thinking')
    const parked = chat.thinkingOrphans[SLOT] ?? []
    expect(parked).toHaveLength(1)
    expect(parked[0].anchor).toEqual({ text: 'OLD ANSWER', ts: '2026-01-01T00:00:01Z' })
    expect(parked[0].msg.content).toBe('old reasoning')
  })

  it('re-seats the parked block, before its anchor, once that anchor pages in', async () => {
    const store = await reopenStore([A('NEWEST ANSWER', 2)], true)
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page([A('OLD ANSWER', 1)], false, 0) as never)
    await store.dispatch(loadOlderMessages())
    const after = store.getState().chat
    const think = after.messages.findIndex(r => r.role === 'thinking')
    const anchor = after.messages.findIndex(r => r.content === 'OLD ANSWER')
    expect(think).toBeGreaterThanOrEqual(0)
    expect(think).toBeLessThan(anchor)
    expect(after.thinkingOrphans[SLOT] ?? []).toHaveLength(0)
  })

  it('re-seats on an exact recorded ts even while the window is INCOMPLETE and the text duplicated', async () => {
    // An exact `ts` is strictly more evidence than the complete-window path's text plus
    // ordinal, so refusing it on ambiguity and on count GROWTH hid the block permanently.
    const dup = [THINK, A('DUP ANSWER', 2), A('NEWEST ANSWER', 3)]
    const store = await reopenStore([A('NEWEST ANSWER', 3)], true, dup)
    expect(store.getState().chat.thinkingOrphans[SLOT] ?? []).toHaveLength(1)
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(
      page([A('DUP ANSWER', 1), A('DUP ANSWER', 2)], true, 100) as never)
    await store.dispatch(loadOlderMessages())
    const after = store.getState().chat
    const think = after.messages.findIndex(r => r.role === 'thinking')
    expect(think).toBeGreaterThanOrEqual(0)
    // Seated above its OWN duplicate (ts 02), never the older text twin (ts 01).
    expect(after.messages[think + 1]?.ts).toBe('2026-01-01T00:00:02Z')
    expect(after.thinkingOrphans[SLOT] ?? []).toHaveLength(0)
  })

  it('re-seats parked reasoning on a REFRESH, not only on a slot switch', async () => {
    // refreshSlot fires on reconnect and at end-of-turn and rebuilds `messages`, so
    // without re-seating here the block stays parked and invisible for the session.
    const store = await reopenStore([A('NEWEST ANSWER', 2)], true)
    expect(store.getState().chat.thinkingOrphans[SLOT]).toHaveLength(1)
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page([A('OLD ANSWER', 1), A('NEWEST ANSWER', 2)], false, 0) as never)
    await store.dispatch(refreshSlot(SLOT))
    const after = store.getState().chat
    const think = after.messages.findIndex(r => r.role === 'thinking')
    const anchor = after.messages.findIndex(r => r.content === 'OLD ANSWER')
    expect(think).toBeGreaterThanOrEqual(0)
    expect(think).toBeLessThan(anchor)
    expect(after.thinkingOrphans[SLOT] ?? []).toHaveLength(0)
  })

  it('leaves it parked, and NOT below the newest answer, when a refresh still lacks the anchor', async () => {
    // Opposite direction: re-seating must not degrade into a tail append on refresh.
    const store = await reopenStore([A('NEWEST ANSWER', 2)], true)
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page([A('NEWEST ANSWER', 2)], false, 0) as never)
    await store.dispatch(refreshSlot(SLOT))
    const after = store.getState().chat
    expect(after.thinkingOrphans[SLOT] ?? []).toHaveLength(1)
    expect(after.messages.map(r => r.role)).not.toContain('thinking')
  })

  it('refuses an AMBIGUOUS anchor rather than attaching reasoning to the wrong turn', async () => {
    // A bounded page can omit the real anchor while including a later turn that repeats
    // its text; matching on content alone then seats the reasoning under the wrong answer.
    const dup = [THINK, A('DUP ANSWER', 1), A('MIDDLE', 2), A('DUP ANSWER', 3)]
    const store = await reopenStore([A('MIDDLE', 2), A('DUP ANSWER', 3)], true, dup)
    const after = store.getState().chat
    expect(after.messages.map(r => r.role)).not.toContain('thinking')
    expect((after.thinkingOrphans[SLOT] ?? []).map(o => o.anchor)).toEqual([{ text: 'DUP ANSWER', ts: '2026-01-01T00:00:01Z' }])
  })

  it('still seats a duplicated anchor when the window is COMPLETE', async () => {
    // Opposite direction: with every row present the first match IS the real anchor, so
    // refusing there would strand reasoning that could be placed correctly.
    const dup = [THINK, A('DUP ANSWER', 1), A('MIDDLE', 2), A('DUP ANSWER', 3)]
    const m = await reopen([A('DUP ANSWER', 1), A('MIDDLE', 2), A('DUP ANSWER', 3)], false, dup)
    const think = m.findIndex(r => r.role === 'thinking')
    expect(think).toBe(0)
    expect(m[think + 1].content).toBe('DUP ANSWER')
  })

  it('defers a TEXT anchor on a partial page-back, even when it is UNIQUE in the window', async () => {
    // The genuine older anchor is still off-window, so the repeated text occurs exactly
    // ONCE here -- a count-based check calls that unambiguous and seats the wrong turn.
    const dup = [THINK, A('DUP ANSWER', 1), A('MIDDLE', 2), A('DUP ANSWER', 3)]
    const store = await reopenStore([A('MIDDLE', 2), A('DUP ANSWER', 3)], true, dup)
    expect(store.getState().chat.thinkingOrphans[SLOT] ?? []).toHaveLength(1)
    // A page-back that prepends history WITHOUT reaching DUP ANSWER(1), and leaves
    // more history behind it -- so the anchor is still unresolvable.
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page([A('EARLIER', 0)], true, 100) as never)
    await store.dispatch(loadOlderMessages())
    const after = store.getState().chat
    expect((after.thinkingOrphans[SLOT] ?? []).map(o => o.anchor)).toEqual([{ text: 'DUP ANSWER', ts: '2026-01-01T00:00:01Z' }])
    // Assert the placement absence explicitly: the pre-fix defect SEATS the block
    // here rather than throwing, so "nothing threw" would pass either way.
    expect(after.messages.map(r => r.role)).not.toContain('thinking')
  })

  it('still re-seats a TOOL-ID anchor while more history remains', async () => {
    // Tool ids are 1:1 with bursts (#4578) so they cannot address the wrong turn; this
    // fails if the deferral is widened from text anchors to all anchors.
    const TOOL: Row & { meta: { tool_call_id: string } } = {
      role: 'tool', content: 'ran a tool', cls: 'msg msg-tool',
      ts: '2026-01-01T00:00:01Z', meta: { tool_call_id: 'tc-1' },
    }
    const store = await reopenStore([A('NEWEST ANSWER', 2)], true, [THINK, TOOL, A('NEWEST ANSWER', 2)])
    expect((store.getState().chat.thinkingOrphans[SLOT] ?? []).map(o => o.anchor)).toEqual([{ tool: 'tc-1' }])
    // hasMore stays TRUE: an unambiguous id does not need a complete window.
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page([TOOL], true, 100) as never)
    await store.dispatch(loadOlderMessages())
    const after = store.getState().chat
    expect(after.thinkingOrphans[SLOT] ?? []).toHaveLength(0)
    const think = after.messages.findIndex(r => r.role === 'thinking')
    const anchor = after.messages.findIndex(r => r.role === 'tool')
    expect(think).toBeGreaterThanOrEqual(0)
    expect(think).toBeLessThan(anchor)
  })

  it('seats a UNIQUE text anchor on its exact ts while the window is incomplete', async () => {
    // Frequency still cannot license this -- the real anchor could be an off-window twin.
    // The recorded server `ts` can, and refusing it is what hid the block for good.
    const chat = (await reopenStore([A('OLD ANSWER', 1), A('NEWEST ANSWER', 2)], true)).getState().chat
    const think = chat.messages.findIndex(r => r.role === 'thinking')
    expect(think).toBeGreaterThanOrEqual(0)
    expect(chat.messages[think + 1]?.ts).toBe('2026-01-01T00:00:01Z')
    expect(chat.thinkingOrphans[SLOT] ?? []).toHaveLength(0)
  })

  it('does NOT hand a deleted slot\u2019s reasoning to a recreated slot of the same name', async () => {
    // The re-seat matches on answer TEXT, never slot identity, and a deterministic
    // name is reusable -- so reasoning outliving its slot lands in another chat.
    const store = await reopenStore([A('NEWEST ANSWER', 2)], true)
    expect(store.getState().chat.thinkingOrphans[SLOT]).toHaveLength(1)

    store.dispatch({ type: deleteSlot.fulfilled.type, payload: SLOT })
    expect(store.getState().chat.thinkingOrphans[SLOT] ?? []).toHaveLength(0)

    // Same name recreated, and its history happens to contain the anchor text.
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page([A('OLD ANSWER', 1), A('NEWEST ANSWER', 2)], false, 0) as never)
    await store.dispatch(switchSlot(SLOT))
    const after = store.getState().chat
    expect(after.messages.map(r => r.role)).not.toContain('thinking')
    expect(after.thinkingOrphans[SLOT] ?? []).toHaveLength(0)
  })

  it('keeps a PEER\u2019s parked reasoning when the fallback switch to it FAILS', async () => {
    // deleteSlot navigates to a peer and switchSlot.pending makes that peer active
    // before its fetch can reject, so a clear here lands on an innocent bystander.
    const store = await reopenStore([A('NEWEST ANSWER', 2)], true)
    expect(store.getState().chat.thinkingOrphans[SLOT]).toHaveLength(1)

    // Exactly what deleteSlot's fallback does when the peer's history fetch rejects.
    vi.spyOn(api, 'chatSlotDetail').mockRejectedValue(new Error('network'))
    await store.dispatch(switchSlot(SLOT)).unwrap().catch(() => store.dispatch({ type: 'chat/clearSlotState' }))
    expect(store.getState().chat.activeSlot).toBe(SLOT)
    expect(store.getState().chat.thinkingOrphans[SLOT] ?? []).toHaveLength(1)

    // The surviving copy is still USABLE, not merely present: a reopen re-seats it.
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page([A('OLD ANSWER', 1), A('NEWEST ANSWER', 2)], false, 0) as never)
    await store.dispatch(switchSlot(SLOT))
    const after = store.getState().chat
    expect(after.messages.map(r => r.role)).toContain('thinking')
    expect(after.thinkingOrphans[SLOT] ?? []).toHaveLength(0)
  })

  it('re-seats a parked block under ITS OWN duplicate, neither the first nor never', async () => {
    // Both wrong answers are silent: the first duplicate is the wrong turn, and refusing
    // every duplicate hides the reasoning for good. The parked occurrence decides.
    const dupCache = [A('DUP ANSWER', 0), THINK, A('DUP ANSWER', 1), A('NEWEST ANSWER', 2)]
    const store = await reopenStore([A('NEWEST ANSWER', 2)], true, dupCache)
    expect((store.getState().chat.thinkingOrphans[SLOT] ?? []).map(o => o.anchor)).toEqual([{ text: 'DUP ANSWER', ts: '2026-01-01T00:00:01Z' }])

    // A full refresh loads BOTH duplicates, so windowComplete is true here.
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page([A('DUP ANSWER', 0), A('DUP ANSWER', 1), A('NEWEST ANSWER', 2)], false, 0) as never)
    await store.dispatch(refreshSlot(SLOT))
    const after = store.getState().chat
    // Naming the rows makes a regression read as a position, not as a boolean.
    expect(after.messages.map(r => r.role === 'thinking' ? 'THINKING' : r.content))
      .toEqual(['DUP ANSWER', 'THINKING', 'DUP ANSWER', 'NEWEST ANSWER'])
    // Seated, so nothing is left waiting for an anchor that is already loaded.
    expect(after.thinkingOrphans[SLOT] ?? []).toHaveLength(0)
  })

  it('does NOT attach to a lone SURVIVING duplicate that is not its own turn', async () => {
    // Recorded as the SECOND of two; a rewind drops that anchor and leaves the FIRST, so the
    // count falls to one. Skipping validation there seats reasoning under an unrelated turn.
    const dupCache = [A('DUP ANSWER', 0), THINK, A('DUP ANSWER', 1), A('NEWEST ANSWER', 2)]
    const store = await reopenStore([A('NEWEST ANSWER', 2)], true, dupCache)
    expect(store.getState().chat.thinkingOrphans[SLOT] ?? []).toHaveLength(1)

    // Complete window, but its own anchor is gone -- exactly ONE 'DUP ANSWER' remains.
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page([A('DUP ANSWER', 0), A('OTHER', 3)], false, 0) as never)
    await store.dispatch(refreshSlot(SLOT))
    const after = store.getState().chat
    expect(after.messages.map(r => r.role === 'thinking' ? 'THINKING' : r.content))
      .toEqual(['DUP ANSWER', 'OTHER'])
    // Refused, not discarded.
    expect(after.thinkingOrphans[SLOT] ?? []).toHaveLength(1)
  })

  it('DOES attach when the surviving duplicate IS its own turn', async () => {
    // Recorded as the FIRST of two and the rewind dropped the LATER one, so the row that
    // survives is its genuine anchor. Refusing on the count alone would strand it for good.
    const dupCache = [THINK, A('DUP ANSWER', 0), A('MIDDLE', 1), A('DUP ANSWER', 2)]
    const store = await reopenStore([A('MIDDLE', 1), A('DUP ANSWER', 2)], true, dupCache)
    expect(store.getState().chat.thinkingOrphans[SLOT] ?? []).toHaveLength(1)

    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page([A('DUP ANSWER', 0), A('MIDDLE', 1)], false, 0) as never)
    await store.dispatch(refreshSlot(SLOT))
    const after = store.getState().chat
    expect(after.messages.map(r => r.role === 'thinking' ? 'THINKING' : r.content))
      .toEqual(['THINKING', 'DUP ANSWER', 'MIDDLE'])
    expect(after.thinkingOrphans[SLOT] ?? []).toHaveLength(0)
  })

  it('seats a grown-count block on its exact ts, in position and not at the tail', async () => {
    // A third occurrence paging in shifts every index, so the ordinal can never match again
    // -- but the recorded `ts` still names the row, so refusing only hid the block.
    const dupCache = [A('DUP ANSWER', 0), THINK, A('DUP ANSWER', 1), A('NEWEST ANSWER', 2)]
    const store = await reopenStore([A('NEWEST ANSWER', 2)], true, dupCache)
    expect(store.getState().chat.thinkingOrphans[SLOT] ?? []).toHaveLength(1)

    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page(
      [A('DUP ANSWER', 3), A('DUP ANSWER', 0), A('DUP ANSWER', 1), A('NEWEST ANSWER', 2)], false, 0) as never)
    await store.dispatch(refreshSlot(SLOT))
    const after = store.getState().chat
    // Above its OWN duplicate, with nothing appended past the newest reply.
    expect(after.messages.map(r => r.role === 'thinking' ? 'THINKING' : r.content))
      .toEqual(['DUP ANSWER', 'DUP ANSWER', 'THINKING', 'DUP ANSWER', 'NEWEST ANSWER'])
    const think = after.messages.findIndex(r => r.role === 'thinking')
    expect(after.messages[think + 1]?.ts).toBe('2026-01-01T00:00:01Z')
    expect(after.thinkingOrphans[SLOT] ?? []).toHaveLength(0)
  })

  it('still refuses on a grown count when NO row carries the recorded ts', async () => {
    // The occurrence guard's own case, kept live now that an exact ts bypasses it: every
    // loaded twin has a different ts, so only the ordinal is left and growth voids it.
    const dupCache = [A('DUP ANSWER', 0), THINK, A('DUP ANSWER', 1), A('NEWEST ANSWER', 2)]
    const store = await reopenStore([A('NEWEST ANSWER', 2)], true, dupCache)
    expect(store.getState().chat.thinkingOrphans[SLOT] ?? []).toHaveLength(1)

    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page(
      [A('DUP ANSWER', 3), A('DUP ANSWER', 4), A('DUP ANSWER', 5), A('NEWEST ANSWER', 2)], false, 0) as never)
    await store.dispatch(refreshSlot(SLOT))
    const after = store.getState().chat
    expect(after.messages.map(r => r.role)).not.toContain('thinking')
    // Refused, not discarded.
    expect(after.thinkingOrphans[SLOT] ?? []).toHaveLength(1)
  })

  it('seats a preserved block under ITS OWN duplicate, not the first one', async () => {
    // The block still sits in the cached list here, so its own turn IS knowable --
    // one earlier row repeats the text, so the second occurrence is the anchor.
    const dupCache = [A('DUP ANSWER', 0), THINK, A('DUP ANSWER', 1), A('NEWEST ANSWER', 2)]
    const m = await reopen([A('DUP ANSWER', 0), A('DUP ANSWER', 1), A('NEWEST ANSWER', 2)], false, dupCache)
    expect(m.map(r => r.role === 'thinking' ? 'THINKING' : r.content))
      .toEqual(['DUP ANSWER', 'THINKING', 'DUP ANSWER', 'NEWEST ANSWER'])
  })

  it('drops parked reasoning on /clear, so a later matching answer cannot resurrect it', async () => {
    // `/clear` deletes the transcript; parked reasoning is client-only state that the
    // delete has to reach, or a refresh carrying the anchor re-seats deleted content.
    const store = await reopenStore([A('NEWEST ANSWER', 2)], true)
    expect(store.getState().chat.thinkingOrphans[SLOT]).toHaveLength(1)
    store.dispatch(clearMessages())
    expect(store.getState().chat.thinkingOrphans[SLOT] ?? []).toHaveLength(0)
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page([A('OLD ANSWER', 1), A('NEWEST ANSWER', 2)], false, 0) as never)
    await store.dispatch(refreshSlot(SLOT))
    expect(store.getState().chat.messages.map(r => r.role)).not.toContain('thinking')
  })

  /** An assistant row carrying `meta.mid`, the server-minted row identity. */
  const AM = (content: string, i: number, mid: string): Row => ({ ...A(content, i), meta: { mid } })

  it('does NOT attach parked reasoning to a REGENERATED answer of identical text', async () => {
    // A regenerate supersedes the anchoring turn, so one row carries that text at the
    // same ordinal: the frequency guard reads "unambiguous" about a different turn.
    const cached = [THINK, AM('DRAFT ANSWER', 1, 'm-old'), A('NEWEST ANSWER', 2)]
    const store = await reopenStore([A('NEWEST ANSWER', 2)], true, cached)
    expect(store.getState().chat.thinkingOrphans[SLOT] ?? []).toHaveLength(1)

    // Complete window: the superseded turn is gone, the regenerated one carries a NEW mid.
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page([AM('DRAFT ANSWER', 1, 'm-new'), A('LATER', 3)], false, 0) as never)
    await store.dispatch(refreshSlot(SLOT))
    const after = store.getState().chat
    expect(after.messages.map(r => r.role === 'thinking' ? 'THINKING' : r.content))
      .toEqual(['DRAFT ANSWER', 'LATER'])
    // Refused, not discarded -- the real turn may still page in.
    expect(after.thinkingOrphans[SLOT] ?? []).toHaveLength(1)
  })

  it('still attaches a parked block whose anchor carried NO mid', async () => {
    // A live streaming turn is locally minted and has no `meta.mid`, so requiring one
    // would drop exactly the reasoning this path exists to preserve.
    const store = await reopenStore([A('NEWEST ANSWER', 2)], true)
    expect(store.getState().chat.thinkingOrphans[SLOT]).toHaveLength(1)
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page([A('OLD ANSWER', 1), A('NEWEST ANSWER', 2)], false, 0) as never)
    await store.dispatch(refreshSlot(SLOT))
    const after = store.getState().chat
    const think = after.messages.findIndex(r => r.role === 'thinking')
    const anchor = after.messages.findIndex(r => r.content === 'OLD ANSWER')
    expect(think).toBeGreaterThanOrEqual(0)
    expect(think).toBeLessThan(anchor)
    expect(after.thinkingOrphans[SLOT] ?? []).toHaveLength(0)
  })

  it('re-seats on an EXACT recorded id even while the window is incomplete', async () => {
    // The ambiguity guards exist because TEXT cannot name a turn; an exact row id can,
    // so refusing it hides reasoning whose own anchor row is already loaded.
    const dup = [THINK, AM('DUP ANSWER', 1, 'm-1'), A('MIDDLE', 2), AM('DUP ANSWER', 3, 'm-3')]
    const store = await reopenStore([A('MIDDLE', 2), AM('DUP ANSWER', 3, 'm-3')], true, dup)
    expect((store.getState().chat.thinkingOrphans[SLOT] ?? []).map(o => o.anchor?.mid)).toEqual(['m-1'])

    // Page-back loads the anchor row itself, but hasMore stays TRUE so the window is
    // still incomplete -- the state in which the completeness guard refuses.
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page([AM('DUP ANSWER', 1, 'm-1')], true, 100) as never)
    await store.dispatch(loadOlderMessages())
    const after = store.getState().chat
    const think = after.messages.findIndex(r => r.role === 'thinking')
    expect(think).toBe(0)
    expect(after.messages[1]?.meta?.mid).toBe('m-1')
    expect(after.thinkingOrphans[SLOT] ?? []).toHaveLength(0)
  })

  it('seats a STILL-CACHED block on an exact recorded id, without a complete window', async () => {
    // The re-seat matcher already bypasses these guards on an exact id; the merge that runs
    // while the block is still cached did not, so it parked an anchor it could already name.
    const dupCache = [A('DUP ANSWER', 0), THINK, AM('DUP ANSWER', 1, 'm-1'), A('NEWEST ANSWER', 2)]
    // Bounded page: the earlier duplicate is off-window, so the recorded ordinal (1) does not
    // match this occurrence (0) -- the completeness AND ordinal terms both reject beforehand.
    const store = await reopenStore([AM('DUP ANSWER', 1, 'm-1'), A('NEWEST ANSWER', 2)], true, dupCache)
    const after = store.getState().chat
    // The off-window duplicate sits ABOVE the page's first row, so switchSlot's
    // older-head cut keeps it as scrollback -- it precedes the block here.
    expect(after.messages.map(r => r.role === 'thinking' ? 'THINKING' : r.content))
      .toEqual(['DUP ANSWER', 'THINKING', 'DUP ANSWER', 'NEWEST ANSWER'])
    // This test's own subject: the block seats immediately before its m-1 anchor.
    const think = after.messages.findIndex(r => r.role === 'thinking')
    expect(after.messages[think + 1]?.meta?.mid).toBe('m-1')
    // Seated in place, so nothing is left waiting for a row that is already loaded.
    expect(after.thinkingOrphans[SLOT] ?? []).toHaveLength(0)
  })

  it('falls back to the recorded ts when the cached anchor carried NO mid', async () => {
    // The id removed and nothing else changed: text alone still cannot tell this loaded
    // duplicate from the off-window twin, but the twin's ts differs, so ts settles it.
    const dupCache = [A('DUP ANSWER', 0), THINK, A('DUP ANSWER', 1), A('NEWEST ANSWER', 2)]
    const store = await reopenStore([A('DUP ANSWER', 1), A('NEWEST ANSWER', 2)], true, dupCache)
    const after = store.getState().chat
    const think = after.messages.findIndex(r => r.role === 'thinking')
    expect(think).toBeGreaterThanOrEqual(0)
    expect(after.messages[think + 1]?.ts).toBe('2026-01-01T00:00:01Z')
    expect(after.thinkingOrphans[SLOT] ?? []).toHaveLength(0)
  })

  it('survives a state that predates the parked-reasoning field', async () => {
    // A store rehydrated from a build without `thinkingOrphans` would otherwise
    // throw inside switchSlot, taking chat switching down rather than one feature.
    const legacy = { ...createTestStore().getState().chat } as Record<string, unknown>
    delete legacy.thinkingOrphans
    const store = createTestStore({ chat: legacy } as never)
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page([A('NEWEST ANSWER', 2)], true, 240) as never)
    await store.dispatch(switchSlot(SLOT))
    expect(store.getState().chat.messages.map(r => r.content)).toContain('NEWEST ANSWER')
  })
})

/* switchSlot fetches a BOUNDED page while `pending` restores the slot's cached
 * transcript, so assigning that page wholesale collapsed a window the reader had
 * paged in -- the discard `warmSlotCache` already guards with an older-head cut.
 *
 * The vacuous assertion here is "messages is non-empty", which passes either way.
 * These assert the OLDER HEAD specifically: the rows above the page's first row.
 */
describe('switching back keeps a paged-in window above the bounded page', () => {
  const SLOT2 = 'slot-window'
  /** A server row WITH identity. The cut is `meta.mid`-only, so a row without one
   *  is declined rather than guessed at -- pinned by the second control below. */
  const M = (mid: string, i: number, extra?: Record<string, unknown>): Row => ({
    role: 'assistant', content: `row-${mid}`, cls: 'msg msg-a',
    ts: `2026-01-01T00:01:${String(i).padStart(2, '0')}Z`, meta: { mid, ...extra },
  })
  /** Rows the reader paged in (above the page) plus the page's own rows. */
  const HEAD = [M('m8', 8), M('m9', 9)]
  const PAGE = [M('m10', 10), M('m11', 11), M('m12', 12)]

  async function switchBack(cached: Row[], servedPage: Row[]) {
    const base = createTestStore().getState().chat
    const store = createTestStore({ chat: { ...base, slotMessages: { [SLOT2]: cached } } } as never)
    // next_before=10: the page starts at row 10, so the two kept rows sit at 8-9.
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page(servedPage, true, 10) as never)
    await store.dispatch(switchSlot(SLOT2))
    return store.getState().chat
  }

  it('keeps the older head the reader paged in', async () => {
    const chat = await switchBack([...HEAD, ...PAGE], PAGE)
    const mids = chat.messages.map(r => r.meta?.mid)
    // The whole point: rows above the page survive rather than needing a re-page.
    expect(mids).toEqual(['m8', 'm9', 'm10', 'm11', 'm12'])
  })

  it('shifts the paging cursor past the head it kept', async () => {
    const chat = await switchBack([...HEAD, ...PAGE], PAGE)
    // The cursor is a row OFFSET. Left at the page's own next_before (10) the next
    // "load earlier" re-fetches rows 8-9 and dedupes them away -- a dead click.
    expect(chat.slotOldestIndex).toBe(8)
  })

  it('excludes a client-only permission card from the kept head\u2019s server-row count', async () => {
    // `permission` is in the backend's `_TRANSIENT_ROLES` and `_build_message_entry_uncached`
    // returns None for it, so it occupies NO server offset and must not shift the cursor.
    const PERM: Row = {
      role: 'permission', content: 'Allow this tool?', cls: 'msg msg-permission',
      ts: '2026-01-01T00:01:09Z', meta: { approval_id: 'a1', resolved: 'accepted' },
    }
    const chat = await switchBack([M('m8', 8), PERM, M('m9', 9), ...PAGE], PAGE)
    // Two SERVER rows kept (m8, m9), so 10 - 2. Counting the card gives 7 and the
    // next "load earlier" fetches before row 7, skipping genuine row 7 entirely.
    expect(chat.slotOldestIndex).toBe(8)
  })

  it('still counts error and mcp_oauth rows, which the server DOES persist', async () => {
    // NEGATIVE CONTROL for the exclusion above: neither role appears in
    // `_TRANSIENT_ROLES`, so both are written to history and hold a real offset.
    const ERR: Row = { role: 'error', content: 'boom', cls: 'msg msg-error', ts: '2026-01-01T00:01:08Z' }
    const OAUTH: Row = { role: 'mcp_oauth', content: 'authorize', cls: 'msg msg-oauth', ts: '2026-01-01T00:01:09Z' }
    const chat = await switchBack([M('m8', 8), ERR, OAUTH, M('m9', 9), ...PAGE], PAGE)
    // Four kept rows all persist, so 10 - 4. Widening the permission exclusion to
    // these two would read 8 here and strand two rows the reader already has.
    expect(chat.slotOldestIndex).toBe(6)
  })

  it('declines to keep a head whose rows carry no identity', async () => {
    // OPPOSITE direction: no mid means decline, so an unidentifiable head must
    // not be PREPENDED. Head-only: the trailing-reply reattach may still append.
    const noMid = rows(4, 'legacy')
    const chat = await switchBack(noMid, PAGE)
    expect(chat.messages.slice(0, 3).map(r => r.meta?.mid)).toEqual(['m10', 'm11', 'm12'])
    expect(chat.slotOldestIndex).toBe(10)
  })

  it('keeps nothing when the cache holds only the page', async () => {
    const chat = await switchBack([...PAGE], PAGE)
    expect(chat.messages.map(r => r.meta?.mid)).toEqual(['m10', 'm11', 'm12'])
    expect(chat.slotOldestIndex).toBe(10)
  })
})

/* Shifting the cursor for a kept head has two boundaries a `Math.max(0, ...)`
 * clamp conflates, and they fail in OPPOSITE directions.
 *
 * The vacuous assertions here are `slotOldestIndex === 0` (true under both the
 * defect and a wrong fix) and anything about the PARTIAL case (already green
 * before the fix). These assert the PAIR: the state must never advertise more
 * history at an offset `loadOlderMessages` refuses.
 */
describe('the paging cursor a kept head installs', () => {
  const SLOT3 = 'slot-cursor'
  const M = (mid: string, i: number): Row => ({
    role: 'assistant', content: `row-${mid}`, cls: 'msg msg-a',
    ts: `2026-01-01T00:02:${String(i).padStart(2, '0')}Z`, meta: { mid },
  })
  const PAGE = [M('p0', 10), M('p1', 11), M('p2', 12)]

  /** `nextBefore` is the offset the page starts at, so rows [0, nextBefore) are
   *  the older-than-page ones and a head of exactly that many holds them all. */
  async function reopen(head: Row[], nextBefore: number) {
    const base = createTestStore().getState().chat
    const store = createTestStore({ chat: { ...base, slotMessages: { [SLOT3]: [...head, ...PAGE] } } } as never)
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page(PAGE, true, nextBefore) as never)
    await store.dispatch(switchSlot(SLOT3))
    return store.getState().chat
  }

  it('does not advertise more history at an offset paging refuses', async () => {
    // Fully paged: the 2-row head covers [0, 2), so nothing older remains.
    const chat = await reopen([M('h0', 8), M('h1', 9)], 2)
    // The pair, not either half: loadOlderMessages returns null at <= 0, so a true
    // flag here renders an affordance that can never load -- permanently.
    expect({ hasMore: chat.slotHasMore, cursor: chat.slotOldestIndex })
      .toEqual({ hasMore: false, cursor: 0 })
  })

  it('keeps paging live when older history is genuinely unfetched', async () => {
    // OPPOSITE direction: a head far smaller than the offset leaves real history
    // behind it, so flipping the flag would STRAND it.
    const chat = await reopen([M('h0', 8), M('h1', 9)], 40)
    expect(chat.slotHasMore).toBe(true)
    expect(chat.slotOldestIndex).toBeGreaterThan(0)
    expect(chat.slotOldestIndex).toBe(38)
  })

  it('declines to claim completeness when the counts disagree', async () => {
    // Head holds MORE server rows than the offset says exist above the page, so
    // completeness is unproven -- fall back rather than strand the remainder.
    const chat = await reopen([M('h0', 6), M('h1', 7), M('h2', 8)], 2)
    expect(chat.slotHasMore).toBe(true)
    expect(chat.slotOldestIndex).toBe(2)
  })
})

/* The retained head decides TWO things: the paging cursor and whether the loaded
 * window is complete. Handing the reasoning machinery the FETCH's completeness
 * while the cursor carries the WINDOW's parks a text-anchored block at the same
 * moment paging is disabled -- so no later page can ever seat it.
 *
 * Vacuous here: asserting the parked list drains, or `slotOldestIndex === 0` --
 * both hold under the defect and under a wrong fix.
 */
describe('reasoning seating uses the retained window, not the fetched page', () => {
  const SLOT4 = 'slot-window-complete'
  /** No mid: its anchor can only be TEXT, which is what the guard distrusts. */
  const A = (content: string, i: number): Row => ({
    role: 'assistant', content, cls: 'msg msg-a', ts: `2026-01-01T00:03:0${i}Z`,
  })
  const AM = (content: string, i: number, mid: string): Row => ({
    role: 'assistant', content, cls: 'msg msg-a', ts: `2026-01-01T00:03:0${i}Z`, meta: { mid },
  })
  const THINK: Row = { role: 'thinking', content: 'kept reasoning', cls: 'msg msg-think', ts: '2026-01-01T00:03:00Z' }
  const NEWEST = AM('NEWEST ANSWER', 2, 'p0')
  /** THINK anchors to the row after it -- a head row, retained by the cut. */
  const CACHED = [THINK, A('HEAD ANSWER', 1), NEWEST]

  async function reopen(nextBefore: number) {
    const base = createTestStore().getState().chat
    const store = createTestStore({ chat: { ...base, slotMessages: { [SLOT4]: CACHED } } } as never)
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page([NEWEST], true, nextBefore) as never)
    await store.dispatch(switchSlot(SLOT4))
    return store.getState().chat
  }

  it('seats reasoning when the retained head completes the window', async () => {
    // nextBefore=1 and a 1-row head: the head covers [0,1), so the cursor saturates
    // and paging is disabled -- parking here would be permanent.
    const chat = await reopen(1)
    expect(chat.slotHasMore).toBe(false)
    const think = chat.messages.findIndex(r => r.role === 'thinking')
    expect(think).toBeGreaterThanOrEqual(0)
    expect(chat.messages[think + 1]?.content).toBe('HEAD ANSWER')
    expect(chat.thinkingOrphans?.[SLOT4] ?? []).toHaveLength(0)
  })

  it('seats on the retained head\u2019s exact ts while older history still remains', async () => {
    // Paging stays live here, which is this block's subject -- but the anchor row itself is
    // in the retained head, so its recorded ts places it and parking would only hide it.
    const chat = await reopen(40)
    expect(chat.slotHasMore).toBe(true)
    const think = chat.messages.findIndex(r => r.role === 'thinking')
    expect(think).toBeGreaterThanOrEqual(0)
    expect(chat.messages[think + 1]?.content).toBe('HEAD ANSWER')
    expect(chat.messages[think + 1]?.ts).toBe('2026-01-01T00:03:01Z')
    expect(chat.thinkingOrphans?.[SLOT4] ?? []).toHaveLength(0)
  })
})
