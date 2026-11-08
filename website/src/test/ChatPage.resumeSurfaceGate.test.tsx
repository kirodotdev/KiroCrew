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
 * SCOPE NOTE: `handleResumeSession` is a `useCallback` defined inline inside the
 * very large `ChatPage` component and is not exported, and the existing
 * `ChatPage.*.test.tsx` harnesses stub out the sidebar/virtualization layers
 * rather than drive them live. Following the precedent set by
 * `ChatPage.handleFork.test.tsx`, this file mounts the same body against the
 * REAL `resumeFromHistory` / `deleteSlot` thunks and the real `api` module
 * (mocked at the network boundary), so the gate is exercised through the real
 * reducer that produces the condition it guards. The callback body is duplicated
 * from `ChatPage.tsx` rather than imported; if that body changes, update this
 * file with it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

vi.mock('../api/client', () => ({
  api: {
    resumeChatSlot: vi.fn(),
    deleteChatSlot: vi.fn(() => Promise.resolve({ ok: true })),
  },
}))

import chatReducer, { captureForkOrigin, createSlot, resumeFromHistory, deleteSlot, setActiveSlot } from '../store/chatSlice'
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
 * Verbatim reproduction of ChatPage.tsx's `handleResumeSession` body (see the
 * file header for why it is duplicated rather than imported). `drafts` stands in
 * for the component's draft refs so the discard is observable.
 */
function makeHandleResumeSession(
  store: ReturnType<typeof makeStore>,
  activeSlot: string | null,
  drafts: Record<string, string>,
) {
  return async (key: string, title: string) => {
    try {
      const resuming = store.dispatch(resumeFromHistory({ key, title }) as never) as unknown as { requestId: string; unwrap: () => Promise<{ ok: boolean; key: string; surface: string }> }
      const result = await resuming.unwrap()
      if (!result.ok || !isChatPageSurface(result.surface)) return
      if (store.getState().chat.activeSlot !== result.key) return
      const opened = store.getState().chat.lastResumeActivation
      if (opened?.requestId !== resuming.requestId || opened.replaced !== activeSlot) return
      if (activeSlot && activeSlot !== key) {
        delete drafts[activeSlot]
        await store.dispatch(deleteSlot(activeSlot) as never).unwrap().catch(() => {})
      }
    } catch { /* resume failed - keep current slot */ }
  }
}

/** The origin meta `createSlot` sends for a create started on the store now. */
function captureForkOriginMeta(store: ReturnType<typeof makeStore>) {
  const o = captureForkOrigin(store.getState() as never)
  return { originActiveSlot: o.activeSlot, originActiveSlotMoves: o.moves, originActiveSlotAutoMoves: o.autoMoves, originActiveSlotRequestMoves: o.requestMoves, originRequestSeq: o.seq }
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
    store.dispatch(setActiveSlot('chat-1'))
    const drafts: Record<string, string> = { 'chat-1': 'half-typed question' }

    await makeHandleResumeSession(store, 'chat-1', drafts)('chat-9', 'Older chat')

    expect(deleteChatSlotMock).toHaveBeenCalledWith('chat-1')
    expect(drafts['chat-1']).toBeUndefined()
    expect(store.getState().chat.activeSlot).toBe('chat-9')
  })

  // The resume answered after the user opened another session, so it did not
  // take the view (`forkMayOpen`). The tab it would have replaced stays.
  it('does not delete the origin tab when the user moved while the resume ran', async () => {
    let land!: (v: unknown) => void
    resumeChatSlotMock.mockReturnValue(new Promise(r => { land = r }))
    const store = makeStore()
    store.dispatch(setActiveSlot('chat-1'))
    const drafts: Record<string, string> = { 'chat-1': 'half-typed question' }

    const done = makeHandleResumeSession(store, 'chat-1', drafts)('chat-9', 'Older chat')
    await vi.waitFor(() => expect(resumeChatSlotMock).toHaveBeenCalled())
    store.dispatch(setActiveSlot('chat-2'))
    land({ ok: true, key: 'chat-9', mode: '', messages: [] })
    await done

    expect(deleteChatSlotMock).not.toHaveBeenCalled()
    expect(drafts['chat-1']).toBe('half-typed question')
    expect(store.getState().chat.activeSlot).toBe('chat-2')
  })

  // The user left chat-1 for chat-2 and clicked the same row again within the
  // lease. The repeat sends nothing and moves the pending resume's origin to
  // chat-2, so the answer opens chat-9 from there. That swap replaced chat-2,
  // not the tab the first click captured, so chat-1 and its draft stay.
  it('does not delete the first tab when a repeated click opened the resume from another session', async () => {
    let land!: (v: unknown) => void
    resumeChatSlotMock.mockReturnValue(new Promise(r => { land = r }))
    const store = makeStore()
    store.dispatch(setActiveSlot('chat-1'))
    const drafts: Record<string, string> = { 'chat-1': 'half-typed question' }

    const first = makeHandleResumeSession(store, 'chat-1', drafts)('chat-9', 'Older chat')
    await vi.waitFor(() => expect(resumeChatSlotMock).toHaveBeenCalled())
    store.dispatch(setActiveSlot('chat-2'))
    await store.dispatch(resumeFromHistory({ key: 'chat-9', title: 'Older chat' }) as never)
    expect(resumeChatSlotMock).toHaveBeenCalledTimes(1)
    land({ ok: true, key: 'chat-9', mode: '', messages: [] })
    await first

    expect(store.getState().chat.activeSlot).toBe('chat-9')
    expect(deleteChatSlotMock).not.toHaveBeenCalled()
    expect(drafts['chat-1']).toBe('half-typed question')
  })

  // New was clicked first and opened before the resume answered, so the
  // resume replaced the new chat, not chat-1. chat-1 keeps its draft.
  it('does not delete the first tab when a create opened before the resume answered', async () => {
    let land!: (v: unknown) => void
    resumeChatSlotMock.mockReturnValue(new Promise(r => { land = r }))
    const store = makeStore()
    store.dispatch(setActiveSlot('chat-1'))
    const drafts: Record<string, string> = { 'chat-1': 'half-typed question' }

    // New first: its origin is taken as it starts, before the resume click.
    store.dispatch(createSlot.pending('create-1', undefined))
    const createOrigin = captureForkOriginMeta(store)
    const done = makeHandleResumeSession(store, 'chat-1', drafts)('chat-9', 'Older chat')
    await vi.waitFor(() => expect(resumeChatSlotMock).toHaveBeenCalled())
    store.dispatch(createSlot.fulfilled({ key: 'chat-new', title: 'New', messages: 0, running: false } as never, 'create-1', undefined, {
      ...createOrigin, activate: true,
    }))
    expect(store.getState().chat.activeSlot).toBe('chat-new')
    land({ ok: true, key: 'chat-9', mode: '', messages: [] })
    await done

    expect(store.getState().chat.activeSlot).toBe('chat-9')
    expect(deleteChatSlotMock).not.toHaveBeenCalled()
    expect(drafts['chat-1']).toBe('half-typed question')
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
