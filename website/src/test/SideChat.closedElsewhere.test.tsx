/** A question this tab queues or edits while another tab closes the side conversation.
 *  The gateway drops the queue with the conversation, so the submitting tab
 *  must preserve the latest text that no longer has a queue card. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import reducer, {
  readSideSteerSubmittedText,
  recordSideSteerSubmittedText,
  sideClose,
  sideOptimisticAppend,
  sseSideQueue,
  sseSideResult,
  takeSideSteerDroppedByClose,
} from '../store/chatSlice'
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
import { TAB_ID } from '../api/tabId'

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

  it('hands back a named steer when the requeue frame never reached this tab', async () => {
    const raw = 'steer whose requeue frame was missed'
    recordSideSteerSubmittedText(SLOT, 'steer-missed-frame', raw)
    appStore.dispatch(sideClose(SLOT))
    appStore.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1', message_id: 'u1' }))
    renderWithProviders(<SocketHarness />, { store: appStore })
    const socket = WS_INSTANCES[0]

    act(() => {
      socket.simulateMessage({
        type: 'chat.side_closed',
        data: { kind: 'side', slot: SLOT, origin_client: 'another-tab', dropped: [{ queue_id: 'q-missed-frame', steer_id: 'steer-missed-frame' }] },
      })
      socket.simulateMessage({
        type: 'chat.side_closed',
        data: { kind: 'side', slot: SLOT, origin_client: 'another-tab', dropped: [{ queue_id: 'q-missed-frame', steer_id: 'steer-missed-frame' }] },
      })
    })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe(raw))
    expect(readSideSteerSubmittedText(SLOT, 'steer-missed-frame')).toBeUndefined()
  })

  it('hands back a named steer on this tab own echo without clearing the slot', async () => {
    const raw = 'steer dropped by this tab close'
    recordSideSteerSubmittedText(SLOT, 'steer-own-close', raw)
    appStore.dispatch(sideClose(SLOT))
    appStore.dispatch(sideOptimisticAppend({
      slot: SLOT,
      message: { role: 'user', content: 'replacement question', ts: '2026-10-26T00:00:00.000Z' },
    }))
    renderWithProviders(<SocketHarness />, { store: appStore })
    const socket = WS_INSTANCES[0]

    act(() => {
      socket.simulateMessage({
        type: 'chat.side_closed',
        data: { kind: 'side', slot: SLOT, origin_client: TAB_ID, dropped: [{ queue_id: 'q-own-close', steer_id: 'steer-own-close' }] },
      })
    })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe(raw))
    expect(appStore.getState().chat.slotSide[SLOT]?.messages).toMatchObject([
      { role: 'user', content: 'replacement question', optimistic: true },
    ])
    expect(readSideSteerSubmittedText(SLOT, 'steer-own-close')).toBeUndefined()
  })

  it('hands a card and its named steer back exactly once', async () => {
    const raw = 'steer whose card arrived'
    recordSideSteerSubmittedText(SLOT, 'steer-with-card', raw)
    appStore.dispatch(sideClose(SLOT))
    appStore.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1', message_id: 'u1' }))
    appStore.dispatch(sseSideQueue({
      slot: SLOT,
      action: 'push',
      queue_id: 'q-with-card',
      content: '[scrubbed]',
      front: true,
      steer_id: 'steer-with-card',
    }))
    renderWithProviders(<SocketHarness />, { store: appStore })
    const socket = WS_INSTANCES[0]

    act(() => {
      socket.simulateMessage({
        type: 'chat.side_closed',
        data: { kind: 'side', slot: SLOT, origin_client: 'another-tab', dropped: [{ queue_id: 'q-with-card', steer_id: 'steer-with-card' }] },
      })
    })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe(raw))
    expect(readSideSteerSubmittedText(SLOT, 'steer-with-card')).toBeUndefined()
  })

  it('does not hand back a named steer this tab did not submit', async () => {
    appStore.dispatch(sideClose(SLOT))
    appStore.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1', message_id: 'u1' }))
    renderWithProviders(<SocketHarness />, { store: appStore })
    const socket = WS_INSTANCES[0]

    act(() => {
      socket.simulateMessage({
        type: 'chat.side_closed',
        data: { kind: 'side', slot: SLOT, origin_client: 'another-tab', dropped: [{ queue_id: 'q-from-another-tab', steer_id: 'steer-from-another-tab' }] },
      })
    })

    await waitFor(() => expect(appStore.getState().chat.slotSide[SLOT]).toBeUndefined())
    expect(readSideChatDraft(SLOT)).toBe('')
  })

  it('hands a missed queued question back after a replacement conversation opens', async () => {
    const raw = 'question whose queue frame was missed'
    let answer: (body: SideTurnBody) => void = () => {}
    vi.mocked(api.sideTurn).mockImplementationOnce(() => new Promise<SideTurnBody>(resolve => { answer = resolve }))
    const store = busyStore()
    renderWithProviders(<><SocketHarness /><SideChat slot={SLOT} /></>, { store })
    await screen.findByRole('note')
    const socket = WS_INSTANCES[0]
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Ask a side question'), raw)
    await user.click(screen.getByTestId('busy-send-caret'))
    await user.click(screen.getByTestId('busy-send-mode-queue'))
    await user.click(screen.getByTestId('busy-send-button'))
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, raw, undefined))

    act(() => {
      socket.simulateMessage({
        type: 'chat.side_closed',
        data: { kind: 'side', slot: SLOT, origin_client: 'another-tab', dropped: [{ queue_id: 'q-missed' }] },
      })
      socket.simulateMessage({
        type: 'chat.side_result',
        data: { slot: SLOT, run_id: 'r2', role: 'user', content: 'replacement question', message_id: 'u2' },
      })
    })
    await act(async () => {
      answer({ ok: true, queued: true, queue_id: 'q-missed', still_queued: true, depth: 1 })
    })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe(raw))
    expect(store.getState().chat.slotSide[SLOT]?.queue).toBeUndefined()
  })

  it('hands a missed requeued steer back when its pending receipt arrives', async () => {
    const raw = 'steer whose requeue frame was missed'
    let answer: (body: SideTurnBody) => void = () => {}
    vi.mocked(api.sideTurn).mockImplementationOnce(() => new Promise<SideTurnBody>(resolve => { answer = resolve }))
    const store = busyStore()
    renderWithProviders(<><SocketHarness /><SideChat slot={SLOT} /></>, { store })
    await screen.findByRole('note')
    const socket = WS_INSTANCES[0]
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Ask a side question'), raw)
    await user.click(screen.getByTestId('busy-send-caret'))
    await user.click(screen.getByTestId('busy-send-mode-steer'))
    await user.click(screen.getByTestId('busy-send-button'))
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, raw, { steer: true }))

    act(() => {
      socket.simulateMessage({
        type: 'chat.side_closed',
        data: {
          kind: 'side', slot: SLOT, origin_client: 'another-tab',
          dropped: [{ queue_id: 'q-steer-missed', steer_id: 'steer-missed' }],
        },
      })
    })
    await act(async () => {
      answer({ ok: true, queued: false, pending: true, run_id: 'r1', steer_id: 'steer-missed' })
    })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe(raw))
    expect(store.getState().chat.slotSide[SLOT]?.queue).toBeUndefined()
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

  it('hands a requeued steer back when its pending receipt arrives after another tab closes it', async () => {
    const raw = 'pending steer from the closed conversation'
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
      socket.simulateMessage({
        type: 'chat.side_queue',
        data: {
          slot: SLOT, action: 'push', queue_id: 'q-pending-requeued', content: '[scrubbed]',
          front: true, steer_id: 'steer-pending-requeued',
        },
      })
      socket.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: 'another-tab' } })
    })
    await act(async () => {
      answer({ ok: true, queued: false, pending: true, run_id: 'r1', steer_id: 'steer-pending-requeued' })
    })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe(raw))
    expect(takeSideSteerDroppedByClose(SLOT, 'steer-pending-requeued')).toBe(false)
    expect(appStore.getState().chat.slotSide[SLOT]?.queue).toBeUndefined()
  })

  it('does not hand a pending steer back after a close without its requeued card', async () => {
    const raw = 'pending steer already in the running turn'
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
      socket.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: 'another-tab' } })
    })
    await act(async () => {
      answer({ ok: true, queued: false, pending: true, run_id: 'r1', steer_id: 'steer-without-card' })
    })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe(''))
    expect(takeSideSteerDroppedByClose(SLOT, 'steer-without-card')).toBe(false)
  })

  it('hands a pending requeued steer back after its panel switches slots', async () => {
    const raw = 'pending steer from the switched slot'
    let answer: (body: SideTurnBody) => void = () => {}
    vi.mocked(api.sideTurn).mockImplementationOnce(() => new Promise<SideTurnBody>(resolve => { answer = resolve }))
    act(() => {
      appStore.dispatch(sideClose(SLOT))
      appStore.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1', message_id: 'u1' }))
      appStore.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'partial' }))
    })
    const view = renderWithProviders(<><SocketHarness /><SideChat slot={SLOT} /></>, { store: appStore })
    await screen.findByRole('note')
    const socket = WS_INSTANCES[0]
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Ask a side question'), raw)
    await user.click(screen.getByTestId('busy-send-caret'))
    await user.click(screen.getByTestId('busy-send-mode-steer'))
    await user.click(screen.getByTestId('busy-send-button'))
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, raw, { steer: true }))

    await act(async () => {
      answer({ ok: true, queued: false, pending: true, run_id: 'r1', steer_id: 'steer-switched-slot' })
    })
    view.rerender(<><SocketHarness /><SideChat slot="another-slot" /></>)
    act(() => {
      socket.simulateMessage({
        type: 'chat.side_queue',
        data: {
          slot: SLOT, action: 'push', queue_id: 'q-switched-slot', content: '[scrubbed]',
          front: true, steer_id: 'steer-switched-slot',
        },
      })
      socket.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: 'another-tab' } })
    })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe(raw))
    expect(takeSideSteerDroppedByClose(SLOT, 'steer-switched-slot')).toBe(false)
    expect(appStore.getState().chat.slotSide[SLOT]?.queue).toBeUndefined()
    expect(readSideChatDraft(SLOT)).toBe(raw)
  })

  it('hands a pending requeued steer back while its panel is unmounted', async () => {
    const raw = 'pending steer from the unmounted panel'
    let answer: (body: SideTurnBody) => void = () => {}
    vi.mocked(api.sideTurn).mockImplementationOnce(() => new Promise<SideTurnBody>(resolve => { answer = resolve }))
    act(() => {
      appStore.dispatch(sideClose(SLOT))
      appStore.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1', message_id: 'u1' }))
      appStore.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'partial' }))
    })
    const view = renderWithProviders(<><SocketHarness /><SideChat slot={SLOT} /></>, { store: appStore })
    await screen.findByRole('note')
    const socket = WS_INSTANCES[0]
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Ask a side question'), raw)
    await user.click(screen.getByTestId('busy-send-caret'))
    await user.click(screen.getByTestId('busy-send-mode-steer'))
    await user.click(screen.getByTestId('busy-send-button'))
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, raw, { steer: true }))

    await act(async () => {
      answer({ ok: true, queued: false, pending: true, run_id: 'r1', steer_id: 'steer-unmounted-panel' })
    })
    view.rerender(<><SocketHarness /></>)
    act(() => {
      socket.simulateMessage({
        type: 'chat.side_queue',
        data: {
          slot: SLOT, action: 'push', queue_id: 'q-unmounted-panel', content: '[scrubbed]',
          front: true, steer_id: 'steer-unmounted-panel',
        },
      })
      socket.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: 'another-tab' } })
    })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe(raw))
    expect(takeSideSteerDroppedByClose(SLOT, 'steer-unmounted-panel')).toBe(false)
    expect(appStore.getState().chat.slotSide[SLOT]?.queue).toBeUndefined()
    expect(readSideChatDraft(SLOT)).toBe(raw)
  })

  it('does not hand a settled pending steer back without a requeued card', async () => {
    const raw = 'pending steer without a card after receipt'
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

    await act(async () => {
      answer({ ok: true, queued: false, pending: true, run_id: 'r1', steer_id: 'steer-settled-without-card' })
    })
    act(() => {
      socket.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: 'another-tab' } })
    })

    await waitFor(() => expect(appStore.getState().chat.slotSide[SLOT]).toBeUndefined())
    expect(readSideChatDraft(SLOT)).toBe('')
    expect(takeSideSteerDroppedByClose(SLOT, 'steer-settled-without-card')).toBe(false)
  })

  it('hands an identical plain-text requeued steer back after its pending receipt settles', async () => {
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

    await act(async () => {
      answer({ ok: true, queued: false, pending: true, run_id: 'r1', steer_id: 'steer-requeued' })
    })
    act(() => {
      appStore.dispatch(sseSideQueue({
        slot: SLOT, action: 'push', queue_id: 'q-requeued', content: raw,
        front: true, steer_id: 'steer-requeued',
      }))
    })
    await waitFor(() => expect(appStore.getState().chat.slotSide[SLOT]?.queue?.[0]).toEqual(
      expect.objectContaining({ content: raw, raw: true }),
    ))

    act(() => {
      socket.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: 'another-tab' } })
    })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe(raw))
    expect(appStore.getState().chat.slotSide[SLOT]?.queue).toBeUndefined()
  })


  it('hands an identical plain-text requeued steer back once when its demoted receipt precedes the close', async () => {
    const raw = 'plain question whose receipt arrives first'
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
        slot: SLOT, action: 'push', queue_id: 'q-requeued-plain', content: raw,
        front: true, steer_id: 'steer-receipt-plain',
      }))
    })
    await act(async () => {
      answer({ ok: true, queued: true, demoted: true, depth: 1, steer_id: 'steer-receipt-plain' })
    })
    await waitFor(() => expect(appStore.getState().chat.slotSide[SLOT]?.queue?.[0]).toEqual(
      expect.objectContaining({ content: raw, raw: true }),
    ))
    expect(readSideChatDraft(SLOT)).toBe('')

    act(() => {
      socket.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: 'another-tab' } })
    })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe(raw))
    expect(takeSideSteerDroppedByClose(SLOT, 'steer-receipt-plain')).toBe(false)
    expect(readSideChatDraft(SLOT)).toBe(raw)
  })

  it('keeps an edited requeued steer\'s wording and hands that wording back', async () => {
    const raw = 'steer question before its edit'
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
        slot: SLOT, action: 'push', queue_id: 'q-requeued-edited', content: '[scrubbed]',
        front: true, steer_id: 'steer-edited',
      }))
    })
    await act(async () => {
      answer({ ok: true, queued: true, demoted: true, depth: 1, steer_id: 'steer-edited' })
    })
    await waitFor(() => expect(appStore.getState().chat.slotSide[SLOT]?.queue?.[0]).toEqual(
      expect.objectContaining({ content: raw, raw: true }),
    ))

    await user.click(screen.getByLabelText('Edit queued message'))
    await user.clear(screen.getByLabelText('Edit queued message'))
    await user.type(screen.getByLabelText('Edit queued message'), 'steer question after its edit{Enter}')
    await waitFor(() => expect(api.sideQueueEdit).toHaveBeenCalledWith(SLOT, 'q-requeued-edited', 'steer question after its edit'))
    await waitFor(() => expect(appStore.getState().chat.slotSide[SLOT]?.queue?.[0]).toEqual(
      expect.objectContaining({ content: 'steer question after its edit', raw: true }),
    ))

    act(() => {
      socket.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: 'another-tab' } })
    })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe('steer question after its edit'))
    expect(readSideChatDraft(SLOT)).not.toContain(raw)
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

  it('hands an open queue editor\'s uncommitted text back once when another tab closes the conversation', async () => {
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
    await user.type(screen.getByLabelText('Edit queued message'), 'unsaved wording')

    act(() => {
      socket.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: 'another-tab' } })
    })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe('unsaved wording'))
    expect(readSideChatDraft(SLOT)).not.toContain('old wording')
  })

  it('hands a blank open queue editor back as the card\'s saved wording', async () => {
    appStore.dispatch(sideClose(SLOT))
    appStore.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1', message_id: 'u1' }))
    appStore.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'partial', message_id: 'a1' }))
    appStore.dispatch(sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q-1', content: 'saved wording', raw: true }))

    renderWithProviders(<><SocketHarness /><SideChat slot={SLOT} /></>, { store: appStore })
    await screen.findByRole('note')
    const socket = WS_INSTANCES[0]
    const user = userEvent.setup()
    await user.click(screen.getByLabelText('Edit queued message'))
    await user.clear(screen.getByLabelText('Edit queued message'))

    act(() => {
      socket.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: 'another-tab' } })
    })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe('saved wording'))
  })

  it('hands a blank open queue editor on a knowledge card back as the card\'s whole saved content', async () => {
    const saved = '[KNOWLEDGE CONTEXT — injected by user from knowledge library]\n\n## Doc\nkb line one\n\n[END KNOWLEDGE CONTEXT]\n\nsaved question'
    appStore.dispatch(sideClose(SLOT))
    appStore.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1', message_id: 'u1' }))
    appStore.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'partial', message_id: 'a1' }))
    appStore.dispatch(sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q-1', content: saved, raw: true }))

    renderWithProviders(<><SocketHarness /><SideChat slot={SLOT} /></>, { store: appStore })
    await screen.findByRole('note')
    const socket = WS_INSTANCES[0]
    const user = userEvent.setup()
    await user.click(screen.getByLabelText('Edit queued message'))
    expect(screen.getByLabelText('Edit queued message')).toHaveValue('saved question')
    await user.clear(screen.getByLabelText('Edit queued message'))

    act(() => {
      socket.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: 'another-tab' } })
    })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe(saved))
    expect(readSideChatDraft(SLOT).split('saved question')).toHaveLength(2)
  })

  it('drops a cancelled live edit so a later close hands back the original wording', async () => {
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
    await user.type(screen.getByLabelText('Edit queued message'), 'discarded wording{Escape}')

    act(() => {
      socket.simulateMessage({ type: 'chat.side_closed', data: { kind: 'side', slot: SLOT, origin_client: 'another-tab' } })
    })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe('old wording'))
    expect(readSideChatDraft(SLOT)).not.toContain('discarded wording')
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
