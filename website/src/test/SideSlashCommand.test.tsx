import { describe, it, expect, vi, beforeEach } from 'vitest'
import { createTestStore } from './helpers'

const { mockSideOpen, mockSideTurn, mockSendChat } = vi.hoisted(() => ({
  mockSideOpen: vi.fn().mockResolvedValue({ ok: true, open: true, messages: 0, last_run_id: '', created_at: '' }),
  mockSideTurn: vi.fn().mockResolvedValue({ ok: true, run_id: 'r1', messages: 1 }),
  mockSendChat: vi.fn().mockResolvedValue({ ok: true }),
}))

vi.mock('../api/client', () => ({
  api: new Proxy({ sideOpen: mockSideOpen, sideTurn: mockSideTurn, sendChat: mockSendChat }, {
    get: (t, prop) => {
      if (prop in t) return (t as Record<string, unknown>)[prop as string]
      return vi.fn().mockResolvedValue({})
    },
  }),
  SEARCH_MIN_CHARS: 2,
}))

import { interceptSlashCommand } from '../pages/chat/ChatInput'
import { ApiError, AcceptedBodyUnreadable } from '../api/apiError'

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
