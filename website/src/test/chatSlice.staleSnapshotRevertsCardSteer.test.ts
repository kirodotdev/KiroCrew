import { describe, it, expect } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { switchSlot, refreshSlot, setActiveSlot, sseChatMessagePatchByTs, warmSlotCache } from '../store/chatSlice'

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

describe('#22795: the row revision orders a guide card whichever way its status moved', () => {
  const CARD_TS = '2026-08-25T08:31:00Z'
  const CARD_MID = 'm-guide-1'
  const guideCard = (status: string, rev?: number) => ({ surface: 'guide', id: 'g1', slot: KEY, status, ...(rev ? { rev } : {}) })
  const guideRow = (status: string, rev?: number): Row => ({
    role: 'card', content: 'guide offered', cls: 'msg msg-card', ts: CARD_TS, meta: { mid: CARD_MID, card: guideCard(status, rev) },
  })
  // The live `chat_message_update` frame `chat_cards._patch_row` broadcasts.
  const livePatch = (status: string, rev: number) =>
    sseChatMessagePatchByTs({ slot: KEY, ts: CARD_TS, mid: CARD_MID, meta: { mid: CARD_MID, card: guideCard(status, rev) } })
  const newerReply: Row = { role: 'assistant', content: 'newer reply', cls: 'msg msg-a', ts: '2026-08-25T08:50:00Z', meta: { mid: 'm-new-H' } }
  const cardStatus = (store: ReturnType<typeof makeStore>) => {
    const cards = store.getState().chat.messages.filter(m => m.role === 'card')
    expect(cards.length).toBe(1)
    return (cards[0].meta?.card as { status?: string } | undefined)?.status
  }

  it('switchSlot keeps a replayed guide offered over a snapshot taken while it was completed', () => {
    const store = makeStore()
    store.dispatch(switchSlot.pending('seed', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, [...olderServerRows(6), guideRow('completed', 1)], false), 'seed', KEY))
    store.dispatch(livePatch('offered', 2))

    store.dispatch(switchSlot.pending('req-stale', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, [guideRow('completed', 1), newerReply], true), 'req-stale', KEY))

    expect(cardStatus(store), 'a replayed guide was reverted to completed by a stale snapshot').toBe('offered')
  })

  it('refreshSlot keeps a completed guide over a snapshot taken while it was offered', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(KEY))
    store.dispatch(refreshSlot.fulfilled(slotPayload(KEY, [...olderServerRows(6), guideRow('offered')], false), 'r-seed', KEY))
    store.dispatch(livePatch('completed', 1))

    store.dispatch(refreshSlot.fulfilled(slotPayload(KEY, [guideRow('offered'), newerReply], true), 'r-stale', KEY))

    expect(cardStatus(store), 'a completed guide was reverted to offered by a stale snapshot').toBe('completed')
  })

  it('takes a newer snapshot over a local copy that missed the live patch', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(KEY))
    store.dispatch(refreshSlot.fulfilled(slotPayload(KEY, [...olderServerRows(6), guideRow('completed', 1)], false), 'r-seed', KEY))

    store.dispatch(refreshSlot.fulfilled(slotPayload(KEY, [guideRow('offered', 2), newerReply], true), 'r-fresh', KEY))

    expect(cardStatus(store), 'a newer snapshot lost to an older local copy').toBe('offered')
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

  it('keeps a taken-back steer taken back against a snapshot captured while it was written', () => {
    const store = makeStore()
    const liveHead: Row[] = [
      ...olderServerRows(6),
      {
        role: 'user',
        content: 'earlier request',
        cls: 'msg msg-u',
        ts: STEER_TS,
        meta: { mid: STEER_MID, steer: true, steerState: 'retracted', steerRetractAction: 'unsend', sendId: 's-3' },
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
        meta: { mid: STEER_MID, steer: true, steerState: 'written', steerRetract: true, sendId: 's-3' },
      },
      { role: 'assistant', content: 'newer reply', cls: 'msg msg-a', ts: '2026-08-25T08:50:00Z', meta: { mid: 'm-new-E' } },
    ]
    store.dispatch(switchSlot.pending('req-stale', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, stalePage, true), 'req-stale', KEY))

    const steers = store.getState().chat.messages.filter(m => m.meta?.steer)
    expect(steers.length).toBe(1)
    expect(steers[0].meta?.steerState, 'a stale snapshot revived the take-back controls').toBe('retracted')
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

describe('a refresh captured before a requeued steer drained', () => {
  const STEER_TS = '2026-08-25T08:31:00Z'
  const STEER_MID = 'm-steer-sup'
  const oldSteer = (steerState: string): Row => ({
    role: 'user',
    content: 'run this next',
    cls: 'msg msg-u',
    ts: STEER_TS,
    meta: { mid: STEER_MID, steer: true, steerState },
  })
  const successor: Row = {
    role: 'user', content: 'run this next', cls: 'msg msg-u', ts: '2026-08-25T08:40:00Z', meta: { mid: 'm-fresh' },
  }

  function seedSuperseded() {
    const store = makeStore()
    store.dispatch(setActiveSlot(KEY))
    store.dispatch(refreshSlot.fulfilled(slotPayload(KEY, [
      ...olderServerRows(6), oldSteer('superseded'), successor,
    ], false), 'r-seed', KEY))
    return store
  }

  for (const stale of ['written', 'requeued']) {
    it(`drops a page that still shows the steer ${stale}, keeping the successor row`, () => {
      const store = seedSuperseded()
      store.dispatch(refreshSlot.fulfilled(slotPayload(KEY, [
        ...olderServerRows(6), oldSteer(stale),
      ], false), 'r-stale', KEY))

      const rows = store.getState().chat.messages.filter(m => m.role === 'user')
      expect(rows.map(m => m.meta?.mid)).toEqual([STEER_MID, 'm-fresh'])
      expect(rows[0].meta?.steerState).toBe('superseded')
    })
  }

  it('applies a page that agrees the steer is superseded', () => {
    const store = seedSuperseded()
    store.dispatch(refreshSlot.fulfilled(slotPayload(KEY, [
      ...olderServerRows(6), oldSteer('superseded'), successor,
      { role: 'assistant', content: 'newer reply', cls: 'msg msg-a', ts: '2026-08-25T08:41:00Z', meta: { mid: 'm-new-S' } },
    ], false), 'r-fresh', KEY))

    expect(store.getState().chat.messages.some(m => m.meta?.mid === 'm-new-S')).toBe(true)
  })

  it('merges the dropped page queue without the drained steer entry', () => {
    // The page was read after the requeue and before the drain: its queue still
    // lists the steer, and it lists a send whose `queue_push` this tab missed.
    // The tab's own newer queued row stays.
    const store = seedSuperseded()
    store.dispatch(refreshSlot.fulfilled({
      ...slotPayload(KEY, [...olderServerRows(6), oldSteer('superseded'), successor], false),
      queue: [{ queueId: 'q-mine', content: 'queued here', ts: '2026-08-25T08:42:00Z' }],
    }, 'r-seed-q', KEY))
    store.dispatch(refreshSlot.fulfilled({
      ...slotPayload(KEY, [...olderServerRows(6), oldSteer('requeued')], false),
      queue: [
        { queueId: 'q-steer', content: 'run this next', ts: '2026-08-25T08:35:00Z' },
        { queueId: 'q-other', content: 'sent from another tab', ts: '2026-08-25T08:36:00Z' },
      ],
    }, 'r-stale-q', KEY))

    const chat = store.getState().chat
    expect(chat.messages.filter(m => m.role === 'user').map(m => [m.meta?.mid, m.meta?.steerState]))
      .toEqual([[STEER_MID, 'superseded'], ['m-fresh', undefined]])
    expect(chat.messages.filter(m => m.role === 'queued').map(m => m.meta?.queueId).sort())
      .toEqual(['q-mine', 'q-other'])
  })

  it('drops the drained entry by identity when a same-text send sits ahead of it', () => {
    // The person moved a send with the same words ahead of the requeued card.
    // The live rebuild names the card it came from, so that card goes and the
    // other send keeps its card even though its text matches.
    const store = seedSuperseded()
    const landed = store.getState().chat.messages.find(m => m.meta?.mid === 'm-fresh')!
    store.dispatch(refreshSlot.fulfilled(slotPayload(KEY, [
      ...olderServerRows(6), oldSteer('superseded'),
      { ...landed, meta: { landedFromSteer: 'q-steer' } },
    ], false), 'r-seed-landed', KEY))
    store.dispatch(refreshSlot.fulfilled({
      ...slotPayload(KEY, [...olderServerRows(6), oldSteer('requeued')], false),
      queue: [
        { queueId: 'q-ord', content: 'run this next', ts: '2026-08-25T08:34:00Z' },
        { queueId: 'q-steer', content: 'run this next', ts: '2026-08-25T08:35:00Z' },
      ],
    }, 'r-stale-ord', KEY))
    expect(store.getState().chat.messages.filter(m => m.role === 'queued').map(m => m.meta?.queueId))
      .toEqual(['q-ord'])
  })

  it('keeps a same-text send on a page read before the requeue', () => {
    // A page that still shows the steer `written` predates the requeue, so its
    // queue holds no entry for the steer. A send with the same text that this
    // tab missed is a different message and must keep its card.
    const store = seedSuperseded()
    store.dispatch(refreshSlot.fulfilled({
      ...slotPayload(KEY, [...olderServerRows(6), oldSteer('written')], false),
      queue: [{ queueId: 'q-same', content: 'run this next', ts: '2026-08-25T08:36:00Z' }],
    }, 'r-stale-w', KEY))
    expect(store.getState().chat.messages.filter(m => m.role === 'queued').map(m => m.meta?.queueId))
      .toEqual(['q-same'])
  })
})

describe('a warm or switch page captured before a requeued steer drained', () => {
  const STEER_TS = '2026-08-25T08:31:00Z'
  const STEER_MID = 'm-steer-warm'
  const oldSteer = (steerState: string): Row => ({
    role: 'user', content: 'run this next', cls: 'msg msg-u', ts: STEER_TS,
    meta: steerState === 'superseded'
      ? { mid: STEER_MID, steer: true, steerState, supersededBy: 'm-fresh' }
      : { mid: STEER_MID, steer: true, steerState },
  })
  const successor: Row = {
    role: 'user', content: 'run this next', cls: 'msg msg-u', ts: '2026-08-25T08:40:00Z', meta: { mid: 'm-fresh' },
  }
  const userMids = (rows: { role: string; meta?: Record<string, unknown> }[] | undefined) =>
    (rows ?? []).filter(m => m.role === 'user').map(m => [m.meta?.mid, m.meta?.steerState])

  it('a background warm keeps the pane cache that already retired the steer', () => {
    const BG = 'bg-pane'
    const store = makeStore()
    store.dispatch(setActiveSlot(KEY))
    store.dispatch(warmSlotCache.fulfilled(slotPayload(BG, [...olderServerRows(6), oldSteer('superseded'), successor], false), 'w-seed', BG))
    store.dispatch(warmSlotCache.fulfilled(slotPayload(BG, [...olderServerRows(6), oldSteer('requeued')], false), 'w-stale', BG))
    expect(userMids(store.getState().chat.slotMessages?.[BG])).toEqual([[STEER_MID, 'superseded'], ['m-fresh', undefined]])
  })

  it('a switch keeps the view that already retired the steer', () => {
    const store = makeStore()
    store.dispatch(switchSlot.pending('s-seed', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, [...olderServerRows(6), oldSteer('superseded'), successor], false), 's-seed', KEY))
    store.dispatch(switchSlot.pending('s-stale', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, [...olderServerRows(6), oldSteer('written')], false), 's-stale', KEY))
    expect(userMids(store.getState().chat.messages)).toEqual([[STEER_MID, 'superseded'], ['m-fresh', undefined]])
  })

  it('a switch that keeps its view still installs the paging cursor', () => {
    // `pending` clears the cursor and only this fulfilment sets it again, so a
    // kept view with no cursor cannot page older until a later refresh.
    const store = makeStore()
    store.dispatch(switchSlot.pending('s-seed', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, [...olderServerRows(6), oldSteer('superseded'), successor], false), 's-seed', KEY))
    store.dispatch(switchSlot.pending('s-stale', KEY))
    expect(store.getState().chat.slotCursorKey).toBeNull()
    store.dispatch(switchSlot.fulfilled({ ...slotPayload(KEY, [...olderServerRows(6), oldSteer('requeued')], true), nextBefore: 40 }, 's-stale', KEY))
    const chat = store.getState().chat
    expect(userMids(chat.messages)).toEqual([[STEER_MID, 'superseded'], ['m-fresh', undefined]])
    expect(chat.slotCursorKey).toBe(KEY)
    expect(chat.slotHasMore).toBe(true)
    expect(chat.slotOldestIndex).toBe(40)
  })

  it('a switch whose view does not reach the page applies it and keeps the steer superseded', () => {
    // The restored view starts below the page's first row, so it is older than
    // the page: the page applies, and the old row stays retired over its
    // earlier copy. Its successor is not on the page, so the row is drawn.
    const store = makeStore()
    store.dispatch(switchSlot.pending('s-seed', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, [oldSteer('superseded'), successor], false), 's-seed', KEY))
    store.dispatch(switchSlot.pending('s-stale', KEY))
    // Read between the requeue and the drain, the page still lists the steer's
    // queue entry; the view already saw the drain pop it.
    store.dispatch(switchSlot.fulfilled({
      ...slotPayload(KEY, [...olderServerRows(6), oldSteer('requeued')], true),
      queue: [{ queueId: 'q-steer', content: 'run this next', ts: '2026-08-25T08:35:00Z' }],
    }, 's-stale', KEY))
    const chat = store.getState().chat
    expect(chat.messages.some(m => m.meta?.mid === 'm-old-0')).toBe(true)
    expect(userMids(chat.messages)[0]).toEqual([STEER_MID, 'superseded'])
    expect(chat.messages.filter(m => m.role === 'queued' && m.content === 'run this next')).toHaveLength(0)
    expect(chat.slotCursorKey).toBe(KEY)
  })

  it('a switch whose view does not reach the page keeps a queued entry the view still shows', () => {
    const store = makeStore()
    store.dispatch(switchSlot.pending('s-seed', KEY))
    store.dispatch(switchSlot.fulfilled({
      ...slotPayload(KEY, [oldSteer('superseded'), successor], false),
      queue: [{ queueId: 'q-later', content: 'and then this', ts: '2026-08-25T08:41:00Z' }],
    }, 's-seed', KEY))
    store.dispatch(switchSlot.pending('s-stale', KEY))
    store.dispatch(switchSlot.fulfilled({
      ...slotPayload(KEY, [...olderServerRows(6), oldSteer('requeued')], true),
      queue: [
        { queueId: 'q-steer', content: 'run this next', ts: '2026-08-25T08:35:00Z' },
        { queueId: 'q-later', content: 'and then this', ts: '2026-08-25T08:41:00Z' },
      ],
    }, 's-stale', KEY))
    const queued = store.getState().chat.messages.filter(m => m.role === 'queued').map(m => m.meta?.queueId)
    expect(queued).toEqual(['q-later'])
  })

  // The page may list a send whose `queue_push` this tab missed (a socket drop):
  // only the drained steer's entry is dropped, so that send can still be edited
  // or cancelled before it drains.
  const staleQueue = [
    { queueId: 'q-steer', content: 'run this next', ts: '2026-08-25T08:35:00Z' },
    { queueId: 'q-other', content: 'sent from another tab', ts: '2026-08-25T08:36:00Z' },
  ]

  it('a switch whose view does not reach the page keeps a page-only queued entry', () => {
    const store = makeStore()
    store.dispatch(switchSlot.pending('s-seed', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, [oldSteer('superseded'), successor], false), 's-seed', KEY))
    store.dispatch(switchSlot.pending('s-stale', KEY))
    store.dispatch(switchSlot.fulfilled({ ...slotPayload(KEY, [...olderServerRows(6), oldSteer('requeued')], true), queue: staleQueue }, 's-stale', KEY))
    const queued = store.getState().chat.messages.filter(m => m.role === 'queued').map(m => m.meta?.queueId)
    expect(queued).toEqual(['q-other'])
  })

  it('a switch that keeps its view merges the page queue without the drained steer entry', () => {
    const store = makeStore()
    store.dispatch(switchSlot.pending('s-seed', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, [...olderServerRows(6), oldSteer('superseded'), successor], false), 's-seed', KEY))
    store.dispatch(switchSlot.pending('s-stale', KEY))
    store.dispatch(switchSlot.fulfilled({ ...slotPayload(KEY, [...olderServerRows(6), oldSteer('requeued')], false), queue: staleQueue }, 's-stale', KEY))
    const chat = store.getState().chat
    expect(userMids(chat.messages)).toEqual([[STEER_MID, 'superseded'], ['m-fresh', undefined]])
    expect(chat.messages.filter(m => m.role === 'queued').map(m => m.meta?.queueId)).toEqual(['q-other'])
  })

  it('a background warm merges the page queue into the kept cache without the drained steer entry', () => {
    const BG = 'bg-pane-q'
    const store = makeStore()
    store.dispatch(setActiveSlot(KEY))
    store.dispatch(warmSlotCache.fulfilled(slotPayload(BG, [...olderServerRows(6), oldSteer('superseded'), successor], false), 'w-seed', BG))
    store.dispatch(warmSlotCache.fulfilled({ ...slotPayload(BG, [...olderServerRows(6), oldSteer('requeued')], false), queue: staleQueue }, 'w-stale', BG))
    const cache = store.getState().chat.slotMessages?.[BG]
    expect(userMids(cache)).toEqual([[STEER_MID, 'superseded'], ['m-fresh', undefined]])
    expect((cache ?? []).filter(m => m.role === 'queued').map(m => m.meta?.queueId)).toEqual(['q-other'])
  })
})

describe('an automation steer row (`nudge`) against a stale page', () => {
  // A work-ledger wake steered into a running turn is a `nudge` row with
  // `meta.steer`, and the row its own queued turn writes is a `nudge` row too.
  const STEER_TS = '2026-08-25T08:31:00Z'
  const STEER_MID = 'm-nudge-steer'
  const WAKE = '[work-ledger wake] item=it_1 status=question'
  const NUDGE = { cycle: 4, loop_id: 'loop-1' }
  const nudgeSteer = (steerState: string): Row => ({
    role: 'nudge', content: WAKE, cls: 'msg msg-nudge', ts: STEER_TS,
    meta: steerState === 'superseded'
      ? { mid: STEER_MID, steer: true, steerState, supersededBy: 'm-fresh', nudge: NUDGE }
      : { mid: STEER_MID, steer: true, steerState, nudge: NUDGE },
  })
  const successor: Row = {
    role: 'nudge', content: WAKE, cls: 'msg msg-nudge', ts: '2026-08-25T08:40:00Z', meta: { mid: 'm-fresh', nudge: NUDGE },
  }
  const nudgeMids = (rows: { role: string; meta?: Record<string, unknown> }[] | undefined) =>
    (rows ?? []).filter(m => m.role === 'nudge').map(m => [m.meta?.mid, m.meta?.steerState])

  it('a refresh drops a page that still shows the nudge steer requeued, and its drained queue entry', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(KEY))
    store.dispatch(refreshSlot.fulfilled(slotPayload(KEY, [
      ...olderServerRows(6), nudgeSteer('superseded'), successor,
    ], false), 'r-seed', KEY))
    store.dispatch(refreshSlot.fulfilled({
      ...slotPayload(KEY, [...olderServerRows(6), nudgeSteer('requeued')], false),
      queue: [
        { queueId: 'q-wake', content: WAKE, ts: '2026-08-25T08:35:00Z' },
        { queueId: 'q-other', content: 'sent from another tab', ts: '2026-08-25T08:36:00Z' },
      ],
    }, 'r-stale', KEY))

    const chat = store.getState().chat
    expect(nudgeMids(chat.messages)).toEqual([[STEER_MID, 'superseded'], ['m-fresh', undefined]])
    expect(chat.messages.filter(m => m.role === 'queued').map(m => m.meta?.queueId)).toEqual(['q-other'])
  })

  it('a switch keeps a consumed nudge steer over a snapshot captured while it was written', () => {
    const store = makeStore()
    store.dispatch(switchSlot.pending('seed', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, [...olderServerRows(6), nudgeSteer('consumed')], false), 'seed', KEY))
    store.dispatch(switchSlot.pending('req-stale', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, [
      nudgeSteer('written'),
      { role: 'assistant', content: 'newer reply', cls: 'msg msg-a', ts: '2026-08-25T08:50:00Z', meta: { mid: 'm-new-N' } },
    ], true), 'req-stale', KEY))

    expect(nudgeMids(store.getState().chat.messages)).toEqual([[STEER_MID, 'consumed']])
  })
})
