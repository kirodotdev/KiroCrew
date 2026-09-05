/**
 * ChatPage's "Continue a previous chat" resume must not retire the tab the user
 * is sitting in when the resume did not actually take effect (#5925).
 *
 * `handleResumeSession` performs a SWAP: resume the picked session, then delete
 * the slot it replaces and drop that slot's drafts. A resume answering with a
 * surface the chat page cannot display never performs the first half -- the
 * `resumeFromHistory.fulfilled` reducer short-circuits, leaving `activeSlot`
 * and the history row exactly as they were -- so running the second half closed
 * the tab the user was in, discarded the text they had just typed into it (the
 * suggestions list only appears once they type), and bounced them to an
 * unrelated peer, while the session they asked for never opened. The thunk's
 * `ok` cannot distinguish the two cases: the wire request succeeds either way,
 * which is why it returns `surface` at all (#3624).
 *
 * SCOPE NOTE: `handleResumeSession` is a `useCallback` defined inline inside
 * `pages/chat/useChatPageSessionController.ts` and is not exported on its own,
 * and the existing `ChatPage.*.test.tsx` harnesses stub out the
 * sidebar/virtualization layers rather than drive them live. Following the
 * precedent set by `ChatPage.handleFork.test.tsx`, this file mounts the same
 * body against the REAL `resumeFromHistory` / `deleteSlot` thunks and the real
 * `api` module (mocked at the network boundary), so the gate is exercised
 * through the real reducer that produces the condition it guards. The callback
 * body is duplicated from that controller rather than imported; if that body
 * changes, update this file with it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

vi.mock('../api/client', () => ({
  api: {
    resumeChatSlot: vi.fn(),
    deleteChatSlot: vi.fn(() => Promise.resolve({ ok: true })),
  },
}))

import chatReducer, { resumeFromHistory, deleteSlot } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import { isChatPageSurface } from '../utils/channelOrigin'
import { api } from '../api/client'

const resumeChatSlotMock = api.resumeChatSlot as unknown as ReturnType<typeof vi.fn>
const deleteChatSlotMock = api.deleteChatSlot as unknown as ReturnType<typeof vi.fn>

function makeStore() {
  return configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer },
    middleware: (getDefault) => getDefault({ serializableCheck: false, immutableCheck: false }),
  })
}

/**
 * Verbatim reproduction of `useChatPageSessionController`'s
 * `handleResumeSession` body (see the file header for why it is duplicated
 * rather than imported). `drafts` stands in for the controller's draft refs so
 * the discard is observable.
 */
function makeHandleResumeSession(
  store: ReturnType<typeof makeStore>,
  activeSlot: string | null,
  drafts: Record<string, string>,
) {
  return async (key: string, title: string) => {
    try {
      const result = await store.dispatch(resumeFromHistory({ key, title }) as never).unwrap()
      if (!result.ok || !isChatPageSurface(result.surface)) return
      if (activeSlot && activeSlot !== key) {
        delete drafts[activeSlot]
        await store.dispatch(deleteSlot(activeSlot) as never).unwrap().catch(() => {})
      }
    } catch { /* resume failed - keep current slot */ }
  }
}

beforeEach(() => {
  resumeChatSlotMock.mockReset()
  deleteChatSlotMock.mockClear()
})

describe('ChatPage handleResumeSession surface gate (#5925)', () => {
  it('does not delete the current slot or its draft when the resume resolves to an undisplayable surface', async () => {
    resumeChatSlotMock.mockResolvedValue({ ok: true, key: 'dashboard_ops', mode: 'dashboard', messages: [] })
    const store = makeStore()
    const drafts: Record<string, string> = { 'chat-1': 'half-typed question' }

    await makeHandleResumeSession(store, 'chat-1', drafts)('dashboard_ops', 'Ops board')

    expect(deleteChatSlotMock).not.toHaveBeenCalled()
    expect(drafts['chat-1']).toBe('half-typed question')
    // And the resume genuinely did not take effect, which is what makes running
    // the teardown wrong rather than merely early.
    expect(store.getState().chat.activeSlot).toBeNull()
    expect(store.getState().chat.unresumableResume).not.toBeNull()
  })

  it('still completes the swap when the resume lands on a surface the chat page shows', async () => {
    resumeChatSlotMock.mockResolvedValue({ ok: true, key: 'chat-9', mode: '', messages: [] })
    const store = makeStore()
    const drafts: Record<string, string> = { 'chat-1': 'half-typed question' }

    await makeHandleResumeSession(store, 'chat-1', drafts)('chat-9', 'Older chat')

    expect(deleteChatSlotMock).toHaveBeenCalledWith('chat-1')
    expect(drafts['chat-1']).toBeUndefined()
    expect(store.getState().chat.activeSlot).toBe('chat-9')
  })

  it('leaves the current slot alone when the resume failed outright', async () => {
    resumeChatSlotMock.mockResolvedValue({ ok: false, key: 'gone', messages: [] })
    const store = makeStore()
    const drafts: Record<string, string> = { 'chat-1': 'half-typed question' }

    await makeHandleResumeSession(store, 'chat-1', drafts)('gone', 'Gone')

    expect(deleteChatSlotMock).not.toHaveBeenCalled()
    expect(drafts['chat-1']).toBe('half-typed question')
  })
})
