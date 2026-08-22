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
 * refreshSlot is pinned unbounded too: it REPLACES a transcript it did not page,
 * so bounding it would drop rows the reader already had. The background warm is
 * pinned at PANE_HYDRATE_LIMIT instead of unbounded, because #3240 made that
 * path recoverable -- a bounded pane carries a has-older marker and an
 * open-session row -- so what is pinned there is that THIS file's bound does
 * not leak into it.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { createTestStore } from './helpers'
import { switchSlot, refreshSlot, warmSlotCache, loadOlderMessages, clearMessages, deleteSlot, PANE_HYDRATE_LIMIT } from '../store/chatSlice'
import { shouldPaginateOlder } from '../pages/chat/pagination'
import { api } from '../api/client'

/** Matches OLDER_PAGE_LIMIT in chatSlice (module-private). */
const EXPECTED_BOUND = 100

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

describe('bounded initial slot fetch', () => {
  it('sends the page bound when a chat is opened', async () => {
    const detail = vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page(rows(3, 'm'), true, 240) as never)
    await open(detail)
    // A `before` cursor would make this a page-BACK, not an initial open.
    expect(detail).toHaveBeenCalledWith(SLOT, EXPECTED_BOUND)
  })

  it('leaves the automatic refresh unbounded', async () => {
    // refreshSlot fires on websocket reconnect and at end-of-turn, and replaces
    // `messages` wholesale — a bound would discard history the user paged in.
    const detail = vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page(rows(3, 'm'), true, 240) as never)
    const { store } = await open(detail)
    detail.mockClear()
    await store.dispatch(refreshSlot(SLOT))
    expect(detail).toHaveBeenCalledWith(SLOT)
  })

  it('leaves the background cache warm at the pane bound, not this one', async () => {
    // The warm path's bound is the PANE's, so a leak of THIS bound into it would
    // page a background pane against a ceiling chosen for the open transcript.
    const detail = vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page(rows(3, 'm'), true, 240) as never)
    const store = createTestStore()
    await store.dispatch(warmSlotCache('other-slot'))
    expect(detail).toHaveBeenCalledWith('other-slot', PANE_HYDRATE_LIMIT)
    expect(PANE_HYDRATE_LIMIT).not.toBe(EXPECTED_BOUND)
  })
})

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

    expect(detail).toHaveBeenLastCalledWith(SLOT, EXPECTED_BOUND, 240, expect.any(AbortSignal))
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
      .mockResolvedValue(page(rows(EXPECTED_BOUND, 'm'), true, 500) as never)
    const { store } = await open(detail)
    const chat = store.getState().chat
    expect(chat.messages).toHaveLength(EXPECTED_BOUND)
    expect(chat.messages[EXPECTED_BOUND - 1].content).toBe(`m${EXPECTED_BOUND - 1}`)
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

  it('still appends an anchored-but-absent block when the window is COMPLETE', async () => {
    // A complete window that simply lacks the anchor keeps the documented
    // never-silently-lost behaviour: the gate is on the bound, not global.
    const m = await reopen([A('NEWEST ANSWER', 2)], false)
    expect(m.map(r => r.role)).toContain('thinking')
  })

  it('PARKS the off-window block rather than discarding it', async () => {
    // Reasoning is client-only, so `messages` was its ONLY copy: skipping the row
    // without keeping it anywhere loses it for good, page-back included.
    const chat = (await reopenStore([A('NEWEST ANSWER', 2)], true)).getState().chat
    expect(chat.messages.map(r => r.role)).not.toContain('thinking')
    const parked = chat.thinkingOrphans[SLOT] ?? []
    expect(parked).toHaveLength(1)
    expect(parked[0].anchor).toEqual({ text: 'OLD ANSWER' })
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
    expect((after.thinkingOrphans[SLOT] ?? []).map(o => o.anchor)).toEqual([{ text: 'DUP ANSWER' }])
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
    expect((after.thinkingOrphans[SLOT] ?? []).map(o => o.anchor)).toEqual([{ text: 'DUP ANSWER' }])
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

  it('parks a UNIQUE text anchor too while the window is incomplete', async () => {
    // The anchor row IS loaded and its text occurs once, yet the real anchor may still
    // be the off-window turn -- so frequency cannot license the match. Tool ids can.
    const chat = (await reopenStore([A('OLD ANSWER', 1), A('NEWEST ANSWER', 2)], true)).getState().chat
    expect(chat.messages.map(r => r.role)).not.toContain('thinking')
    expect((chat.thinkingOrphans[SLOT] ?? []).map(o => o.anchor)).toEqual([{ text: 'OLD ANSWER' }])
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
    expect((store.getState().chat.thinkingOrphans[SLOT] ?? []).map(o => o.anchor)).toEqual([{ text: 'DUP ANSWER' }])

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

  it('stays parked when the list gained duplicates, rather than trusting a stale ordinal', async () => {
    // The ordinal was measured against two occurrences; a third paged in above shifts every
    // index, so the count mismatch is the signal that it can no longer name the turn.
    const dupCache = [A('DUP ANSWER', 0), THINK, A('DUP ANSWER', 1), A('NEWEST ANSWER', 2)]
    const store = await reopenStore([A('NEWEST ANSWER', 2)], true, dupCache)
    expect(store.getState().chat.thinkingOrphans[SLOT] ?? []).toHaveLength(1)

    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue(page(
      [A('DUP ANSWER', 3), A('DUP ANSWER', 0), A('DUP ANSWER', 1), A('NEWEST ANSWER', 2)], false, 0) as never)
    await store.dispatch(refreshSlot(SLOT))
    const after = store.getState().chat
    expect(after.messages.map(r => r.role === 'thinking' ? 'THINKING' : r.content))
      .toEqual(['DUP ANSWER', 'DUP ANSWER', 'DUP ANSWER', 'NEWEST ANSWER'])
    // Refused, not discarded -- the record survives for a list it can still address.
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
