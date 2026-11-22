/**
 * A background slot's sent paste stays collapsed when a server page replaces
 * its rows: the Bots page thread is such a slot.
 *
 * The backend stores only the expanded wire text of a send, never
 * `meta.pastes`, so every reducer that writes a server page has to restore the
 * collapse from the pane's own optimistic row (by `sendId`) or from the side
 * table the composer saved under the wire text, as the active-slot writers do.
 * Without it a Bot's bubble expands at the turn-end warm and stays expanded
 * after a reload.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import { api } from '../api/client'
import chatReducer, { hydrateSlotMessages, loadOlderSlotMessages, warmSlotCache } from '../store/chatSlice'
import { buildOutgoingTurn } from '../chat-core/composer/outgoingTurn'
import { formatToken, saveStoredPaste, type PasteBlock } from '../utils/pasteTokens'
import type { ChatMessage } from '../types'

const SLOT = 'member-mate.memory-member-mate'
const SEND_ID = 's-bot-1'
const P1: PasteBlock = {
  id: 'p1', seq: 1, lines: 60,
  content: Array.from({ length: 60 }, (_, i) => `log line ${i + 1}`).join('\n'),
}
const TOKEN = formatToken(P1)
const turn = buildOutgoingTurn({ text: `${TOKEN} see above`, pastes: [P1] }, 'send')

/** The row the server re-serves: the wire text and the send's id, no pastes. */
const serverUser = (mid: string, ts: string): ChatMessage => ({
  role: 'user', content: turn.wire.trimEnd(), cls: '', ts, meta: { mid, sendId: SEND_ID },
})
const reply = (mid: string, ts: string): ChatMessage => ({
  role: 'assistant', content: `reply ${mid}`, cls: '', ts, meta: { mid },
})

function makeStore(extra: Record<string, unknown>) {
  const base = chatReducer(undefined, { type: '@@INIT' })
  return configureStore({
    reducer: { chat: chatReducer },
    preloadedState: { chat: { ...base, activeSlot: 'front', ...extra } },
  })
}

function expectCollapsed(row: ChatMessage | undefined) {
  expect(row?.content).toContain(TOKEN)
  expect(row?.content).not.toContain('log line 30')
  expect(row?.meta?.pastes).toEqual([P1])
  expect(row?.meta?.sendId).toBe(SEND_ID)
}

beforeEach(() => { localStorage.clear() })
afterEach(() => vi.restoreAllMocks())

describe('background slot page writers keep a sent paste collapsed', () => {
  it('warmSlotCache: the turn-end warm keeps the optimistic bubble collapsed', async () => {
    expect(turn.wire).toContain('log line 30')
    const optimistic: ChatMessage = {
      role: 'user', content: turn.storedPaste!.display, cls: '', ts: '2026-11-15T02:00:00Z',
      meta: { pastes: [P1], sendId: SEND_ID },
    }
    const store = makeStore({ slotMessages: { [SLOT]: [optimistic] } })
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue({
      messages: [serverUser('m-1', '2026-11-15T02:00:00Z'), reply('m-2', '2026-11-15T02:00:01Z')],
      running: false, has_more: false, total: 2, queue: [],
    })
    await store.dispatch(warmSlotCache(SLOT)).unwrap()
    const rows = store.getState().chat.slotMessages[SLOT]
    expect(rows.map(m => m.meta?.mid)).toEqual(['m-1', 'm-2'])
    expectCollapsed(rows[0])
  })

  it('hydrateSlotMessages: a cold pane re-collapses the row from the side table', () => {
    saveStoredPaste(turn.storedPaste!.expanded, turn.storedPaste!.display, turn.storedPaste!.pastes, undefined, SEND_ID)
    const store = makeStore({})
    store.dispatch(hydrateSlotMessages({
      slot: SLOT,
      messages: [serverUser('m-1', '2026-11-15T02:00:00Z'), reply('m-2', '2026-11-15T02:00:01Z')],
      hasMore: false,
    }))
    const rows = store.getState().chat.slotMessages[SLOT]
    expect(rows.map(m => m.meta?.mid)).toEqual(['m-1', 'm-2'])
    expectCollapsed(rows[0])
  })

  it('loadOlderSlotMessages: an older page re-collapses the row from the side table', async () => {
    saveStoredPaste(turn.storedPaste!.expanded, turn.storedPaste!.display, turn.storedPaste!.pastes, undefined, SEND_ID)
    const store = makeStore({
      slotMessages: { [SLOT]: [reply('m-3', '2026-11-15T02:00:02Z')] },
      slotPaneHasMore: { [SLOT]: true },
      slotPaneNextBefore: { [SLOT]: 2 },
    })
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue({
      messages: [serverUser('m-1', '2026-11-15T02:00:00Z'), reply('m-2', '2026-11-15T02:00:01Z')],
      running: false, has_more: false, next_before: 0,
    })
    await store.dispatch(loadOlderSlotMessages(SLOT)).unwrap()
    const rows = store.getState().chat.slotMessages[SLOT]
    expect(rows.map(m => m.meta?.mid)).toEqual(['m-1', 'm-2', 'm-3'])
    expectCollapsed(rows[0])
  })
})
