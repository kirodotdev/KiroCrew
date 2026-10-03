/** A merge card's fork link: switch to the fork when the open chat on its key is
 *  the transcript the card recorded, and otherwise reopen it from History with
 *  that identity, deciding at click time with one callback identity. */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act } from '@testing-library/react'
import { createTestStore, renderHookWithProviders } from './helpers'
import { sseSlots } from '../store/dashboardSlice'
import { useOpenMergedFork } from '../pages/chat/useOpenMergedFork'
import type { MergedFromBlock } from '../types/mergeBack'

const { switchSlot, resumeFromHistory } = vi.hoisted(() => ({
  switchSlot: vi.fn((arg: unknown) => ({ type: 'test/switchSlot', payload: arg })),
  resumeFromHistory: vi.fn((arg: unknown) => ({ type: 'test/resumeFromHistory', payload: arg })),
}))

vi.mock('../store/chatSlice', async importOriginal => ({
  ...(await importOriginal<typeof import('../store/chatSlice')>()),
  switchSlot,
  resumeFromHistory,
}))

const BLOCK: MergedFromBlock = {
  session: 'dashboard:chat-2-fork',
  slot: 'chat-2-fork',
  title: '↳ Fork of Speed up large uploads',
  createdAt: '2026-10-02T18:00:00+00:00',
  through: 'm4',
  messages: 4,
}

const OPEN_FORK = { key: 'chat-2-fork', title: BLOCK.title, created: BLOCK.createdAt, messages: 8, running: false }

afterEach(() => {
  vi.clearAllMocks()
})

describe('useOpenMergedFork', () => {
  it('switches to the fork when it is open', () => {
    const store = createTestStore()
    store.dispatch(sseSlots([OPEN_FORK]))
    const { result } = renderHookWithProviders(() => useOpenMergedFork(), { store })

    act(() => result.current(BLOCK))

    expect(switchSlot).toHaveBeenCalledWith({ key: 'chat-2-fork', announceOnMissing: true })
    expect(resumeFromHistory).not.toHaveBeenCalled()
  })

  it('reopens the fork from History when it is not open', () => {
    const { result } = renderHookWithProviders(() => useOpenMergedFork())

    act(() => result.current(BLOCK))

    expect(resumeFromHistory).toHaveBeenCalledWith({
      key: 'dashboard:chat-2-fork',
      title: BLOCK.title,
      expectedCreatedAt: BLOCK.createdAt,
    })
    expect(switchSlot).not.toHaveBeenCalled()
  })

  it('hands an open slot whose transcript identity differs to the guarded reopen', () => {
    const store = createTestStore()
    store.dispatch(sseSlots([{ ...OPEN_FORK, created: '2026-10-02T19:00:00+00:00' }]))
    const { result } = renderHookWithProviders(() => useOpenMergedFork(), { store })

    act(() => result.current(BLOCK))

    expect(resumeFromHistory).toHaveBeenCalledWith({
      key: 'dashboard:chat-2-fork',
      title: BLOCK.title,
      expectedCreatedAt: BLOCK.createdAt,
    })
    expect(switchSlot).not.toHaveBeenCalled()
  })

  it('hands an empty recorded identity to the guarded reopen, never a switch', () => {
    const store = createTestStore()
    store.dispatch(sseSlots([{ ...OPEN_FORK, created: '' }]))
    const { result } = renderHookWithProviders(() => useOpenMergedFork(), { store })

    act(() => result.current({ ...BLOCK, createdAt: '' }))

    expect(resumeFromHistory).toHaveBeenCalledWith({
      key: 'dashboard:chat-2-fork',
      title: BLOCK.title,
      expectedCreatedAt: '',
    })
    expect(switchSlot).not.toHaveBeenCalled()
  })

  it('reports a guarded resume rejection without opening a slot', () => {
    const store = createTestStore()
    resumeFromHistory.mockImplementationOnce(((arg: unknown) => (dispatch: (action: unknown) => void) => {
      const meta = { arg, requestId: 'identity-mismatch', requestStatus: 'pending' }
      dispatch({ type: 'chat/resumeFromHistory/pending', meta })
      dispatch({
        type: 'chat/resumeFromHistory/rejected',
        meta: { ...meta, requestStatus: 'rejected' },
        error: { message: 'the requested session is no longer available' },
      })
      return Promise.resolve()
    }) as never)
    const { result } = renderHookWithProviders(() => useOpenMergedFork(), { store })

    act(() => result.current(BLOCK))

    expect(store.getState().chat.unresumableResume).toMatchObject({
      key: BLOCK.session,
      title: BLOCK.title,
      reason: 'failed',
    })
    expect(store.getState().dashboard.slots).toEqual([])
    expect(switchSlot).not.toHaveBeenCalled()
  })

  it('reads the open chats when clicked, keeping one callback', () => {
    const store = createTestStore()
    const { result, rerender } = renderHookWithProviders(() => useOpenMergedFork(), { store })
    const first = result.current

    act(() => {
      store.dispatch(sseSlots([OPEN_FORK]))
    })
    rerender()
    act(() => result.current(BLOCK))

    expect(result.current).toBe(first)
    expect(switchSlot).toHaveBeenCalledWith({ key: 'chat-2-fork', announceOnMissing: true })
  })
})
