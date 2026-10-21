import { describe, it, expect } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { switchSlot, refreshSlot, setActiveSlot } from '../store/chatSlice'

/** Regression test for GitHub #20833 (remainder after #20818 + #20904): a stale
 * slot-detail snapshot must not revert a newer card or steer row.
 *
 * #20818 fixed stop_event; #20904 fixed mcp_oauth. Two more durable row types
 * are upserted in place by the backend keeping the same (mid, role, ts), so the
 * same stale-snapshot merge could revert them:
 *   - card (role="card", meta.card.status, chat_cards._patch_row via
 *     slot.update_message): an irreversible finished status reverts to a LIVE
 *     one (e.g. an `applied` change card shown as `applying`/running again).
 *     `completed` (guide) and `failed` (change) are reversible and excluded.
 *   - steer (role="user", meta.steer, steer_queue._mark_steer_row_state via
 *     slot.update_message): meta.steerState reverts consumed/requeued -> written
 *     (the "pending steer" badge revives on a steer the turn already took).
 *
 * Both carry a server-minted meta.mid the snapshot preserves (meta is rebuilt
 * from the JSON `cls`, which keeps `mid` for every role), so the live settled
 * copy and a stale pre-settle page copy share (mid, role, ts). The merge seats
 * [...olderHead(live), ...page(stale)] and deduplicateByMid keeps the LAST
 * occurrence — the stale page copy — reverting the state.
 *
 * These drive the REAL switchSlot/refreshSlot merge. They FAILED on origin/main
 * (the revert happened) and PASS on this branch via preserveSettledUpsertRows,
 * the card+steer sibling of #20818's stop rule and #20904's mcp_oauth rule, run
 * (like mcp_oauth) BEFORE deduplicateByMid at the same two merge sites.
 */

function makeStore() {
  return configureStore({ reducer: { chat: chatReducer } })
}

type Row = { role: string; content: string; cls?: string; ts?: string; meta?: Record<string, unknown> }

function slotPayload(key: string, messages: Row[], hasMore: boolean) {
  return { key, messages, running: false, hasMore, total: messages.length, queue: [], stopping: false }
}

function olderServerRows(n: number): Row[] {
  const rows: Row[] = []
  for (let i = 0; i < n; i++) {
    rows.push({ role: 'assistant', content: `old reply ${i}`, cls: 'msg msg-a', ts: `2026-08-25T08:0${i}:00Z`, meta: { mid: `m-old-${i}` } })
  }
  return rows
}

const KEY = 'cs-slot'

describe('#20833 gate-3: a stale snapshot reverts a newer card row', () => {
  const CARD_TS = '2026-08-25T08:31:00Z'
  const CARD_MID = 'm-card-1'

  it('reverts an applied card to applying via switchSlot merge', () => {
    const store = makeStore()

    // LIVE view: scrollback, then a change card the backend already settled to
    // `applied` in place (same ts/mid/role). `applied` is irreversible (it only
    // ever goes to `undone`, never back to a live status), so it anchors.
    const liveHead: Row[] = [
      ...olderServerRows(6),
      {
        role: 'card',
        content: 'change proposed',
        cls: 'msg msg-card',
        ts: CARD_TS,
        meta: { mid: CARD_MID, card: { surface: 'change', id: 'c1', slot: KEY, status: 'applied', summary: 'done' } },
      },
    ]
    store.dispatch(switchSlot.pending('seed', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, liveHead, false), 'seed', KEY))

    const seeded = store.getState().chat.messages.find(m => m.role === 'card')
    expect((seeded?.meta?.card as { status?: string } | undefined)?.status).toBe('applied')

    // Stale bounded page captured BEFORE the settle: same (mid, role, ts) but
    // status still 'applying'. hasMore=true so the live head stays above it.
    const stalePage: Row[] = [
      {
        role: 'card',
        content: 'change proposed',
        cls: 'msg msg-card',
        ts: CARD_TS,
        meta: { mid: CARD_MID, card: { surface: 'change', id: 'c1', slot: KEY, status: 'applying' } },
      },
      { role: 'assistant', content: 'newer reply A', cls: 'msg msg-a', ts: '2026-08-25T08:50:00Z', meta: { mid: 'm-new-A' } },
      { role: 'assistant', content: 'newer reply B', cls: 'msg msg-a', ts: '2026-08-25T08:51:00Z', meta: { mid: 'm-new-B' } },
    ]
    store.dispatch(switchSlot.pending('req-stale', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, stalePage, true), 'req-stale', KEY))

    const cards = store.getState().chat.messages.filter(m => m.role === 'card')
    expect(cards.length).toBe(1)
    expect((cards[0].meta?.card as { status?: string } | undefined)?.status,
      'a settled card was reverted to applying by a stale snapshot').toBe('applied')
  })

  it('refreshSlot.fulfilled also reverts an applied card to applying', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(KEY))
    store.dispatch(refreshSlot.fulfilled(slotPayload(KEY, [
      ...olderServerRows(6),
      {
        role: 'card',
        content: 'change proposed',
        cls: 'msg msg-card',
        ts: CARD_TS,
        meta: { mid: CARD_MID, card: { surface: 'change', id: 'c1', slot: KEY, status: 'applied' } },
      },
    ], false), 'r-seed', KEY))

    store.dispatch(refreshSlot.fulfilled(slotPayload(KEY, [
      {
        role: 'card',
        content: 'change proposed',
        cls: 'msg msg-card',
        ts: CARD_TS,
        meta: { mid: CARD_MID, card: { surface: 'change', id: 'c1', slot: KEY, status: 'applying' } },
      },
      { role: 'assistant', content: 'newer reply', cls: 'msg msg-a', ts: '2026-08-25T08:50:00Z', meta: { mid: 'm-new-C' } },
    ], true), 'r-stale', KEY))

    const cards = store.getState().chat.messages.filter(m => m.role === 'card')
    expect(cards.length).toBe(1)
    expect((cards[0].meta?.card as { status?: string } | undefined)?.status).toBe('applied')
  })

  it('does NOT overwrite a genuine completed->offered guide replay (completed is reversible)', () => {
    // guide_runs.py replay() ("Show again") takes a completed guide back to
    // `offered`, patching the same transcript row in place. `completed` must
    // NOT anchor a carry-forward, or the replay's fresh `offered` row would be
    // reverted to a stale `completed`.
    const store = makeStore()
    store.dispatch(switchSlot.pending('seed', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, [
      ...olderServerRows(6),
      {
        role: 'card',
        content: 'guide offered',
        cls: 'msg msg-card',
        ts: CARD_TS,
        meta: { mid: CARD_MID, card: { surface: 'guide', id: 'g1', slot: KEY, status: 'completed' } },
      },
    ], false), 'seed', KEY))

    // A fresh snapshot carrying the replay: same (mid, role, ts), status now
    // `offered` again. hasMore=true keeps the live head above it.
    store.dispatch(switchSlot.pending('req-replay', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, [
      {
        role: 'card',
        content: 'guide offered',
        cls: 'msg msg-card',
        ts: CARD_TS,
        meta: { mid: CARD_MID, card: { surface: 'guide', id: 'g1', slot: KEY, status: 'offered' } },
      },
      { role: 'assistant', content: 'newer reply', cls: 'msg msg-a', ts: '2026-08-25T08:50:00Z', meta: { mid: 'm-new-G' } },
    ], true), 'req-replay', KEY))

    const cards = store.getState().chat.messages.filter(m => m.role === 'card')
    expect(cards.length).toBe(1)
    expect((cards[0].meta?.card as { status?: string } | undefined)?.status,
      'a completed->offered guide replay was wrongly reverted to completed').toBe('offered')
  })

  it('does NOT overwrite a genuine failed->pending retry (failed is retryable)', () => {
    // change_cards.py: `failed --retry--> pending`. The live view may still hold
    // the old `failed` row while a fresh snapshot already carries the retry's
    // `pending`. `failed` must NOT anchor a carry-forward, or the retry is hidden.
    const store = makeStore()
    store.dispatch(switchSlot.pending('seed', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, [
      ...olderServerRows(6),
      {
        role: 'card',
        content: 'change proposed',
        cls: 'msg msg-card',
        ts: CARD_TS,
        meta: { mid: CARD_MID, card: { surface: 'change', id: 'c1', slot: KEY, status: 'failed' } },
      },
    ], false), 'seed', KEY))

    // A fresh (NOT stale) snapshot carrying the retry: same (mid, role, ts),
    // status now `pending` again. hasMore=true keeps the live head above it.
    store.dispatch(switchSlot.pending('req-retry', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, [
      {
        role: 'card',
        content: 'change proposed',
        cls: 'msg msg-card',
        ts: CARD_TS,
        meta: { mid: CARD_MID, card: { surface: 'change', id: 'c1', slot: KEY, status: 'pending' } },
      },
      { role: 'assistant', content: 'newer reply', cls: 'msg msg-a', ts: '2026-08-25T08:50:00Z', meta: { mid: 'm-new-R' } },
    ], true), 'req-retry', KEY))

    const cards = store.getState().chat.messages.filter(m => m.role === 'card')
    expect(cards.length).toBe(1)
    expect((cards[0].meta?.card as { status?: string } | undefined)?.status,
      'a failed->pending retry was wrongly reverted to failed').toBe('pending')
  })
})

describe('#20833 gate-3: a stale snapshot reverts a newer steer row', () => {
  const STEER_TS = '2026-08-25T08:31:00Z'
  const STEER_MID = 'm-steer-1'

  it('reverts a consumed steer back to written via switchSlot merge', () => {
    const store = makeStore()

    const liveHead: Row[] = [
      ...olderServerRows(6),
      {
        role: 'user',
        content: 'also check the logs',
        cls: 'msg msg-u',
        ts: STEER_TS,
        meta: { mid: STEER_MID, steer: true, steerState: 'consumed', sendId: 's-1' },
      },
    ]
    store.dispatch(switchSlot.pending('seed', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, liveHead, false), 'seed', KEY))

    const seeded = store.getState().chat.messages.find(m => m.meta?.steer)
    expect(seeded?.meta?.steerState).toBe('consumed')

    const stalePage: Row[] = [
      {
        role: 'user',
        content: 'also check the logs',
        cls: 'msg msg-u',
        ts: STEER_TS,
        meta: { mid: STEER_MID, steer: true, steerState: 'written', sendId: 's-1' },
      },
      { role: 'assistant', content: 'newer reply A', cls: 'msg msg-a', ts: '2026-08-25T08:50:00Z', meta: { mid: 'm-new-A' } },
      { role: 'assistant', content: 'newer reply B', cls: 'msg msg-a', ts: '2026-08-25T08:51:00Z', meta: { mid: 'm-new-B' } },
    ]
    store.dispatch(switchSlot.pending('req-stale', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, stalePage, true), 'req-stale', KEY))

    const steers = store.getState().chat.messages.filter(m => m.meta?.steer)
    expect(steers.length).toBe(1)
    expect(steers[0].meta?.steerState,
      'a consumed steer was reverted to written (pending badge revived) by a stale snapshot').toBe('consumed')
  })

  it('reverts a requeued steer back to written via switchSlot merge', () => {
    const store = makeStore()
    const liveHead: Row[] = [
      ...olderServerRows(6),
      {
        role: 'user',
        content: 'earlier request',
        cls: 'msg msg-u',
        ts: STEER_TS,
        meta: { mid: STEER_MID, steer: true, steerState: 'requeued', sendId: 's-2' },
      },
    ]
    store.dispatch(switchSlot.pending('seed', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, liveHead, false), 'seed', KEY))

    const stalePage: Row[] = [
      {
        role: 'user',
        content: 'earlier request',
        cls: 'msg msg-u',
        ts: STEER_TS,
        meta: { mid: STEER_MID, steer: true, steerState: 'written', sendId: 's-2' },
      },
      { role: 'assistant', content: 'newer reply', cls: 'msg msg-a', ts: '2026-08-25T08:50:00Z', meta: { mid: 'm-new-D' } },
    ]
    store.dispatch(switchSlot.pending('req-stale', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, stalePage, true), 'req-stale', KEY))

    const steers = store.getState().chat.messages.filter(m => m.meta?.steer)
    expect(steers.length).toBe(1)
    expect(steers[0].meta?.steerState).toBe('requeued')
  })

  it('does NOT overwrite a DISTINCT row that merely reuses the same mid (ts differs)', () => {
    // GPT finding F1: two distinct steer rows can share (mid, role) but differ
    // in ts (a crafted meta.mid reused across two POSTs; `_ChatSlot.append`
    // preserves it and assigns distinct ts). `deduplicateByMid` keeps BOTH
    // (ts differs), so the helper must key on ts too — otherwise it would copy
    // the consumed row's text onto the distinct written row, hiding its text.
    const store = makeStore()
    store.dispatch(switchSlot.pending('seed', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, [
      ...olderServerRows(6),
      {
        role: 'user',
        content: 'first message',
        cls: 'msg msg-u',
        ts: '2026-08-25T08:31:00Z',
        meta: { mid: 'm-dup', steer: true, steerState: 'consumed', sendId: 's-a' },
      },
    ], false), 'seed', KEY))

    // A snapshot carrying BOTH the settled first row AND a DISTINCT second row
    // that reuses the same mid but has its own ts and its own text, still
    // `written`. The helper must leave the second row's text intact.
    store.dispatch(switchSlot.pending('req', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, [
      {
        role: 'user',
        content: 'first message',
        cls: 'msg msg-u',
        ts: '2026-08-25T08:31:00Z',
        meta: { mid: 'm-dup', steer: true, steerState: 'consumed', sendId: 's-a' },
      },
      {
        role: 'user',
        content: 'second message',
        cls: 'msg msg-u',
        ts: '2026-08-25T08:33:00Z',
        meta: { mid: 'm-dup', steer: true, steerState: 'written', sendId: 's-b' },
      },
      { role: 'assistant', content: 'newer reply', cls: 'msg msg-a', ts: '2026-08-25T08:50:00Z', meta: { mid: 'm-new-E' } },
    ], true), 'req', KEY))

    const steers = store.getState().chat.messages.filter(m => m.meta?.steer)
    // Both distinct rows survive (deduplicateByMid keeps both: ts differs) and
    // neither's text is clobbered by the other.
    expect(steers.length).toBe(2)
    const second = steers.find(s => s.ts === '2026-08-25T08:33:00Z')
    expect(second?.content, 'a distinct same-mid row had its text overwritten').toBe('second message')
    expect(second?.meta?.steerState).toBe('written')
  })
})

describe('#20833 gate-3: the helper is a no-op without a settled prior', () => {
  it('leaves a genuinely live card and a written steer untouched', () => {
    const store = makeStore()
    store.dispatch(switchSlot.pending('req-open', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, [
      {
        role: 'card',
        content: 'change proposed',
        cls: 'msg msg-card',
        ts: '2026-08-25T08:31:00Z',
        meta: { mid: 'm-card-open', card: { surface: 'change', id: 'c2', slot: KEY, status: 'active' } },
      },
      {
        role: 'user',
        content: 'steer me',
        cls: 'msg msg-u',
        ts: '2026-08-25T08:32:00Z',
        meta: { mid: 'm-steer-open', steer: true, steerState: 'written', sendId: 's-3' },
      },
    ], false), 'req-open', KEY))

    const card = store.getState().chat.messages.find(m => m.role === 'card')
    const steer = store.getState().chat.messages.find(m => m.meta?.steer)
    // No settled prior for either id -> the helper does nothing; the live
    // states stand exactly as the page sent them.
    expect((card?.meta?.card as { status?: string } | undefined)?.status).toBe('active')
    expect(steer?.meta?.steerState).toBe('written')
  })
})
