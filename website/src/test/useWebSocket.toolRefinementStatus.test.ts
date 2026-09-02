/**
 * A tool-call refinement must not blank the live status line's purpose.
 *
 * Every real tool call arrives as TWO `tool_call` frames: the initial one (which
 * carries the agent-written `purpose`) and a refinement tagged `is_update`,
 * which carries only the fields it refines — the completed title/input, and a
 * `purpose` only when the backend could recover one. `setSlotStatusDetail`
 * replaces the detail wholesale, so treating a purposeless refinement as
 * authoritative overwrote the purpose with the raw command, and the session-list
 * row of a running session flipped mid-call from "List the temp dir" to
 * "ls /tmp" (or to nothing at all when the refinement carried no title either).
 *
 * These tests pin the merge: an absent field on a refinement means "keep what
 * the initial frame supplied", and the merge only applies to the SAME
 * tool_call_id so parallel tool calls cannot inherit each other's purpose.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useWebSocket } from '../hooks/useWebSocket'
import { store as globalStore } from '../store'
import { setActiveSlot, clearSlotState, sseChatMessage } from '../store/chatSlice'
import { toolStatusLabel } from '../utils/toolStatusLabel'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
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

  constructor() {
    WS_INSTANCES.push(this)
  }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }
}

/** The SINGLETON store, for the reason spelled out in the churn test: the hook
 *  dispatches through the Provider store but reads the merge's base state off
 *  the imported singleton, so a separate store would make reads and writes
 *  diverge and these tests would pass against the buggy code too. */
function storeOnSlot1() {
  globalStore.dispatch(clearSlotState())
  globalStore.dispatch(setActiveSlot('slot-1'))
  globalStore.dispatch(sseChatMessage({ slot: 'slot-1', role: 'user', content: 'list the temp dir' }))
  return globalStore
}

describe('useWebSocket tool-call refinement status detail', () => {
  let queryClient: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    globalStore.dispatch(clearSlotState())
    globalStore.dispatch(setActiveSlot(null))
  })

  function mount(store: ReturnType<typeof storeOnSlot1>) {
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store },
        createElement(QueryClientProvider, { client: queryClient }, children))
    }
    const hook = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return { hook, ws }
  }

  const initial = (over: Record<string, unknown> = {}) => ({
    type: 'tool_call',
    data: {
      slot: 'slot-1',
      tool: 'Terminal',
      kind: 'execute',
      purpose: 'List the temp dir',
      input_preview: '',
      tool_call_id: 'tc-1',
      ...over,
    },
  })

  const refinement = (over: Record<string, unknown> = {}) => ({
    type: 'tool_call',
    data: {
      slot: 'slot-1',
      tool: 'ls /tmp',
      kind: 'execute',
      input_preview: '{"command":"ls /tmp"}',
      tool_call_id: 'tc-1',
      is_update: true,
      ...over,
    },
  })

  const detail = () => globalStore.getState().chat.slotStatusDetail['slot-1']
  /** What the session-list row actually paints, under the default
   *  `simplifiedToolNames` preference. The stored `text` is only half the input
   *  — toolStatusLabel owns the fallback from an absent purpose to the title —
   *  so the label is the assertion that matches what a user sees. */
  const label = () => toolStatusLabel(detail(), true, 'en')

  it('keeps the initial purpose when the refinement carries none', () => {
    const store = storeOnSlot1()
    const { ws } = mount(store)

    act(() => { ws.simulateMessage(initial()) })
    expect(label()).toBe('List the temp dir')

    act(() => { ws.simulateMessage(refinement()) })
    // The purpose survives; the refined title still lands on toolName so raw
    // mode (simplifiedToolNames off) shows the real command, not the stub.
    expect(label()).toBe('List the temp dir')
    expect(detail().toolName).toBe('ls /tmp')
    expect(toolStatusLabel(detail(), false, 'en')).toBe('ls /tmp')
  })

  it('advances a purposeless call to the refined command', () => {
    const store = storeOnSlot1()
    const { ws } = mount(store)

    // Native tools (Terminal / grep / Read) commonly send no purpose at all.
    // The initial frame is then a generic stub and the refinement is the ONLY
    // event carrying the real target, so the row must advance to it. Storing
    // the stub as if it were a purpose would pin "Terminal" for the whole call.
    act(() => { ws.simulateMessage(initial({ purpose: '' })) })
    expect(label()).toBe('Terminal')

    act(() => { ws.simulateMessage(refinement()) })
    expect(label()).toBe('ls /tmp')
    expect(detail().text).toBe('')
  })

  it('adopts a purpose the refinement does supply', () => {
    const store = storeOnSlot1()
    const { ws } = mount(store)

    act(() => { ws.simulateMessage(initial({ purpose: '' })) })
    // With no purpose anywhere yet the row falls back to the tool title.
    expect(label()).toBe('Terminal')

    act(() => { ws.simulateMessage(refinement({ purpose: 'List the temp dir' })) })
    expect(label()).toBe('List the temp dir')
  })

  it('keeps the initial title when the refinement carries none', () => {
    const store = storeOnSlot1()
    const { ws } = mount(store)

    act(() => { ws.simulateMessage(initial()) })
    act(() => { ws.simulateMessage(refinement({ tool: '' })) })

    // A kind-only refinement must not blank the row into an empty label.
    expect(label()).toBe('List the temp dir')
    expect(detail().toolName).toBe('Terminal')
  })

  it('does not let a refinement inherit a parallel call\u2019s purpose', () => {
    const store = storeOnSlot1()
    const { ws } = mount(store)

    // Two tools dispatched in one turn, then the FIRST one refines.
    act(() => { ws.simulateMessage(initial()) })
    act(() => { ws.simulateMessage(initial({ tool: 'grep', purpose: 'Find the caller', tool_call_id: 'tc-2' })) })
    act(() => { ws.simulateMessage(refinement()) })

    // tc-1's purposeless refinement must not adopt tc-2's purpose; with no
    // same-call base to merge into it falls back to its own title.
    expect(label()).toBe('ls /tmp')
    expect(detail().toolCallId).toBe('tc-1')
  })

  it('replaces the purpose wholesale on the next tool call', () => {
    const store = storeOnSlot1()
    const { ws } = mount(store)

    act(() => { ws.simulateMessage(initial()) })
    act(() => { ws.simulateMessage(refinement()) })
    act(() => { ws.simulateMessage(initial({ tool: 'grep', purpose: 'Find the caller', tool_call_id: 'tc-9' })) })

    // A fresh (non-update) frame is authoritative — no merge, no stale purpose.
    expect(label()).toBe('Find the caller')
    expect(detail().toolName).toBe('grep')
  })
})
