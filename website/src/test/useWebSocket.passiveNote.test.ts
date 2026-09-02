/**
 * A note breadcrumb arrives as `role: 'inject'` with `cls: 'reconcile-note'`. It
 * records something on the transcript but starts no agent turn, so no
 * `chat_done` ever follows it.
 *
 * Two effects on the inbound-prompt path assume a turn is beginning: they stop
 * text-to-speech, and they set a "Thinking…" status that only turn completion
 * clears. Applied to a note, the first cuts speech off mid-sentence and the
 * second leaves a status stuck on the slot forever. Both directions are pinned
 * here — a note is exempt, a plain inject still triggers both.
 *
 * `stopVoice()` is a hook-local callback with nothing to spy on, so it is
 * observed through its one store write, `setVoicePlaying(false)`: inside the
 * `chat_message` case that dispatch has no other caller.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useWebSocket } from '../hooks/useWebSocket'
import { store } from '../store'
import { setActiveSlot, clearMessages, clearSlotState, setVoicePlaying } from '../store/chatSlice'
import { RECONCILE_NOTE_CLS, isReconcileNote } from '../lib/noteContract'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: true }),
    voiceSynthesize: vi.fn().mockResolvedValue({}),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
  },
}))

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

// A true -> false flip of voicePlaying means stopVoice ran: inside the
// chat_message case that dispatch has no other caller.
const voicePlaying = () => store.getState().chat.voicePlaying
const statusKind = (slot: string) => store.getState().chat.slotStatusDetail[slot]?.kind

describe('useWebSocket passive note (role=inject, cls=reconcile-note)', () => {
  let queryClient: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    // clearMessages does NOT reset slotStatusDetail, so a "Thinking…" set by one
    // test would otherwise leak forward and make these assertions order-dependent.
    store.dispatch(clearSlotState())
    store.dispatch(setActiveSlot('slot-1'))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    store.dispatch(clearMessages())
    store.dispatch(clearSlotState())
    store.dispatch(setActiveSlot(null))
    store.dispatch(setVoicePlaying(false))
  })

  async function mount() {
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store },
        createElement(QueryClientProvider, { client: queryClient }, children))
    }
    const hook = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    await act(async () => {})
    return { hook, ws }
  }

  it('leaves speech running and sets no thinking status', async () => {
    const { hook, ws } = await mount()

    // Speech is in progress when the note lands.
    act(() => { store.dispatch(setVoicePlaying(true)) })
    expect(voicePlaying()).toBe(true)

    act(() => {
      ws.simulateMessage({
        type: 'chat_message',
        data: { slot: 'slot-1', role: 'inject', cls: RECONCILE_NOTE_CLS, content: 'a note', ts: '10.0' },
      })
    })
    await act(async () => {})

    expect(voicePlaying()).toBe(true)
    expect(statusKind('slot-1')).toBeUndefined()

    hook.unmount()
  })

  it('still stops speech and sets a thinking status for a plain inject', async () => {
    const { hook, ws } = await mount()

    act(() => { store.dispatch(setVoicePlaying(true)) })
    expect(voicePlaying()).toBe(true)

    // A queued prompt: no cls, a real turn follows, so both effects are correct.
    act(() => {
      ws.simulateMessage({
        type: 'chat_message',
        data: { slot: 'slot-1', role: 'inject', content: 'a queued prompt', ts: '11.0' },
      })
    })
    await act(async () => {})

    expect(voicePlaying()).toBe(false)
    expect(statusKind('slot-1')).toBe('thinking')

    hook.unmount()
  })

  it('records the note on the transcript and bumps slot recency', async () => {
    const { hook, ws } = await mount()

    // The exemption is scoped to the two turn-scoped effects: a note is still a
    // visible row, and still counts as activity on the slot.
    act(() => {
      ws.simulateMessage({
        type: 'chat_message',
        data: { slot: 'slot-1', role: 'inject', cls: RECONCILE_NOTE_CLS, content: 'a visible note', ts: '12.0' },
      })
    })
    await act(async () => {})

    const messages = store.getState().chat.messages
    expect(messages.some(m => m.content === 'a visible note')).toBe(true)

    hook.unmount()
  })

  it('is exempt when the token arrives alongside other classes', async () => {
    const { hook, ws } = await mount()

    act(() => { store.dispatch(setVoicePlaying(true)) })

    // `cls` is a class LIST — most rows in the tree look like `msg msg-a`. A
    // producer emitting the token as one class among several is the same note.
    act(() => {
      ws.simulateMessage({
        type: 'chat_message',
        data: { slot: 'slot-1', role: 'inject', cls: `msg ${RECONCILE_NOTE_CLS}`, content: 'a wrapped note', ts: '13.0' },
      })
    })
    await act(async () => {})

    expect(voicePlaying()).toBe(true)
    expect(statusKind('slot-1')).toBeUndefined()

    hook.unmount()
  })

  it('is not exempt for a different class that merely contains the token', async () => {
    const { hook, ws } = await mount()

    act(() => { store.dispatch(setVoicePlaying(true)) })

    // Membership, not substring: a longer class name is a different class.
    act(() => {
      ws.simulateMessage({
        type: 'chat_message',
        data: { slot: 'slot-1', role: 'inject', cls: `msg ${RECONCILE_NOTE_CLS}-draft`, content: 'not a note', ts: '14.0' },
      })
    })
    await act(async () => {})

    expect(voicePlaying()).toBe(false)
    expect(statusKind('slot-1')).toBe('thinking')

    hook.unmount()
  })

  it('pins the sentinel spelling and the membership rule', () => {
    // The tests above build their frames from the constant, so they would follow
    // a rename silently. This pins the wire value the producer must emit.
    expect(RECONCILE_NOTE_CLS).toBe('reconcile-note')

    expect(isReconcileNote('reconcile-note')).toBe(true)
    expect(isReconcileNote('msg reconcile-note')).toBe(true)
    expect(isReconcileNote('  msg   reconcile-note  ')).toBe(true)
    expect(isReconcileNote('msg reconcile-note-draft')).toBe(false)
    expect(isReconcileNote('msg msg-u')).toBe(false)
    expect(isReconcileNote(undefined)).toBe(false)
    expect(isReconcileNote('')).toBe(false)
  })
})
