import { describe, it, expect } from 'vitest'
import reducer, { appendMessage, loadOlderMessages, selectContinuable, selectSlotPendingApproval, sseChatMessage, sseThinkingChunk, startLocalTurn } from '../store/chatSlice'
import type { ChatMessage } from '../types'
import './mockApiClient'

/**
 * A send made while a `refreshSlot` fetch is in flight. The refresh fires on a
 * socket reconnect and on `chat_done`; the user sends before its page lands.
 * The page was read BEFORE the send, so it does not carry the bubble, and its
 * `running: false` describes the slot before the turn the send opened.
 *
 * `refreshSlot.fulfilled` must keep the sends that landed after its dispatch
 * (the `idsAtDispatch` baseline the thunk records), whether still
 * optimistic or echoed, must not double one the page caught, and must keep the
 * run state the send established -- the same guard as `switchSlot.fulfilled`.
 */

const SLOT = 'open-slot'
const init = () => reducer(undefined, { type: '@@INIT' })

const user = (content: string, meta: Record<string, unknown>, ts = '2026-10-09T13:40:00.000Z'): ChatMessage =>
  ({ role: 'user', content, cls: '', ts, meta })
const reply = (content: string, mid: string, ts = '2026-10-09T13:00:01.000Z'): ChatMessage =>
  ({ role: 'assistant', content, cls: '', ts, meta: { mid } })

/** The slot open on screen with one finished exchange. */
function openWithHistory() {
  let s = reducer(init(), {
    type: 'chat/switchSlot/pending',
    meta: { arg: SLOT, requestId: 'r0', requestStatus: 'pending' },
  })
  s = reducer(s, {
    type: 'chat/switchSlot/fulfilled',
    meta: { arg: SLOT, requestId: 'r0', requestStatus: 'fulfilled' },
    payload: {
      key: SLOT,
      messages: [user('first', { sendId: 's-0', mid: 'm-0' }, '2026-10-09T13:00:00.000Z'), reply('first reply', 'm-0a')],
      running: false,
      hasMore: false,
      queue: [],
    },
  })
  return s
}

let refreshSeq = 0
function refreshLands(
  state: ReturnType<typeof init>,
  messages: ChatMessage[],
  idsAtDispatch: string[],
  extra: Record<string, unknown> = {},
) {
  refreshSeq += 1
  return reducer(state, {
    type: 'chat/refreshSlot/fulfilled',
    meta: { arg: SLOT, requestId: `rf${refreshSeq}`, requestStatus: 'fulfilled' },
    payload: {
      key: SLOT,
      messages,
      running: false,
      hasMore: false,
      queue: [],
      refreshSeq,
      liveFrameSeqAtDispatch: state.liveFrameSeq ?? 0,
      idsAtDispatch,
      ...extra,
    },
  })
}

const history = () => [user('first', { sendId: 's-0', mid: 'm-0' }, '2026-10-09T13:00:00.000Z'), reply('first reply', 'm-0a')]
/** The row identities on screen when the refresh went out: `history()`. */
const AT_DISPATCH = ['mid:m-0', 'send:s-0', 'mid:m-0a']

const continuable = (chat: ReturnType<typeof init>) =>
  selectContinuable({ chat, dashboard: { slots: [] } } as never)

describe('refreshSlot.fulfilled keeps a send made while its fetch was in flight', () => {
  it('keeps the still-optimistic bubble when the page predates the send', () => {
    let s = openWithHistory()
    // The refresh is dispatched here (baseline: s-0), then the user sends.
    s = reducer(s, appendMessage(user('next step?', { sendId: 's-1' })))
    s = refreshLands(s, history(), AT_DISPATCH)
    expect(s.messages.map(m => m.content)).toEqual(['first', 'first reply', 'next step?'])
    expect(s.messages[2].meta?.sendId).toBe('s-1')
  })

  it('keeps the bubble after its echo gave it the server mid', () => {
    let s = openWithHistory()
    s = reducer(s, appendMessage(user('hello', { sendId: 's-2' })))
    s = reducer(s, sseChatMessage({
      slot: SLOT, role: 'user', content: 'hello', ts: '2026-10-09T13:40:01.000Z',
      meta: { sendId: 's-2', mid: 'm-server', human: true },
    }))
    s = refreshLands(s, history(), AT_DISPATCH)
    expect(s.messages.map(m => m.content)).toEqual(['first', 'first reply', 'hello'])
    expect(s.messages[2].meta?.mid).toBe('m-server')
  })

  it('does not double a send the page already caught', () => {
    let s = openWithHistory()
    s = reducer(s, appendMessage(user('hello', { sendId: 's-3' })))
    s = refreshLands(s, [...history(), user('hello', { sendId: 's-3', mid: 'm-3', human: true }, '2026-10-09T13:40:01.000Z')], AT_DISPATCH)
    expect(s.messages.filter(m => m.content === 'hello')).toHaveLength(1)
    expect(s.messages.at(-1)?.meta?.mid).toBe('m-3')
  })

  it('does not rescue a row that was already on screen when the refresh went out', () => {
    // A row present at dispatch is the page's to keep or drop: the server
    // since dropped it, and the refresh must be allowed to say so.
    let s = openWithHistory()
    s = reducer(s, appendMessage(user('dropped', { sendId: 's-4' })))
    s = refreshLands(s, history(), [...AT_DISPATCH, 'send:s-4'])
    expect(s.messages.filter(m => m.content === 'dropped')).toHaveLength(0)
  })

  it('does not double a send the page lists in its queue', () => {
    // The backend queued the send before the page was read: the page's queued
    // bubble is that send, so its user row must not be rescued beside it.
    let s = openWithHistory()
    s = reducer(s, appendMessage(user('queued one', { sendId: 's-5', optimistic: true })))
    s = refreshLands(s, history(), AT_DISPATCH, {
      queue: [{ sendId: 's-5', queueId: 'q-5', content: 'queued one', ts: '2026-10-09T13:40:00.000Z' }],
    })
    expect(s.messages.filter(m => m.content === 'queued one').map(m => m.role)).toEqual(['queued'])
  })

  it('does not double a queued send that drained while the page was in flight', () => {
    // The row was a queued bubble at dispatch, then the pop turned it into a
    // user row; the stale page still lists it as queued.
    let s = openWithHistory()
    s = reducer(s, appendMessage(user('drained', { sendId: 's-6', queueId: 'q-6' })))
    s = refreshLands(s, history(), AT_DISPATCH, {
      queue: [{ sendId: 's-6', queueId: 'q-6', content: 'drained', ts: '2026-10-09T13:40:00.000Z' }],
    })
    expect(s.messages.filter(m => m.content === 'drained')).toHaveLength(1)
  })

  it('keeps the rescued send above the permission card its turn raised', () => {
    // The send's turn asked for approval before the page landed: the card is
    // kept, and the send must stay above it or the approval bar goes blank.
    let s = openWithHistory()
    s = reducer(s, appendMessage(user('run it', { sendId: 's-7' })))
    s = reducer(s, sseChatMessage({
      slot: SLOT, role: 'permission', content: 'approve rm?', ts: '2026-10-09T13:40:02.000Z',
      cls: JSON.stringify({ request_id: 'req-7', tool_input: 'rm x', is_read_only: '' }),
    }))
    s = refreshLands(s, history(), AT_DISPATCH)
    expect(s.messages.map(m => m.role)).toEqual(['user', 'assistant', 'user', 'permission'])
    expect(selectSlotPendingApproval({ chat: s } as never, SLOT)?.meta?.approval_id).toBe('req-7')
  })

  it('keeps the rescued send above the live reasoning its turn streamed', () => {
    // The reasoning row carries only its client-minted id; the send must stay
    // above it, or the turn grouper files that reasoning under the previous turn.
    let s = openWithHistory()
    s = reducer(s, appendMessage(user('think it over', { sendId: 's-8' })))
    s = reducer(s, sseThinkingChunk({ slot: SLOT, content: 'weighing options' }))
    expect(s.messages.map(m => m.role)).toEqual(['user', 'assistant', 'user', 'thinking'])
    s = refreshLands(s, history(), AT_DISPATCH)
    expect(s.messages.map(m => m.role)).toEqual(['user', 'assistant', 'user', 'thinking'])
    expect(s.messages[2].meta?.sendId).toBe('s-8')
  })

  it('does not treat older history paged in while the page was in flight as a send', () => {
    // The user scrolled up during the round trip: the older page is prepended
    // above the rows on screen at dispatch, and its user rows carry sendIds the
    // baseline never saw. The refresh keeps them as its older head, once.
    let s = openWithHistory()
    s = reducer(s, startLocalTurn(SLOT))
    s = reducer(s, {
      type: loadOlderMessages.fulfilled.type,
      meta: { arg: undefined, requestId: 'lo1', requestStatus: 'fulfilled' },
      payload: {
        slot: SLOT,
        messages: [user('older', { sendId: 's-old', mid: 'm-old' }, '2026-10-09T12:00:00.000Z'), reply('older reply', 'm-olda', '2026-10-09T12:00:01.000Z')],
        hasMore: false,
      },
    })
    expect(s.messages.map(m => m.content)).toEqual(['older', 'older reply', 'first', 'first reply'])
    s = refreshLands(s, history(), AT_DISPATCH)
    expect(s.messages.map(m => m.content)).toEqual(['older', 'older reply', 'first', 'first reply'])
    expect(s.slotRunning).toBe(false)
    expect(s.pendingTurnSlot).toBe(null)
  })
})

describe('refreshSlot.fulfilled leaves the run state of a send made while its fetch was in flight', () => {
  it('keeps the slot running when an idle page predates the send', () => {
    let s = openWithHistory()
    s = reducer(s, appendMessage(user('next step?', { sendId: 's-10' })))
    s = reducer(s, startLocalTurn(SLOT))
    expect(s.slotRunning).toBe(true)
    expect(s.pendingTurnSlot).toBe(SLOT)

    s = refreshLands(s, history(), AT_DISPATCH)
    expect(s.slotRunning).toBe(true)
    expect(s.pendingTurnSlot).toBe(SLOT)
    // No Resume offer: the slot is busy with the turn just sent.
    expect(continuable(s)).toBe(false)
  })

  it('takes the idle verdict when no send happened during the fetch', () => {
    let s = openWithHistory()
    s = reducer(s, startLocalTurn(SLOT))
    s = refreshLands(s, history(), AT_DISPATCH)
    expect(s.slotRunning).toBe(false)
    expect(s.pendingTurnSlot).toBe(null)
  })

  it('takes the idle verdict when the page caught the send', () => {
    let s = openWithHistory()
    s = reducer(s, appendMessage(user('hello', { sendId: 's-11' })))
    s = reducer(s, startLocalTurn(SLOT))
    s = refreshLands(s, [...history(), user('hello', { sendId: 's-11', mid: 'm-11' }, '2026-10-09T13:40:01.000Z'), reply('done', 'm-11a', '2026-10-09T13:40:05.000Z')], AT_DISPATCH)
    expect(s.slotRunning).toBe(false)
    expect(s.pendingTurnSlot).toBe(null)
  })
})
