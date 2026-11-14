/**
 * A pane re-reads its slot after a Blocked link card's Allow / Undo on it
 * (#36862), so the restored link shows in a Bot's chat, which the chat page's
 * own listener does not reach.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, cleanup } from '@testing-library/react'

const dispatch = vi.fn()
const state = { chat: { activeSlot: 'chat-1' } }
vi.mock('../store', () => ({
  useAppDispatch: () => dispatch,
  store: { getState: () => state },
}))
vi.mock('../store/chatSlice', () => ({
  refreshSlot: (arg: unknown) => ({ type: 'refreshSlot', arg }),
  warmSlotCache: (arg: unknown) => ({ type: 'warmSlotCache', arg }),
}))

import { useRedactionHostsReload } from '../components/chat/useRedactionHostsReload'

const fire = (detail: Record<string, unknown>) => window.dispatchEvent(new CustomEvent('mc:redaction-hosts-changed', { detail }))

beforeEach(() => { dispatch.mockReset(); state.chat.activeSlot = 'chat-1' })
afterEach(() => cleanup())

describe('useRedactionHostsReload', () => {
  it("warms the pane's background slot (a Bot's chat) when its card allows a host", () => {
    renderHook(() => useRedactionHostsReload('member-radar'))
    fire({ slot: 'member-radar', messageTs: '2026-11-13T09:00:00Z' })
    expect(dispatch).toHaveBeenCalledWith({ type: 'warmSlotCache', arg: { key: 'member-radar', reachTs: '2026-11-13T09:00:00Z' } })
  })

  it('refreshes the active slot, reaching the reply the card named', () => {
    state.chat.activeSlot = 'member-radar'
    renderHook(() => useRedactionHostsReload('member-radar'))
    fire({ slot: 'member-radar', messageTs: '2026-11-13T09:00:00Z' })
    expect(dispatch).toHaveBeenCalledWith({ type: 'refreshSlot', arg: { key: 'member-radar', reachTs: '2026-11-13T09:00:00Z' } })
  })

  it.each([undefined, '', 42])('keeps a plain background reload for timestamp %s', (messageTs) => {
    renderHook(() => useRedactionHostsReload('member-radar'))
    fire({ slot: 'member-radar', messageTs })
    expect(dispatch).toHaveBeenCalledWith({ type: 'warmSlotCache', arg: 'member-radar' })
  })

  it("ignores another slot's card, and stops listening once unmounted", () => {
    const { unmount } = renderHook(() => useRedactionHostsReload('member-radar'))
    fire({ slot: 'other' })
    unmount()
    fire({ slot: 'member-radar' })
    expect(dispatch).not.toHaveBeenCalled()
  })
})
