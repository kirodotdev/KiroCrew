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
    expect(mockSideTurn).toHaveBeenCalledWith(SLOT, 'what model is this')
    expect(mockSendChat).not.toHaveBeenCalled()
  })

  it('does not intercept regular messages', async () => {
    const result = await interceptSlashCommand('hello world', SLOT, store.dispatch)
    expect(result.intercepted).toBe(false)
    expect(mockSideOpen).not.toHaveBeenCalled()
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
    expect(result).toEqual({ intercepted: true, failed: true })
  })

  it('reports failed when sideOpen rejects', async () => {
    mockSideOpen.mockRejectedValueOnce(new Error('boom'))
    const result = await interceptSlashCommand('/side', SLOT, store.dispatch)
    expect(result).toEqual({ intercepted: true, failed: true })
  })

  it('reports failed when sideTurn rejects (e.g. 409 turn in flight)', async () => {
    mockSideTurn.mockRejectedValueOnce(new Error('409: side turn already in flight'))
    const result = await interceptSlashCommand('/side my question', SLOT, store.dispatch)
    expect(result).toEqual({ intercepted: true, failed: true })
    // The panel still opened — only the turn was rejected.
    expect(store.getState().chat.activityTab).toBe('side')
  })
})
