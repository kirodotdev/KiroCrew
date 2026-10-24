/** A question this tab queues or edits while another tab closes the side conversation.
 *  The gateway drops the queue with the conversation, so the submitting tab
 *  must preserve the latest text that no longer has a queue card. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import reducer, { sideClose, sseSideQueue, sseSideResult, takeSideSteerDroppedByClose } from '../store/chatSlice'
import { store as appStore } from '../store'
import { renderWithProviders, createTestStore } from './helpers'
import dashboardReducer from '../store/dashboardSlice'
import { useWebSocket } from '../hooks/useWebSocket'

vi.mock('../api/client', () => ({
  api: {
    sideState: vi.fn().mockResolvedValue({ open: false }),
    sideOpen: vi.fn().mockResolvedValue({ ok: true, open: true, messages: 2, last_run_id: 'r1', created_at: '2026-05-20T00:00:00Z' }),
    sideTurn: vi.fn(),
    sideTools: vi.fn().mockResolvedValue({ read_only_tools: true }),
    sideQueueEdit: vi.fn().mockResolvedValue({ ok: true, depth: 1 }),
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))

import SideChat from '../pages/chat/SideChat'
import { api } from '../api/client'
import { clearSideChatDrafts, readSideChatDraft } from '../chat-core/composer/sideChatDrafts'

type SideTurnBody = Awaited<ReturnType<typeof api.sideTurn>>

const SLOT = 'closed-elsewhere-slot'
const initial = reducer(undefined, { type: '@@INIT' })
const WS_INSTANCES: MockWebSocket[] = []

class MockWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  readyState = MockWebSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn()
  close = vi.fn()

  constructor() { WS_INSTANCES.push(this) }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }
}

function SocketHarness() {
  useWebSocket()
  return null
}

/** A side mid-turn, so a submit can only steer or queue. */
function busyStore() {
  return createTestStore({
    dashboard: { ...dashboardReducer(undefined, { type: '@@INIT' }), connected: true },
    chat: {
      ...initial,
      activeSlot: SLOT,
      slotSideRestored: { [SLOT]: true },
      slotSide: {
        [SLOT]: {
          messages: [
            { role: 'user' as const, content: 'q1', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
            { role: 'assistant' as const, content: 'partial', ts: '2026-05-20T00:00:01Z', run_id: 'r1' },
          ],
          lastRunId: 'r1',
          pending: false,
          streaming: true,
          openedAtTurnCount: 0,
          createdAt: '2026-05-20T00:00:00Z',
        },
      },
    },
  })
}

/** A settled answer with suggestions while the side is still busy (a steer awaiting
 *  consumption), so a suggestion's send is queued rather than started. */
function chipStore() {
  return createTestStore({
    dashboard: { ...dashboardReducer(undefined, { type: '@@INIT' }), connected: true },
    chat: {
      ...initial,
      activeSlot: SLOT,
      slotSideRestored: { [SLOT]: true },
      slotSide: {
        [SLOT]: {
          messages: [
            { role: 'user' as const, content: 'q1', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
            { role: 'assistant' as const, content: 'Next? [OPTIONS: Run tests | Skip]', ts: '2026-05-20T00:00:01Z', run_id: 'r1' },
          ],
          lastRunId: 'r1',
          pending: true,
          streaming: false,
          openedAtTurnCount: 0,
          createdAt: '2026-05-20T00:00:00Z',
        },
      },
    },
  })
}

describe('SideChat: the conversation closes in another tab while a question is on its way', () => {
  beforeEach(() => {
    localStorage.clear()
    clearSideChatDrafts()
    WS_INSTANCES.length = 0
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.mocked(api.sideTurn).mockReset()
    vi.mocked(api.sideQueueEdit).mockReset().mockResolvedValue({ ok: true, depth: 1 })
  })

  afterEach(() => {
    act(() => {
      appStore.dispatch(sideClose(SLOT))
      clearSideChatDrafts()
    })
    vi.unstubAllGlobals()
  })

  it('hands a requeued steer back after a replacement conversation reopens before its demoted receipt', async () => {
    const raw = 'question from the closed conversation'
    let answer: (body: SideTurnBody) => void = () => {}
    vi.mocked(api.sideTurn).mockImplementationOnce(() => new Promise<SideTurnBody>(resolve => { answer = resolve }))
    act(() => {
      appStore.dispatch(sideClose(SLOT))
      appStore.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1', message_id: 'u1' }))
      appStore.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'partial' }))
    })
    renderWithProviders(<><SocketHarness /><SideChat slot={SLOT} /></>, { store: appStore })
    await screen.findByRole('note')
    const socket = WS_INSTANCES[0]
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Ask a side question'), raw)
    await user.click(screen.getByTestId('busy-send-caret'))
    await user.click(screen.getByTestId('busy-send-mode-steer'))
    await user.click(screen.getByTestId('busy-send-button'))
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, raw, { steer: true }))

    act(() => {
      appStore.dispatch(sseSideQueue({
        slot: SLOT, action: 'push', queue_id: 'q-requeued', content: '[scrubbed]',
        front: true, steer_id: 'steer-requeued',
      }))
      socket.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: 'another-tab' } })
      socket.simulateMessage({
        type: 'chat.side_result',
        data: { slot: SLOT, run_id: 'r2', role: 'user', content: 'replacement question', message_id: 'u2' },
      })
    })
    await act(async () => {
      answer({ ok: true, queued: true, demoted: true, depth: 1, steer_id: 'steer-requeued' })
    })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe(raw))
    expect(appStore.getState().chat.slotSide[SLOT]?.queue).toBeUndefined()
  })

  it('hands a requeued steer back once when its demoted receipt precedes the close', async () => {
    const raw = 'question whose receipt arrives first'
    let answer: (body: SideTurnBody) => void = () => {}
    vi.mocked(api.sideTurn).mockImplementationOnce(() => new Promise<SideTurnBody>(resolve => { answer = resolve }))
    act(() => {
      appStore.dispatch(sideClose(SLOT))
      appStore.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1', message_id: 'u1' }))
      appStore.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'partial' }))
    })
    renderWithProviders(<><SocketHarness /><SideChat slot={SLOT} /></>, { store: appStore })
    await screen.findByRole('note')
    const socket = WS_INSTANCES[0]
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Ask a side question'), raw)
    await user.click(screen.getByTestId('busy-send-caret'))
    await user.click(screen.getByTestId('busy-send-mode-steer'))
    await user.click(screen.getByTestId('busy-send-button'))
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, raw, { steer: true }))

    act(() => {
      appStore.dispatch(sseSideQueue({
        slot: SLOT, action: 'push', queue_id: 'q-requeued-first', content: '[scrubbed]',
        front: true, steer_id: 'steer-receipt-first',
      }))
    })
    await act(async () => {
      answer({ ok: true, queued: true, demoted: true, depth: 1, steer_id: 'steer-receipt-first' })
    })
    await waitFor(() => expect(appStore.getState().chat.slotSide[SLOT]?.queue?.[0]).toEqual(
      expect.objectContaining({ content: raw, raw: true }),
    ))
    expect(readSideChatDraft(SLOT)).toBe('')

    act(() => {
      socket.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: 'another-tab' } })
    })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe(raw))
    expect(takeSideSteerDroppedByClose(SLOT, 'steer-receipt-first')).toBe(false)
    expect(readSideChatDraft(SLOT)).toBe(raw)
  })

  it('hands a queued question back after a replacement conversation reopens before its receipt', async () => {
    let answer: (body: SideTurnBody) => void = () => {}
    vi.mocked(api.sideTurn).mockImplementationOnce(() => new Promise<SideTurnBody>(resolve => { answer = resolve }))
    const store = busyStore()
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    await screen.findByRole('note')
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Ask a side question'), 'old conversation question')
    await user.click(screen.getByTestId('busy-send-caret'))
    await user.click(screen.getByTestId('busy-send-mode-queue'))
    await user.click(screen.getByTestId('busy-send-button'))
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, 'old conversation question', undefined))

    act(() => {
      store.dispatch(sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q-old', content: '[scrubbed]' }))
      store.dispatch(sideClose(SLOT))
      store.dispatch(sseSideResult({ slot: SLOT, run_id: 'r2', role: 'user', content: 'replacement question', message_id: 'u2' }))
      store.dispatch(sseSideResult({ slot: SLOT, run_id: 'r2', role: 'assistant', content: 'partial replacement answer' }))
    })
    await act(async () => { answer({ ok: true, queued: true, queue_id: 'q-old', still_queued: true, depth: 1 }) })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe('old conversation question'))
    expect(store.getState().chat.slotSide[SLOT]?.queue).toBeUndefined()
  })

  it('keeps a question queued into the reopened conversation as a card', async () => {
    vi.mocked(api.sideTurn).mockResolvedValueOnce({ ok: true, queued: true, queue_id: 'q-new', still_queued: true, depth: 1 })
    const store = busyStore()
    act(() => {
      store.dispatch(sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q-gone', content: '[scrubbed]' }))
      store.dispatch(sideClose(SLOT))
      store.dispatch(sseSideResult({ slot: SLOT, run_id: 'r2', role: 'user', content: 'replacement question', message_id: 'u2' }))
      store.dispatch(sseSideResult({ slot: SLOT, run_id: 'r2', role: 'assistant', content: 'partial replacement answer' }))
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    await screen.findByRole('note')
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Ask a side question'), 'new conversation question')
    await user.click(screen.getByTestId('busy-send-caret'))
    await user.click(screen.getByTestId('busy-send-mode-queue'))
    await user.click(screen.getByTestId('busy-send-button'))

    await waitFor(() => expect(store.getState().chat.slotSide[SLOT]?.queue).toEqual([
      expect.objectContaining({ id: 'q-new', content: 'new conversation question', raw: true }),
    ]))
    expect(readSideChatDraft(SLOT)).toBe('')
  })

  it('hands a queued question back when the close lands before its receipt', async () => {
    let answer: (body: SideTurnBody) => void = () => {}
    vi.mocked(api.sideTurn).mockImplementationOnce(() => new Promise<SideTurnBody>(resolve => { answer = resolve }))
    const store = busyStore()
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    await screen.findByRole('note')
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Ask a side question'), 'later please')
    await user.click(screen.getByTestId('busy-send-caret'))
    await user.click(screen.getByTestId('busy-send-mode-queue'))
    await user.click(screen.getByTestId('busy-send-button'))
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, 'later please', undefined))

    act(() => {
      store.dispatch(sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q-1', content: 'later please' }))
      store.dispatch(sideClose(SLOT))
    })
    await act(async () => { answer({ ok: true, queued: true, queue_id: 'q-1', still_queued: true, depth: 1 }) })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toContain('later please'))
    expect(store.getState().chat.slotSide[SLOT]).toBeUndefined()
  })

  it('hands a queued question back when the close lands before its receipt and its queue frame never arrived', async () => {
    let answer: (body: SideTurnBody) => void = () => {}
    vi.mocked(api.sideTurn).mockImplementationOnce(() => new Promise<SideTurnBody>(resolve => { answer = resolve }))
    const store = busyStore()
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    await screen.findByRole('note')
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Ask a side question'), 'lost frame question')
    await user.click(screen.getByTestId('busy-send-caret'))
    await user.click(screen.getByTestId('busy-send-mode-queue'))
    await user.click(screen.getByTestId('busy-send-button'))
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, 'lost frame question', undefined))

    // The queue frame for this question was lost with a dropped socket; only the close arrives.
    act(() => { store.dispatch(sideClose(SLOT)) })
    await act(async () => { answer({ ok: true, queued: true, queue_id: 'q-lost', still_queued: true, depth: 1 }) })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe('lost frame question'))
    expect(store.getState().chat.slotSide[SLOT]).toBeUndefined()
  })

  it('hands a suggestion\'s question back beside the draft when the close lands before its receipt', async () => {
    let answer: (body: SideTurnBody) => void = () => {}
    vi.mocked(api.sideTurn).mockImplementationOnce(() => new Promise<SideTurnBody>(resolve => { answer = resolve }))
    const store = chipStore()
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    await screen.findByRole('note')
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Ask a side question'), 'my own draft')
    await user.click(screen.getByLabelText('Send now: Run tests'))
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, 'Run tests', undefined))

    act(() => {
      store.dispatch(sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q-1', content: 'Run tests' }))
      store.dispatch(sideClose(SLOT))
    })
    await act(async () => { answer({ ok: true, queued: true, queue_id: 'q-1', still_queued: true, depth: 1 }) })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe('my own draft\n\nRun tests'))
    expect(store.getState().chat.slotSide[SLOT]).toBeUndefined()
  })

  it('keeps the saved edit once when its broadcast and a close beat its successful receipt', async () => {
    let finishEdit: (body: { ok: true; depth: number }) => void = () => {}
    vi.mocked(api.sideQueueEdit).mockImplementationOnce(
      () => new Promise(resolve => { finishEdit = resolve })
    )
    appStore.dispatch(sideClose(SLOT))
    appStore.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1', message_id: 'u1' }))
    appStore.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'partial', message_id: 'a1' }))
    appStore.dispatch(sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q-1', content: 'old wording', raw: true }))

    renderWithProviders(<><SocketHarness /><SideChat slot={SLOT} /></>, { store: appStore })
    await screen.findByRole('note')
    const socket = WS_INSTANCES[0]
    const user = userEvent.setup()
    await user.click(screen.getByLabelText('Edit queued message'))
    await user.clear(screen.getByLabelText('Edit queued message'))
    await user.type(screen.getByLabelText('Edit queued message'), 'edited wording{Enter}')
    await waitFor(() => expect(api.sideQueueEdit).toHaveBeenCalledWith(SLOT, 'q-1', 'edited wording'))

    act(() => {
      socket.simulateMessage({ type: 'chat.side_queue', data: { slot: SLOT, action: 'edit', queue_id: 'q-1', content: 'edited wording' } })
      socket.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: 'another-tab' } })
    })
    await act(async () => { finishEdit({ ok: true, depth: 1 }) })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe('edited wording'))
    expect(readSideChatDraft(SLOT)).not.toContain('old wording')
  })

  it('keeps the saved edit once when the queue is gone and the edit fails', async () => {
    let failEdit: (error: Error) => void = () => {}
    vi.mocked(api.sideQueueEdit).mockImplementationOnce(
      () => new Promise((_resolve, reject) => { failEdit = reject })
    )
    appStore.dispatch(sideClose(SLOT))
    appStore.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1', message_id: 'u1' }))
    appStore.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'partial', message_id: 'a1' }))
    appStore.dispatch(sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q-1', content: 'old wording', raw: true }))

    renderWithProviders(<><SocketHarness /><SideChat slot={SLOT} /></>, { store: appStore })
    await screen.findByRole('note')
    const socket = WS_INSTANCES[0]
    const user = userEvent.setup()
    await user.click(screen.getByLabelText('Edit queued message'))
    await user.clear(screen.getByLabelText('Edit queued message'))
    await user.type(screen.getByLabelText('Edit queued message'), 'edited wording{Enter}')
    await waitFor(() => expect(api.sideQueueEdit).toHaveBeenCalledWith(SLOT, 'q-1', 'edited wording'))

    act(() => {
      socket.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: 'another-tab' } })
    })
    await act(async () => { failEdit(new Error('queue entry not found')); await Promise.resolve() })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe('edited wording'))
  })

  it('keeps a broadcast-confirmed edit when its failed response precedes the close', async () => {
    let loseResponse: (error: Error) => void = () => {}
    vi.mocked(api.sideQueueEdit).mockImplementationOnce(
      () => new Promise((_resolve, reject) => { loseResponse = reject })
    )
    appStore.dispatch(sideClose(SLOT))
    appStore.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1', message_id: 'u1' }))
    appStore.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'partial', message_id: 'a1' }))
    appStore.dispatch(sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q-1', content: 'old wording', raw: true }))

    renderWithProviders(<><SocketHarness /><SideChat slot={SLOT} /></>, { store: appStore })
    await screen.findByRole('note')
    const socket = WS_INSTANCES[0]
    const user = userEvent.setup()
    await user.click(screen.getByLabelText('Edit queued message'))
    await user.clear(screen.getByLabelText('Edit queued message'))
    await user.type(screen.getByLabelText('Edit queued message'), 'edited wording{Enter}')
    await waitFor(() => expect(api.sideQueueEdit).toHaveBeenCalledWith(SLOT, 'q-1', 'edited wording'))

    act(() => {
      socket.simulateMessage({ type: 'chat.side_queue', data: { slot: SLOT, action: 'edit', queue_id: 'q-1', content: 'edited wording' } })
    })
    await act(async () => { loseResponse(new Error('response lost')); await Promise.resolve() })
    act(() => {
      socket.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: 'another-tab' } })
    })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe('edited wording'))
    expect(readSideChatDraft(SLOT)).not.toContain('old wording')
  })

  it('keeps the saved edit once when its response is lost after the broadcast and close', async () => {
    let loseResponse: (error: Error) => void = () => {}
    vi.mocked(api.sideQueueEdit).mockImplementationOnce(
      () => new Promise((_resolve, reject) => { loseResponse = reject })
    )
    appStore.dispatch(sideClose(SLOT))
    appStore.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1', message_id: 'u1' }))
    appStore.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'partial', message_id: 'a1' }))
    appStore.dispatch(sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q-1', content: 'old wording', raw: true }))

    renderWithProviders(<><SocketHarness /><SideChat slot={SLOT} /></>, { store: appStore })
    await screen.findByRole('note')
    const socket = WS_INSTANCES[0]
    const user = userEvent.setup()
    await user.click(screen.getByLabelText('Edit queued message'))
    await user.clear(screen.getByLabelText('Edit queued message'))
    await user.type(screen.getByLabelText('Edit queued message'), 'edited wording{Enter}')
    await waitFor(() => expect(api.sideQueueEdit).toHaveBeenCalledWith(SLOT, 'q-1', 'edited wording'))

    act(() => {
      socket.simulateMessage({ type: 'chat.side_queue', data: { slot: SLOT, action: 'edit', queue_id: 'q-1', content: 'edited wording' } })
      socket.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: 'another-tab' } })
    })
    await act(async () => { loseResponse(new Error('response lost')); await Promise.resolve() })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe('edited wording'))
  })
})
