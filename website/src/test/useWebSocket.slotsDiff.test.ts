/**
 * A `slots_diff` frame rebuilds the slot list a full `slots` frame would carry.
 *
 * A tab that declares `slot_diff.1` gets a numbered full list (`slotsSeq`) and
 * then only the rows that changed. The hook rebuilds the list from the last one
 * the gateway sent and hands it to the same handler a full frame uses, so the
 * store ends up exactly where the full frame would have put it. A delta built on
 * a list the tab does not hold asks for a resync instead of applying.
 *
 * Deliberately the SINGLETON store, as in useWebSocket.slotsPartial.test.ts: the
 * hook reads slots off the imported singleton.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { store as globalStore } from '../store'
import { sseSlots } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'
import { useWebSocket } from '../hooks/useWebSocket'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockReturnValue(new Promise(() => {})),
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
  url: string
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn()
  close = vi.fn()

  constructor(url: string) {
    this.url = url
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

const rowA = { key: 'slot-a', title: 'A', agent: 'kirocrew', running: false } as ChatSlot
const rowB = { key: 'slot-b', title: 'B', agent: 'kirocrew', running: false } as ChatSlot
const envelope = { yolo: false, channelTrusted: false, folders: [], foldersGeneration: 1, governanceGeneration: 1, gitlabHostsGeneration: 1 }

const listed = () => JSON.stringify(globalStore.getState().dashboard.slots.map(s => ({ key: s.key, title: s.title })))
const resyncs = (ws: MockWebSocket) => ws.send.mock.calls.filter(([m]) => JSON.parse(m).type === 'slots_resync').length

describe('useWebSocket slots_diff', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    globalStore.dispatch(sseSlots([]))
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: globalStore },
      createElement(QueryClientProvider, { client: qc }, children),
    )
  }

  function mountOpened() {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return ws
  }

  it('declares the capability', () => {
    expect(mountOpened().url).toContain('slot_diff.1')
  })

  it('lands where the equivalent full frames land', () => {
    const ws = mountOpened()
    act(() => { ws.simulateMessage({ type: 'slots', data: [rowA, rowB], ...envelope, slotsSeq: 1 }) })
    act(() => { ws.simulateMessage({ type: 'slots_diff', data: { base: 1, seq: 2, rows: [{ key: 'slot-b', row: { ...rowB, title: 'B2' } }], removed: [], envelope: {} } }) })
    const afterDiff = listed()
    act(() => { ws.simulateMessage({ type: 'slots_diff', data: { base: 2, seq: 3, rows: [], removed: ['slot-a'], envelope: { yolo: true } } }) })
    const afterRemoval = listed()
    expect(globalStore.getState().dashboard.approvalMode).toBe('yolo')
    expect(resyncs(ws)).toBe(0)

    // The same lists, as full frames, on a fresh store.
    globalStore.dispatch(sseSlots([]))
    act(() => { ws.simulateMessage({ type: 'slots', data: [rowA, { ...rowB, title: 'B2' }], ...envelope }) })
    expect(listed()).toBe(afterDiff)
    act(() => { ws.simulateMessage({ type: 'slots', data: [{ ...rowB, title: 'B2' }], ...envelope, yolo: true }) })
    expect(listed()).toBe(afterRemoval)
  })

  it('asks once for a resync and leaves the list alone when the base is not the held list', () => {
    const ws = mountOpened()
    act(() => { ws.simulateMessage({ type: 'slots', data: [rowA], ...envelope, slotsSeq: 7 }) })
    const before = listed()
    act(() => { ws.simulateMessage({ type: 'slots_diff', data: { base: 6, seq: 8, rows: [{ key: 'slot-a', row: { ...rowA, title: 'X' } }], removed: [], envelope: {} } }) })
    expect(listed()).toBe(before)
    expect(resyncs(ws)).toBe(1)
    // Deltas before the answering list are ignored, and an unnumbered list (the
    // connect snapshot) is not that answer.
    act(() => { ws.simulateMessage({ type: 'slots_diff', data: { base: 8, seq: 9, rows: [], removed: [], envelope: {} } }) })
    act(() => { ws.simulateMessage({ type: 'slots', data: [rowA], ...envelope }) })
    act(() => { ws.simulateMessage({ type: 'slots_diff', data: { base: 9, seq: 10, rows: [], removed: [], envelope: {} } }) })
    expect(resyncs(ws)).toBe(1)
    // The numbered list ends the wait; a later stray delta asks again.
    act(() => { ws.simulateMessage({ type: 'slots', data: [rowA], ...envelope, slotsSeq: 11 }) })
    act(() => { ws.simulateMessage({ type: 'slots_diff', data: { base: 11, seq: 12, rows: [], removed: [], envelope: {} } }) })
    expect(resyncs(ws)).toBe(1)
    act(() => { ws.simulateMessage({ type: 'slots_diff', data: { base: 5, seq: 13, rows: [], removed: [], envelope: {} } }) })
    expect(resyncs(ws)).toBe(2)
  })
})
