import { beforeEach, describe, expect, it, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import { api } from '../api/client'
import baseReducer, {
  OLDER_PAGE_LIMIT,
  PANE_HYDRATE_LIMIT,
  appendMessage,
  refreshSlot,
  setActiveSlot,
  slotCoverageShortfall,
  sseChatMessage,
  switchSlot,
  warmSlotCache,
} from '../store/chatSlice'
import type { ChatMessage } from '../types'

vi.mock('../api/client')

type ChatState = ReturnType<typeof baseReducer>
type ChatAction = Parameters<typeof baseReducer>[1]

/** Most cases below exercise settlement behavior directly. Give those synthetic
 * fulfilled actions the pending registration a real async thunk always creates.
 * Tests for missing/evicted ownership call `baseReducer` explicitly. */
const reducer = (state: ChatState | undefined, action: ChatAction): ChatState => {
  let current = state ?? baseReducer(undefined, { type: '@@INIT' })
  const meta = (action as { meta?: { requestId?: string } }).meta
  const key = (action as { payload?: { key?: unknown } }).payload?.key
  if (typeof meta?.requestId === 'string' && typeof key === 'string') {
    if (action.type === refreshSlot.fulfilled.type
        && current.refreshIssueByRequest[meta.requestId] === undefined) {
      current = baseReducer(current, refreshSlot.pending(meta.requestId, key))
    }
    if (action.type === switchSlot.fulfilled.type
        && current.slotSwitchChunkClaim == null
        && current.slotSwitchRequestId === null) {
      current = baseReducer(current, switchSlot.pending(meta.requestId, key))
    }
  }
  return baseReducer(current, action)
}

const SLOT = 'chat-123'
const SEND_ID = 's-accepted-before-stale-save'
const USER_MID = 'm-accepted-before-stale-save'

const staleDetail = (messages: ChatMessage[]) => ({
  key: SLOT,
  messages,
  running: true,
  stopping: false,
  hasMore: false,
  total: messages.length,
  queue: [],
})

const staleRunningMessages = (): ChatMessage[] => [{
  role: 'assistant',
  content: 'Ready for you to merge.',
  cls: 'msg msg-a',
  ts: '2026-09-25T23:42:05.140489+00:00',
  meta: { mid: 'm-prior-assistant' },
}, {
  role: 'streaming',
  content: 'The server is still streaming.',
  cls: 'msg msg-a',
  seq: 7,
  gen: 'g-running',
}]

const confirmUser = (state: ChatState, content: string, steer = false): ChatState => {
  const steerMeta = steer ? { steer: true } : {}
  let next = reducer(state, appendMessage({
    role: 'user',
    content,
    cls: 'msg msg-u',
    ts: '2026-09-25T23:43:34.022507+00:00',
    meta: { optimistic: true, sendId: SEND_ID, ...steerMeta },
  }))
  next = reducer(next, sseChatMessage({
    slot: SLOT,
    role: 'user',
    content,
    cls: 'msg msg-u',
    ts: '2026-09-25T23:43:34.022507+00:00',
    meta: { sendId: SEND_ID, mid: USER_MID, ...steerMeta },
  }))
  return next
}

const appendNextChunk = (state: ChatState): ChatState => reducer(state, sseChatMessage({
  slot: SLOT,
  role: 'chunk',
  content: 'The post-steer stream.',
  seq: 8,
  gen: 'g-running',
}))

describe('stale snapshot confirmed-tail reconciliation', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('keeps the first confirmed send across an empty running snapshot', () => {
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, sseChatMessage({
      slot: SLOT,
      role: 'user',
      content: 'An older confirmed turn.',
      ts: '2026-09-25T23:40:00.000000+00:00',
      meta: { sendId: 's-old', mid: 'm-old-user' },
    }))
    state = reducer(state, sseChatMessage({
      slot: SLOT,
      role: 'assistant',
      content: 'An older reply separates the current tail.',
      ts: '2026-09-25T23:41:00.000000+00:00',
      meta: { mid: 'm-old-assistant' },
    }))
    state = confirmUser(state, 'The first confirmed message must stay visible.')

    state = reducer(state, refreshSlot.fulfilled(staleDetail([]), 'empty-stale', SLOT))

    expect(state.messages).toEqual([expect.objectContaining({
      role: 'user',
      content: 'The first confirmed message must stay visible.',
      meta: expect.objectContaining({
        mid: USER_MID,
        sendId: SEND_ID,
      }),
    })])
    expect(state.messages[0].meta?.optimistic).toBeUndefined()
  })

  it('keeps a confirmed send behind a trailing thinking row on an empty running page', () => {
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = confirmUser(state, 'Keep the confirmed send behind reasoning.')
    state = reducer(state, sseChatMessage({
      slot: SLOT,
      role: 'thinking',
      content: '',
    }))

    state = reducer(state, refreshSlot.fulfilled(staleDetail([]), 'thinking-stale', SLOT))

    expect(state.messages.find(message => message.meta?.mid === USER_MID)).toEqual(
      expect.objectContaining({
        role: 'user',
        content: 'Keep the confirmed send behind reasoning.',
        meta: expect.objectContaining({ sendId: SEND_ID }),
      }),
    )
    expect(state.messages.find(message => message.meta?.mid === USER_MID)?.meta?.optimistic)
      .toBeUndefined()
  })

  it('keeps ordinary confirmed sends after the page stream without finalizing it', () => {
    const stalePage = staleRunningMessages()

    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(staleDetail(stalePage), 'seed', SLOT))
    state = confirmUser(state, 'I typed "Merged", but this message is gone.')

    expect(state.messages.find(message => message.meta?.mid === USER_MID)?.meta?.optimistic)
      .toBeUndefined()

    // A concurrent queued-prompt writer can make persistence reject the whole
    // save. A refresh in that interval reads the prior disk image even though
    // the user row already reached the gateway and was confirmed over WS.
    state = reducer(state, refreshSlot.fulfilled(staleDetail(stalePage), 'stale', SLOT))

    expect(state.messages.map(message => message.role)).toEqual(['assistant', 'streaming', 'user'])
    expect(state.messages[1]).toEqual(expect.objectContaining({
      role: 'streaming',
      content: 'The server is still streaming.',
    }))
    expect(state.messages.filter(message => message.meta?.mid === USER_MID))
      .toEqual([expect.objectContaining({
        role: 'user',
        content: 'I typed "Merged", but this message is gone.',
      })])

    const confirmed = state.messages.find(message => message.meta?.mid === USER_MID)!
    expect(confirmed.meta).toEqual(expect.objectContaining({ sendId: SEND_ID }))
    expect(confirmed.meta).not.toHaveProperty('clientPendingPersistence')
    expect(confirmed.meta?.optimistic).toBeUndefined()
    expect(slotCoverageShortfall({ cached: [confirmed], window: [] })).toBe(0)

    // JSON is the server boundary: the proof must not serialize, and a row that
    // comes back through that boundary must be measured as ordinary history.
    const serialized = JSON.stringify(confirmed)
    expect(serialized).not.toContain('clientPendingPersistence')
    const canonical = JSON.parse(serialized) as ChatMessage
    expect(slotCoverageShortfall({ cached: [canonical], window: [] })).toBe(1)
    const caughtUpPage = [...stalePage, canonical]
    state = reducer(state, refreshSlot.fulfilled(
      staleDetail(caughtUpPage), 'caught-up', SLOT,
    ))
    expect(state.messages.filter(message => message.meta?.mid === USER_MID)).toHaveLength(1)
    const caughtUp = state.messages.find(message => message.meta?.mid === USER_MID)!
    expect(caughtUp.meta).not.toHaveProperty('clientPendingPersistence')
    expect(slotCoverageShortfall({ cached: [caughtUp], window: [] })).toBe(1)
    // Reducers may clone retained rows, but never mutate action payload rows.
    expect(caughtUpPage.at(-1)).toEqual(canonical)

    // Once the slot is idle, the server page is authoritative. Do not turn this
    // race guard into permanent resurrection of a row the server removed.
    state = reducer(state, refreshSlot.fulfilled(
      { ...staleDetail([stalePage[0]]), running: false }, 'idle', SLOT,
    ))
    expect(state.messages.filter(message => message.meta?.mid === USER_MID)).toHaveLength(0)
  })

  it('keeps a client-pending confirmed row through bounded warm and switch-back reads', async () => {
    const stalePage = staleRunningMessages()
    let retained = reducer(undefined, { type: '@@INIT' })
    retained = reducer(retained, setActiveSlot(SLOT))
    retained = reducer(retained, refreshSlot.fulfilled(staleDetail(stalePage), 'seed', SLOT))
    retained = confirmUser(retained, 'Keep this cached while persistence catches up.')
    retained = reducer(retained, refreshSlot.fulfilled(staleDetail(stalePage), 'stale', SLOT))
    const rescued = retained.messages.find(message => message.meta?.mid === USER_MID)!
    expect(rescued.meta).not.toHaveProperty('clientPendingPersistence')
    expect(slotCoverageShortfall({ cached: [rescued], window: [] })).toBe(0)

    const base = reducer(undefined, { type: '@@INIT' })
    const store = configureStore({
      reducer: { chat: reducer },
      preloadedState: {
        chat: {
          ...base,
          activeSlot: 'chat-other',
          slotMessages: { [SLOT]: retained.messages },
          slotRun: { [SLOT]: { state: 'streaming' as const } },
        },
      },
    })
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      messages: stalePage,
      running: true,
      stopping: false,
      has_more: false,
      total: stalePage.length,
      queue: [],
    })

    await store.dispatch(warmSlotCache(SLOT) as never)

    expect(api.chatSlotDetail).toHaveBeenCalledTimes(1)
    expect(api.chatSlotDetail).toHaveBeenCalledWith(SLOT, PANE_HYDRATE_LIMIT + 1)
    expect(api.chatSlotDetail).not.toHaveBeenCalledWith(SLOT)
    const warmed = store.getState().chat.slotMessages[SLOT]
      .find(message => message.meta?.mid === USER_MID)!
    expect(warmed.meta).not.toHaveProperty('clientPendingPersistence')
    expect(slotCoverageShortfall({ cached: [warmed], window: [] })).toBe(0)

    vi.clearAllMocks()
    await store.dispatch(switchSlot(SLOT) as never)

    expect(api.chatSlotDetail).toHaveBeenCalledTimes(1)
    expect(api.chatSlotDetail).toHaveBeenCalledWith(SLOT, OLDER_PAGE_LIMIT)
    expect(api.chatSlotDetail).not.toHaveBeenCalledWith(SLOT)
    const visible = store.getState().chat.messages.find(message => message.meta?.mid === USER_MID)
    expect(visible).toEqual(expect.objectContaining({
      role: 'user',
      content: 'Keep this cached while persistence catches up.',
      meta: expect.objectContaining({ sendId: SEND_ID }),
    }))
    expect(visible?.meta).not.toHaveProperty('clientPendingPersistence')
    expect(visible?.meta?.optimistic).toBeUndefined()
    expect(slotCoverageShortfall({ cached: [visible!], window: [] })).toBe(0)
  })

  it('refresh finalizes the stale page stream before a retained steer', () => {
    const stalePage = staleRunningMessages()

    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(staleDetail(stalePage), 'seed', SLOT))
    state = confirmUser(state, 'Steer the running turn.', true)
    state = reducer(state, refreshSlot.fulfilled(staleDetail(stalePage), 'stale', SLOT))

    expect(state.messages.map(message => message.role)).toEqual(['assistant', 'assistant', 'user'])
    expect(state.messages[1]).toEqual(expect.objectContaining({
      content: 'The server is still streaming.',
      rawText: 'The server is still streaming.',
    }))
    expect(state.messages[2]).toEqual(expect.objectContaining({
      role: 'user',
      content: 'Steer the running turn.',
      meta: expect.objectContaining({ steer: true, mid: USER_MID }),
    }))
    expect(stalePage[1]).toEqual(expect.objectContaining({
      role: 'streaming',
      content: 'The server is still streaming.',
    }))
    expect(stalePage[1].rawText).toBeUndefined()

    state = appendNextChunk(state)
    expect(state.messages.map(message => message.role)).toEqual(['assistant', 'assistant', 'user', 'streaming'])
    expect(state.messages[3].content).toBe('The post-steer stream.')
  })

  it('switch-back finalizes the stale page stream before a retained steer', () => {
    const stalePage = staleRunningMessages()

    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(staleDetail(stalePage), 'seed', SLOT))
    state = confirmUser(state, 'Steer the switched-back turn.', true)
    state = reducer(state, switchSlot.fulfilled(staleDetail(stalePage), 'switch', SLOT))

    expect(state.messages.map(message => message.role)).toEqual(['assistant', 'assistant', 'user'])
    expect(state.messages[1]).toEqual(expect.objectContaining({
      content: 'The server is still streaming.',
      rawText: 'The server is still streaming.',
    }))
    expect(state.messages[2]).toEqual(expect.objectContaining({
      role: 'user',
      content: 'Steer the switched-back turn.',
      meta: expect.objectContaining({ steer: true, mid: USER_MID }),
    }))
    expect(stalePage[1]).toEqual(expect.objectContaining({ role: 'streaming' }))
    expect(stalePage[1].rawText).toBeUndefined()

    state = appendNextChunk(state)
    expect(state.messages.map(message => message.role)).toEqual(['assistant', 'assistant', 'user', 'streaming'])
    expect(state.messages[3].content).toBe('The post-steer stream.')
  })

  it('keeps a post-steer live stream after the retained steer on switch-back', () => {
    const stalePage = staleRunningMessages()

    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(staleDetail(stalePage), 'seed', SLOT))
    state = confirmUser(state, 'Steer before the next segment.', true)
    state = reducer(state, switchSlot.pending('away', 'chat-other'))
    state = reducer(state, switchSlot.pending('switch-back', SLOT))
    state = reducer(state, sseChatMessage({
      slot: SLOT,
      role: 'chunk',
      content: 'Post-steer segment.',
      seq: 8,
      gen: 'g-running',
    }))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail(stalePage), 'switch-back', SLOT,
    ))

    expect(state.messages.map(message => [message.role, message.content])).toEqual([
      ['assistant', 'Ready for you to merge.'],
      ['assistant', 'The server is still streaming.'],
      ['user', 'Steer before the next segment.'],
      ['streaming', 'Post-steer segment.'],
    ])
    expect(state.messages[2].meta).toEqual(expect.objectContaining({
      steer: true,
      mid: USER_MID,
    }))

    state = reducer(state, sseChatMessage({
      slot: SLOT,
      role: 'chunk',
      content: ' Continued.',
      seq: 9,
      gen: 'g-running',
    }))
    expect(state.messages.at(-1)).toEqual(expect.objectContaining({
      role: 'streaming',
      content: 'Post-steer segment. Continued.',
    }))
  })

  it('does not overwrite a retained steer when finalization removes a placeholder', () => {
    const placeholderPage: ChatMessage[] = [staleRunningMessages()[0], {
      role: 'streaming',
      content: '...',
      cls: 'msg msg-a',
      seq: 7,
      gen: 'g-running',
    }]

    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(
      staleDetail(placeholderPage), 'seed-placeholder', SLOT,
    ))
    state = confirmUser(state, 'Steer after the placeholder.', true)
    state = reducer(state, switchSlot.pending('away-placeholder', 'chat-other'))
    state = reducer(state, switchSlot.pending('back-placeholder', SLOT))
    state = reducer(state, sseChatMessage({
      slot: SLOT,
      role: 'chunk',
      content: 'Real post-steer output.',
      seq: 8,
      gen: 'g-running',
    }))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail(placeholderPage), 'back-placeholder', SLOT,
    ))

    expect(state.messages.map(message => [message.role, message.content])).toEqual([
      ['assistant', 'Ready for you to merge.'],
      ['user', 'Steer after the placeholder.'],
      ['streaming', 'Real post-steer output.'],
    ])
    expect(state.messages.filter(message => message.meta?.mid === USER_MID)).toHaveLength(1)
    expect(state.messages[1].meta).toEqual(expect.objectContaining({ steer: true }))
    expect(placeholderPage[1]).toEqual(expect.objectContaining({
      role: 'streaming',
      content: '...',
    }))
  })

  it('keeps an identity-less same-text stream under a later user anchor', () => {
    const earlierPage: ChatMessage[] = [{
      role: 'user',
      content: 'Finish the first task.',
      cls: 'msg msg-u',
      ts: '2026-09-25T23:45:00.000000+00:00',
      meta: { mid: 'm-first-user', sendId: 's-first-user' },
    }, {
      role: 'assistant',
      content: 'Done.',
      cls: 'msg msg-a',
      ts: '2026-09-25T23:45:01.000000+00:00',
      meta: { mid: 'm-first-assistant' },
    }]

    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(staleDetail(earlierPage), 'seed-first', SLOT))
    state = reducer(state, sseChatMessage({
      slot: SLOT,
      role: 'user',
      content: 'Finish the second task.',
      cls: 'msg msg-u',
      ts: '2026-09-25T23:46:00.000000+00:00',
      meta: { mid: 'm-second-user', sendId: 's-second-user' },
    }))
    state = reducer(state, switchSlot.pending('away-same-text', 'chat-other'))
    state = reducer(state, switchSlot.pending('back-same-text', SLOT))
    state = reducer(state, sseChatMessage({
      slot: SLOT,
      role: 'chunk',
      content: 'Done.',
      seq: 8,
      gen: 'g-same-text',
    }))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail(earlierPage), 'back-same-text', SLOT,
    ))

    expect(state.messages.map(message => [message.role, message.content])).toEqual([
      ['user', 'Finish the first task.'],
      ['assistant', 'Done.'],
      ['user', 'Finish the second task.'],
      ['streaming', 'Done.'],
    ])

    state = reducer(state, sseChatMessage({
      slot: SLOT,
      role: 'chunk',
      content: ' The second task is complete.',
      seq: 9,
      gen: 'g-same-text',
    }))
    expect(state.messages.at(-1)).toEqual(expect.objectContaining({
      role: 'streaming',
      content: 'Done. The second task is complete.',
    }))
  })

  it('uses a shared sendId anchor to prefer the canonical finalized assistant', () => {
    const localUser: ChatMessage = {
      role: 'user',
      content: 'Show the credential safely.',
      cls: 'msg msg-u',
      ts: '2026-09-25T23:47:00.000000+00:00',
      meta: { sendId: 's-redacted-user' },
    }
    const pageUser: ChatMessage = {
      ...localUser,
      meta: { mid: 'm-redacted-user', sendId: 's-redacted-user' },
    }
    const canonical: ChatMessage = {
      role: 'assistant',
      content: 'The token is [REDACTED: credential].',
      cls: 'msg msg-a',
      ts: '2026-09-25T23:47:01.000000+00:00',
      meta: { mid: 'm-redacted-assistant' },
    }

    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(staleDetail([localUser]), 'seed-redacted', SLOT))
    state = reducer(state, switchSlot.pending('away-redacted', 'chat-other'))
    state = reducer(state, switchSlot.pending('back-redacted', SLOT))
    state = reducer(state, sseChatMessage({
      slot: SLOT,
      role: 'chunk',
      content: 'The token is raw before server redaction.',
      seq: 1,
      gen: 'g-redacted',
    }))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail([pageUser, canonical]), 'back-redacted', SLOT,
    ))

    expect(state.messages.filter(message => message.role === 'assistant')).toEqual([canonical])
    expect(state.messages.some(message => message.role === 'streaming')).toBe(false)
  })

  it('keeps a second segment of one turn that the stale page cannot have finalized', () => {
    // One prompt, two assistant segments. Both the page's finalized segment 1
    // and the live segment 2 follow the SAME durable user row, so the anchor
    // identity matches on both sides while naming different segments -- the
    // false proof that deleted segment 2. The assistant barrier declines it.
    const userA: ChatMessage = {
      role: 'user',
      content: 'Do both halves of the task.',
      cls: 'msg msg-u',
      ts: '2026-09-25T23:51:00.000000+00:00',
      meta: { mid: 'm-two-segment-user', sendId: 's-two-segment-user' },
    }
    const segmentOne: ChatMessage = {
      role: 'assistant',
      content: 'First half done.',
      cls: 'msg msg-a',
      ts: '2026-09-25T23:51:01.000000+00:00',
      meta: { mid: 'm-two-segment-first' },
    }

    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(
      staleDetail([userA, segmentOne]), 'seed-two-segment', SLOT,
    ))
    state = reducer(state, switchSlot.pending('away-two-segment', 'chat-other'))
    state = reducer(state, switchSlot.pending('back-two-segment', SLOT))
    state = reducer(state, sseChatMessage({
      slot: SLOT,
      role: 'chunk',
      content: 'Second half starting.',
      seq: 4,
      gen: 'g-two-segment',
    }))

    // The fetch answers with the page as it stood BEFORE segment 2 opened: its
    // newest statement is segment 1, under that same user anchor.
    state = reducer(state, switchSlot.fulfilled(
      staleDetail([userA, segmentOne]), 'back-two-segment', SLOT,
    ))

    expect(state.messages.map(message => [message.role, message.content])).toEqual([
      ['user', 'Do both halves of the task.'],
      ['assistant', 'First half done.'],
      ['streaming', 'Second half starting.'],
    ])

    // Still the live accumulator, so the turn's next chunk lands on it.
    state = reducer(state, sseChatMessage({
      slot: SLOT,
      role: 'chunk',
      content: ' Second half done.',
      seq: 5,
      gen: 'g-two-segment',
    }))
    expect(state.messages.at(-1)).toEqual(expect.objectContaining({
      role: 'streaming',
      content: 'Second half starting. Second half done.',
    }))
  })

  it.each([
    {
      proof: 'missing',
      localUsers: [{
        role: 'user', content: 'An unidentified question.', cls: 'msg msg-u',
        ts: '2026-09-25T23:48:00.000000+00:00',
      }] satisfies ChatMessage[],
      pageUsers: [{
        role: 'user', content: 'An unidentified question.', cls: 'msg msg-u',
        ts: '2026-09-25T23:48:00.000000+00:00',
      }] satisfies ChatMessage[],
    },
    {
      proof: 'ambiguous',
      localUsers: [{
        role: 'user', content: 'First duplicate.', cls: 'msg msg-u',
        ts: '2026-09-25T23:49:00.000000+00:00', meta: { mid: 'm-duplicate-user' },
      }, {
        role: 'user', content: 'Second duplicate.', cls: 'msg msg-u',
        ts: '2026-09-25T23:49:01.000000+00:00', meta: { mid: 'm-duplicate-user' },
      }] satisfies ChatMessage[],
      pageUsers: [{
        role: 'user', content: 'First duplicate.', cls: 'msg msg-u',
        ts: '2026-09-25T23:49:00.000000+00:00', meta: { mid: 'm-duplicate-user' },
      }, {
        role: 'user', content: 'Second duplicate.', cls: 'msg msg-u',
        ts: '2026-09-25T23:49:01.000000+00:00', meta: { mid: 'm-duplicate-user' },
      }] satisfies ChatMessage[],
    },
  ])('preserves live content when user-anchor proof is $proof', ({ localUsers, pageUsers }) => {
    const canonical: ChatMessage = {
      role: 'assistant',
      content: 'Canonical completion.',
      cls: 'msg msg-a',
      ts: '2026-09-25T23:50:00.000000+00:00',
      meta: { mid: 'm-canonical-completion' },
    }
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(staleDetail(localUsers), 'seed-proof', SLOT))
    state = reducer(state, switchSlot.pending('away-proof', 'chat-other'))
    state = reducer(state, switchSlot.pending('back-proof', SLOT))
    state = reducer(state, sseChatMessage({
      slot: SLOT,
      role: 'chunk',
      content: 'Unproven live completion.',
      seq: 1,
      gen: 'g-proof',
    }))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail([...pageUsers, canonical]), 'back-proof', SLOT,
    ))

    expect(state.messages.some(message =>
      message.role === 'streaming' && message.content === 'Unproven live completion.',
    )).toBe(true)
  })

  it('keeps a direct assistant that arrives over an empty switch cache', () => {
    const staleOpen: ChatMessage = {
      role: 'streaming', content: 'Stale partial.', cls: 'msg msg-a', seq: 4, gen: 'g-unobserved',
    }
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, switchSlot.pending('direct-assistant', SLOT))
    state = reducer(state, sseChatMessage({
      slot: SLOT,
      role: 'assistant',
      content: 'Canonical completion.',
      seq: 5,
      gen: 'g-unobserved',
      meta: { mid: 'm-direct-assistant' },
    }))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail([staleOpen]), 'direct-assistant', SLOT,
    ))

    expect(state.messages.map(message => [message.role, message.content, message.meta?.mid]))
      .toEqual([['assistant', 'Canonical completion.', 'm-direct-assistant']])
    expect(state.slotSwitchChunkClaim).toEqual(expect.objectContaining({
      clientTs: [expect.any(String)],
      finalizers: [expect.objectContaining({
        clientTs: expect.any(String), seq: 5, gen: 'g-unobserved',
      })],
      rowlessFinalizer: null,
    }))
    expect(state.slotSwitchChunkClaim?.finalizers.map(item => item.clientTs))
      .toEqual(state.slotSwitchChunkClaim?.clientTs)
  })

  it('uses an exact unique mid to finalize the same open page row without sequence proof', () => {
    const pageOpen: ChatMessage = {
      role: 'streaming', content: 'Stale copy.', cls: 'msg msg-a', seq: 4,
      meta: { mid: 'm-direct-exact' },
    }
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, switchSlot.pending('direct-exact-mid', SLOT))
    state = reducer(state, sseChatMessage({
      slot: SLOT, role: 'assistant', content: 'Canonical completion.',
      meta: { mid: 'm-direct-exact' },
    }))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail([pageOpen]), 'direct-exact-mid', SLOT,
    ))

    expect(state.messages.map(message => [message.role, message.content, message.meta?.mid]))
      .toEqual([['assistant', 'Canonical completion.', 'm-direct-exact']])
  })

  it('retains an unordered direct assistant once before a known-generation page', () => {
    const pageOpen: ChatMessage = {
      role: 'streaming', content: 'Known process segment.', cls: 'msg msg-a',
      seq: 4, gen: 'g-page-known',
    }
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, switchSlot.pending('direct-unordered', SLOT))
    state = reducer(state, sseChatMessage({
      slot: SLOT, role: 'assistant', content: 'Unordered completion.',
      meta: { mid: 'm-direct-unordered' },
    }))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail([pageOpen]), 'direct-unordered', SLOT,
    ))

    expect(state.messages.map(message => [message.role, message.content])).toEqual([
      ['assistant', 'Unordered completion.'],
      ['streaming', 'Known process segment.'],
    ])
    expect(state.messages.filter(message => message.meta?.mid === 'm-direct-unordered'))
      .toHaveLength(1)
  })

  it.each(['_segment', '_done'] as const)(
    'keeps a rowless %s boundary over a stale open switch page',
    (role) => {
      const staleOpen: ChatMessage = {
        role: 'streaming', content: 'Complete before the boundary.', cls: 'msg msg-a',
        seq: 4, gen: 'g-unobserved',
      }
      let state = reducer(undefined, { type: '@@INIT' })
      state = reducer(state, setActiveSlot(SLOT))
      state = reducer(state, switchSlot.pending(`rowless-${role}`, SLOT))
      state = reducer(state, sseChatMessage({
        slot: SLOT, role, content: '', seq: 5, gen: 'g-unobserved',
      }))

      state = reducer(state, switchSlot.fulfilled(
        staleDetail([staleOpen]), `rowless-${role}`, SLOT,
      ))

      expect(state.messages.map(message => [message.role, message.content])).toEqual([
        ['assistant', 'Complete before the boundary.'],
      ])
      expect(state.slotSwitchChunkClaim).toEqual(expect.objectContaining({
        clientTs: [],
        finalizers: [],
        rowlessFinalizer: { seq: 5, gen: 'g-unobserved' },
      }))
      state = reducer(state, sseChatMessage({
        slot: SLOT, role: 'chunk', content: 'Next segment.', seq: 5,
      }))
      expect(state.messages.map(message => [message.role, message.content])).toEqual([
        ['assistant', 'Complete before the boundary.'],
        ['streaming', 'Next segment.'],
      ])
    },
  )

  it('keeps a rowless-finalized page segment before a later local segment', () => {
    const pageOpen: ChatMessage = {
      role: 'streaming', content: 'Segment A.', cls: 'msg msg-a',
      seq: 4, gen: 'g-rowless-next',
    }
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, switchSlot.pending('rowless-then-next', SLOT))
    state = reducer(state, sseChatMessage({
      slot: SLOT, role: '_segment', content: '', seq: 5, gen: 'g-rowless-next',
    }))
    state = reducer(state, sseChatMessage({
      slot: SLOT, role: 'chunk', content: 'Segment B.', seq: 6, gen: 'g-rowless-next',
    }))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail([pageOpen]), 'rowless-then-next', SLOT,
    ))

    expect(state.messages.map(message => [message.role, message.content])).toEqual([
      ['assistant', 'Segment A.'],
      ['streaming', 'Segment B.'],
    ])
  })

  it('keeps both open segments when their shared anchor is unknown', () => {
    const localOpen: ChatMessage = {
      role: 'streaming', content: 'Local segment B.', cls: 'msg msg-a',
      seq: 8, gen: 'g-unknown-open',
    }
    const pageOpen: ChatMessage = {
      role: 'streaming', content: 'Page segment A.', cls: 'msg msg-a',
      seq: 4, gen: 'g-unknown-open',
    }
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(
      staleDetail([localOpen]), 'seed-unknown-open', SLOT,
    ))
    state = reducer(state, switchSlot.pending('unknown-open-anchor', SLOT))
    state = reducer(state, switchSlot.fulfilled(
      staleDetail([pageOpen]), 'unknown-open-anchor', SLOT,
    ))

    expect(state.messages.map(message => [message.role, message.content])).toEqual([
      ['assistant', 'Page segment A.'],
      ['streaming', 'Local segment B.'],
    ])
  })

  it('does not turn a removed placeholder into a rowless boundary candidate', () => {
    const placeholder: ChatMessage = {
      role: 'streaming', content: '...', cls: 'msg msg-a', seq: 4, gen: 'g-placeholder',
    }
    const fetched: ChatMessage = {
      role: 'streaming', content: 'Newer fetched output.', cls: 'msg msg-a', seq: 5, gen: 'g-placeholder',
    }
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(
      staleDetail([placeholder]), 'seed-rowless-control', SLOT,
    ))
    state = reducer(state, switchSlot.pending('rowless-control', SLOT))
    state = reducer(state, sseChatMessage({ slot: SLOT, role: '_segment', content: '' }))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail([fetched]), 'rowless-control', SLOT,
    ))

    expect(state.messages).toEqual([fetched])
    expect(state.messages[0].role).toBe('streaming')
    expect(state.slotSwitchChunkClaim).toEqual(expect.objectContaining({
      clientTs: [expect.any(String)],
      finalizers: [expect.objectContaining({
        clientTs: expect.any(String), seq: 4, gen: 'g-placeholder',
      })],
      rowlessFinalizer: null,
    }))
  })

  it('keeps a new-generation page authoritative over a rowless boundary', () => {
    const fetched: ChatMessage = {
      role: 'streaming', content: 'Fresh process output.', cls: 'msg msg-a', seq: 1, gen: 'g-new',
    }
    let state = {
      ...reducer(undefined, { type: '@@INIT' }),
      activeSlot: SLOT,
      lastChunkSeq: 9,
      lastChunkGen: 'g-old',
    }
    state = reducer(state, switchSlot.pending('rowless-new-generation', SLOT))
    state = reducer(state, sseChatMessage({ slot: SLOT, role: '_segment', content: '' }))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail([fetched]), 'rowless-new-generation', SLOT,
    ))

    expect(state.messages).toEqual([fetched])
    expect(state.messages[0].role).toBe('streaming')
    expect(state.lastChunkGen).toBe('g-new')
    expect(state.slotSwitchChunkClaim).toEqual(expect.objectContaining({
      clientTs: [],
      finalizers: [],
      rowlessFinalizer: { seq: 9, gen: 'g-old' },
    }))
  })

  it.each([
    { role: '_segment' as const, content: '', expected: 'Complete before switch.', running: true },
    { role: '_done' as const, content: '', expected: 'Complete before switch.', running: false },
    { role: 'assistant' as const, content: 'Canonical assistant.', expected: 'Canonical assistant.', running: true },
  ])('claims a cached stream when $role finalizes it during the fetch', ({ role, content, expected, running }) => {
    const anchor: ChatMessage = {
      role: 'user', content: 'Finish this reply.', cls: 'msg msg-u',
      meta: { mid: 'm-finalizer-anchor', sendId: 's-finalizer-anchor' },
    }
    const cachedOpen: ChatMessage = {
      role: 'streaming', content: 'Complete before switch.', cls: 'msg msg-a',
      seq: 9, gen: 'g-finalizer',
    }
    const staleOpen: ChatMessage = {
      role: 'streaming', content: 'Stale partial.', cls: 'msg msg-a',
      seq: 5, gen: 'g-finalizer',
    }
    const payload = [anchor, staleOpen]
    const payloadBefore = structuredClone(payload)
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(
      staleDetail([anchor, cachedOpen]), 'seed-finalizer-only', SLOT,
    ))
    state = reducer(state, switchSlot.pending(`claim-${role}`, SLOT))
    state = reducer(state, sseChatMessage({
      slot: SLOT,
      role,
      content,
      ...(role === 'assistant'
        ? { ts: '2026-09-25T23:52:00.000000+00:00', meta: { mid: 'm-finalizer-assistant' } }
        : {}),
    }))

    state = reducer(state, switchSlot.fulfilled(
      { ...staleDetail(payload), running }, `claim-${role}`, SLOT,
    ))

    expect(state.messages.map(message => [message.role, message.content])).toEqual([
      ['user', 'Finish this reply.'],
      ['assistant', expected],
    ])
    expect(state.slotRunning).toBe(running)
    expect(state.slotState).toBe(running ? 'streaming' : 'idle')
    expect(state.slotSwitchChunkClaim?.clientTs).toHaveLength(1)
    expect(state.slotSwitchChunkClaim?.finalizers.map(item => item.clientTs))
      .toEqual(state.slotSwitchChunkClaim?.clientTs)
    expect(state.slotSwitchChunkClaim?.finalizers[0]).toEqual(expect.objectContaining({
      seq: 9, gen: 'g-finalizer',
    }))
    expect(state.slotSwitchChunkClaim?.rowlessFinalizer).toBeNull()
    if (role === 'assistant') {
      expect(state.messages[1].meta).toEqual(expect.objectContaining({
        mid: 'm-finalizer-assistant',
        clientTs: expect.any(String),
      }))
    }
    expect(payload).toEqual(payloadBefore)

    state = reducer(state, sseChatMessage({
      slot: SLOT, role: 'chunk', content: 'Next segment.', seq: 10, gen: 'g-finalizer',
    }))
    expect(state.messages.map(message => message.role)).toEqual(['user', 'assistant', 'streaming'])
    expect(state.messages.at(-1)?.content).toBe('Next segment.')
  })

  it.each([
    {
      name: 'local floor is newer',
      local: { content: 'Hello wor', seq: 9, gen: 'g-open' },
      page: { content: 'Hello', seq: 5, gen: 'g-open' },
      expected: { content: 'Hello wor', seq: 9, gen: 'g-open' },
    },
    {
      name: 'page sequence is newer',
      local: { content: 'Hello', seq: 5, gen: 'g-open' },
      page: { content: 'Hello world', seq: 9, gen: 'g-open' },
      expected: { content: 'Hello world', seq: 9, gen: 'g-open' },
    },
    {
      name: 'page generation is newer',
      local: { content: 'Old process text', seq: 9, gen: 'g-old' },
      page: { content: 'Fresh process text', seq: 1, gen: 'g-new' },
      expected: { content: 'Fresh process text', seq: 1, gen: 'g-new' },
    },
  ])('reconciles an unclaimed cached open row when $name', ({ local, page, expected }) => {
    const anchor: ChatMessage = {
      role: 'user', content: 'Continue streaming.', cls: 'msg msg-u',
      ts: '2026-09-25T23:50:00.000000+00:00',
      meta: { mid: 'm-open-anchor', sendId: 's-open-anchor' },
    }
    const localOpen: ChatMessage = {
      role: 'streaming', content: local.content, cls: 'msg msg-a',
      seq: local.seq, gen: local.gen,
    }
    const pageOpen: ChatMessage = {
      role: 'streaming', content: page.content, cls: 'msg msg-a',
      seq: page.seq, gen: page.gen,
    }
    const payload = [anchor, pageOpen]
    const payloadBefore = structuredClone(payload)
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(
      staleDetail([anchor, localOpen]), 'seed-unclaimed-open', SLOT,
    ))
    state = reducer(state, switchSlot.pending('unclaimed-open', SLOT))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail(payload), 'unclaimed-open', SLOT,
    ))

    expect(state.messages.at(-1)).toEqual(expect.objectContaining({
      role: 'streaming',
      content: expected.content,
    }))
    expect(state.lastChunkSeq).toBe(expected.seq)
    expect(state.lastChunkGen).toBe(expected.gen)
    expect(state.slotSwitchChunkClaim?.clientTs).toEqual([])
    expect(payload).toEqual(payloadBefore)
  })

  it('keeps a claimed segment after chat_segment finalizes it', () => {
    const anchor: ChatMessage = {
      role: 'user', content: 'Explain it.', cls: 'msg msg-u',
      meta: { mid: 'm-final-anchor', sendId: 's-final-anchor' },
    }
    const staleOpen: ChatMessage = {
      role: 'streaming', content: 'Stale partial.', cls: 'msg msg-a',
      seq: 1, gen: 'g-final',
    }
    const tool: ChatMessage = {
      role: 'tool', content: 'Checked.', cls: 'msg msg-tool', meta: { mid: 'm-final-tool' },
    }
    const page = [anchor, staleOpen, tool]
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(staleDetail([anchor, staleOpen]), 'seed-final', SLOT))
    state = reducer(state, switchSlot.pending('claim-final', SLOT))
    state = reducer(state, sseChatMessage({
      slot: SLOT, role: 'chunk', content: ' Local finish.', seq: 2, gen: 'g-final',
    }))
    state = reducer(state, sseChatMessage({ slot: SLOT, role: '_segment', content: '' }))
    expect(state.slotSwitchChunkClaim?.clientTs).toHaveLength(1)
    state = reducer(state, sseChatMessage({ slot: SLOT, ...tool }))

    state = reducer(state, switchSlot.fulfilled(staleDetail(page), 'claim-final', SLOT))

    expect(state.messages.map(message => [message.role, message.content])).toEqual([
      ['user', 'Explain it.'],
      ['assistant', 'Stale partial. Local finish.'],
      ['tool', 'Checked.'],
    ])
    expect(page).toEqual([anchor, staleOpen, tool])
  })

  it('lets a disjoint unique assistant mid outrank a shared user anchor', () => {
    const anchor: ChatMessage = {
      role: 'user', content: 'Same turn.', cls: 'msg msg-u',
      meta: { mid: 'm-disjoint-user', sendId: 's-disjoint-user' },
    }
    const canonical: ChatMessage = {
      role: 'assistant', content: 'Page row.', cls: 'msg msg-a', meta: { mid: 'm-page' },
    }
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(staleDetail([anchor]), 'seed-disjoint', SLOT))
    state = reducer(state, switchSlot.pending('claim-disjoint', SLOT))
    state = reducer(state, sseChatMessage({
      slot: SLOT, role: 'chunk', content: 'Local row.', seq: 1, gen: 'g-disjoint',
    }))
    state = reducer(state, sseChatMessage({
      slot: SLOT, role: 'assistant', content: 'Local row.', meta: { mid: 'm-local' },
    }))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail([anchor, canonical]), 'claim-disjoint', SLOT,
    ))

    expect(state.messages.filter(message => message.role === 'assistant')
      .map(message => message.meta?.mid)).toEqual(['m-page', 'm-local'])
  })

  it('preserves a claimed segment when its assistant mid is ambiguous', () => {
    const anchor: ChatMessage = {
      role: 'user', content: 'Same ambiguous turn.', cls: 'msg msg-u',
      meta: { mid: 'm-ambiguous-user', sendId: 's-ambiguous-user' },
    }
    const canonical: ChatMessage = {
      role: 'assistant', content: 'Page row.', cls: 'msg msg-a', meta: { mid: 'm-page-ambiguous' },
    }
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(staleDetail([anchor]), 'seed-ambiguous-mid', SLOT))
    state = reducer(state, switchSlot.pending('claim-ambiguous-mid', SLOT))
    state = reducer(state, sseChatMessage({
      slot: SLOT, role: 'chunk', content: 'Local ambiguous row.', seq: 1, gen: 'g-ambiguous-mid',
    }))
    state = reducer(state, sseChatMessage({
      slot: SLOT, role: 'assistant', content: 'Local ambiguous row.', meta: { mid: 'm-local-ambiguous' },
    }))
    state = reducer(state, appendMessage({
      role: 'tool', content: 'Duplicate the id without naming the assistant.', cls: 'msg msg-tool',
      meta: { mid: 'm-local-ambiguous' },
    }))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail([anchor, canonical]), 'claim-ambiguous-mid', SLOT,
    ))

    expect(state.messages.filter(message => message.role === 'assistant')
      .map(message => message.meta?.mid)).toEqual(['m-page-ambiguous', 'm-local-ambiguous'])
  })

  it('does not rescue an older assistant after a claimed placeholder is removed', () => {
    const placeholder: ChatMessage = {
      role: 'streaming', content: '...', cls: 'msg msg-a',
      seq: 1, gen: 'g-placeholder-claim',
    }
    const fetched: ChatMessage = {
      role: 'streaming', content: 'Fetched authority.', cls: 'msg msg-a',
      seq: 2, gen: 'g-placeholder-claim',
    }
    const payload = [fetched]
    const payloadBefore = structuredClone(payload)
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, sseChatMessage({
      slot: SLOT, role: 'assistant', content: 'Older local assistant.',
      meta: { mid: 'm-older-local' },
    }))
    state = reducer(state, refreshSlot.fulfilled(
      staleDetail([placeholder]), 'seed-placeholder-claim', SLOT,
    ))
    state = reducer(state, switchSlot.pending('claim-placeholder', SLOT))
    state = reducer(state, sseChatMessage({ slot: SLOT, role: '_segment', content: '' }))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail(payload), 'claim-placeholder', SLOT,
    ))

    expect(state.messages).toEqual([fetched])
    expect(state.slotSwitchChunkClaim?.clientTs).toHaveLength(1)
    expect(payload).toEqual(payloadBefore)
  })

  it('keeps an empty fetched stream from a new generation authoritative', () => {
    const fetched: ChatMessage = {
      role: 'streaming', content: '', cls: 'msg msg-a', seq: 1, gen: 'g-new',
    }
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, switchSlot.pending('claim-new-gen', SLOT))
    state = reducer(state, sseChatMessage({
      slot: SLOT, role: 'chunk', content: 'Old process text.', seq: 9, gen: 'g-old',
    }))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail([fetched]), 'claim-new-gen', SLOT,
    ))

    expect(state.messages).toEqual([fetched])
    expect(state.lastChunkGen).toBe('g-new')
    expect(state.lastChunkSeq).toBe(1)
  })

  it.each(['old-first', 'new-first'] as const)(
    'keeps the newer same-target claim when settlements arrive %s',
    (settlementOrder) => {
      const stale: ChatMessage = {
        role: 'assistant', content: 'Older request page.', cls: 'msg msg-a',
        meta: { mid: 'm-stale-request' },
      }
      let state = reducer(undefined, { type: '@@INIT' })
      state = reducer(state, setActiveSlot(SLOT))
      state = reducer(state, switchSlot.pending('same-target-old', SLOT))
      state = reducer(state, switchSlot.pending('same-target-new', SLOT))
      if (settlementOrder === 'old-first') {
        state = reducer(state, switchSlot.fulfilled(
          staleDetail([stale]), 'same-target-old', SLOT,
        ))
      }
      state = reducer(state, sseChatMessage({
        slot: SLOT, role: 'chunk', content: 'Newest request text.', seq: 1, gen: 'g-order',
      }))
      state = reducer(state, switchSlot.fulfilled(
        staleDetail([]), 'same-target-new', SLOT,
      ))
      if (settlementOrder === 'new-first') {
        state = reducer(state, switchSlot.fulfilled(
          staleDetail([stale]), 'same-target-old', SLOT,
        ))
      }

      expect(state.messages.map(message => message.content)).toEqual(['Newest request text.'])
    },
  )

  it('keeps multiple claimed segments in local order', () => {
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, switchSlot.pending('claim-many', SLOT))
    for (const [content, seq] of [['First segment.', 1], ['Second segment.', 2]] as const) {
      state = reducer(state, sseChatMessage({
        slot: SLOT, role: 'chunk', content, seq, gen: 'g-many',
      }))
      state = reducer(state, sseChatMessage({ slot: SLOT, role: '_segment', content: '' }))
    }

    state = reducer(state, switchSlot.fulfilled(staleDetail([]), 'claim-many', SLOT))

    expect(state.messages.map(message => [message.role, message.content])).toEqual([
      ['assistant', 'First segment.'],
      ['assistant', 'Second segment.'],
    ])
  })

  it('keeps a newer same-generation open page after an older claimed finalizer', () => {
    const cachedOpen: ChatMessage = {
      role: 'streaming', content: 'Segment one complete.', cls: 'msg msg-a',
      seq: 4, gen: 'g-finalizer-order',
    }
    const pageOpen: ChatMessage = {
      role: 'streaming', content: 'Segment two from the page.', cls: 'msg msg-a',
      seq: 5, gen: 'g-finalizer-order',
    }
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(
      staleDetail([cachedOpen]), 'seed-older-finalizer', SLOT,
    ))
    state = reducer(state, switchSlot.pending('older-finalizer', SLOT))
    state = reducer(state, sseChatMessage({
      slot: SLOT, role: '_segment', content: '',
    }))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail([pageOpen]), 'older-finalizer', SLOT,
    ))

    expect(state.messages.map(message => [message.role, message.content])).toEqual([
      ['assistant', 'Segment one complete.'],
      ['streaming', 'Segment two from the page.'],
    ])
    state = reducer(state, sseChatMessage({
      slot: SLOT, role: 'chunk', content: ' duplicated', seq: 5, gen: 'g-finalizer-order',
    }))
    expect(state.messages.at(-1)?.content).toBe('Segment two from the page.')
    state = reducer(state, sseChatMessage({
      slot: SLOT, role: 'chunk', content: ' continued', seq: 6, gen: 'g-finalizer-order',
    }))
    expect(state.messages.at(-1)?.content).toBe('Segment two from the page. continued')
  })

  it('applies a higher-sequence finalizer from the same known generation', () => {
    const cachedOpen: ChatMessage = {
      role: 'streaming', content: 'Locally completed segment.', cls: 'msg msg-a',
      seq: 5, gen: 'g-finalizer-ordered',
    }
    const pageOpen: ChatMessage = {
      role: 'streaming', content: 'Stale page prefix.', cls: 'msg msg-a',
      seq: 4, gen: 'g-finalizer-ordered',
    }
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(
      staleDetail([cachedOpen]), 'seed-ordered-finalizer', SLOT,
    ))
    state = reducer(state, switchSlot.pending('ordered-finalizer', SLOT))
    state = reducer(state, sseChatMessage({ slot: SLOT, role: '_segment', content: '' }))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail([pageOpen]), 'ordered-finalizer', SLOT,
    ))

    expect(state.messages.map(message => [message.role, message.content])).toEqual([
      ['assistant', 'Locally completed segment.'],
    ])
  })

  it('preserves a known-generation page when the finalizer generation is missing', () => {
    const cachedOpen: ChatMessage = {
      role: 'streaming', content: 'Unvouched local segment.', cls: 'msg msg-a',
      seq: 5,
    }
    const pageOpen: ChatMessage = {
      role: 'streaming', content: 'Known process segment.', cls: 'msg msg-a',
      seq: 4, gen: 'g-page-known',
    }
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(
      staleDetail([cachedOpen]), 'seed-missing-finalizer-gen', SLOT,
    ))
    state = reducer(state, switchSlot.pending('missing-finalizer-gen', SLOT))
    state = reducer(state, sseChatMessage({ slot: SLOT, role: '_segment', content: '' }))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail([pageOpen]), 'missing-finalizer-gen', SLOT,
    ))

    expect(state.messages.map(message => [message.role, message.content])).toEqual([
      ['assistant', 'Unvouched local segment.'],
      ['streaming', 'Known process segment.'],
    ])
    expect(state.lastChunkGen).toBe('g-page-known')
    expect(state.lastChunkSeq).toBe(4)
  })

  it('preserves an open page when a rowless finalizer has no ordering evidence', () => {
    const pageOpen: ChatMessage = {
      role: 'streaming', content: 'Page-only open segment.', cls: 'msg msg-a',
      seq: 3, gen: 'g-rowless-unordered',
    }
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, switchSlot.pending('unordered-rowless', SLOT))
    state = reducer(state, sseChatMessage({ slot: SLOT, role: '_segment', content: '' }))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail([pageOpen]), 'unordered-rowless', SLOT,
    ))

    expect(state.messages).toEqual([pageOpen])
    expect(state.messages[0].role).toBe('streaming')
  })

  it('does not let a rowless boundary finalize segment two after exact-mid segment one', () => {
    const segmentOne: ChatMessage = {
      role: 'assistant', content: 'Segment one.', cls: 'msg msg-a',
      meta: { mid: 'm-exact-segment-one' },
    }
    const segmentTwo: ChatMessage = {
      role: 'streaming', content: 'Segment two remains open.', cls: 'msg msg-a',
      seq: 1, gen: 'g-exact-mid-rowless',
    }
    let state = {
      ...reducer(undefined, { type: '@@INIT' }),
      activeSlot: SLOT,
      lastChunkSeq: 2,
      lastChunkGen: 'g-exact-mid-rowless',
    }
    state = reducer(state, switchSlot.pending('exact-mid-rowless', SLOT))
    state = reducer(state, sseChatMessage({
      slot: SLOT, role: 'assistant', content: 'Segment one.',
      meta: { mid: 'm-exact-segment-one' },
    }))
    state = reducer(state, sseChatMessage({ slot: SLOT, role: '_segment', content: '' }))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail([segmentOne, segmentTwo]), 'exact-mid-rowless', SLOT,
    ))

    expect(state.messages).toEqual([segmentOne, segmentTwo])
    expect(state.messages[1].role).toBe('streaming')
  })

  it('retains an unordered anchor-matched assistant before the fetched open row', () => {
    const anchor: ChatMessage = {
      role: 'user', content: 'Continue this anchored turn.', cls: 'msg msg-u',
      meta: { mid: 'm-unordered-anchor', sendId: 's-unordered-anchor' },
    }
    const cachedOpen: ChatMessage = {
      role: 'streaming', content: 'Local process completion.', cls: 'msg msg-a',
      seq: 7, gen: 'g-anchor-local',
    }
    const pageOpen: ChatMessage = {
      role: 'streaming', content: 'Fetched process continuation.', cls: 'msg msg-a',
      seq: 2, gen: 'g-anchor-page',
    }
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(
      staleDetail([anchor, cachedOpen]), 'seed-anchor-finalizer', SLOT,
    ))
    state = reducer(state, switchSlot.pending('anchor-finalizer', SLOT))
    state = reducer(state, sseChatMessage({ slot: SLOT, role: '_segment', content: '' }))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail([anchor, pageOpen]), 'anchor-finalizer', SLOT,
    ))

    expect(state.messages.map(message => [message.role, message.content])).toEqual([
      ['user', 'Continue this anchored turn.'],
      ['assistant', 'Local process completion.'],
      ['streaming', 'Fetched process continuation.'],
    ])
    state = reducer(state, sseChatMessage({
      slot: SLOT, role: 'chunk', content: ' Next page chunk.', seq: 3, gen: 'g-anchor-page',
    }))
    expect(state.messages.at(-1)).toEqual(expect.objectContaining({
      role: 'streaming',
      content: 'Fetched process continuation. Next page chunk.',
    }))
  })

  it('keeps a genuinely different observed page generation authoritative', () => {
    const cachedOpen: ChatMessage = {
      role: 'streaming', content: 'Old process segment.', cls: 'msg msg-a',
      seq: 9, gen: 'g-finalizer-old',
    }
    const pageOpen: ChatMessage = {
      role: 'streaming', content: 'New process segment.', cls: 'msg msg-a',
      seq: 1, gen: 'g-finalizer-new',
    }
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(
      staleDetail([cachedOpen]), 'seed-new-generation-finalizer', SLOT,
    ))
    state = reducer(state, switchSlot.pending('new-generation-finalizer', SLOT))
    state = reducer(state, sseChatMessage({ slot: SLOT, role: '_segment', content: '' }))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail([pageOpen]), 'new-generation-finalizer', SLOT,
    ))

    expect(state.messages.map(message => [message.role, message.content])).toEqual([
      ['assistant', 'Old process segment.'],
      ['streaming', 'New process segment.'],
    ])
    expect(state.lastChunkGen).toBe('g-finalizer-new')
    expect(state.lastChunkSeq).toBe(1)
  })

  it('bounds finalizer claim evidence to one switch page', () => {
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, switchSlot.pending('bounded-finalizers', SLOT))
    for (let i = 1; i <= OLDER_PAGE_LIMIT + 2; i++) {
      state = reducer(state, sseChatMessage({
        slot: SLOT, role: 'chunk', content: `segment-${i}`, seq: i, gen: 'g-bounded',
      }))
      state = reducer(state, sseChatMessage({ slot: SLOT, role: '_segment', content: '' }))
    }

    const claim = state.slotSwitchChunkClaim
    expect(claim?.clientTs).toHaveLength(OLDER_PAGE_LIMIT)
    expect(claim?.finalizers).toHaveLength(OLDER_PAGE_LIMIT)
    expect(claim?.finalizers.map(item => item.clientTs)).toEqual(claim?.clientTs)
    expect(JSON.parse(JSON.stringify(claim))).toEqual(claim)
  })

  it('does not retain an unconfirmed optimistic row', () => {
    const stalePage = staleRunningMessages()

    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(staleDetail(stalePage), 'seed', SLOT))
    state = reducer(state, appendMessage({
      role: 'user',
      content: 'Still awaiting confirmation.',
      cls: 'msg msg-u',
      ts: '2026-09-25T23:43:34.022507+00:00',
      meta: { optimistic: true, sendId: 's-unconfirmed' },
    }))

    state = reducer(state, refreshSlot.fulfilled(staleDetail(stalePage), 'stale', SLOT))

    expect(state.messages.some(message => message.meta?.sendId === 's-unconfirmed')).toBe(false)
  })
})

describe('assistant system notices are not reply segments', () => {
  it('keeps a stuck-turn notice after the open partial during switch reconciliation', () => {
    const anchor: ChatMessage = {
      role: 'user', content: 'Keep the open reply.', cls: 'msg msg-u',
      meta: { mid: 'm-notice-anchor', sendId: 's-notice-anchor' },
    }
    const open: ChatMessage = {
      role: 'streaming', content: 'Visible partial output.', cls: 'msg msg-a',
      seq: 4, gen: 'g-notice',
    }
    const notice: ChatMessage = {
      role: 'assistant', content: 'The turn appears stuck.', cls: 'msg msg-a',
      meta: { mid: 'm-stuck-meta', kind: 'compaction', notice: 'stuck_turn' },
    }
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, switchSlot.pending('notice-after-partial', SLOT))

    state = reducer(state, switchSlot.fulfilled(
      staleDetail([anchor, open, notice]), 'notice-after-partial', SLOT,
    ))

    expect(state.messages.map(message => [message.role, message.content])).toEqual([
      ['user', anchor.content],
      ['streaming', open.content],
      ['assistant', notice.content],
    ])
  })
})

/* The UNCLAIMED fallback: replies that streamed and finalized while the slot was
 * in the background (`applyNonActiveFrame`, so each cached row carries only a
 * `clientTs`, never a `mid`), with no live frame between the switch's `pending`
 * and its `fulfilled`. The HTTP page can predate them, or be fresher than them.
 * Every local reply row reconciles by ONE exact identity: the nearest anchor row
 * both arrays identify (a user send, a dispatched inject, a previously fetched
 * assistant) plus the segment ordinal after it. A row is dropped only when the
 * page positively holds that key; it is kept when the page provably predates it
 * (the page's range reaches above the row and lacks it). Content and timestamps
 * are never compared. */
describe('unclaimed switch-back fallback keeps background-finalized replies', () => {
  const idle = (messages: ChatMessage[]) => ({ ...staleDetail(messages), running: false })

  const priorUser: ChatMessage = {
    role: 'user', content: 'Set up the nightly digest.', cls: 'msg msg-u',
    ts: '2026-10-01T08:00:00.000000+00:00',
    meta: { mid: 'm-digest-user', sendId: 's-digest-user' },
  }
  const priorAssistant: ChatMessage = {
    role: 'assistant', content: 'Digest scheduled.', cls: 'msg msg-a',
    ts: '2026-10-01T08:00:05.000000+00:00',
    meta: { mid: 'm-digest-assistant' },
  }
  const cronInject: ChatMessage = {
    role: 'inject', content: '[Cron notification from "digest"]', cls: 'msg msg-inject',
    ts: '2026-10-02T09:00:00.000000+00:00',
    meta: { mid: 'm-digest-inject', injectKind: 'cron' },
  }
  const noteInject: ChatMessage = {
    role: 'inject', content: 'Remember the deadline.', cls: 'msg msg-inject reconcile-note',
    ts: '2026-10-02T09:00:01.000000+00:00',
    meta: { mid: 'm-digest-note', noteSession: 'note-1' },
  }
  const rendered = (state: ChatState) => state.messages.map(message => [message.role, message.content])

  /** Background the slot, stream and finalize `replies` there as consecutive
   *  segments (`_segment` between, `_done` last), then open the switch back.
   *  Returns the state while the fetch is still in flight. */
  const backgroundReplies = (seed: ChatMessage[], replies: string[], gen = 'g-background'): ChatState => {
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(staleDetail(seed), 'seed-background', SLOT))
    state = reducer(state, switchSlot.pending('away-background', 'chat-other'))
    replies.forEach((reply, i) => {
      state = reducer(state, sseChatMessage({ slot: SLOT, role: 'chunk', content: reply, seq: 11 + i, gen }))
      state = reducer(state, sseChatMessage({ slot: SLOT, role: i === replies.length - 1 ? '_done' : '_segment', content: '' }))
    })
    const cached = state.slotMessages[SLOT].slice(-replies.length)
    expect(cached.map(message => [message.role, message.content, message.meta?.mid]))
      .toEqual(replies.map(reply => ['assistant', reply, undefined]))
    state = reducer(state, switchSlot.pending('back-background', SLOT))
    expect(state.slotSwitchChunkClaim?.clientTs).toEqual([])
    return state
  }

  describe('cron / auto-nudge inject opener', () => {
    const reply = 'Nothing new in the digest today.'

    it('keeps the reply over a stale finalized page that ends at the previous turn', () => {
      // The page predates the inject row and its reply. The page holds the user
      // row before the reply, so its range reaches above it and lacks it.
      let state = backgroundReplies([priorUser, priorAssistant, cronInject], [reply])
      state = reducer(state, switchSlot.fulfilled(idle([priorUser, priorAssistant]), 'back-background', SLOT))
      expect(rendered(state)).toEqual([
        ['user', 'Set up the nightly digest.'],
        ['assistant', 'Digest scheduled.'],
        ['assistant', reply],
      ])
    })

    it('keeps the reply over a stale finalized page that holds the inject but not the reply', () => {
      let state = backgroundReplies([priorUser, priorAssistant, cronInject], [reply])
      state = reducer(state, switchSlot.fulfilled(
        idle([priorUser, priorAssistant, cronInject]), 'back-background', SLOT,
      ))
      expect(rendered(state)).toEqual([
        ['user', 'Set up the nightly digest.'],
        ['assistant', 'Digest scheduled.'],
        ['inject', '[Cron notification from "digest"]'],
        ['assistant', reply],
      ])
      expect(state.slotMessages[SLOT].at(-1)).toEqual(expect.objectContaining({ role: 'assistant', content: reply }))
    })

    it('does not duplicate the reply a fresh page holds under its server mid', () => {
      // Same local shape; the page is fresh. The inject row anchors the reply on
      // both sides (ordinal 1 after it), so the server copy is recognised.
      const canonical: ChatMessage = {
        role: 'assistant', content: reply, cls: 'msg msg-a',
        ts: '2026-10-02T09:00:04.000000+00:00', meta: { mid: 'm-digest-reply' },
      }
      let state = backgroundReplies([priorUser, priorAssistant, cronInject], [reply])
      state = reducer(state, switchSlot.fulfilled(
        idle([priorUser, priorAssistant, cronInject, canonical]), 'back-background', SLOT,
      ))
      expect(rendered(state)).toEqual([
        ['user', 'Set up the nightly digest.'],
        ['assistant', 'Digest scheduled.'],
        ['inject', '[Cron notification from "digest"]'],
        ['assistant', reply],
      ])
      expect(state.messages.at(-1)?.meta?.mid).toBe('m-digest-reply')
    })

    it('does not let a passive note inject anchor a segment', () => {
      // The note sits between the opener and the reply on both sides, yet the
      // reply still keys off the dispatched inject: a stale page holding the
      // note but not the reply keeps the reply, and a fresh page is recognised.
      let state = backgroundReplies([priorUser, priorAssistant, cronInject, noteInject], [reply])
      const stale = reducer(state, switchSlot.fulfilled(
        idle([priorUser, priorAssistant, cronInject, noteInject]), 'back-background', SLOT,
      ))
      expect(rendered(stale).at(-1)).toEqual(['assistant', reply])
      expect(rendered(stale)).toHaveLength(5)

      // Fresh page in which the note was flushed AFTER the reply (client-first
      // append, deferred persistence): the note's own position differs, and a
      // note anchor would have read the reply as a different segment.
      const canonical: ChatMessage = {
        role: 'assistant', content: reply, cls: 'msg msg-a',
        ts: '2026-10-02T09:00:04.000000+00:00', meta: { mid: 'm-digest-reply' },
      }
      state = backgroundReplies([priorUser, priorAssistant, cronInject, noteInject], [reply])
      const fresh = reducer(state, switchSlot.fulfilled(
        idle([priorUser, priorAssistant, cronInject, canonical, noteInject]), 'back-background', SLOT,
      ))
      expect(fresh.messages.filter(message => message.content === reply)).toEqual([canonical])
    })
  })

  describe('user opener, several segments', () => {
    const segments = ['First half done.', 'Second half done.', 'Third half done.']
    const canonical = (i: number): ChatMessage => ({
      role: 'assistant', content: segments[i], cls: 'msg msg-a',
      ts: `2026-10-02T09:00:0${i + 2}.000000+00:00`, meta: { mid: `m-segment-${i + 1}` },
    })

    it('keeps segment 3 when the stale page ends after segment 2', () => {
      let state = backgroundReplies([priorUser], segments)
      state = reducer(state, switchSlot.fulfilled(
        idle([priorUser, canonical(0), canonical(1)]), 'back-background', SLOT,
      ))
      expect(rendered(state)).toEqual([
        ['user', 'Set up the nightly digest.'],
        ['assistant', segments[0]],
        ['assistant', segments[1]],
        ['assistant', segments[2]],
      ])
      // Segments 1 and 2 are the page's canonical copies; 3 is the local one.
      expect(state.messages.slice(1).map(message => message.meta?.mid))
        .toEqual(['m-segment-1', 'm-segment-2', undefined])
    })

    it('keeps segments 2 and 3 when the stale page ends after segment 1', () => {
      let state = backgroundReplies([priorUser], segments)
      state = reducer(state, switchSlot.fulfilled(idle([priorUser, canonical(0)]), 'back-background', SLOT))
      expect(rendered(state).slice(1)).toEqual(segments.map(segment => ['assistant', segment]))
    })

    it('does not duplicate any segment of a fresh same-segment canonical page', () => {
      let state = backgroundReplies([priorUser], segments)
      state = reducer(state, switchSlot.fulfilled(
        idle([priorUser, canonical(0), canonical(1), canonical(2)]), 'back-background', SLOT,
      ))
      expect(rendered(state).slice(1)).toEqual(segments.map(segment => ['assistant', segment]))
      expect(state.messages.slice(1).map(message => message.meta?.mid))
        .toEqual(['m-segment-1', 'm-segment-2', 'm-segment-3'])
    })

    it('does not duplicate the reply a redacted canonical copy replaced', () => {
      // Positive same-segment proof with different bytes: the page's copy is
      // server-redacted. Still ordinal 1 after the shared user anchor.
      const reply = 'The token is raw before server redaction.'
      const redacted: ChatMessage = {
        role: 'assistant', content: 'The token is [REDACTED: credential].', cls: 'msg msg-a',
        ts: '2026-10-02T09:00:04.000000+00:00', meta: { mid: 'm-redacted-background' },
      }
      let state = backgroundReplies([priorUser], [reply])
      state = reducer(state, switchSlot.fulfilled(idle([priorUser, redacted]), 'back-background', SLOT))
      expect(rendered(state)).toEqual([
        ['user', 'Set up the nightly digest.'],
        ['assistant', 'The token is [REDACTED: credential].'],
      ])
    })
  })

  describe('bounded page beginning mid-turn', () => {
    it('keys off the shared fetched assistant that opens the window', () => {
      // The cache and the page both begin at a previously fetched assistant
      // row; no user or inject row is in either. That shared row anchors the
      // background segment after it on both sides.
      const opener: ChatMessage = {
        role: 'assistant', content: 'Earlier segment of a long turn.', cls: 'msg msg-a',
        ts: '2026-10-02T09:00:00.000000+00:00', meta: { mid: 'm-window-opener' },
      }
      const reply = 'The long turn concludes.'
      let state = backgroundReplies([opener], [reply])
      const stale = reducer(state, switchSlot.fulfilled(
        { ...staleDetail([opener]), hasMore: true }, 'back-background', SLOT,
      ))
      expect(rendered(stale)).toEqual([
        ['assistant', 'Earlier segment of a long turn.'],
        ['assistant', reply],
      ])

      const canonical: ChatMessage = {
        role: 'assistant', content: reply, cls: 'msg msg-a',
        ts: '2026-10-02T09:00:04.000000+00:00', meta: { mid: 'm-window-reply' },
      }
      state = backgroundReplies([opener], [reply])
      const fresh = reducer(state, switchSlot.fulfilled(
        { ...staleDetail([opener, canonical]), running: false, hasMore: true }, 'back-background', SLOT,
      ))
      expect(fresh.messages.map(message => message.meta?.mid)).toEqual(['m-window-opener', 'm-window-reply'])
    })

    it('leaves a window that shares no identity with the cache authoritative', () => {
      // Nothing places the local rows against this page: no row before the
      // reply is held by the page, and the page is not the whole transcript.
      // The page keeps its authority, as coverage and the retained head do.
      const reply = 'Unplaceable reply.'
      let state = backgroundReplies([priorUser], [reply])
      const other: ChatMessage = {
        role: 'assistant', content: 'A different window.', cls: 'msg msg-a',
        ts: '2026-10-02T09:00:04.000000+00:00', meta: { mid: 'm-other-window' },
      }
      state = reducer(state, switchSlot.fulfilled(
        { ...staleDetail([other]), running: false, hasMore: true }, 'back-background', SLOT,
      ))
      expect(rendered(state)).toEqual([['assistant', 'A different window.']])
    })
  })

  describe('open page', () => {
    const reply = 'Digest complete: three items.'

    it('converges a stale open page onto the one completed local segment', () => {
      // The page's disk image still projects the reply as a partial `streaming`
      // row and reports running. Same anchor, same ordinal, same generation:
      // the completed local copy replaces the stale partial. One segment.
      const pageOpen: ChatMessage = {
        role: 'streaming', content: 'Digest complete:', cls: 'msg msg-a', seq: 9, gen: 'g-background',
      }
      let state = backgroundReplies([priorUser], [reply])
      state = reducer(state, switchSlot.fulfilled(staleDetail([priorUser, pageOpen]), 'back-background', SLOT))
      expect(rendered(state)).toEqual([
        ['user', 'Set up the nightly digest.'],
        ['assistant', reply],
      ])
      expect(state.slotMessages[SLOT].filter(message => message.content.startsWith('Digest complete'))).toHaveLength(1)
    })

    it('converges the open copy of segment 2 and keeps segment 3', () => {
      const pageOpen: ChatMessage = {
        role: 'streaming', content: 'Second half', cls: 'msg msg-a', seq: 12, gen: 'g-background',
      }
      const segmentOne: ChatMessage = {
        role: 'assistant', content: 'First half done.', cls: 'msg msg-a',
        ts: '2026-10-02T09:00:02.000000+00:00', meta: { mid: 'm-segment-1' },
      }
      let state = backgroundReplies([priorUser], ['First half done.', 'Second half done.', 'Third half done.'])
      state = reducer(state, switchSlot.fulfilled(
        staleDetail([priorUser, segmentOne, pageOpen]), 'back-background', SLOT,
      ))
      expect(rendered(state)).toEqual([
        ['user', 'Set up the nightly digest.'],
        ['assistant', 'First half done.'],
        ['assistant', 'Second half done.'],
        ['assistant', 'Third half done.'],
      ])
    })

    it('retains the completed segment before an open copy from another gateway generation', () => {
      // A different generation is another gateway process: its open row is not
      // provably the same stream, so the unordered rule of the claimed path
      // applies and both are kept, the local one first.
      const pageOpen: ChatMessage = {
        role: 'streaming', content: 'Digest complete:', cls: 'msg msg-a', seq: 1, gen: 'g-restarted',
      }
      let state = backgroundReplies([priorUser], [reply])
      state = reducer(state, switchSlot.fulfilled(staleDetail([priorUser, pageOpen]), 'back-background', SLOT))
      expect(rendered(state)).toEqual([
        ['user', 'Set up the nightly digest.'],
        ['assistant', reply],
        ['streaming', 'Digest complete:'],
      ])
      expect(state.lastChunkGen).toBe('g-restarted')
    })

    it('leaves a genuinely newer open page untouched and continues it', () => {
      // The page is NEWER: it finalized the reply under the same anchor AND
      // has a later turn open. The local copy is consumed; nothing is inserted
      // over or before the open row, which the next live chunk continues.
      const canonical: ChatMessage = {
        role: 'assistant', content: reply, cls: 'msg msg-a',
        ts: '2026-10-02T09:00:04.000000+00:00', meta: { mid: 'm-digest-reply' },
      }
      const laterUser: ChatMessage = {
        role: 'user', content: 'Now summarize the third item.', cls: 'msg msg-u',
        ts: '2026-10-02T09:01:00.000000+00:00', meta: { mid: 'm-third-user', sendId: 's-third-user' },
      }
      const laterOpen: ChatMessage = {
        role: 'streaming', content: 'The third item is', cls: 'msg msg-a', seq: 14, gen: 'g-background',
      }
      let state = backgroundReplies([priorUser], [reply])
      state = reducer(state, switchSlot.fulfilled(
        staleDetail([priorUser, canonical, laterUser, laterOpen]), 'back-background', SLOT,
      ))
      expect(rendered(state)).toEqual([
        ['user', 'Set up the nightly digest.'],
        ['assistant', reply],
        ['user', 'Now summarize the third item.'],
        ['streaming', 'The third item is'],
      ])
      expect(state.messages[1].meta?.mid).toBe('m-digest-reply')
      state = reducer(state, sseChatMessage({
        slot: SLOT, role: 'chunk', content: ' a reminder.', seq: 15, gen: 'g-background',
      }))
      expect(state.messages.at(-1)).toEqual(expect.objectContaining({
        role: 'streaming', content: 'The third item is a reminder.',
      }))
    })

    it('leaves a newer open page untouched when the local reply is its own earlier segment', () => {
      // Ordinals, not just the shared opener: the page has segment 1 finalized
      // and segment 2 open under the SAME user anchor. The local finalized
      // segment 1 matches the finalized copy, never the open segment 2.
      const canonical: ChatMessage = {
        role: 'assistant', content: reply, cls: 'msg msg-a',
        ts: '2026-10-02T09:00:04.000000+00:00', meta: { mid: 'm-digest-reply' },
      }
      const segmentTwoOpen: ChatMessage = {
        role: 'streaming', content: 'And one more thing', cls: 'msg msg-a', seq: 14, gen: 'g-background',
      }
      let state = backgroundReplies([priorUser], [reply])
      state = reducer(state, switchSlot.fulfilled(
        staleDetail([priorUser, canonical, segmentTwoOpen]), 'back-background', SLOT,
      ))
      expect(rendered(state)).toEqual([
        ['user', 'Set up the nightly digest.'],
        ['assistant', reply],
        ['streaming', 'And one more thing'],
      ])
      expect(state.messages[1].meta?.mid).toBe('m-digest-reply')
    })
  })
})

/* A remote REWIND: rows removed server-side between the read that set the
 * retained comparable count and this switch. The generalized fallback must not
 * re-attach them, and the proof is the warm-cache invariant from slotRefresh.ts:
 * a fall in the server's own comparable count. The baseline is the one THIS
 * request was dispatched against (captured on the request-owned claim at
 * `pending`), never a later global value. */
describe('switch-back fallback against a remote rewind', () => {
  const idle = (messages: ChatMessage[]) => ({ ...staleDetail(messages), running: false })
  const anchor: ChatMessage = {
    role: 'user', content: 'Anchor turn.', cls: 'msg msg-u',
    ts: '2026-10-02T10:00:00.000000+00:00', meta: { mid: 'm1', sendId: 's1' },
  }
  const forward1: ChatMessage = {
    role: 'assistant', content: 'Forward one.', cls: 'msg msg-a',
    ts: '2026-10-02T10:00:01.000000+00:00', meta: { mid: 'm2' },
  }
  const forward2: ChatMessage = {
    role: 'assistant', content: 'Forward two.', cls: 'msg msg-a',
    ts: '2026-10-02T10:00:02.000000+00:00', meta: { mid: 'm3' },
  }
  const rendered = (state: ChatState) => state.messages.map(message => [message.role, message.content])

  /** Seed the cached transcript through an idle read so the comparable count is
   *  retained (`retainServerTotal`), then open the switch. */
  const seeded = (seed: ChatMessage[], retain = true): ChatState => {
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(
      retain ? idle(seed) : { ...idle(seed), total: undefined as unknown as number }, 'seed-rewind', SLOT,
    ))
    expect(state.slotServerTotal?.[SLOT]).toBe(retain ? seed.length : undefined)
    return reducer(state, switchSlot.pending('switch-rewind', SLOT))
  }

  const claimFinalizer = (state: ChatState, content = 'Claimed finalizer.'): ChatState => {
    let next = reducer(state, sseChatMessage({
      slot: SLOT, role: 'chunk', content, seq: 9, gen: 'g-live',
    }))
    next = reducer(next, sseChatMessage({ slot: SLOT, role: '_done', content: '' }))
    expect(next.slotSwitchChunkClaim?.clientTs).toHaveLength(1)
    return next
  }

  const refreshPage = (
    key: string,
    messages: ChatMessage[],
    issueSeq: number,
    extra: Record<string, unknown> = {},
  ) => ({ ...idle(messages), key, refreshSeq: issueSeq, ...extra })

  const issueRefresh = (state: ChatState, key: string, requestId: string) => {
    const next = reducer(state, refreshSlot.pending(requestId, key))
    const issue = next.refreshIssueByRequest[requestId]
    expect(issue).toEqual(expect.objectContaining({ key }))
    return { state: next, issueSeq: issue.issueSeq }
  }

  const applyRefresh = (
    state: ChatState,
    key: string,
    messages: ChatMessage[],
    requestId: string,
    extra: Record<string, unknown> = {},
  ): ChatState => {
    const issued = issueRefresh(state, key, requestId)
    return reducer(issued.state, refreshSlot.fulfilled(
      refreshPage(key, messages, issued.issueSeq, extra) as never,
      requestId,
      key,
    ))
  }

  const refreshBaseline = (state: ChatState): number | undefined =>
    state.slotSwitchChunkClaim?.refreshIssuedSeq

  it('lets a newer switch supersede a refresh issued before switch pending', () => {
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    const issued = issueRefresh(state, SLOT, 'refresh-pre-switch')
    state = issued.state
    state = reducer(state, switchSlot.pending('switch-after-refresh', SLOT))
    expect(refreshBaseline(state)).toBe(issued.issueSeq)
    state = reducer(state, refreshSlot.fulfilled(
      refreshPage(SLOT, [anchor], issued.issueSeq) as never,
      'refresh-pre-switch',
      SLOT,
    ))
    expect(state.messages).toEqual([anchor])
    expect(state.slotCursorKey).toBeNull()

    state = reducer(state, switchSlot.fulfilled(
      idle([anchor, forward1]), 'switch-after-refresh', SLOT,
    ))

    expect(state.messages).toEqual([anchor, forward1])
    expect(state.slotSwitchChunkClaim?.settled).toBe(true)
  })

  it('keeps the newest-issued refresh when two refreshes settle out of order', () => {
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    const older = issueRefresh(state, SLOT, 'refresh-older')
    const newer = issueRefresh(older.state, SLOT, 'refresh-newer')
    state = reducer(newer.state, refreshSlot.fulfilled(
      refreshPage(SLOT, [anchor, forward1], newer.issueSeq) as never,
      'refresh-newer',
      SLOT,
    ))
    const newestMessages = state.messages

    state = reducer(state, refreshSlot.fulfilled(
      refreshPage(SLOT, [anchor], older.issueSeq) as never,
      'refresh-older',
      SLOT,
    ))

    expect(state.messages).toBe(newestMessages)
    expect(state.messages).toEqual([anchor, forward1])
    expect(state.refreshIssuedSeq[SLOT]).toBe(newer.issueSeq)
    expect(state.refreshAppliedSeq[SLOT]).toBe(newer.issueSeq)
    expect(state.refreshIssueByRequest['refresh-older']).toBeUndefined()
    expect(state.refreshIssueByRequest['refresh-newer']).toBeUndefined()
  })

  it('cleans a rejected refresh without superseding its switch', () => {
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, switchSlot.pending('switch-before-rejection', SLOT))
    const issued = issueRefresh(state, SLOT, 'refresh-rejected')
    state = reducer(issued.state, refreshSlot.rejected(
      new Error('refresh failed'), 'refresh-rejected', SLOT,
    ))
    expect(state.refreshIssueByRequest['refresh-rejected']).toBeUndefined()
    expect(state.refreshIssuedSeq[SLOT]).toBe(issued.issueSeq)
    expect(state.refreshAppliedSeq[SLOT]).toBeUndefined()

    state = reducer(state, switchSlot.fulfilled(
      idle([anchor, forward1]), 'switch-before-rejection', SLOT,
    ))

    expect(state.messages).toEqual([anchor, forward1])
    expect(state.slotSwitchChunkClaim?.settled).toBe(true)
  })

  it('keeps a newer refresh authoritative when an older cached switch settles', () => {
    const refreshedOpen: ChatMessage = {
      role: 'streaming', content: 'Canonical refreshed partial.', cls: 'msg msg-a',
      seq: 12, gen: 'g-refresh-order',
    }
    let state = seeded([anchor, forward1, forward2])
    expect(refreshBaseline(state)).toBe(state.refreshIssuedSeq[SLOT])
    state = applyRefresh(
      state,
      SLOT,
      [anchor, refreshedOpen],
      'refresh-newer',
      { running: true, boundedRead: true, total: 2, hasMore: true, nextBefore: 11 },
    )
    const messagesAfterRefresh = state.messages
    const authoritative = {
      cursorKey: state.slotCursorKey,
      hasMore: state.slotHasMore,
      oldest: state.slotOldestIndex,
      total: state.slotServerTotal?.[SLOT],
      loading: state.slotLoading,
      running: state.slotRunning,
      state: state.slotState,
      seq: state.lastChunkSeq,
      gen: state.lastChunkGen,
      refresh: state.refreshAppliedSeq[SLOT],
    }
    expect(authoritative).toMatchObject({
      cursorKey: SLOT,
      hasMore: true,
      oldest: 11,
    })

    state = reducer(state, switchSlot.fulfilled(idle([anchor]), 'switch-rewind', SLOT))

    expect(state.messages).toBe(messagesAfterRefresh)
    expect({
      cursorKey: state.slotCursorKey,
      hasMore: state.slotHasMore,
      oldest: state.slotOldestIndex,
      total: state.slotServerTotal?.[SLOT],
      loading: state.slotLoading,
      running: state.slotRunning,
      state: state.slotState,
      seq: state.lastChunkSeq,
      gen: state.lastChunkGen,
      refresh: state.refreshAppliedSeq[SLOT],
    }).toEqual(authoritative)
    expect(state.slotSwitchChunkClaim?.settled).toBe(true)
    expect(state.slotSwitchRequestId).toBeNull()
    expect(state.slotSwitchTarget).toBeNull()
  })

  it('keeps a newer refresh authoritative when an older cached switch rejects', () => {
    const refreshedOpen: ChatMessage = {
      role: 'streaming', content: 'Canonical refreshed partial.', cls: 'msg msg-a',
      seq: 12, gen: 'g-refresh-reject-order',
    }
    let state = seeded([anchor, forward1, forward2])
    state = applyRefresh(
      state,
      SLOT,
      [anchor, refreshedOpen],
      'refresh-before-rejection',
      { running: true, boundedRead: true, total: 2, hasMore: true, nextBefore: 11 },
    )
    const messagesAfterRefresh = state.messages
    expect({
      cursorKey: state.slotCursorKey,
      hasMore: state.slotHasMore,
      oldest: state.slotOldestIndex,
    }).toEqual({ cursorKey: SLOT, hasMore: true, oldest: 11 })

    state = reducer(state, switchSlot.rejected(
      new Error('older switch failed'), 'switch-rewind', SLOT,
    ))

    expect(state.messages).toBe(messagesAfterRefresh)
    expect(state.messages).toEqual([anchor, refreshedOpen])
    expect(state.slotCursorKey).toBe(SLOT)
    expect(state.slotHasMore).toBe(true)
    expect(state.slotOldestIndex).toBe(11)
    expect(state.slotSwitchChunkClaim?.settled).toBe(true)
    expect(state.slotSwitchRequestId).toBeNull()
    expect(state.slotSwitchTarget).toBeNull()
    expect(state.slotLoading).toBe(false)
  })

  it('clears loading when a newer refresh supplies an uncached switch target', () => {
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot('other-slot'))
    state = reducer(state, switchSlot.pending('switch-uncached', SLOT))
    expect(state.slotLoading).toBe(true)
    state = applyRefresh(state, SLOT, [anchor], 'refresh-uncached')
    const messagesAfterRefresh = state.messages

    state = reducer(state, switchSlot.fulfilled(
      idle([forward1]), 'switch-uncached', SLOT,
    ))

    expect(state.messages).toBe(messagesAfterRefresh)
    expect(state.messages).toEqual([anchor])
    expect(state.slotLoading).toBe(false)
    expect(state.refreshAppliedSeq[SLOT]).toBe(state.refreshIssuedSeq[SLOT])
    expect(state.slotSwitchRequestId).toBeNull()
    expect(state.slotSwitchChunkClaim?.settled).toBe(true)
  })

  it('does not treat another slot\'s newer refresh as target authority', () => {
    const other = 'other-slot'
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, switchSlot.pending('switch-target', SLOT))
    expect(refreshBaseline(state)).toBe(0)
    state = reducer(state, setActiveSlot(other))
    state = applyRefresh(state, other, [forward1], 'refresh-other')
    state = reducer(state, setActiveSlot(SLOT))

    state = reducer(state, switchSlot.fulfilled(
      idle([anchor, forward2]), 'switch-target', SLOT,
    ))

    expect(state.messages).toContainEqual(anchor)
    expect(state.messages).toContainEqual(forward2)
    expect(state.refreshAppliedSeq[other]).toBe(state.refreshIssuedSeq[other])
    expect(state.refreshAppliedSeq[SLOT]).toBeUndefined()
    expect(state.slotSwitchChunkClaim?.settled).toBe(true)
  })

  it.each([false, true])(
    'lets the switch settle with an equal issued-refresh baseline (seeded=%s)',
    (seededRefresh) => {
      let state = reducer(undefined, { type: '@@INIT' })
      state = reducer(state, setActiveSlot(SLOT))
      if (seededRefresh) {
        state = applyRefresh(state, SLOT, [anchor], 'refresh-baseline')
      }
      state = reducer(state, switchSlot.pending('switch-equal', SLOT))
      expect(refreshBaseline(state)).toBe(state.refreshIssuedSeq[SLOT] ?? 0)

      state = reducer(state, switchSlot.fulfilled(
        idle([anchor, forward1]), 'switch-equal', SLOT,
      ))

      expect(state.messages).toEqual([anchor, forward1])
      expect(state.slotSwitchChunkClaim?.settled).toBe(true)
    },
  )

  it('does not let an old same-target settlement clear a newer switch baseline', () => {
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, switchSlot.pending('switch-old', SLOT))
    state = applyRefresh(state, SLOT, [anchor], 'refresh-between')
    state = reducer(state, switchSlot.pending('switch-new', SLOT))
    const newerClaim = state.slotSwitchChunkClaim
    expect(refreshBaseline(state)).toBe(state.refreshIssuedSeq[SLOT])

    state = reducer(state, switchSlot.fulfilled(idle([forward1]), 'switch-old', SLOT))

    expect(state.slotSwitchChunkClaim).toBe(newerClaim)
    expect(state.slotSwitchChunkClaim?.settled).toBe(false)
    expect(refreshBaseline(state)).toBe(state.refreshIssuedSeq[SLOT])
    expect(state.slotSwitchRequestId).toBe('switch-new')
    expect(state.slotSwitchTarget).toBe(SLOT)
    expect(state.messages).toEqual([anchor])
  })

  it('drops both unclaimed forward assistants on an idle whole-page 3->1 rewind', () => {
    let state = seeded([anchor, forward1, forward2])
    expect(state.slotSwitchChunkClaim?.serverTotal).toBe(3)
    state = reducer(state, switchSlot.fulfilled(idle([anchor]), 'switch-rewind', SLOT))
    expect(rendered(state)).toEqual([['user', 'Anchor turn.']])
    expect(state.slotMessages[SLOT]).toHaveLength(1)
    expect(state.slotServerTotal?.[SLOT]).toBe(1)
  })

  it('drops an identified cached reply replaced by a same-count whole page', () => {
    const replacement: ChatMessage = {
      role: 'assistant', content: 'Rewritten forward one.', cls: 'msg msg-a',
      ts: '2026-10-02T10:00:04.000000+00:00', meta: { mid: 'm-rewritten-forward' },
    }
    let state = seeded([anchor, forward1])
    expect(state.slotSwitchChunkClaim?.serverTotal).toBe(2)

    state = reducer(state, switchSlot.fulfilled(
      idle([anchor, replacement]), 'switch-rewind', SLOT,
    ))

    expect(rendered(state)).toEqual([
      ['user', 'Anchor turn.'], ['assistant', 'Rewritten forward one.'],
    ])
  })

  it('does not rescue a confirmed send removed by a running bounded switch rewind', () => {
    const removedPrompt: ChatMessage = {
      role: 'user', content: 'Remove this prompt.', cls: 'msg msg-u',
      ts: '2026-10-02T10:00:03.000000+00:00',
      meta: { mid: 'm-removed-prompt', sendId: 's-removed-prompt' },
    }
    let state = seeded([anchor, removedPrompt])

    state = reducer(state, switchSlot.fulfilled({
      ...staleDetail([anchor]), running: true, boundedRead: true, total: 1,
    }, 'switch-rewind', SLOT))

    expect(state.messages).toEqual([anchor])
    expect(state.slotMessages[SLOT]).toEqual([anchor])
  })

  it('does not rescue a confirmed send removed by a running bounded refresh rewind', () => {
    const removedPrompt: ChatMessage = {
      role: 'user', content: 'Remove this refresh prompt.', cls: 'msg msg-u',
      ts: '2026-10-02T10:00:03.000000+00:00',
      meta: { mid: 'm-refresh-removed', sendId: 's-refresh-removed' },
    }
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = applyRefresh(state, SLOT, [anchor, removedPrompt], 'refresh-seed')

    state = applyRefresh(state, SLOT, [anchor], 'refresh-rewind', {
      running: true, boundedRead: true, total: 1,
    })

    expect(state.messages).toEqual([anchor])
    expect(state.slotServerTotal[SLOT]).toBe(1)
  })

  it('still rescues a background finalized tail from a same-count stale page', () => {
    // Same baseline (3) and a stale page reporting the same count: no fall, so
    // the mid-less reply finalized in the background is kept.
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = reducer(state, refreshSlot.fulfilled(idle([anchor, forward1, forward2]), 'seed-same', SLOT))
    state = reducer(state, switchSlot.pending('away-same', 'chat-other'))
    state = reducer(state, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'Background tail.', seq: 5, gen: 'g-same' }))
    state = reducer(state, sseChatMessage({ slot: SLOT, role: '_done', content: '' }))
    state = reducer(state, switchSlot.pending('back-same', SLOT))
    expect(state.slotSwitchChunkClaim?.serverTotal).toBe(3)
    state = reducer(state, switchSlot.fulfilled(idle([anchor, forward1, forward2]), 'back-same', SLOT))
    expect(rendered(state)).toEqual([
      ['user', 'Anchor turn.'], ['assistant', 'Forward one.'], ['assistant', 'Forward two.'],
      ['assistant', 'Background tail.'],
    ])
  })

  it('declines without a retained baseline and keeps the rescue', () => {
    let state = seeded([anchor, forward1, forward2], false)
    expect(state.slotSwitchChunkClaim?.serverTotal).toBeUndefined()
    state = reducer(state, switchSlot.fulfilled(idle([anchor]), 'switch-rewind', SLOT))
    // No delta to read: the page is the whole transcript and lacks identified
    // local rows, so the staleness rule keeps them (decline, not guess).
    expect(rendered(state)).toEqual([
      ['user', 'Anchor turn.'], ['assistant', 'Forward one.'], ['assistant', 'Forward two.'],
    ])
  })

  it('does not read a running non-comparable total as shrink proof', () => {
    let state = seeded([anchor, forward1, forward2])
    // Running, not bounded, no comparableTotal: `retainServerTotal` would not
    // establish a baseline from it, so neither does the shrink check.
    state = reducer(state, switchSlot.fulfilled(
      { ...staleDetail([anchor]), running: true, total: 1 }, 'switch-rewind', SLOT,
    ))
    expect(rendered(state)).toEqual([
      ['user', 'Anchor turn.'], ['assistant', 'Forward one.'], ['assistant', 'Forward two.'],
    ])
    expect(state.slotServerTotal?.[SLOT]).toBe(3)
  })

  it('drops a request-claimed finalizer when a later comparable response proves a rewind', () => {
    let state = seeded([anchor, forward1, forward2])
    // The finalizer lands after switch dispatch, but the server can retry its
    // slot-detail snapshot after this and observe a later remote rewind.
    state = claimFinalizer(state, 'Finalized before rewind.')

    state = reducer(state, switchSlot.fulfilled(idle([anchor]), 'switch-rewind', SLOT))

    expect(rendered(state)).toEqual([['user', 'Anchor turn.']])
  })

  it('keeps a request-claimed finalizer when the comparable count is equal', () => {
    let state = claimFinalizer(seeded([anchor, forward1, forward2]))

    state = reducer(state, switchSlot.fulfilled(
      idle([anchor, forward1, forward2]), 'switch-rewind', SLOT,
    ))

    expect(rendered(state).at(-1)).toEqual(['assistant', 'Claimed finalizer.'])
  })

  it.each(['baseline', 'response'] as const)(
    'keeps a request-claimed finalizer when the %s count is absent',
    (missing) => {
      let state = claimFinalizer(seeded(
        [anchor, forward1, forward2], missing !== 'baseline',
      ))
      const response = missing === 'response'
        ? { ...idle([anchor]), total: undefined as unknown as number }
        : idle([anchor])

      state = reducer(state, switchSlot.fulfilled(response, 'switch-rewind', SLOT))

      expect(rendered(state).at(-1)).toEqual(['assistant', 'Claimed finalizer.'])
    },
  )

  it('keeps a request-claimed finalizer for a running non-comparable total', () => {
    let state = claimFinalizer(seeded([anchor, forward1, forward2]))

    state = reducer(state, switchSlot.fulfilled(
      { ...staleDetail([anchor]), running: true, total: 1 }, 'switch-rewind', SLOT,
    ))

    expect(rendered(state).at(-1)).toEqual(['assistant', 'Claimed finalizer.'])
    expect(state.slotServerTotal?.[SLOT]).toBe(3)
  })

  it('keeps a request-claimed finalizer when the comparable count rises', () => {
    const serverNewer: ChatMessage = {
      role: 'user', content: 'Server-side addition.', cls: 'msg msg-u',
      ts: '2026-10-02T10:00:03.000000+00:00', meta: { mid: 'm4', sendId: 's4' },
    }
    let state = claimFinalizer(seeded([anchor, forward1, forward2]))

    state = reducer(state, switchSlot.fulfilled(
      idle([anchor, forward1, forward2, serverNewer]), 'switch-rewind', SLOT,
    ))

    expect(rendered(state).filter(([, content]) => content === 'Claimed finalizer.'))
      .toEqual([['assistant', 'Claimed finalizer.']])
    expect(state.slotServerTotal?.[SLOT]).toBe(4)
  })

  it('uses the refresh dispatch baseline when a switch grows the global count mid-flight', () => {
    const confirmedPrompt: ChatMessage = {
      role: 'user', content: 'Confirmed after refresh dispatch.', cls: 'msg msg-u',
      ts: '2026-10-02T10:00:03.000000+00:00',
      meta: { mid: 'm-confirmed-after-refresh', sendId: 's-confirmed-after-refresh' },
    }
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    state = applyRefresh(state, SLOT, [anchor, forward1], 'refresh-baseline')
    const issued = issueRefresh(state, SLOT, 'refresh-before-switch-growth')
    expect(issued.state.refreshIssueByRequest['refresh-before-switch-growth'])
      .toEqual(expect.objectContaining({ serverTotal: 2 }))
    state = reducer(issued.state, switchSlot.pending('switch-growth', SLOT))
    state = reducer(state, switchSlot.fulfilled({
      ...staleDetail([anchor, forward1, confirmedPrompt]),
      running: true,
      boundedRead: true,
      total: 3,
    }, 'switch-growth', SLOT))
    expect(state.slotServerTotal[SLOT]).toBe(3)

    state = reducer(state, refreshSlot.fulfilled(
      refreshPage(SLOT, [anchor, forward1], issued.issueSeq, {
        running: true, boundedRead: true, total: 2,
      }) as never,
      'refresh-before-switch-growth',
      SLOT,
    ))

    expect(state.messages.filter(message => message.meta?.sendId === 's-confirmed-after-refresh'))
      .toEqual([expect.objectContaining({ content: 'Confirmed after refresh dispatch.' })])
  })

  it('does not let a superseded settlement consume another request\'s baseline', () => {
    let state = seeded([anchor, forward1, forward2])
    // Another idle response moves the GLOBAL baseline mid-flight (a refresh that
    // already saw the rewind), then a newer same-target switch replaces the claim
    // and captures that moved baseline as its own.
    state = reducer(state, refreshSlot.fulfilled(idle([anchor]), 'refresh-mid-flight', SLOT))
    expect(state.slotServerTotal?.[SLOT]).toBe(1)
    state = reducer(state, switchSlot.pending('switch-newer', SLOT))
    expect(state.slotSwitchChunkClaim?.serverTotal).toBe(1)
    const before = state.messages
    // The OLD request's settlement arrives: it owns no claim and must not read
    // the newer claim's (or the global) baseline; it is declined wholesale.
    state = reducer(state, switchSlot.fulfilled(idle([anchor]), 'switch-rewind', SLOT))
    expect(state.messages).toBe(before)
    expect(state.slotSwitchChunkClaim?.settled).toBe(false)
    // The live request settles against ITS baseline (1): same count, no shrink.
    state = reducer(state, switchSlot.fulfilled(idle([anchor]), 'switch-newer', SLOT))
    expect(rendered(state)).toEqual([['user', 'Anchor turn.']])
  })

  it('declines a refresh whose issue registration was evicted', () => {
    let state = reducer(undefined, { type: '@@INIT' })
    state = reducer(state, setActiveSlot(SLOT))
    const issued = issueRefresh(state, SLOT, 'refresh-before-eviction')
    const recreated = {
      ...issued.state,
      messages: [forward2],
      refreshIssueByRequest: {},
      refreshIssuedSeq: {},
      refreshAppliedSeq: {},
    }
    const before = recreated.messages

    const next = baseReducer(recreated, refreshSlot.fulfilled(
      refreshPage(SLOT, [anchor], issued.issueSeq) as never,
      'refresh-before-eviction',
      SLOT,
    ))

    expect(next.messages).toBe(before)
    expect(next.messages).toEqual([forward2])
  })

  it('declines a metadata-free refresh without a current issue registration', () => {
    const state = { ...reducer(undefined, { type: '@@INIT' }), activeSlot: SLOT, messages: [forward2] }
    const next = baseReducer(state, {
      type: refreshSlot.fulfilled.type,
      payload: refreshPage(SLOT, [anchor], 1),
      meta: { arg: SLOT },
    } as never)
    expect(next.messages).toBe(state.messages)
  })

  it('declines a switch fulfillment without a current same-target claim', () => {
    const state = { ...reducer(undefined, { type: '@@INIT' }), activeSlot: SLOT, messages: [anchor] }
    const next = baseReducer(state, switchSlot.fulfilled(
      idle([forward1]), 'switch-after-eviction', SLOT,
    ))
    expect(next.messages).toBe(state.messages)
  })

  it('declines a metadata-free switch without a current same-target claim', () => {
    const state = { ...reducer(undefined, { type: '@@INIT' }), activeSlot: SLOT, messages: [anchor] }
    const next = baseReducer(state, {
      type: switchSlot.fulfilled.type,
      payload: idle([forward1]),
      meta: { arg: SLOT },
    } as never)
    expect(next.messages).toBe(state.messages)
  })
})
