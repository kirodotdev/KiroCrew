import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createTestStore, renderWithProviders } from './helpers'
import ChatInput from '../src/components/ChatInput'
import type { RootState } from '../src/store'
import { setStopPressedAt, requestStop } from '../src/store/chatSlice'
import { SOFT_STOP_DEBOUNCE_MS } from '../src/pages/chat/types'

vi.mock('../src/api/client', async () => {
  const actual = await vi.importActual<typeof import('../src/api/client')>('../src/api/client')
  return {
    ...actual,
    api: {
      ...actual.api,
      stopChatSlot: vi.fn().mockResolvedValue({ ok: true }),
      stopChatSlotForce: vi.fn().mockResolvedValue({ ok: true }),
    },
  }
})
import { api } from '../src/api/client'
const mockStopSlot = vi.mocked(api.stopChatSlot)
const mockStopSlotForce = vi.mocked(api.stopChatSlotForce)

const SLOT = 'test-slot'

function makeStore(overrides?: Partial<RootState['chat']> & { stopState?: 'idle' | 'soft_pending' | 'killing' }) {
  const { stopState, ...chatOverrides } = overrides ?? {}
  return createTestStore({
    chat: {
      activeSlot: SLOT,
      messages: [],
      slotRunning: true,
      slotStopping: false,
      slotState: 'streaming',
      slotStatusDetail: {},
      slotHasMore: false,
      slotOldestIndex: 0,
      loadingOlder: false,
      lastChunkSeq: undefined,
      _wsChunkedDuringFetch: false,
      history: [],
      historyHasMore: false,
      historyOffset: 0,
      pendingInput: null,
      slotContextPct: {},
      voicePlaying: false,
      voiceAudio: null,
      subagents: {},
      toolLog: [],
      activityOpen: false,
      activityTab: 'tools',
      slotActivity: {},
      stopPressedAt: {},
      ...chatOverrides,
    } as RootState['chat'],
    dashboard: {
      slots: [{ key: SLOT, messages: 0, running: true, stop_state: stopState ?? 'idle' }],
    } as any,
  })
}

describe('ChatInput stop button rendering', () => {
  let onStopFn: ReturnType<typeof vi.fn>

  beforeEach(() => {
    onStopFn = vi.fn()
  })

  it('renders pulsing button when stopState is soft_pending', () => {
    renderWithProviders(
      <ChatInput value="" onChange={() => {}} onSend={() => {}} isRunning onStop={onStopFn} stopState="soft_pending" />,
      { store: makeStore({ stopState: 'soft_pending' }) },
    )

    expect(screen.getByTestId('stop-button-pulsing')).toBeInTheDocument()
  })

  it('fires onStop on click in armed state', async () => {
    const user = userEvent.setup()
    renderWithProviders(
      <ChatInput value="" onChange={() => {}} onSend={() => {}} isRunning onStop={onStopFn} stopState="idle" />,
      { store: makeStore() },
    )

    await user.click(screen.getByTestId('stop-button-armed'))
    expect(onStopFn).toHaveBeenCalledTimes(1)
  })

  it('fires onStop on click in soft_pending state', async () => {
    const user = userEvent.setup()
    renderWithProviders(
      <ChatInput value="" onChange={() => {}} onSend={() => {}} isRunning onStop={onStopFn} stopState="soft_pending" />,
      { store: makeStore({ stopState: 'soft_pending' }) },
    )

    await user.click(screen.getByTestId('stop-button-pulsing'))
    expect(onStopFn).toHaveBeenCalledTimes(1)
  })

  it('returns to armed when stopState resolves from soft_pending to idle', () => {
    const { rerender } = renderWithProviders(
      <ChatInput value="" onChange={() => {}} onSend={() => {}} isRunning onStop={onStopFn} stopState="soft_pending" />,
      { store: makeStore({ stopState: 'soft_pending' }) },
    )
    expect(screen.getByTestId('stop-button-pulsing')).toBeInTheDocument()

    rerender(
      <ChatInput value="" onChange={() => {}} onSend={() => {}} isRunning onStop={onStopFn} stopState="idle" />,
    )

    expect(screen.getByTestId('stop-button-armed')).toBeInTheDocument()
    expect(screen.queryByTestId('stop-button-pulsing')).not.toBeInTheDocument()
  })
})

describe('requestStop thunk', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mockStopSlot.mockReset().mockResolvedValue({ ok: true })
    mockStopSlotForce.mockReset().mockResolvedValue({ ok: true })
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('calls stopChatSlot (soft) when force=false', async () => {
    const store = makeStore()
    await act(async () => {
      await store.dispatch(requestStop({ slotId: SLOT, force: false }) as any)
    })
    expect(mockStopSlot).toHaveBeenCalledWith(SLOT)
    expect(mockStopSlotForce).not.toHaveBeenCalled()
  })

  it('calls stopChatSlotForce when force=true', async () => {
    const store = makeStore({ stopState: 'soft_pending' })
    await act(async () => {
      await store.dispatch(requestStop({ slotId: SLOT, force: true }) as any)
    })
    expect(mockStopSlotForce).toHaveBeenCalledWith(SLOT)
    expect(mockStopSlot).not.toHaveBeenCalled()
  })

  it('debounces a second force=false dispatch within 150ms', async () => {
    const store = makeStore()

    // Stamp stopPressedAt to a very recent time to simulate the user's first press
    act(() => { store.dispatch(setStopPressedAt({ slotId: SLOT, ts: Date.now() })) })

    // Second press within the debounce window — the thunk should noop
    await act(async () => {
      await store.dispatch(requestStop({ slotId: SLOT, force: false }) as any)
    })
    expect(mockStopSlot).not.toHaveBeenCalled()

    // Advance past the debounce window and try again — should go through
    act(() => { vi.advanceTimersByTime(SOFT_STOP_DEBOUNCE_MS + 10) })
    await act(async () => {
      await store.dispatch(requestStop({ slotId: SLOT, force: false }) as any)
    })
    expect(mockStopSlot).toHaveBeenCalledWith(SLOT)
  })

  it('force=true bypasses the debounce window', async () => {
    const store = makeStore({ stopState: 'soft_pending' })

    // Stamp stopPressedAt to "just now" so a force=false call would debounce
    act(() => { store.dispatch(setStopPressedAt({ slotId: SLOT, ts: Date.now() })) })

    // Force press still calls the force endpoint
    await act(async () => {
      await store.dispatch(requestStop({ slotId: SLOT, force: true }) as any)
    })
    expect(mockStopSlotForce).toHaveBeenCalledWith(SLOT)
  })
})
