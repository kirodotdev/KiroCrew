/**
 * A rejected gateway write must reach the in-page notice, not just roll back.
 *
 * Each case drives the real mutation with a rejecting api so the assertion is on
 * the failure path the user actually hits, and reads the shared store rather
 * than the component that raised it — the menu subtree unmounts on close.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const apiMock = vi.hoisted(() => ({
  forkChatSlot: vi.fn(),
  setSlotPin: vi.fn(),
  setSlotMode: vi.fn(),
  chatSlots: vi.fn(),
  setSlotFolder: vi.fn(),
  chatSlotReload: vi.fn(),
}))
vi.mock('../api/client', () => ({
  api: apiMock,
  // The real shape: `status` is what the reload copy branches on.
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.status = status
      this.body = body
    }
  },
}))

const copySessionLink = vi.hoisted(() => vi.fn())
vi.mock('../utils/shareUrl', () => ({ copySessionLink }))

const chatConfig = vi.hoisted(() => ({ confirmCloseSession: true }))
vi.mock('../pages/chat/ChatSettings', () => ({ loadChatConfig: () => chatConfig }))

import { store } from '../store'
import { removeSlotOptimistic, sseSlots } from '../store/dashboardSlice'
import { ApiError } from '../api/client'
import { useSessionActions } from '../hooks/useSessionActions'
import { useMoveSlotToFolder } from '../hooks/useMoveSlotToFolder'
import { __resetActionFailureForTests, useActionFailure } from '../utils/actionFailure'
import { recordError, __resetErrorJournalForTests } from '../utils/errorReport'

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  return (
    <Provider store={store}>
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    </Provider>
  )
}

function seedSlot() {
  store.dispatch(sseSlots([
    { key: 'chat-a', title: 'A', mode: 'chat', pinned: false, folder_id: null } as never,
  ]))
}

describe('rejected session writes surface in the page', () => {
  beforeEach(() => {
    __resetActionFailureForTests()
    vi.clearAllMocks()
    // Both toggles confirm first, and jsdom answers falsy, which would bail out
    // before the mutation this test is about ever runs.
    vi.stubGlobal('confirm', () => true)
    apiMock.chatSlots.mockResolvedValue({ slots: [] })
    seedSlot()
  })

  it('reports a rejected mode switch', async () => {
    apiMock.setSlotMode.mockRejectedValue(new Error('nope'))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.toggleMode('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/mode/i))
  })

  it('reports a rejected pin', async () => {
    apiMock.setSlotPin.mockRejectedValue(new Error('nope'))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.togglePin('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/pin/i))
  })

  it('stays silent when a rejected pin is the state the server kept', async () => {
    apiMock.setSlotPin.mockRejectedValue(new Error('nope'))
    apiMock.chatSlots.mockResolvedValue([
      { key: 'chat-a', title: 'A', mode: 'chat', pinned: true, folder_id: null },
    ] as never)
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.togglePin('chat-a') })
    await waitFor(() => expect(apiMock.chatSlots).toHaveBeenCalled())
    await waitFor(() =>
      expect(store.getState().dashboard.slots.find(s => s.key === 'chat-a')?.pinned).toBe(true))
    expect(result.current.failure.failure).toBeNull()
  })

  it('names every session a bulk pin rejection reverted, not just the last', async () => {
    store.dispatch(sseSlots([
      { key: 'chat-a', title: 'A', mode: 'chat', pinned: false, folder_id: null },
      { key: 'chat-b', title: 'B', mode: 'chat', pinned: false, folder_id: null },
    ] as never))
    apiMock.setSlotPin.mockRejectedValue(new Error('nope'))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => {
      result.current.actions.togglePin('chat-a')
      result.current.actions.togglePin('chat-b')
    })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/pin/i))
    const failure = result.current.failure.failure!
    // The count leads; the shared single-subject lead would have read the two
    // titles as ONE session called "A, B".
    expect(failure.heading).toBe('Couldn’t update 2 sessions')
    expect(failure.heading).not.toContain('“')
    // The names are still told, as a list the language joins — not a joined slot.
    expect(failure.message).toBe('The pin change was undone for A and B.')
    expect(failure.subject).toBe('A and B')
    // Keyed per session, never per set: either one landing takes it down.
    expect(failure.actionKeys).toEqual(['pin:chat-a', 'pin:chat-b'])
  })

  it('a single reverted pin keeps the shared lead, unchanged by the bulk path', async () => {
    apiMock.setSlotPin.mockRejectedValue(new Error('nope'))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.togglePin('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/pin/i))
    const failure = result.current.failure.failure!
    expect(failure.subject).toBe('A')
    expect(failure.heading).toBeUndefined()
    expect(failure.message).toBe('The pin change was undone.')
  })

  it('names a rapidly toggled session once, not once per rejected click', async () => {
    // Three clicks on ONE session, all rejected: three batch entries, one
    // session. Judging the entries would count "2 sessions" and list "A and A"
    // — a session the reader does not have.
    apiMock.setSlotPin.mockRejectedValue(new Error('nope'))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => {
      result.current.actions.togglePin('chat-a')
      result.current.actions.togglePin('chat-a')
      result.current.actions.togglePin('chat-a')
    })
    await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledTimes(3))
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/pin/i))
    const failure = result.current.failure.failure!
    expect(failure.subject).toBe('A')
    expect(failure.heading).toBeUndefined()
    expect(failure.message).toBe('The pin change was undone.')
    // One session, so one key: re-pinning A takes it down.
    expect(failure.actionKeys).toEqual(['pin:chat-a'])
  })

  it('counts only the sessions it can name when one left the list mid-write', async () => {
    store.dispatch(sseSlots([
      { key: 'chat-a', title: 'A', mode: 'chat', pinned: false, folder_id: null },
      { key: 'chat-b', title: 'B', mode: 'chat', pinned: false, folder_id: null },
      { key: 'chat-c', title: 'C', mode: 'chat', pinned: false, folder_id: null },
    ] as never))
    const rejecters: ((reason: unknown) => void)[] = []
    apiMock.setSlotPin.mockImplementation(() => new Promise((_resolve, reject) => { rejecters.push(reject) }))
    // The authoritative frame: the server kept all three unpinned.
    apiMock.chatSlots.mockResolvedValue([
      { key: 'chat-a', title: 'A', mode: 'chat', pinned: false, folder_id: null },
      { key: 'chat-b', title: 'B', mode: 'chat', pinned: false, folder_id: null },
      { key: 'chat-c', title: 'C', mode: 'chat', pinned: false, folder_id: null },
    ] as never)
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => {
      result.current.actions.togglePin('chat-a')
      result.current.actions.togglePin('chat-b')
      result.current.actions.togglePin('chat-c')
    })
    await waitFor(() => expect(rejecters).toHaveLength(3))
    // C is closed while its pin is in flight: at report time it has no row, so
    // no title to list — and a heading that still counted it would promise a
    // third session the sentence never names.
    act(() => { store.dispatch(removeSlotOptimistic('chat-c')) })
    await act(async () => { for (const reject of rejecters) reject(new Error('nope')); await Promise.resolve() })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/pin/i))
    const failure = result.current.failure.failure!
    expect(failure.message).toBe('The pin change was undone for A and B.')
    expect(failure.heading).toBe('Couldn’t update 2 sessions')
  })

  it('attaches the journal report, so the hand-off carries endpoint and status', async () => {
    __resetErrorJournalForTests()
    recordError({ source: 'api', message: 'nope', status: 409, endpoint: '/api/chat/slots/chat-a/mode' })
    apiMock.setSlotMode.mockRejectedValue(new Error('nope'))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.toggleMode('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/mode/i))
    expect(result.current.failure.failure?.report?.status).toBe(409)
    expect(result.current.failure.failure?.report?.endpoint).toBe('/api/chat/slots/chat-a/mode')
  })

  it('reports a rejected reload instead of raising a native alert', async () => {
    apiMock.chatSlotReload.mockRejectedValue(new Error('nope'))
    const alerted = vi.fn()
    vi.stubGlobal('alert', alerted)
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.reload('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/reload/i))
    expect(alerted).not.toHaveBeenCalled()
  })

  it('drops the unreachable hedge when the gateway answered', async () => {
    // An ApiError means the gateway replied, and reload is offline-gated by this
    // PR, so naming the gateway unreachable sends the reader down a dead end.
    apiMock.chatSlotReload.mockRejectedValue(new ApiError(409, 'a turn is in flight'))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.reload('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/reload/i))
    expect(result.current.failure.failure?.message).not.toMatch(/unreachable/i)
  })

  it('tells a 409 reader to wait for idle, the one answer waiting can outlast', async () => {
    apiMock.chatSlotReload.mockRejectedValue(new ApiError(409, 'a turn is in flight', '{"code":"turn_in_flight"}'))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.reload('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/reload/i))
    expect(result.current.failure.failure?.message).toMatch(/mid-turn/i)
    expect(result.current.failure.failure?.message).toMatch(/idle/i)
  })

  it.each([
    [404, '{"error":"not found","code":"slot_not_found"}'],
    [500, ''],
  ])('does not send a %i reader to wait for idle', async (status, body) => {
    // The slot is gone, or the gateway fell over: neither is a busy agent, so
    // "try again when the session is idle" would be advice that can never land.
    apiMock.chatSlotReload.mockRejectedValue(new ApiError(status, `HTTP ${status}`, body))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.reload('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/reload/i))
    const message = result.current.failure.failure?.message ?? ''
    expect(message).not.toMatch(/mid-turn|idle/i)
    expect(message).not.toMatch(/unreachable|reconnect/i)
    // What the reader needs to know is what did NOT happen.
    expect(message).toMatch(/not restarted/i)
  })

  it('names the sub-agents when the 409 says so, ahead of the generic busy copy', async () => {
    apiMock.chatSlotReload.mockRejectedValue(
      new ApiError(409, 'sub-agents are running', '{"error":"sub-agents are running","code":"slot_subagents_running"}'),
    )
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.reload('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/sub-agents/i))
    expect(result.current.failure.failure?.message).not.toMatch(/mid-turn/i)
  })

  it('states the transport failure rather than hedging about it', async () => {
    apiMock.chatSlotReload.mockRejectedValue(new TypeError('Failed to fetch'))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.reload('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/reload/i))
    const message = result.current.failure.failure?.message ?? ''
    expect(message).toMatch(/unreachable/i)
    // The wait-for-idle recovery belongs to the busy case this branch ruled out.
    expect(message).not.toMatch(/may be|mid-turn|idle/i)
    expect(message).toMatch(/reconnect/i)
  })

  it('reports a rejected folder move', async () => {
    apiMock.setSlotFolder.mockRejectedValue(new Error('nope'))
    const { result } = renderHook(() => ({
      move: useMoveSlotToFolder(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.move('chat-a', 'f1') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/move/i))
  })

  it('stays silent when a superseded move fails, since nothing was undone', async () => {
    let rejectFirst: (e: unknown) => void = () => {}
    apiMock.setSlotFolder
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectFirst = reject }))
      .mockResolvedValue({} as never)
    const { result } = renderHook(() => ({
      move: useMoveSlotToFolder(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.move('chat-a', 'f1') })
    await act(async () => { result.current.move('chat-a', 'f2') })
    await waitFor(() =>
      expect(store.getState().dashboard.slots.find(s => s.key === 'chat-a')?.folder_id).toBe('f2'))
    await act(async () => { rejectFirst(new Error('nope')); await Promise.resolve() })
    await waitFor(() => expect(apiMock.setSlotFolder).toHaveBeenCalledTimes(2))
    expect(result.current.failure.failure).toBeNull()
    expect(store.getState().dashboard.slots.find(s => s.key === 'chat-a')?.folder_id).toBe('f2')
  })

  it('reports a rejected fork, which otherwise switched to no session at all', async () => {
    apiMock.forkChatSlot.mockRejectedValue(new Error('nope'))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.duplicate('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.heading).toMatch(/duplicate/i))
    // Its own lead: a fork that failed created nothing, so "Couldn’t update" — the
    // shared lead for a write that reverted — would name a change that never was.
    expect(result.current.failure.failure?.heading).toContain('“A”')
    expect(result.current.failure.failure?.heading).not.toMatch(/update/i)
    expect(result.current.failure.failure?.message).toBe('No new session was created.')
  })

  it('shows a sentence rather than the transport text a reader cannot act on', async () => {
    apiMock.setSlotMode.mockRejectedValue(new Error('Failed to fetch'))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.toggleMode('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toBeTruthy())
    expect(result.current.failure.failure?.message).not.toContain('Failed to fetch')
  })
})

describe('a write that lands takes down the failure it was the retry of', () => {
  beforeEach(() => {
    __resetActionFailureForTests()
    vi.clearAllMocks()
    vi.stubGlobal('confirm', () => true)
    apiMock.chatSlots.mockResolvedValue([])
    store.dispatch(sseSlots([
      { key: 'chat-a', title: 'A', mode: 'chat', pinned: false, folder_id: null },
      { key: 'chat-b', title: 'B', mode: 'chat', pinned: false, folder_id: null },
    ] as never))
  })

  it('a mode switch that lands clears the failed mode switch before it', async () => {
    apiMock.setSlotMode.mockRejectedValueOnce(new Error('nope')).mockResolvedValue({})
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.toggleMode('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/mode/i))
    await act(async () => { result.current.actions.toggleMode('chat-a') })
    await waitFor(() => expect(apiMock.setSlotMode).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(result.current.failure.failure).toBeNull())
  })

  it('a different action landing leaves the failure up', async () => {
    // The banner is about the mode switch; a pin that lands says nothing about
    // it, and the reader may not have read it yet.
    apiMock.setSlotMode.mockRejectedValue(new Error('nope'))
    apiMock.setSlotPin.mockResolvedValue({})
    apiMock.chatSlots.mockResolvedValue([
      { key: 'chat-a', title: 'A', mode: 'chat', pinned: true, folder_id: null },
    ] as never)
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.toggleMode('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/mode/i))
    const shown = result.current.failure.failure
    await act(async () => { result.current.actions.togglePin('chat-a') })
    await waitFor(() => expect(apiMock.chatSlots).toHaveBeenCalled())
    await waitFor(() =>
      expect(store.getState().dashboard.slots.find(s => s.key === 'chat-a')?.pinned).toBe(true))
    expect(result.current.failure.failure).toBe(shown)
  })

  it('the same action landing on another session leaves the failure up', async () => {
    // "The mode change was undone" is still true of A after B's switch lands.
    apiMock.setSlotMode.mockRejectedValueOnce(new Error('nope')).mockResolvedValue({})
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.toggleMode('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.subject).toBe('A'))
    const shown = result.current.failure.failure
    await act(async () => { result.current.actions.toggleMode('chat-b') })
    await waitFor(() => expect(apiMock.setSlotMode).toHaveBeenCalledTimes(2))
    await waitFor(() =>
      expect(store.getState().dashboard.slots.find(s => s.key === 'chat-b')?.mode).toBe('orchestrator'))
    expect(result.current.failure.failure).toBe(shown)
  })

  it('a pin that lands clears the reverted pin before it', async () => {
    apiMock.setSlotPin.mockRejectedValueOnce(new Error('nope')).mockResolvedValue({})
    apiMock.chatSlots
      .mockResolvedValueOnce([] as never)
      .mockResolvedValue([
        { key: 'chat-a', title: 'A', mode: 'chat', pinned: true, folder_id: null },
      ] as never)
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.togglePin('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/pin/i))
    await act(async () => { result.current.actions.togglePin('chat-a') })
    await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(result.current.failure.failure).toBeNull())
  })

  // A bulk revert's banner is keyed per (action, slot), never per set: the first
  // named slot whose write lands takes it down, however that write is batched.
  describe('a bulk pin revert over A and B', () => {
    const pinnedRow = (key: string, title: string, pinned: boolean) =>
      ({ key, title, mode: 'chat', pinned, folder_id: null })

    async function revertBoth(result: { current: { actions: ReturnType<typeof useSessionActions>; failure: ReturnType<typeof useActionFailure> } }) {
      apiMock.setSlotPin.mockRejectedValueOnce(new Error('nope')).mockRejectedValueOnce(new Error('nope')).mockResolvedValue({})
      // The authoritative frame after the rejection: neither pin held.
      apiMock.chatSlots.mockResolvedValueOnce([pinnedRow('chat-a', 'A', false), pinnedRow('chat-b', 'B', false), pinnedRow('chat-c', 'C', false)] as never)
      await act(async () => {
        result.current.actions.togglePin('chat-a')
        result.current.actions.togglePin('chat-b')
      })
      await waitFor(() => expect(result.current.failure.failure?.message).toBe('The pin change was undone for A and B.'))
      expect(result.current.failure.failure?.actionKeys).toEqual(['pin:chat-a', 'pin:chat-b'])
    }

    beforeEach(() => {
      store.dispatch(sseSlots([
        pinnedRow('chat-a', 'A', false), pinnedRow('chat-b', 'B', false), pinnedRow('chat-c', 'C', false),
      ] as never))
    })

    it('a retry that lands a SUPERSET of the reverted set takes the banner down', async () => {
      const { result } = renderHook(() => ({
        actions: useSessionActions(),
        failure: useActionFailure(),
      }), { wrapper })
      await revertBoth(result)
      apiMock.chatSlots.mockResolvedValue([pinnedRow('chat-a', 'A', true), pinnedRow('chat-b', 'B', true), pinnedRow('chat-c', 'C', true)] as never)
      await act(async () => {
        result.current.actions.togglePin('chat-a')
        result.current.actions.togglePin('chat-b')
        result.current.actions.togglePin('chat-c')
      })
      await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledTimes(5))
      await waitFor(() => expect(result.current.failure.failure).toBeNull())
    })

    it('retries split across batches take the banner down as each lands', async () => {
      const { result } = renderHook(() => ({
        actions: useSessionActions(),
        failure: useActionFailure(),
      }), { wrapper })
      await revertBoth(result)
      apiMock.chatSlots.mockResolvedValueOnce([pinnedRow('chat-a', 'A', true), pinnedRow('chat-b', 'B', false), pinnedRow('chat-c', 'C', false)] as never)
      await act(async () => { result.current.actions.togglePin('chat-a') })
      await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledTimes(3))
      await waitFor(() => expect(result.current.failure.failure).toBeNull())
      apiMock.chatSlots.mockResolvedValueOnce([pinnedRow('chat-a', 'A', true), pinnedRow('chat-b', 'B', true), pinnedRow('chat-c', 'C', false)] as never)
      await act(async () => { result.current.actions.togglePin('chat-b') })
      await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledTimes(4))
      await waitFor(() =>
        expect(store.getState().dashboard.slots.find(s => s.key === 'chat-b')?.pinned).toBe(true))
      expect(result.current.failure.failure).toBeNull()
    })

    it('re-pinning ONE of the N reverted sessions alone takes the banner down', async () => {
      // Under a set key this retry matched nothing and "was undone for A and B"
      // stayed up over a pinned B.
      const { result } = renderHook(() => ({
        actions: useSessionActions(),
        failure: useActionFailure(),
      }), { wrapper })
      await revertBoth(result)
      apiMock.chatSlots.mockResolvedValue([pinnedRow('chat-a', 'A', false), pinnedRow('chat-b', 'B', true), pinnedRow('chat-c', 'C', false)] as never)
      await act(async () => { result.current.actions.togglePin('chat-b') })
      await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledTimes(3))
      await waitFor(() => expect(result.current.failure.failure).toBeNull())
    })

    it('a pin landing on a session the banner does not name leaves it up', async () => {
      const { result } = renderHook(() => ({
        actions: useSessionActions(),
        failure: useActionFailure(),
      }), { wrapper })
      await revertBoth(result)
      const shown = result.current.failure.failure
      apiMock.chatSlots.mockResolvedValue([pinnedRow('chat-a', 'A', false), pinnedRow('chat-b', 'B', false), pinnedRow('chat-c', 'C', true)] as never)
      await act(async () => { result.current.actions.togglePin('chat-c') })
      await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledTimes(3))
      await waitFor(() =>
        expect(store.getState().dashboard.slots.find(s => s.key === 'chat-c')?.pinned).toBe(true))
      expect(result.current.failure.failure).toBe(shown)
    })
  })

  it('a move that lands clears the reverted move before it', async () => {
    apiMock.setSlotFolder.mockRejectedValueOnce(new Error('nope')).mockResolvedValue({})
    const { result } = renderHook(() => ({
      move: useMoveSlotToFolder(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.move('chat-a', 'f1') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/move/i))
    await act(async () => { result.current.move('chat-a', 'f1') })
    await waitFor(() => expect(apiMock.setSlotFolder).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(result.current.failure.failure).toBeNull())
  })

  it('a reload that lands clears the failed reload before it', async () => {
    apiMock.chatSlotReload.mockRejectedValueOnce(new Error('nope')).mockResolvedValue({})
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.reload('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/reload/i))
    await act(async () => { result.current.actions.reload('chat-a') })
    await waitFor(() => expect(apiMock.chatSlotReload).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(result.current.failure.failure).toBeNull())
  })

  it('a fork that creates a session clears the failed fork before it', async () => {
    apiMock.forkChatSlot.mockRejectedValueOnce(new Error('nope')).mockResolvedValue({ ok: true, key: 'chat-a2' })
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.duplicate('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.heading).toMatch(/duplicate/i))
    await act(async () => { result.current.actions.duplicate('chat-a') })
    await waitFor(() => expect(apiMock.forkChatSlot).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(result.current.failure.failure).toBeNull())
  })
})
