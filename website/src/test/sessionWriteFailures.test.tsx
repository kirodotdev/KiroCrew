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
import { pinMutationKeysInFlight, useSessionActions } from '../hooks/useSessionActions'
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

  it('reports nothing for a rejected mode switch on a session that left the list mid-write', async () => {
    // Same shape as the pin case: the absent row read as mode '' — the very value
    // a switch back to normal chat writes — so that direction reported an undo of
    // a row that no longer exists, while a switch to Autopilot stayed silent.
    store.dispatch(sseSlots([
      { key: 'chat-a', title: 'A', mode: 'orchestrator', pinned: false, folder_id: null } as never,
    ]))
    let rejectMode!: (reason: unknown) => void
    apiMock.setSlotMode.mockImplementation(() => new Promise((_resolve, reject) => { rejectMode = reject }))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.toggleMode('chat-a') })
    await waitFor(() => expect(apiMock.setSlotMode).toHaveBeenCalledWith('chat-a', ''))
    act(() => { store.dispatch(removeSlotOptimistic('chat-a')) })
    await act(async () => { rejectMode(new Error('nope')); await Promise.resolve() })
    expect(result.current.failure.failure).toBeNull()
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
  })

  it('counts and names an untitled bulk pin rejection and attaches its report', async () => {
    __resetErrorJournalForTests()
    store.dispatch(sseSlots([
      { key: 'chat-untitled', title: '', mode: 'chat', pinned: false, folder_id: null },
      { key: 'chat-a', title: 'A', mode: 'chat', pinned: false, folder_id: null },
      { key: 'chat-b', title: 'B', mode: 'chat', pinned: false, folder_id: null },
    ] as never))
    for (const key of ['chat-untitled', 'chat-a', 'chat-b']) {
      recordError({
        source: 'api',
        message: `pin-${key}-rejected`,
        status: 409,
        endpoint: `/api/chat/slots/${key}/pin`,
      })
    }
    apiMock.setSlotPin.mockImplementation((key: string) =>
      Promise.reject(new Error(`pin-${key}-rejected`)))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => {
      result.current.actions.togglePin('chat-untitled')
      result.current.actions.togglePin('chat-a')
      result.current.actions.togglePin('chat-b')
    })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/pin/i))
    const failure = result.current.failure.failure!
    expect(failure.heading).toBe('Couldn’t update 3 sessions')
    expect(failure.message).toBe('The pin change was undone for chat-untitled, A, and B.')
    expect(failure.subject).toBe('chat-untitled, A, and B')
    expect(failure.report?.endpoint).toBe('/api/chat/slots/chat-untitled/pin')
    expect(failure.message).toContain('chat-untitled')
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

  it('reports nothing for a rejected pin on a session that left the list mid-write', async () => {
    // With the session gone there is no row to roll back and none to name. The
    // absent row used to read as unpinned, so a failed PIN raised the unnamed
    // heading while a failed UNPIN stayed silent; both are silent now.
    let rejectPin!: (reason: unknown) => void
    apiMock.setSlotPin.mockImplementation(() => new Promise((_resolve, reject) => { rejectPin = reject }))
    // The authoritative frame no longer carries the session either.
    apiMock.chatSlots.mockResolvedValue([] as never)
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.togglePin('chat-a') })
    await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledWith('chat-a', true))
    act(() => { store.dispatch(removeSlotOptimistic('chat-a')) })
    await act(async () => { rejectPin(new Error('nope')); await Promise.resolve() })
    await waitFor(() => expect(apiMock.chatSlots).toHaveBeenCalled())
    await waitFor(() => expect(pinMutationKeysInFlight()).toEqual([]))
    expect(result.current.failure.failure).toBeNull()

    // The mirror direction: a pinned session unpinned, then closed, then refused.
    apiMock.chatSlots.mockClear()
    store.dispatch(sseSlots([
      { key: 'chat-a', title: 'A', mode: 'chat', pinned: true, folder_id: null } as never,
    ]))
    await act(async () => { result.current.actions.togglePin('chat-a') })
    await waitFor(() => expect(apiMock.setSlotPin).toHaveBeenCalledWith('chat-a', false))
    act(() => { store.dispatch(removeSlotOptimistic('chat-a')) })
    await act(async () => { rejectPin(new Error('nope')); await Promise.resolve() })
    await waitFor(() => expect(apiMock.chatSlots).toHaveBeenCalled())
    await waitFor(() => expect(pinMutationKeysInFlight()).toEqual([]))
    expect(result.current.failure.failure).toBeNull()
  })

  it('still names a session that is present when its pin is rejected', async () => {
    // The vanished-session skip must not swallow a session that is still listed.
    apiMock.setSlotPin.mockRejectedValue(new Error('nope'))
    apiMock.chatSlots.mockResolvedValue([
      { key: 'chat-a', title: 'A', mode: 'chat', pinned: false, folder_id: null },
    ] as never)
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.togglePin('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toBe('The pin change was undone.'))
    expect(result.current.failure.failure?.subject).toBe('A')
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

    recordError({ source: 'api', message: 'nope', status: 409, endpoint: '/api/chat/slots/chat-a/pin' })
    apiMock.setSlotPin.mockRejectedValue(new Error('nope'))
    apiMock.chatSlots.mockResolvedValue([])
    await act(async () => { result.current.actions.togglePin('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/pin/i))
    expect(result.current.failure.failure?.report?.status).toBe(409)
    expect(result.current.failure.failure?.report?.endpoint).toBe('/api/chat/slots/chat-a/pin')
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
    // A reload changes no setting, so it gets its own lead rather than the shared
    // "Couldn’t update", the same rule fork and send follow.
    expect(result.current.failure.failure?.heading).toBe('Couldn’t reload “A”')
    expect(result.current.failure.failure?.subject).toBe('A')
  })

  it('reports nothing for a rejected reload on a session that left the list mid-write', async () => {
    let rejectReload!: (reason: unknown) => void
    apiMock.chatSlotReload.mockImplementation(() =>
      new Promise((_resolve, reject) => { rejectReload = reject }))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.reload('chat-a') })
    await waitFor(() => expect(apiMock.chatSlotReload).toHaveBeenCalledWith('chat-a'))
    act(() => { store.dispatch(removeSlotOptimistic('chat-a')) })
    await act(async () => { rejectReload(new Error('nope')); await Promise.resolve() })
    expect(result.current.failure.failure).toBeNull()
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

  it('tells a turn_in_flight reader definitely to wait for idle', async () => {
    apiMock.chatSlotReload.mockRejectedValue(new ApiError(409, 'a turn is in flight', '{"code":"turn_in_flight"}'))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.reload('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/reload/i))
    const message = result.current.failure.failure?.message ?? ''
    expect(message).toBe('Session reload failed — the agent is mid-turn. Try again when the session is idle.')
    expect(message).not.toMatch(/may be/i)
  })

  it('explains when the session was rebound without sending the reader to idle', async () => {
    apiMock.chatSlotReload.mockRejectedValue(new ApiError(409, 'slot session was rebound', '{"code":"session_rebound"}'))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.reload('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/reload/i))
    const message = result.current.failure.failure?.message ?? ''
    expect(message).toBe('Session reload failed — this session was replaced while the reload ran. Check the session, then try again.')
    expect(message).not.toMatch(/idle/i)
  })

  it('uses the hedged fallback for an unrecognized 409 code', async () => {
    apiMock.chatSlotReload.mockRejectedValue(new ApiError(409, 'conflict', '{"code":"future_conflict"}'))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.reload('chat-a') })
    await waitFor(() => expect(result.current.failure.failure?.message).toMatch(/reload/i))
    expect(result.current.failure.failure?.message).toBe(
      'Session reload failed — the session was not in a state that allows a reload. Try again in a moment.',
    )
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

  it('reports nothing for a rejected move on a session that left the list mid-write', async () => {
    // Same shape as the pin case: the absent row read as folder '' — root, the
    // very target a move-to-root writes — so that direction reported an undo of a
    // row that no longer exists, while a move into a folder stayed silent.
    store.dispatch(sseSlots([
      { key: 'chat-a', title: 'A', mode: 'chat', pinned: false, folder_id: 'f1' } as never,
    ]))
    let rejectMove!: (reason: unknown) => void
    apiMock.setSlotFolder.mockImplementation(() => new Promise((_resolve, reject) => { rejectMove = reject }))
    const { result } = renderHook(() => ({
      move: useMoveSlotToFolder(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.move('chat-a', null) })
    await waitFor(() => expect(apiMock.setSlotFolder).toHaveBeenCalledWith('chat-a', null))
    act(() => { store.dispatch(removeSlotOptimistic('chat-a')) })
    await act(async () => { rejectMove(new Error('nope')); await Promise.resolve() })
    expect(result.current.failure.failure).toBeNull()
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

  it('reports nothing for a rejected fork on a session that left the list mid-write', async () => {
    let rejectFork!: (reason: unknown) => void
    apiMock.forkChatSlot.mockImplementation(() =>
      new Promise((_resolve, reject) => { rejectFork = reject }))
    const { result } = renderHook(() => ({
      actions: useSessionActions(),
      failure: useActionFailure(),
    }), { wrapper })
    await act(async () => { result.current.actions.duplicate('chat-a') })
    await waitFor(() => expect(apiMock.forkChatSlot).toHaveBeenCalledWith('chat-a'))
    act(() => { store.dispatch(removeSlotOptimistic('chat-a')) })
    await act(async () => { rejectFork(new Error('nope')); await Promise.resolve() })
    expect(result.current.failure.failure).toBeNull()
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
