import { describe, it, expect } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { switchSlot, refreshSlot, setActiveSlot } from '../store/chatSlice'

/** Regression test for GitHub #20833: a stale slot-detail snapshot must not
 * revert a newer mcp_oauth banner.
 *
 * #20818 fixes the stop_event (role=system) revert with preserveSettledStopState.
 * #20833 asks whether ANY OTHER row type the backend upserts in place by meta.id
 * can be reverted by a stale snapshot through the same slot-detail merge.
 *
 * mcp_oauth is such a type: role="mcp_oauth" (durable — NOT in CLIENT_ONLY_ROLES),
 * carries a server-minted meta.mid, and the backend MUTATES it in place keeping
 * the SAME ts/mid/role:
 *   - _supersede_open_mcp_oauth_banners -> slot.update_message(ts): drops oauth_url,
 *     sets superseded, rewrites content to "↻ … no longer active".
 *   - _broadcast_expired_oauth_banners / read-time gate: sets expired, oauth_url="".
 *
 * So the LIVE view can hold the newer superseded/expired copy while a stale HTTP
 * snapshot (captured before the supersede) still carries the old
 * "🔐 requires authentication" copy WITH a live-looking oauth_url, under the same
 * (mid, role, ts). The slot-switch merge builds [...olderHead(live), ...page(stale)]
 * and deduplicateByMid keeps the LAST (positionally newest) occurrence — the stale
 * page copy — reviving a dead Authorize link the user already saw retired.
 *
 * This drives the REAL switchSlot.fulfilled merge path. It FAILED on origin/main
 * (the dead link came back) and PASSES on this branch via preserveSettledMcpOauth,
 * the mcp_oauth-scoped sibling of #20818's stop-event rule on the same two merge
 * sites. #20818's helper is stop-event-only and does not cover mcp_oauth.
 */

function makeStore() {
  return configureStore({ reducer: { chat: chatReducer } })
}

type Row = { role: string; content: string; cls?: string; ts?: string; meta?: Record<string, unknown> }

function slotPayload(key: string, messages: Row[], hasMore: boolean) {
  return { key, messages, running: false, hasMore, total: messages.length, queue: [], stopping: false }
}

const KEY = 'mcp-slot'
const BANNER_TS = '2026-08-25T08:31:00Z'
const BANNER_MID = 'm-oauth-1'

// A long enough older head so the bounded snapshot page does NOT contain the
// view's oldest row: the merge then keeps the live head above the page.
function olderServerRows(n: number): Row[] {
  const rows: Row[] = []
  for (let i = 0; i < n; i++) {
    rows.push({ role: 'assistant', content: `old reply ${i}`, cls: 'msg msg-a', ts: `2026-08-25T08:0${i}:00Z`, meta: { mid: `m-old-${i}` } })
  }
  return rows
}

describe('#20833: a stale snapshot reverts a newer mcp_oauth banner', () => {
  it('revives a retired Authorize link: live view holds the superseded banner, snapshot carries the old live-link copy', () => {
    const store = makeStore()

    // LIVE view the user currently sees: an older scrollback head, then the
    // mcp_oauth banner already SUPERSEDED (backend popped oauth_url, set
    // superseded, same ts/mid/role).
    const liveHead: Row[] = [
      ...olderServerRows(6),
      {
        role: 'mcp_oauth',
        content: '↻ example-server sign-in is no longer active — a newer request replaced it.',
        cls: 'msg msg-warn',
        ts: BANNER_TS,
        meta: { mid: BANNER_MID, server_name: 'example-server', superseded: true },
      },
    ]

    // Seat the live view by switching into the slot with the live rows cached,
    // then confirming that switch so state.messages holds them.
    store.dispatch(switchSlot.pending('seed', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, liveHead, false), 'seed', KEY))

    // Quick check: the live view shows the superseded banner, no live oauth_url.
    const seeded = store.getState().chat.messages.find(m => m.role === 'mcp_oauth')
    expect(seeded?.meta?.superseded).toBe(true)
    expect(seeded?.meta?.oauth_url).toBeUndefined()

    // Now a reconnect / slot re-switch fetches a BOUNDED snapshot page that was
    // taken BEFORE the supersede: it carries the OLD banner copy with a
    // live-looking oauth_url, same (mid, role, ts). hasMore=true so this page is
    // a window that does NOT contain the view's oldest row — the merge keeps the
    // live head above it and appends the stale page after it.
    const stalePage: Row[] = [
      {
        role: 'mcp_oauth',
        content: '🔐 example-server requires authentication.',
        cls: 'msg msg-warn',
        ts: BANNER_TS,
        meta: { mid: BANNER_MID, server_name: 'example-server', oauth_url: 'https://idp.example.com/authorize?x=1' },
      },
      { role: 'assistant', content: 'newer reply A', cls: 'msg msg-a', ts: '2026-08-25T08:50:00Z', meta: { mid: 'm-new-A' } },
      { role: 'assistant', content: 'newer reply B', cls: 'msg msg-a', ts: '2026-08-25T08:51:00Z', meta: { mid: 'm-new-B' } },
    ]

    store.dispatch(switchSlot.pending('req-stale', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, stalePage, true), 'req-stale', KEY))

    const banners = store.getState().chat.messages.filter(m => m.role === 'mcp_oauth')
    // There must be exactly one banner (the two copies share mid+role+ts).
    expect(banners.length).toBe(1)

    // THE FIX: preserveSettledMcpOauth carries the live superseded row forward
    // over the stale page copy before deduplicateByMid, so the retired banner
    // stays retired (no revived Authorize link). On origin/main this failed:
    // the stale page copy (last in array) won and revived oauth_url.
    expect(banners[0].meta?.oauth_url, 'a retired Authorize link was revived by a stale snapshot').toBeUndefined()
    expect(banners[0].meta?.superseded, 'the newer superseded state was reverted').toBe(true)
    expect(banners[0].content).toContain('no longer active')
  })

  it('refreshSlot.fulfilled also keeps a settled (expired) banner over a stale open copy', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(KEY))

    // Seat a live view holding an EXPIRED banner (read-time gate withdrew the
    // link: oauth_url cleared, expired set) above some scrollback.
    store.dispatch(refreshSlot.fulfilled(slotPayload(KEY, [
      ...olderServerRows(6),
      {
        role: 'mcp_oauth',
        content: '🔐 example-server requires authentication.',
        cls: 'msg msg-warn',
        ts: BANNER_TS,
        meta: { mid: BANNER_MID, server_name: 'example-server', expired: true },
      },
    ], false), 'r-seed', KEY))

    // A reconnect refresh brings a stale page carrying the OLD open copy
    // (live oauth_url, no terminal flag) under the same (mid, role, ts).
    store.dispatch(refreshSlot.fulfilled(slotPayload(KEY, [
      {
        role: 'mcp_oauth',
        content: '🔐 example-server requires authentication.',
        cls: 'msg msg-warn',
        ts: BANNER_TS,
        meta: { mid: BANNER_MID, server_name: 'example-server', oauth_url: 'https://idp.example.com/authorize?x=1' },
      },
      { role: 'assistant', content: 'newer reply', cls: 'msg msg-a', ts: '2026-08-25T08:50:00Z', meta: { mid: 'm-new-C' } },
    ], true), 'r-stale', KEY))

    const banners = store.getState().chat.messages.filter(m => m.role === 'mcp_oauth')
    expect(banners.length).toBe(1)
    expect(banners[0].meta?.oauth_url, 'refreshSlot revived a retired Authorize link').toBeUndefined()
    expect(banners[0].meta?.expired).toBe(true)
  })

  it('leaves an ordinary open banner untouched when no settled prior exists', () => {
    // No terminal prior row -> the helper is a no-op and a genuinely live
    // banner keeps its Authorize link.
    const store = makeStore()
    store.dispatch(switchSlot.pending('req-open', KEY))
    store.dispatch(switchSlot.fulfilled(slotPayload(KEY, [
      {
        role: 'mcp_oauth',
        content: '🔐 example-server requires authentication.',
        cls: 'msg msg-warn',
        ts: BANNER_TS,
        meta: { mid: BANNER_MID, server_name: 'example-server', oauth_url: 'https://idp.example.com/authorize?x=1' },
      },
    ], false), 'req-open', KEY))

    const banner = store.getState().chat.messages.find(m => m.role === 'mcp_oauth')
    expect(banner?.meta?.oauth_url).toBe('https://idp.example.com/authorize?x=1')
    expect(banner?.meta?.superseded).toBeUndefined()
  })
})
