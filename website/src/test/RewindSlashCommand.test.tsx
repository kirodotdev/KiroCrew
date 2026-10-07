import { describe, it, expect, vi, beforeEach } from 'vitest'
import { createTestStore } from './helpers'

const { mockRewind, mockSendChat, mockSlotDetail } = vi.hoisted(() => ({
  mockRewind: vi.fn(),
  mockSendChat: vi.fn().mockResolvedValue({ ok: true }),
  mockSlotDetail: vi.fn().mockResolvedValue({ messages: [], total: 0 }),
}))

vi.mock('../api/client', () => ({
  api: new Proxy({ forkChatSlot: mockRewind, sendChat: mockSendChat, chatSlotDetail: mockSlotDetail }, {
    get: (t, prop) => {
      if (prop in t) return (t as Record<string, unknown>)[prop as string]
      return vi.fn().mockResolvedValue({})
    },
  }),
  SEARCH_MIN_CHARS: 2,
}))

import { interceptSlashCommand, isInterceptedSlashCommand } from '../pages/chat/ChatInput'
import { queryClient } from '../api/queryClient'

const SLOT = 'rw-slot'

describe('/rewind slash command', () => {
  let store: ReturnType<typeof createTestStore>

  beforeEach(() => {
    store = createTestStore()
    vi.clearAllMocks()
    queryClient.removeQueries({ queryKey: ['slash-commands'] })
    mockRewind.mockResolvedValue({ ok: true, key: 'rw-child', title: 'child', messages: 2 })
  })

  it('forks one turn back by default and never sends the text to the agent', async () => {
    const result = await interceptSlashCommand('/rewind', SLOT, store.dispatch)
    // The fork is named, not switched to: the composer clears before the switch.
    expect(result).toEqual({ intercepted: true, switchTo: 'rw-child' })
    expect(store.getState().chat.activeSlot).not.toBe('rw-child')
    expect(mockRewind).toHaveBeenCalledWith(SLOT, undefined, undefined, undefined, undefined, undefined, 1)
    expect(mockSendChat).not.toHaveBeenCalled()
  })

  it('passes N through as turns_back', async () => {
    await interceptSlashCommand('/rewind 3', SLOT, store.dispatch)
    expect(mockRewind).toHaveBeenCalledWith(SLOT, undefined, undefined, undefined, undefined, undefined, 3)
  })

  // NaN would serialise to null, and a null turns_back is a plain fork of the
  // whole conversation -- the opposite of what the person asked for.
  it('sends a non-integer argument as 0 so the server refuses it', async () => {
    await interceptSlashCommand('/rewind two', SLOT, store.dispatch)
    expect(mockRewind).toHaveBeenCalledWith(SLOT, undefined, undefined, undefined, undefined, undefined, 0)
    expect(JSON.stringify({ turns_back: mockRewind.mock.calls[0][6] })).toBe('{"turns_back":0}')
  })

  it('reports the server refusal so the composer keeps the text', async () => {
    mockRewind.mockRejectedValue(new Error('cannot go back 9 turns: this session has 2'))
    const result = await interceptSlashCommand('/rewind 9', SLOT, store.dispatch)
    expect(result).toEqual({
      intercepted: true,
      failed: true,
      error: 'cannot go back 9 turns: this session has 2',
      stage: 'rewind',
    })
  })

  it('fails without a slot instead of sending', async () => {
    const result = await interceptSlashCommand('/rewind', null, store.dispatch)
    expect(result).toEqual({ intercepted: true, failed: true, stage: 'rewind' })
    expect(mockRewind).not.toHaveBeenCalled()
  })

  it('does not intercept look-alikes or a multi-word message', async () => {
    for (const text of ['/rewinder', '/rewind 2 please', 'please /rewind']) {
      expect(isInterceptedSlashCommand(text)).toBe(false)
      expect(await interceptSlashCommand(text, SLOT, store.dispatch)).toEqual({ intercepted: false })
    }
    expect(mockRewind).not.toHaveBeenCalled()
  })

  // The claude provider forwards every leading slash to its harness, whose own
  // /rewind also restores code. When the harness reports one, it keeps it.
  it('leaves /rewind to the harness when the harness reports its own', async () => {
    queryClient.setQueryData(['slash-commands'], [{ name: '/compact' }, { name: '/rewind' }])
    expect(isInterceptedSlashCommand('/rewind 2')).toBe(false)
    expect(await interceptSlashCommand('/rewind 2', SLOT, store.dispatch)).toEqual({ intercepted: false })
    expect(mockRewind).not.toHaveBeenCalled()
  })

  it('still intercepts when the harness list has no /rewind', async () => {
    queryClient.setQueryData(['slash-commands'], [{ name: '/compact' }])
    expect(isInterceptedSlashCommand('/rewind')).toBe(true)
  })

  // No cached harness list (the menu's fetch has not landed, or failed): the
  // dashboard's fork runs. Pinned so the empty-cache behaviour is a decision.
  it('intercepts when no harness list is cached yet', async () => {
    expect(queryClient.getQueryData(['slash-commands'])).toBeUndefined()
    expect(isInterceptedSlashCommand('/rewind')).toBe(true)
  })
})
