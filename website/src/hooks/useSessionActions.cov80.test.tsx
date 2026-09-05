/**
 * useSessionActions — the surface-agnostic session actions shared by every menu.
 *
 * Each action reads its prior state from the store at CALL time and rolls back
 * on failure, so the tests drive the real global store (the hook reads
 * `store.getState()` directly) and assert both the optimistic write and the
 * rollback. The guarded mode rollback — which must NOT clobber a superseding
 * toggle — is covered explicitly, since that is the branch a naive rollback
 * gets wrong.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const apiMock = vi.hoisted(() => ({
  forkChatSlot: vi.fn(),
  setSlotPin: vi.fn(),
  setSlotMode: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: apiMock }))

const copySessionLink = vi.hoisted(() => vi.fn())
vi.mock('../utils/shareUrl', () => ({ copySessionLink }))

const moveSlotToFolder = vi.hoisted(() => vi.fn())
vi.mock('./useMoveSlotToFolder', () => ({ useMoveSlotToFolder: () => moveSlotToFolder }))

const chatConfig = vi.hoisted(() => ({ confirmCloseSession: true }))
vi.mock('../pages/chat/ChatSettings', () => ({ loadChatConfig: () => chatConfig }))

const deleteSlot = vi.hoisted(() => vi.fn((key: string) => ({ type: 'zzq/deleteSlot', payload: key })))
const switchSlot = vi.hoisted(() => vi.fn((key: string) => ({ type: 'zzq/switchSlot', payload: key })))
vi.mock('../store/chatSlice', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  deleteSlot,
  switchSlot,
}))

import { store } from '../store'
import { sseSlots, markSlotUnread, markSlotRead } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'
import { useSessionActions } from './useSessionActions'

const KEY = 'zzq-slot-1'

function slots(patch: Partial<ChatSlot> = {}) {
  store.dispatch(sseSlots([{ key: KEY, messages: 0, running: false, ...patch } as ChatSlot]))
}

function harness(mode?: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <Provider store={store}>{children}</Provider>
    </QueryClientProvider>
  )
  return { client, ...renderHook(() => useSessionActions(mode), { wrapper }) }
}

const slot = () => store.getState().dashboard.slots.find((s) => s.key === KEY)

beforeEach(() => {
  apiMock.forkChatSlot.mockReset().mockResolvedValue({ ok: true, key: 'zzq-forked' })
  apiMock.setSlotPin.mockReset().mockResolvedValue({ ok: true })
  apiMock.setSlotMode.mockReset().mockResolvedValue({ ok: true })
  copySessionLink.mockClear()
  moveSlotToFolder.mockClear()
  deleteSlot.mockClear()
  switchSlot.mockClear()
  chatConfig.confirmCloseSession = true
  store.dispatch(sseSlots([]))
  store.dispatch(markSlotRead(KEY))
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('duplicate', () => {
  it('forks the slot and switches to the new one', async () => {
    slots()
    const { result } = harness()
    act(() => result.current.duplicate(KEY))
    await waitFor(() => expect(apiMock.forkChatSlot).toHaveBeenCalledWith(KEY))
    await waitFor(() => expect(switchSlot).toHaveBeenCalledWith('zzq-forked'))
  })

  it('switches nowhere when the fork is refused', async () => {
    apiMock.forkChatSlot.mockResolvedValue({ ok: false })
    slots()
    const { result } = harness()
    act(() => result.current.duplicate(KEY))
    await waitFor(() => expect(apiMock.forkChatSlot).toHaveBeenCalled())
    expect(switchSlot).not.toHaveBeenCalled()
  })
})

describe('toggleRead', () => {
  it('marks an unread session read', () => {
    slots()
    store.dispatch(markSlotUnread(KEY))
    const { result } = harness()
    act(() => result.current.toggleRead(KEY))
    expect(store.getState().dashboard.unreadSlots).not.toContain(KEY)
  })

  it('marks a read session unread', () => {
    slots()
    const { result } = harness()
    act(() => result.current.toggleRead(KEY))
    expect(store.getState().dashboard.unreadSlots).toContain(KEY)
  })
})

describe('togglePin', () => {
  it('pins optimistically and persists the new value', async () => {
    slots({ pinned: false })
    const { result } = harness()
    act(() => result.current.togglePin(KEY))
    expect(slot()?.pinned).toBe(true)
    await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledWith(KEY, true))
  })

  it('unpins a pinned session', async () => {
    slots({ pinned: true })
    const { result } = harness()
    act(() => result.current.togglePin(KEY))
    expect(slot()?.pinned).toBe(false)
    await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledWith(KEY, false))
  })

  it('rolls the pin back when the write fails', async () => {
    apiMock.setSlotPin.mockRejectedValue(new Error('zzq offline'))
    slots({ pinned: false })
    const { result } = harness()
    act(() => result.current.togglePin(KEY))
    expect(slot()?.pinned).toBe(true)
    await waitFor(() => expect(slot()?.pinned).toBe(false))
  })
})

describe('toggleMode', () => {
  it('switches to orchestrator once confirmed', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    slots({ mode: '' })
    const { result } = harness()
    act(() => result.current.toggleMode(KEY))
    expect(slot()?.mode).toBe('orchestrator')
    await waitFor(() => expect(apiMock.setSlotMode).toHaveBeenCalledWith(KEY, 'orchestrator'))
  })

  it('switches back to normal chat once confirmed', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    slots({ mode: 'orchestrator' })
    const { result } = harness()
    act(() => result.current.toggleMode(KEY))
    await waitFor(() => expect(apiMock.setSlotMode).toHaveBeenCalledWith(KEY, ''))
  })

  it('changes nothing when the confirm is declined', () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    slots({ mode: '' })
    const { result } = harness()
    act(() => result.current.toggleMode(KEY))
    expect(apiMock.setSlotMode).not.toHaveBeenCalled()
    expect(slot()?.mode).toBe('')
  })

  it('rolls the mode back when the write fails', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    apiMock.setSlotMode.mockRejectedValue(new Error('zzq offline'))
    slots({ mode: '' })
    const { result } = harness()
    act(() => result.current.toggleMode(KEY))
    expect(slot()?.mode).toBe('orchestrator')
    await waitFor(() => expect(slot()?.mode).toBe(''))
  })

  it('does not clobber a superseding toggle when the write fails', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    let release: (() => void) | undefined
    apiMock.setSlotMode.mockImplementation(
      () => new Promise((_res, rej) => { release = () => rej(new Error('zzq offline')) }),
    )
    slots({ mode: '' })
    const { result } = harness()
    act(() => result.current.toggleMode(KEY))
    await waitFor(() => expect(release).toBeTypeOf('function'))
    // A second toggle lands while the first write is still in flight.
    act(() => result.current.toggleMode(KEY))
    expect(slot()?.mode).toBe('')
    act(() => release?.())
    await waitFor(() => expect(apiMock.setSlotMode).toHaveBeenCalledTimes(2))
    // The stale rollback must not restore '' over the newer value.
    expect(slot()?.mode).toBe('')
  })
})

describe('copyLink', () => {
  it('copies the link with the slot title and the caller mode', () => {
    slots({ title: 'zzq title' })
    const { result } = harness('zzq-mode')
    act(() => result.current.copyLink(KEY))
    expect(copySessionLink).toHaveBeenCalledWith(KEY, 'zzq title', undefined, 'zzq-mode')
  })

  it('copies a link for an unknown slot with no title', () => {
    const { result } = harness()
    act(() => result.current.copyLink('zzq-missing'))
    expect(copySessionLink).toHaveBeenCalledWith('zzq-missing', undefined, undefined, undefined)
  })
})

describe('move', () => {
  it('delegates to the shared optimistic move', () => {
    const { result } = harness()
    act(() => result.current.move(KEY, 'zzq-folder'))
    expect(moveSlotToFolder).toHaveBeenCalledWith(KEY, 'zzq-folder')
  })

  it('passes null through for a move to root', () => {
    const { result } = harness()
    act(() => result.current.move(KEY, null))
    expect(moveSlotToFolder).toHaveBeenCalledWith(KEY, null)
  })
})

describe('close', () => {
  it('closes without a prompt when the confirm preference is off', () => {
    chatConfig.confirmCloseSession = false
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const { result } = harness()
    // `void`, not a bare call: `close` returns a promise now, and handing a
    // thenable to a SYNCHRONOUS `act` leaves its scope open — which surfaced as
    // `result.current === null` in the NEXT test, not as a failure here.
    act(() => { void result.current.close(KEY) })
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(deleteSlot).toHaveBeenCalledWith(KEY)
  })

  it('closes after an accepted confirm', () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const { result } = harness()
    // Still synchronous to observe, even though `close` is now an async function:
    // with no `beforeDelete` supplied the `opts?.beforeDelete &&` short-circuits
    // without awaiting, so the delete dispatches in the first synchronous block.
    act(() => { void result.current.close(KEY) })
    expect(deleteSlot).toHaveBeenCalledWith(KEY)
  })

  it('keeps the session on a declined confirm', () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    const { result } = harness()
    act(() => { void result.current.close(KEY) })
    expect(deleteSlot).not.toHaveBeenCalled()
  })
})
