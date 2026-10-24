import { describe, it, expect, vi, beforeEach, onTestFinished } from 'vitest'
import { createTestStore } from './helpers'

const { mockSideOpen, mockSideTurn, mockSendChat, mockSideState } = vi.hoisted(() => ({
  mockSideOpen: vi.fn().mockResolvedValue({ ok: true, open: true, messages: 0, last_run_id: '', created_at: '' }),
  mockSideTurn: vi.fn().mockResolvedValue({ ok: true, run_id: 'r1', messages: 1 }),
  mockSendChat: vi.fn().mockResolvedValue({ ok: true }),
  mockSideState: vi.fn().mockResolvedValue({ open: false }),
}))

vi.mock('../api/client', () => ({
  api: new Proxy({ sideOpen: mockSideOpen, sideTurn: mockSideTurn, sendChat: mockSendChat, sideState: mockSideState }, {
    get: (t, prop) => {
      if (prop in t) return (t as Record<string, unknown>)[prop as string]
      return vi.fn().mockResolvedValue({})
    },
  }),
  SEARCH_MIN_CHARS: 2,
}))

import { interceptSlashCommand } from '../pages/chat/ChatInput'
import { ApiError, AcceptedBodyUnreadable } from '../api/apiError'
import { SEND_ABORT_MS } from '../chat-core/transport/sendTurn'
import { sideClose, sseSideQueue, sseSideResult } from '../store/chatSlice'
import { clearSideChatDrafts, readSideChatDraft } from '../chat-core/composer/sideChatDrafts'

const SLOT = 'test-slot-1'

describe('/side slash command interception', () => {
  let store: ReturnType<typeof createTestStore>

  beforeEach(() => {
    store = createTestStore()
    vi.clearAllMocks()
  })

  it('intercepts "/side" — opens side, switches activity tab, no sendChat', async () => {
    const result = await interceptSlashCommand('/side', SLOT, store.dispatch)
    expect(result.intercepted).toBe(true)
    expect(mockSideOpen).toHaveBeenCalledWith(SLOT)
    expect(mockSideTurn).not.toHaveBeenCalled()
    expect(mockSendChat).not.toHaveBeenCalled()
    expect(store.getState().chat.activityTab).toBe('side')
  })

  it('intercepts "/side <message>" and forwards body to sideTurn', async () => {
    const result = await interceptSlashCommand('/side what model is this', SLOT, store.dispatch)
    expect(result.intercepted).toBe(true)
    expect(mockSideTurn).toHaveBeenCalledWith(SLOT, 'what model is this', undefined)
    expect(mockSendChat).not.toHaveBeenCalled()
  })

  // `/btw` is a pure alias of `/side` — same regex, same handling. Pinned as its
  // own cases so a future SIDE_RE edit cannot silently drop the alias.
  it('intercepts "/btw" exactly like "/side"', async () => {
    const result = await interceptSlashCommand('/btw', SLOT, store.dispatch)
    expect(result.intercepted).toBe(true)
    expect(mockSideOpen).toHaveBeenCalledWith(SLOT)
    expect(mockSideTurn).not.toHaveBeenCalled()
    expect(mockSendChat).not.toHaveBeenCalled()
    expect(store.getState().chat.activityTab).toBe('side')
  })

  it('intercepts "/btw <message>" and forwards body to sideTurn', async () => {
    const result = await interceptSlashCommand('/btw is this cached', SLOT, store.dispatch)
    expect(result.intercepted).toBe(true)
    expect(mockSideTurn).toHaveBeenCalledWith(SLOT, 'is this cached', undefined)
    expect(mockSendChat).not.toHaveBeenCalled()
  })

  it('does not intercept prefix look-alikes ("/btwx", "/sidebar")', async () => {
    for (const text of ['/btwx', '/btwx q', '/sidebar', '/sidebar q']) {
      const result = await interceptSlashCommand(text, SLOT, store.dispatch)
      expect(result).toEqual({ intercepted: false })
    }
    expect(mockSideOpen).not.toHaveBeenCalled()
  })

  it('does not intercept regular messages', async () => {
    const result = await interceptSlashCommand('hello world', SLOT, store.dispatch)
    expect(result.intercepted).toBe(false)
    expect(mockSideOpen).not.toHaveBeenCalled()
  })

  // A quick prompt (/plain) is a BACKEND macro: build_message swaps the token for
  // the instruction it stands for. Intercepting it here would stop the message
  // ever being sent, so the feature would silently do nothing. Pinned because the
  // command appears in the same menu as /side and looks interceptable.
  it('does not intercept "/plain" — it must reach the backend expansion', async () => {
    for (const text of ['/plain', '/plain why is CI red']) {
      const result = await interceptSlashCommand(text, SLOT, store.dispatch)
      expect(result).toEqual({ intercepted: false })
    }
    expect(mockSideOpen).not.toHaveBeenCalled()
    expect(mockSideTurn).not.toHaveBeenCalled()
  })

  it('starts the import gate before replaying onboarding', async () => {
    const listener = vi.fn()
    window.addEventListener('mc-start-import', listener)

    const result = await interceptSlashCommand('/onboarding', null, store.dispatch)

    expect(result.intercepted).toBe(true)
    expect(listener).toHaveBeenCalledOnce()
    expect((listener.mock.calls[0][0] as CustomEvent).detail).toEqual({
      continueOnboarding: true,
    })
    window.removeEventListener('mc-start-import', listener)
  })

  it('reads the side conversation before it asks, so the panel shows what the question joins', async () => {
    mockSideState.mockResolvedValueOnce({
      open: true, created_at: '2026-10-21T06:00:00.000Z', last_run_id: 'r0', running: false, queue: [],
      messages: [
        { id: 'u0', run_id: 'r0', role: 'user', content: 'remember pineapple', ts: '2026-10-21T06:00:01.000Z' },
        { id: 'a0', run_id: 'r0', role: 'assistant', content: 'noted', ts: '2026-10-21T06:00:02.000Z' },
      ],
    })
    const result = await interceptSlashCommand('/side what word?', SLOT, store.dispatch)
    expect(result).toEqual({ intercepted: true })
    expect(store.getState().chat.slotSide[SLOT].messages.map(m => m.content)).toEqual(['remember pineapple', 'noted'])
    expect(mockSideState.mock.invocationCallOrder[0]).toBeLessThan(mockSideTurn.mock.invocationCallOrder[0])
  })

  it('holds the question back when that read fails, so it is not asked into a conversation nobody can see', async () => {
    mockSideState.mockRejectedValueOnce(new Error('offline'))
    const result = await interceptSlashCommand('/side what word?', SLOT, store.dispatch)
    expect(result).toMatchObject({ intercepted: true, failed: true, stage: 'open' })
    // The COMPOSER's wording, not the panel's notice: nothing opened, so there is
    // no Retry button here, and copy that told the reader to press one would point
    // at a control they cannot see.
    expect(result.intercepted && result.error).toMatch(/the command was not run\. Your message is still in the composer\./)
    expect(result.intercepted && result.error).not.toMatch(/Retry before asking/)
    expect(mockSideTurn).not.toHaveBeenCalled()
    expect(store.getState().chat.slotSideRestored[SLOT]).toBeUndefined()
  })

  it('gives up on a read still unanswered at the send deadline and reports it, instead of never answering', async () => {
    vi.useFakeTimers()
    let rejectRead: (e: Error) => void = () => {}
    mockSideState.mockImplementationOnce(() => new Promise((_, reject) => { rejectRead = reject }))
    onTestFinished(async () => {
      // Settle the read: one still in flight is shared per slot, so a later test would wait on it.
      rejectRead(new Error('test over'))
      await vi.runAllTimersAsync()
      vi.useRealTimers()
    })
    let settled = false
    const pending = interceptSlashCommand('/side what word?', SLOT, store.dispatch).then(r => { settled = true; return r })
    await vi.advanceTimersByTimeAsync(SEND_ABORT_MS - 1)
    expect(settled).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(settled).toBe(true)
    const result = await pending
    expect(result).toMatchObject({ intercepted: true, failed: true, stage: 'open' })
    expect(result.intercepted && result.error).toMatch(/the command was not run\. Your message is still in the composer\./)
    expect(mockSideTurn).not.toHaveBeenCalled()
    expect(store.getState().chat.activityTab).not.toBe('side')
  })

  it('gives up on an open still unanswered at the send deadline, and neither reads nor asks', async () => {
    vi.useFakeTimers()
    let failOpen: (e: Error) => void = () => {}
    mockSideOpen.mockImplementationOnce(() => new Promise((_, reject) => { failOpen = reject }))
    onTestFinished(async () => {
      failOpen(new Error('test over'))
      await vi.runAllTimersAsync()
      vi.useRealTimers()
    })
    let settled = false
    const pending = interceptSlashCommand('/side what word?', SLOT, store.dispatch).then(r => { settled = true; return r })
    await vi.advanceTimersByTimeAsync(SEND_ABORT_MS - 1)
    expect(settled).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(await pending).toEqual({ intercepted: true, failed: true, stage: 'open' })
    expect(mockSideState).not.toHaveBeenCalled()
    expect(mockSideTurn).not.toHaveBeenCalled()
  })

  it('asks the gateway again on the /side after a read timed out, and asks the question once that read lands', async () => {
    vi.useFakeTimers()
    let failStalled: (e: Error) => void = () => {}
    mockSideState.mockImplementationOnce(() => new Promise((_, reject) => { failStalled = reject }))
    onTestFinished(async () => {
      failStalled(new Error('test over'))
      await vi.runAllTimersAsync()
      vi.useRealTimers()
    })
    const first = interceptSlashCommand('/side what word?', SLOT, store.dispatch)
    await vi.advanceTimersByTimeAsync(SEND_ABORT_MS)
    expect(await first).toMatchObject({ intercepted: true, failed: true, stage: 'open' })

    // The default mock answers the fresh read at once, so this settles well before
    // a second deadline; waiting on the stalled read instead would time out here.
    const second = interceptSlashCommand('/side what word?', SLOT, store.dispatch)
    await vi.advanceTimersByTimeAsync(SEND_ABORT_MS)
    expect(await second).toEqual({ intercepted: true })
    expect(mockSideState).toHaveBeenCalledTimes(2)
    expect(mockSideTurn).toHaveBeenCalledWith(SLOT, 'what word?', undefined)
  })

  it('reports failed when there is no active slot', async () => {
    const result = await interceptSlashCommand('/side', null, store.dispatch)
    expect(result).toEqual({ intercepted: true, failed: true, stage: 'open' })
  })

  it('reports failed when sideOpen rejects, carrying the reason for the caller to render', async () => {
    mockSideOpen.mockRejectedValueOnce(new Error('boom'))
    const result = await interceptSlashCommand('/side', SLOT, store.dispatch)
    expect(result).toEqual({ intercepted: true, failed: true, error: 'boom', stage: 'open' })
  })

  it('a 2xx whose body was unreadable is UNCONFIRMED: the composer is kept, no error is reported, and the side panel stands its notice', async () => {
    // The turn was probably taken, but the outage that cut the body can also
    // swallow the WS row that would show the question -- so the command must
    // not be cleared as a success, and must not be reported as a refusal
    // either (a retry could run the turn twice). Same policy as SideChat.
    mockSideTurn.mockRejectedValueOnce(new AcceptedBodyUnreadable(new TypeError('network error')))
    const result = await interceptSlashCommand('/side my question', SLOT, store.dispatch)
    expect(result).toEqual({ intercepted: true, failed: true, unconfirmed: true, stage: 'turn' })
    expect(store.getState().chat.slotSide[SLOT]?.sendStatus?.notice?.text).toMatch(/^Delivery not confirmed/)
    expect(store.getState().chat.slotSide[SLOT]?.sendStatus?.error).toBeUndefined()
    // The panel opened -- the notice has somewhere to show.
    expect(store.getState().chat.activityTab).toBe('side')
  })

  it("a later /side that the server accepts clears the earlier one's standing unconfirmed notice", async () => {
    // The notice describes the LAST send; a new submit supersedes it (the rule
    // SideChat's own submit applies), so an accepted Q2 must not sit under Q1's
    // "not confirmed".
    mockSideTurn.mockRejectedValueOnce(new AcceptedBodyUnreadable(new TypeError('network error')))
    await interceptSlashCommand('/side first question', SLOT, store.dispatch)
    expect(store.getState().chat.slotSide[SLOT]?.sendStatus?.notice).toBeDefined()
    const result = await interceptSlashCommand('/side second question', SLOT, store.dispatch)
    expect(result).toEqual({ intercepted: true })
    expect(store.getState().chat.slotSide[SLOT]?.sendStatus).toBeUndefined()
  })

  it('a raw rejection AFTER the turn was dispatched is UNCONFIRMED, never a retry-safe failure', async () => {
    // A connection reset before headers cannot be told from a request the server
    // took whose answer was lost, and /side/open just succeeded on the same
    // link -- so the composer is kept under the panel's notice, not restored
    // with "try again" (which would run the turn twice). Same wire as SideChat.
    mockSideTurn.mockRejectedValueOnce(new TypeError('network error'))
    const result = await interceptSlashCommand('/side my question', SLOT, store.dispatch)
    expect(result).toEqual({ intercepted: true, failed: true, unconfirmed: true, stage: 'turn' })
    expect(store.getState().chat.slotSide[SLOT]?.sendStatus?.notice?.text).toMatch(/^Delivery not confirmed/)
  })

  it('reports failed when sideTurn is REFUSED (e.g. 409 turn in flight, an ApiError from the client)', async () => {
    mockSideTurn.mockRejectedValueOnce(new ApiError(409, '409: side turn already in flight'))
    const result = await interceptSlashCommand('/side my question', SLOT, store.dispatch)
    // `stage: 'turn'` because the panel DID open — the caller's title must not say otherwise.
    expect(result).toEqual({ intercepted: true, failed: true, error: '409: side turn already in flight', stage: 'turn' })
    // The panel still opened — only the turn was rejected.
    expect(store.getState().chat.activityTab).toBe('side')
  })
})

describe('/side question queued behind a running side turn', () => {
  const initialChat = createTestStore().getState().chat
  const busyStore = () => createTestStore({
    chat: {
      ...initialChat,
      activeSlot: SLOT,
      slotSideRestored: { [SLOT]: true },
      slotSide: {
        [SLOT]: { messages: [], lastRunId: 'r1', pending: true, streaming: true, openedAtTurnCount: 0, createdAt: '2026-05-20T00:00:00Z' },
      },
    },
  })

  beforeEach(() => { vi.clearAllMocks() })

  it('puts the raw question on its queue card, so a close in another tab can hand it back', async () => {
    onTestFinished(() => { clearSideChatDrafts() })
    const store = busyStore()
    mockSideTurn.mockResolvedValueOnce({ ok: true, queued: true, queue_id: 'q-slash', still_queued: true, depth: 1 })

    await interceptSlashCommand('/side queued from the composer', SLOT, store.dispatch)

    expect(store.getState().chat.slotSide[SLOT]?.queue).toEqual([
      expect.objectContaining({ id: 'q-slash', content: 'queued from the composer', raw: true }),
    ])
  })

  it('hands the question to the side panel draft when another tab closed the conversation before its receipt', async () => {
    onTestFinished(() => { clearSideChatDrafts() })
    const store = busyStore()
    let answer: (body: unknown) => void = () => {}
    mockSideTurn.mockImplementationOnce(() => new Promise(resolve => { answer = resolve }))

    const pending = interceptSlashCommand('/side asked as it closed', SLOT, store.dispatch)
    await vi.waitFor(() => expect(mockSideTurn).toHaveBeenCalled())
    store.dispatch(sideClose(SLOT))
    answer({ ok: true, queued: true, queue_id: 'q-closed', still_queued: true, depth: 1 })
    await pending

    expect(readSideChatDraft(SLOT)).toBe('asked as it closed')
    expect(store.getState().chat.slotSide[SLOT]).toBeUndefined()
  })

  it('hands the question back when a close dropped its card and the slot reopened before its receipt', async () => {
    onTestFinished(() => { clearSideChatDrafts() })
    const store = busyStore()
    let answer: (body: unknown) => void = () => {}
    mockSideTurn.mockImplementationOnce(() => new Promise(resolve => { answer = resolve }))

    const pending = interceptSlashCommand('/side asked before a reopen', SLOT, store.dispatch)
    await vi.waitFor(() => expect(mockSideTurn).toHaveBeenCalled())
    store.dispatch(sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q-reopen', content: '[scrubbed]' }))
    store.dispatch(sideClose(SLOT))
    store.dispatch(sseSideResult({ slot: SLOT, run_id: 'r2', role: 'user', content: 'replacement question', message_id: 'u2' }))
    answer({ ok: true, queued: true, queue_id: 'q-reopen', still_queued: true, depth: 1 })
    await pending

    expect(readSideChatDraft(SLOT)).toBe('asked before a reopen')
    expect(store.getState().chat.slotSide[SLOT]?.queue ?? []).toEqual([])
  })
})
