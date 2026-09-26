/**
 * Regression: a WebSocket that stays OPEN but stops delivering frames.
 *
 * A phone that changes networks, or a tab the OS froze and resumed, can keep a
 * socket whose `readyState` still reads OPEN while nothing arrives and
 * `onclose` never fires. Without a watchdog the dashboard sat on it until a
 * manual reload, so every one-shot frame was lost. The visible case: a goal
 * loop armed by the agent (`monitor_start`) reaches the composer's automation
 * chip only through an `autonudge_state` frame, so the chip stayed dark until
 * the page was refreshed.
 *
 * The gateway sends a `dashboard` status frame on every socket every 5s, so a
 * visible page whose socket is silent for WS_SILENCE_MS is replaced, and the
 * replacement's reconnect catch-up re-reads the automation collection.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import {
  useWebSocket,
  WS_SILENCE_CHECK_MS,
  WS_SILENCE_MAX_MS,
  WS_SILENCE_MS,
} from '../hooks/useWebSocket'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    autonudgeList: vi.fn().mockResolvedValue({ enabled: true, loops: [] }),
    monitorsList: vi.fn().mockResolvedValue({ enabled: true, monitors: [] }),
    voiceCancel: vi.fn().mockResolvedValue({ ok: true }),
  },
}))

const WS_INSTANCES: MockWebSocket[] = []

class MockWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  static CLOSED = 3
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

/** The agent's goal loop, as the gateway serves it once armed. */
const ARMED_LOOP = {
  id: 'c0b4f6cc', slot_key: 'chat-236-1790420185', message: 'babysit the deploy',
  idle_secs: 600, max_cycles: 48, cycle_count: 0, active: true, last_fire_ts: 0,
}
const STATUS_FRAME = { type: 'dashboard', data: {} }

describe('useWebSocket silence watchdog', () => {
  let testStore: ReturnType<typeof createTestStore>
  let queryClient: QueryClient
  let hidden = false

  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    hidden = false
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
    testStore = createTestStore({})
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    delete (document as { hidden?: boolean }).hidden
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: queryClient }, children),
    )
  }

  /** Advance fake time, then flush what came due at the end of it -- the
   *  replacement socket is created on a 0ms timer the final tick schedules. */
  async function advance(ms: number) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms)
      await vi.advanceTimersByTimeAsync(1)
    })
  }

  async function openFirstSocket() {
    renderHook(() => useWebSocket(), { wrapper })
    await act(async () => { WS_INSTANCES[0].simulateOpen() })
    await advance(0)
  }

  async function hide() {
    hidden = true
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')) })
  }

  async function show() {
    hidden = false
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')) })
  }

  it('replaces a silent open socket and re-seeds an automation armed meanwhile', async () => {
    await openFirstSocket()
    expect(testStore.getState().chat.automations[ARMED_LOOP.slot_key]).toBeUndefined()

    // The agent arms its loop while this socket delivers nothing: the
    // `autonudge_state` frame is lost, and only the REST feed knows.
    vi.mocked(api.autonudgeList).mockResolvedValue({ enabled: true, loops: [ARMED_LOOP] })

    await advance(WS_SILENCE_MS + WS_SILENCE_CHECK_MS)
    expect(WS_INSTANCES).toHaveLength(2)
    expect(WS_INSTANCES[0].close).toHaveBeenCalled()

    await act(async () => { WS_INSTANCES[1].simulateOpen() })
    await advance(0)
    expect(testStore.getState().chat.automations[ARMED_LOOP.slot_key]).toMatchObject({
      kind: 'legacy_goal_loop', id: 'c0b4f6cc', active: true,
    })
  })

  it('releases active voice before replacing a silent socket', async () => {
    await openFirstSocket()
    const errors = vi.fn()
    window.addEventListener('voice-error', errors)
    try {
      act(() => {
        window.dispatchEvent(new CustomEvent('voice-synthesis-start', {
          detail: { slot: 'voice-slot', request_id: 'voice-request' },
        }))
      })

      await advance(WS_SILENCE_MS + WS_SILENCE_CHECK_MS)

      expect(WS_INSTANCES).toHaveLength(2)
      expect(api.voiceCancel).toHaveBeenCalledWith('voice-slot', 'voice-request')
      expect(errors).toHaveBeenCalledOnce()
      expect((errors.mock.calls[0][0] as CustomEvent).detail.code).toBe('voice_playback_failed')
    } finally {
      window.removeEventListener('voice-error', errors)
    }
  })

  it('keeps a socket that delivers the 5s status frame', async () => {
    await openFirstSocket()
    for (let elapsed = 0; elapsed < WS_SILENCE_MS * 4; elapsed += WS_SILENCE_CHECK_MS) {
      await act(async () => { WS_INSTANCES[0].simulateMessage(STATUS_FRAME) })
      await advance(WS_SILENCE_CHECK_MS)
    }
    expect(WS_INSTANCES).toHaveLength(1)
    expect(WS_INSTANCES[0].close).not.toHaveBeenCalled()
  })

  it('leaves a hidden page alone and does not count the hidden time', async () => {
    await openFirstSocket()
    await hide()
    await advance(WS_SILENCE_MS * 3)
    expect(WS_INSTANCES).toHaveLength(1)

    // Back on screen: nothing visible has elapsed yet, so the socket gets a
    // full window before it is read as dead.
    await show()
    await advance(WS_SILENCE_MS - WS_SILENCE_CHECK_MS)
    expect(WS_INSTANCES).toHaveLength(1)

    await advance(WS_SILENCE_CHECK_MS * 2)
    expect(WS_INSTANCES).toHaveLength(2)
  })

  it('adds up visible silence across tab switches shorter than the window', async () => {
    await openFirstSocket()
    // Visible for less than a window: not dead on its own.
    await advance(WS_SILENCE_MS - WS_SILENCE_CHECK_MS)
    expect(WS_INSTANCES).toHaveLength(1)
    await hide()
    await advance(WS_SILENCE_MS * 3)

    // Another visible spell shorter than the window. The two together exceed
    // it, so the dead socket goes even though no single spell reached 20s.
    await show()
    await advance(WS_SILENCE_MS - WS_SILENCE_CHECK_MS)
    expect(WS_INSTANCES).toHaveLength(2)
    expect(WS_INSTANCES[0].close).toHaveBeenCalled()
  })

  it('gives a thawed socket time to deliver its next status frame', async () => {
    await openFirstSocket()
    // Almost a whole window of visible silence, then hidden long enough for
    // the tab to be frozen.
    await advance(WS_SILENCE_MS - 1_000)
    await hide()
    await advance(WS_SILENCE_MS * 3 + 4_000)

    // Back on screen 2s before the next check, which would read the pre-hide
    // silence plus 2s as dead. The socket is live and its status frame lands
    // within one status interval of the return, as a thawed socket's does.
    await show()
    await advance(WS_SILENCE_CHECK_MS - 1_000)
    await act(async () => { WS_INSTANCES[0].simulateMessage(STATUS_FRAME) })
    for (let elapsed = 0; elapsed < WS_SILENCE_MS * 2; elapsed += WS_SILENCE_CHECK_MS) {
      await advance(WS_SILENCE_CHECK_MS)
      await act(async () => { WS_INSTANCES[0].simulateMessage(STATUS_FRAME) })
    }
    expect(WS_INSTANCES).toHaveLength(1)
    expect(WS_INSTANCES[0].close).not.toHaveBeenCalled()
  })

  it('replaces a dead socket seen only in visible spells shorter than the thaw grace', async () => {
    await openFirstSocket()
    // A phone glanced at for 8s and pocketed for a minute, over and over. Each
    // spell is shorter than the two checks a thawed socket is given, so no
    // check inside one may replace the socket; only the return can decide.
    const glance = async () => {
      await advance(WS_SILENCE_CHECK_MS * 2 - 2_000)
      await hide()
      await advance(WS_SILENCE_MS * 3 + 1_000)
      await show()
    }
    // Three glances add up to more than a window of visible silence; the
    // window is crossed during the third, inside its grace, so nothing yet.
    await glance()
    await glance()
    await glance()
    expect(WS_INSTANCES).toHaveLength(1)

    // The fourth return finds the silence already past the window, so no
    // grace applies: the first check of this spell replaces the socket.
    await advance(WS_SILENCE_CHECK_MS)
    expect(WS_INSTANCES).toHaveLength(2)
    expect(WS_INSTANCES[0].close).toHaveBeenCalled()
  })

  it('widens the window after consecutive silent replacements, up to the cap', async () => {
    await openFirstSocket()
    await advance(WS_SILENCE_MS + WS_SILENCE_CHECK_MS)
    expect(WS_INSTANCES).toHaveLength(2)

    // The replacement opens and is silent too: the second window is doubled.
    await act(async () => { WS_INSTANCES[1].simulateOpen() })
    await advance(WS_SILENCE_MS + WS_SILENCE_CHECK_MS)
    expect(WS_INSTANCES).toHaveLength(2)
    await advance(WS_SILENCE_MS)
    expect(WS_INSTANCES).toHaveLength(3)

    // Never wider than the cap, however many silent sockets came before.
    for (let i = 3; i < 10; i += 1) {
      await act(async () => { WS_INSTANCES[i - 1].simulateOpen() })
      await advance(WS_SILENCE_MAX_MS + WS_SILENCE_CHECK_MS)
      expect(WS_INSTANCES).toHaveLength(i + 1)
    }
  })

  it('forgets earlier silent replacements once a socket stays live for a whole window', async () => {
    await openFirstSocket()
    await advance(WS_SILENCE_MS + WS_SILENCE_CHECK_MS)
    await act(async () => { WS_INSTANCES[1].simulateOpen() })

    // Live for more than the doubled window: the count resets.
    for (let elapsed = 0; elapsed <= WS_SILENCE_MS * 2 + WS_SILENCE_CHECK_MS; elapsed += WS_SILENCE_CHECK_MS) {
      await act(async () => { WS_INSTANCES[1].simulateMessage(STATUS_FRAME) })
      await advance(WS_SILENCE_CHECK_MS)
    }
    expect(WS_INSTANCES).toHaveLength(2)

    // Silent again: back to the base window, not the doubled one.
    await advance(WS_SILENCE_MS + WS_SILENCE_CHECK_MS)
    expect(WS_INSTANCES).toHaveLength(3)
  })

  it('keeps the widened window when a hidden tab returns to a still-silent replacement', async () => {
    await openFirstSocket()
    await advance(WS_SILENCE_MS + WS_SILENCE_CHECK_MS)
    expect(WS_INSTANCES).toHaveLength(2)
    await act(async () => { WS_INSTANCES[1].simulateOpen() })

    // Hidden past the doubled window, then back. The replacement delivered
    // nothing meanwhile, so the return must not read as a socket that stayed
    // live for a whole window.
    await hide()
    await advance(WS_SILENCE_MS * 2 + WS_SILENCE_CHECK_MS)
    await show()

    // Still the doubled window from the return, not the base one.
    await advance(WS_SILENCE_MS + WS_SILENCE_CHECK_MS)
    expect(WS_INSTANCES).toHaveLength(2)
    await advance(WS_SILENCE_MS)
    expect(WS_INSTANCES).toHaveLength(3)
  })
})
