/** A merge card's fork link always reopens the fork through the identity-checked
 *  resume, whether or not a chat is open on its key, so a stale slot list can
 *  never open a different conversation. */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { createTestStore, renderHookWithProviders } from './helpers'
import { api } from '../api/client'
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
  vi.restoreAllMocks()
})

/** Renders the hook, then lets the provider stack's mount effects settle inside act(). */
async function renderOpener(options?: Parameters<typeof renderHookWithProviders>[1]) {
  const rendered = renderHookWithProviders(() => useOpenMergedFork(), options)
  await act(async () => {})
  return rendered
}

describe('useOpenMergedFork', () => {
  it('opens an open fork through the guarded resume, never a local switch', async () => {
    const store = createTestStore()
    store.dispatch(sseSlots([OPEN_FORK]))
    const { result } = await renderOpener({ store })

    act(() => result.current(BLOCK))

    expect(resumeFromHistory).toHaveBeenCalledWith({
      key: 'dashboard_chat-2-fork',
      title: BLOCK.title,
      expectedCreatedAt: BLOCK.createdAt,
    })
    expect(switchSlot).not.toHaveBeenCalled()
  })

  it('reopens the fork from History when it is not open', async () => {
    const { result } = await renderOpener()

    act(() => result.current(BLOCK))

    expect(resumeFromHistory).toHaveBeenCalledWith({
      key: 'dashboard_chat-2-fork',
      title: BLOCK.title,
      expectedCreatedAt: BLOCK.createdAt,
    })
    expect(switchSlot).not.toHaveBeenCalled()
  })

  it('resumes under the transcript stem, so the reopened fork leaves the History list', async () => {
    const actual = await vi.importActual<typeof import('../store/chatSlice')>('../store/chatSlice')
    resumeFromHistory.mockImplementationOnce(actual.resumeFromHistory as never)
    const resume = vi.spyOn(api, 'resumeChatSlot').mockResolvedValueOnce({ ok: true, key: 'fork', surface: '', messages: [], total: 0 })
    const store = createTestStore()
    store.dispatch(actual.fetchHistory.fulfilled(
      { sessions: [{ key: 'dashboard_fork', title: 'Fork' }, { key: 'dashboard_other', title: 'Other' }] as never, hasMore: false, offset: 0, append: false },
      'seed-history',
      false,
    ))
    const { result } = await renderOpener({ store })

    act(() => result.current({ ...BLOCK, session: 'dashboard:fork', slot: 'fork' }))

    await waitFor(() => expect(store.getState().chat.history.map(row => row.key)).toEqual(['dashboard_other']))
    expect(resumeFromHistory).toHaveBeenCalledWith({ key: 'dashboard_fork', title: BLOCK.title, expectedCreatedAt: BLOCK.createdAt })
    expect(resume).toHaveBeenCalledWith('dashboard_fork', BLOCK.title, BLOCK.createdAt)
    expect(store.getState().chat.historyOffset).toBe(1)
  })

  it('hands an open slot whose transcript identity differs to the guarded reopen', async () => {
    const store = createTestStore()
    store.dispatch(sseSlots([{ ...OPEN_FORK, created: '2026-10-02T19:00:00+00:00' }]))
    const { result } = await renderOpener({ store })

    act(() => result.current(BLOCK))

    expect(resumeFromHistory).toHaveBeenCalledWith({
      key: 'dashboard_chat-2-fork',
      title: BLOCK.title,
      expectedCreatedAt: BLOCK.createdAt,
    })
    expect(switchSlot).not.toHaveBeenCalled()
  })

  it('hands an empty recorded identity to the guarded reopen, never a switch', async () => {
    const store = createTestStore()
    store.dispatch(sseSlots([{ ...OPEN_FORK, created: '' }]))
    const { result } = await renderOpener({ store })

    act(() => result.current({ ...BLOCK, createdAt: '' }))

    expect(resumeFromHistory).toHaveBeenCalledWith({
      key: 'dashboard_chat-2-fork',
      title: BLOCK.title,
      expectedCreatedAt: '',
    })
    expect(switchSlot).not.toHaveBeenCalled()
  })

  it('reports a guarded resume rejection without opening a slot', async () => {
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
    const { result } = await renderOpener({ store })

    act(() => result.current(BLOCK))

    expect(store.getState().chat.unresumableResume).toMatchObject({
      key: 'dashboard_chat-2-fork',
      title: BLOCK.title,
      reason: 'failed',
    })
    expect(store.getState().dashboard.slots).toEqual([])
    expect(switchSlot).not.toHaveBeenCalled()
  })

  it('keeps one callback while the open chats change', async () => {
    const store = createTestStore()
    const { result, rerender } = await renderOpener({ store })
    const first = result.current

    act(() => {
      store.dispatch(sseSlots([OPEN_FORK]))
    })
    rerender()

    expect(result.current).toBe(first)
  })
})
