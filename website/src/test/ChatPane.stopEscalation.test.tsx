import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, fireEvent, act } from '@testing-library/react'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import chatReducer, { sseChatMessage } from '../store/chatSlice'
import dashboardReducer, { sseSlots } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { FORCE_KILL_ARMING_MS } from '../utils/stopDebounce'

/* The Stop button in a pane that is NOT the active slot — the Crew Members DM
 * thread is the everyday case — must behave like the main chat's (#9547):
 *
 *  - a pending cooperative cancel is VISIBLE (the pulsing button + the
 *    "click again to force stop" hint the composer renders from `stopState`),
 *    instead of the plain armed Stop that reads as "nothing happened";
 *  - the second press while the slot is `soft_pending` escalates through the
 *    shared `handleStopPress` decision (force=true), and a double-tap inside
 *    the arming window is ignored;
 *  - a slots snapshot that says the slot is not running settles the pane's own
 *    run state, so a `_done` that never reached this tab cannot leave a Stop
 *    button behind for a turn the backend finished long ago;
 *  - a `/stop` the backend answers with `not running` settles it the same way.
 */

const apiMock = vi.hoisted(() => ({
  stopChatSlot: vi.fn(),
  stopChatSlotForce: vi.fn(),
}))

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
    stopChatSlot: apiMock.stopChatSlot,
    stopChatSlotForce: apiMock.stopChatSlotForce,
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'

const SLOT = 'member-default'
const OTHER = 'chat-1-main'

type Msg = { role: string; content: string; ts: string }
const USER_MSG: Msg = { role: 'user', content: 'hi', ts: '2026-09-01T00:00:00Z' }

function slotRow(over: Partial<ChatSlot> = {}): ChatSlot {
  return { key: SLOT, messages: 1, running: true, mode: 'member', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined, ...over } as unknown as ChatSlot
}

/** The pane's slot is NOT the active one, so its run state lives in
 *  `slotRun` — the Members DM path. */
function makeStore(runState: string, slot: Partial<ChatSlot> = {}) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [slotRow(slot)],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        ...chatReducer(undefined, { type: '@@INIT' }),
        activeSlot: OTHER,
        slotMessages: { [SLOT]: [USER_MSG] },
        slotRun: { [SLOT]: { state: runState } },
      } as unknown as RootState['chat'],
    } as Partial<RootState>,
  })
}

function mount(store: ReturnType<typeof makeStore>, slotKey: string = SLOT) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tree = (key: string) => (
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ChatPane slotKey={key} agentLocked frameless />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>
  )
  const utils = render(tree(slotKey))
  return { ...utils, repoint: (key: string) => utils.rerender(tree(key)) }
}

beforeEach(() => {
  apiMock.stopChatSlot.mockReset().mockResolvedValue({ ok: true })
  apiMock.stopChatSlotForce.mockReset().mockResolvedValue({ ok: true })
})

describe('ChatPane Stop: parity with the main chat', () => {
  it('shows the pending-cancel state and the force hint while the slot is soft_pending', () => {
    const { queryByTestId } = mount(makeStore('tool_running', { stop_state: 'soft_pending', stopping: true }))
    expect(queryByTestId('stop-button-pulsing')).not.toBeNull()
    expect(queryByTestId('stop-force-hint')).not.toBeNull()
    expect(queryByTestId('stop-button-armed')).toBeNull()
  })

  it('first press is a soft stop; a press while soft_pending escalates to force after the arming window', () => {
    vi.useFakeTimers()
    try {
      const store = makeStore('tool_running')
      const { getByTestId, queryByTestId } = mount(store)
      fireEvent.click(getByTestId('stop-button-armed'))
      expect(apiMock.stopChatSlot).toHaveBeenCalledWith(SLOT)
      expect(apiMock.stopChatSlotForce).not.toHaveBeenCalled()
      // The pane does not wait for the server to say the cancel is pending:
      // the press itself is shown as pending, hint included.
      expect(queryByTestId('stop-button-pulsing')).not.toBeNull()
      expect(queryByTestId('stop-force-hint')).not.toBeNull()
      expect(queryByTestId('stop-button-armed')).toBeNull()

      // A second tap inside the arming window BEFORE the server's
      // soft_pending frame arrives must not send a second soft request — the
      // backend would escalate it to a queue-clearing hard kill.
      act(() => { vi.advanceTimersByTime(200) })
      fireEvent.click(getByTestId('stop-button-pulsing'))
      expect(apiMock.stopChatSlot).toHaveBeenCalledTimes(1)
      expect(apiMock.stopChatSlotForce).not.toHaveBeenCalled()

      // The backend confirms the cancel is pending.
      act(() => { store.dispatch(sseSlots([slotRow({ stop_state: 'soft_pending', stopping: true })])) })
      expect(queryByTestId('stop-button-pulsing')).not.toBeNull()

      // Inside the arming window a second press is an accidental double-tap.
      fireEvent.click(getByTestId('stop-button-pulsing'))
      expect(apiMock.stopChatSlotForce).not.toHaveBeenCalled()

      act(() => { vi.advanceTimersByTime(FORCE_KILL_ARMING_MS + 5) })
      fireEvent.click(getByTestId('stop-button-pulsing'))
      expect(apiMock.stopChatSlotForce).toHaveBeenCalledWith(SLOT)
    } finally {
      vi.useRealTimers()
    }
  })

  it('a second press after the arming window but BEFORE the server frame is a deliberate force, never a second soft', () => {
    // The slow-link case: the WS round-trip carrying `soft_pending` takes
    // longer than the arming window. The snapshot still says idle when the
    // user presses again; a second soft request here would be escalated by
    // the backend into a silent, queue-clearing hard kill.
    vi.useFakeTimers()
    try {
      const store = makeStore('tool_running')
      const { getByTestId, queryByTestId } = mount(store)
      fireEvent.click(getByTestId('stop-button-armed'))
      expect(apiMock.stopChatSlot).toHaveBeenCalledTimes(1)
      expect(store.getState().dashboard.slots[0].stop_state).toBeUndefined()

      act(() => { vi.advanceTimersByTime(FORCE_KILL_ARMING_MS + 50) })
      // Still no server frame: the button still shows pending from the
      // optimistic state, so the user knows the next press is the hard kill.
      expect(queryByTestId('stop-button-pulsing')).not.toBeNull()
      expect(queryByTestId('stop-force-hint')).not.toBeNull()
      fireEvent.click(getByTestId('stop-button-pulsing'))
      expect(apiMock.stopChatSlot).toHaveBeenCalledTimes(1)
      expect(apiMock.stopChatSlotForce).toHaveBeenCalledTimes(1)
      expect(apiMock.stopChatSlotForce).toHaveBeenCalledWith(SLOT)

      // The late frame hands the state over to the server; nothing re-arms.
      act(() => { store.dispatch(sseSlots([slotRow({ stop_state: 'killing', stopping: true })])) })
      expect(queryByTestId('stop-button-pulsing')).toBeNull()
      expect(queryByTestId('stop-button-killing')).not.toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('the optimistic pending state is released when the turn ends without a server stop frame', () => {
    const store = makeStore('tool_running')
    const { getByTestId, queryByTestId } = mount(store)
    fireEvent.click(getByTestId('stop-button-armed'))
    expect(queryByTestId('stop-button-pulsing')).not.toBeNull()
    // The turn finishes (cooperative cancel honoured) — the slot goes idle
    // without ever having published `soft_pending` to this tab.
    act(() => { store.dispatch(sseSlots([slotRow({ running: false })])) })
    expect(queryByTestId('stop-button-pulsing')).toBeNull()
    expect(queryByTestId('stop-button-armed')).toBeNull()
    // A new turn on the slot starts from an un-pressed Stop, not a stale
    // pending one whose next press would be a hard kill.
    act(() => { store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'again', seq: 2 })) })
    expect(queryByTestId('stop-button-armed')).not.toBeNull()
    expect(queryByTestId('stop-button-pulsing')).toBeNull()
  })

  it('a slots snapshot saying not running settles a pane whose _done never arrived', () => {
    const store = makeStore('streaming')
    const { queryByTestId } = mount(store)
    expect(queryByTestId('stop-button-armed')).not.toBeNull()
    act(() => { store.dispatch(sseSlots([slotRow({ running: false })])) })
    expect(store.getState().chat.slotRun[SLOT]?.state).toBe('idle')
    expect(queryByTestId('stop-button-armed')).toBeNull()
  })

  it('a lagging idle snapshot does not settle a newer turn that started in the meantime', () => {
    // Turn A was observed running. Its `_done` lands, turn B starts (first
    // chunk), and only THEN does the snapshot reporting A idle arrive — in the
    // same batch. The settlement is about A; applying it to B would idle B's
    // composer and split its streaming reply.
    const store = makeStore('streaming')
    const { queryByTestId } = mount(store)
    act(() => {
      store.dispatch(sseChatMessage({ slot: SLOT, role: '_done', content: '' }))
      store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'turn b', seq: 7 }))
      store.dispatch(sseSlots([slotRow({ running: false })]))
    })
    expect(store.getState().chat.slotRun[SLOT]?.state).toBe('streaming')
    expect(store.getState().chat.slotMessages[SLOT]?.at(-1)?.role).toBe('streaming')
    expect(queryByTestId('stop-button-armed')).not.toBeNull()
    // B's own end, observed the same way, still settles B.
    act(() => { store.dispatch(sseSlots([slotRow({ running: true })])) })
    act(() => { store.dispatch(sseSlots([slotRow({ running: false })])) })
    expect(store.getState().chat.slotRun[SLOT]?.state).toBe('idle')
    expect(store.getState().chat.slotMessages[SLOT]?.at(-1)?.role).toBe('assistant')
  })

  it('a snapshot that already says not running when the pane mounts does not settle a live pane', () => {
    // Opened mid-turn: live frames mark the pane busy, the snapshot predates
    // the turn. No transition was observed, so nothing is settled.
    const store = makeStore('streaming', { running: false })
    const { queryByTestId } = mount(store)
    expect(store.getState().chat.slotRun[SLOT]?.state).toBe('streaming')
    expect(queryByTestId('stop-button-armed')).not.toBeNull()
    // Once the server has been seen running and then not, the transition settles it.
    act(() => { store.dispatch(sseSlots([slotRow({ running: true })])) })
    act(() => { store.dispatch(sseSlots([slotRow({ running: false })])) })
    expect(store.getState().chat.slotRun[SLOT]?.state).toBe('idle')
  })

  it('re-pointing the pane at another slot does not carry the old running observation', () => {
    // A was observed running; the pane is re-pointed at B, whose snapshot
    // predates its turn (running:false) while live frames mark it busy. B
    // must not be settled on A's observation.
    const OTHER_SLOT = 'member-b'
    const store = makeStore('streaming')
    act(() => {
      store.dispatch(sseSlots([slotRow({ running: true }), { ...slotRow({ running: false }), key: OTHER_SLOT }]))
      store.dispatch(sseChatMessage({ slot: OTHER_SLOT, role: 'chunk', content: 'b', seq: 1 }))
    })
    const { repoint } = mount(store)
    expect(store.getState().chat.slotRun[OTHER_SLOT]?.state).toBe('streaming')
    act(() => { repoint(OTHER_SLOT) })
    expect(store.getState().chat.slotRun[OTHER_SLOT]?.state).toBe('streaming')
  })

  it('a slots snapshot saying running does not promote an idle pane', () => {
    const store = makeStore('idle', { running: false })
    const { queryByTestId } = mount(store)
    act(() => { store.dispatch(sseSlots([slotRow({ running: true })])) })
    expect(store.getState().chat.slotRun[SLOT]?.state).toBe('idle')
    // The composer's running signal for a background pane comes from the live
    // frames; the snapshot alone does not arm a Stop button here.
    expect(queryByTestId('stop-button-armed')).toBeNull()
  })

  it('a Stop request the server fails is shown, not swallowed', async () => {
    // A non-2xx is a definite answer: the backend saw the request and armed
    // nothing, so the button goes back to the plain armed Stop for a retry.
    apiMock.stopChatSlot.mockRejectedValueOnce(new Error('HTTP 500'))
    const store = makeStore('streaming')
    const { getByTestId, queryByTestId } = mount(store)
    await act(async () => { fireEvent.click(getByTestId('stop-button-armed')) })
    const notice = queryByTestId('chat-pane-stop-error')
    expect(notice).not.toBeNull()
    expect(notice?.textContent).toContain('HTTP 500')
    // The turn is still running, so the Stop button stays for a retry.
    expect(queryByTestId('stop-button-armed')).not.toBeNull()
    // A retry that succeeds retires the notice.
    apiMock.stopChatSlot.mockResolvedValueOnce({ ok: true })
    await act(async () => { fireEvent.click(getByTestId('stop-button-armed')) })
    expect(queryByTestId('chat-pane-stop-error')).toBeNull()
  })

  it('a transport failure is phrased for a reader, not as the browser exception', async () => {
    apiMock.stopChatSlot.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const store = makeStore('streaming')
    const { getByTestId, queryByTestId } = mount(store)
    await act(async () => { fireEvent.click(getByTestId('stop-button-armed')) })
    const notice = queryByTestId('chat-pane-stop-error')
    expect(notice).not.toBeNull()
    expect(notice?.textContent).not.toContain('Failed to fetch')
    expect(notice?.textContent).toMatch(/reach the server/i)
  })

  it('a transport failure keeps the pending state: the retry is a deliberate force, never a second soft', async () => {
    // The request may have reached the backend and armed the cancel with only
    // the reply lost. Forgetting the press here would show an un-pressed Stop
    // whose retry is a second soft — which the backend escalates to a
    // queue-clearing hard kill without saying so.
    vi.useFakeTimers()
    try {
      apiMock.stopChatSlot.mockRejectedValueOnce(new TypeError('Failed to fetch'))
      const store = makeStore('streaming')
      const { getByTestId, queryByTestId } = mount(store)
      await act(async () => { fireEvent.click(getByTestId('stop-button-armed')) })
      expect(queryByTestId('chat-pane-stop-error')).not.toBeNull()
      expect(queryByTestId('stop-button-pulsing')).not.toBeNull()
      expect(queryByTestId('stop-force-hint')).not.toBeNull()
      expect(queryByTestId('stop-button-armed')).toBeNull()
      act(() => { vi.advanceTimersByTime(FORCE_KILL_ARMING_MS + 5) })
      await act(async () => { fireEvent.click(getByTestId('stop-button-pulsing')) })
      expect(apiMock.stopChatSlot).toHaveBeenCalledTimes(1)
      expect(apiMock.stopChatSlotForce).toHaveBeenCalledTimes(1)
      // The retry that succeeds retires the notice.
      expect(queryByTestId('chat-pane-stop-error')).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('a definite backend refusal resets the press: the retry is a fresh soft stop', async () => {
    // The backend answered and said no — nothing was armed, so the retry the
    // notice asks for must not read as the second half of a double-tap.
    apiMock.stopChatSlot.mockResolvedValueOnce({ ok: false, error: 'stop refused' })
    const store = makeStore('streaming')
    const { getByTestId, queryByTestId } = mount(store)
    await act(async () => { fireEvent.click(getByTestId('stop-button-armed')) })
    expect(queryByTestId('chat-pane-stop-error')?.textContent).toContain('stop refused')
    expect(queryByTestId('stop-button-pulsing')).toBeNull()
    expect(queryByTestId('stop-button-armed')).not.toBeNull()
    apiMock.stopChatSlot.mockResolvedValueOnce({ ok: true })
    await act(async () => { fireEvent.click(getByTestId('stop-button-armed')) })
    expect(apiMock.stopChatSlot).toHaveBeenCalledTimes(2)
    expect(apiMock.stopChatSlotForce).not.toHaveBeenCalled()
  })

  it('a Stop the backend answers with not running settles the pane', async () => {
    apiMock.stopChatSlot.mockResolvedValueOnce({ ok: true, info: 'not running', already_stopping: false })
    const store = makeStore('streaming')
    const { getByTestId, queryByTestId } = mount(store)
    await act(async () => { fireEvent.click(getByTestId('stop-button-armed')) })
    expect(store.getState().chat.slotRun[SLOT]?.state).toBe('idle')
    expect(queryByTestId('stop-button-armed')).toBeNull()
  })

  it('a late failure for the slot the pane used to show does not disarm the current slot', async () => {
    // Stop pressed on A; the pane is re-pointed at B and B is soft-stopped;
    // A's refusal then lands late. It must not reset B's arming stamp or
    // pending state (B's next press would read as a fresh soft, which the
    // backend escalates), nor show B a notice about A's press.
    const OTHER_SLOT = 'member-b'
    let settleA: (v: unknown) => void = () => {}
    apiMock.stopChatSlot.mockImplementationOnce(() => new Promise((resolve) => { settleA = resolve }))
    const store = makeStore('streaming')
    act(() => {
      store.dispatch(sseSlots([slotRow({ running: true }), { ...slotRow({ running: true }), key: OTHER_SLOT }]))
      store.dispatch(sseChatMessage({ slot: OTHER_SLOT, role: 'chunk', content: 'b', seq: 1 }))
    })
    const { getByTestId, queryByTestId, repoint } = mount(store)
    fireEvent.click(getByTestId('stop-button-armed'))
    expect(apiMock.stopChatSlot).toHaveBeenLastCalledWith(SLOT)
    act(() => { repoint(OTHER_SLOT) })
    // Re-pointing forgets A's press: B starts from an un-pressed Stop.
    expect(queryByTestId('stop-button-armed')).not.toBeNull()
    apiMock.stopChatSlot.mockResolvedValueOnce({ ok: true })
    await act(async () => { fireEvent.click(getByTestId('stop-button-armed')) })
    expect(apiMock.stopChatSlot).toHaveBeenLastCalledWith(OTHER_SLOT)
    expect(queryByTestId('stop-button-pulsing')).not.toBeNull()
    // A's definite refusal lands now.
    await act(async () => { settleA({ ok: false, error: 'stop refused' }) })
    expect(queryByTestId('stop-button-pulsing')).not.toBeNull()
    expect(queryByTestId('chat-pane-stop-error')).toBeNull()
  })
})
