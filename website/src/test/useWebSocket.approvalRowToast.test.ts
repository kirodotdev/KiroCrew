/**
 * useWebSocket `chat_message` (role `permission`) -> OS toast for an
 * interactive tool approval.
 *
 * The chat runner's `permission` row is the only signal an interactive chat
 * sends when it parks on a tool prompt; no `approval` frame and no feed note
 * exist for it, so `useNativeNotification` never sees it. The socket layer
 * posts the toast from the row itself: once per live, unresolved row, only
 * while the window is away, and only when the OS permission is granted.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, cleanup } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { store as globalStore } from '../store'
import { api } from '../api/client'
import { useWebSocket } from '../hooks/useWebSocket'
import { postNativeNotification, nativeNotificationPermitted } from '../lib/nativeNotify'
import { isWindowAway } from '../hooks/windowAway'

vi.mock('../lib/nativeNotify', () => ({
  postNativeNotification: vi.fn(),
  nativeNotificationPermitted: vi.fn(() => true),
}))
vi.mock('../hooks/windowAway', () => ({ isWindowAway: vi.fn(() => true) }))

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    autonudgeList: vi.fn().mockResolvedValue({ enabled: true, loops: [] }),
    monitorsList: vi.fn().mockResolvedValue({ enabled: true, monitors: [] }),
    workflowRuns: vi.fn().mockResolvedValue({ runs: [] }),
  },
}))

const sockets: MockWebSocket[] = []
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
  constructor() { sockets.push(this) }
  open() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }
  frame(type: string, data: unknown) {
    act(() => this.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ type, data }) })))
  }
}

const SLOT = 'chat-parked'

/** The runner's `permission` row as `_broadcast_chat_message` ships it. */
function permissionRow(slot: string, requestId: string, mid: string, extra: Record<string, unknown> = {}) {
  const cls = { request_id: requestId, tool_input: 'ls', tool_call_id: `tc-${requestId}`, ...extra }
  return {
    slot,
    role: 'permission',
    content: 'shell',
    ts: '2026-01-01T00:00:00Z',
    cls: JSON.stringify(cls),
    meta: { approval_id: requestId, tool_input: 'ls', tool_call_id: `tc-${requestId}`, mid, ...extra },
  }
}

describe('interactive approval OS toast from the permission row', () => {
  let testStore: ReturnType<typeof createTestStore>
  let queryClient: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(nativeNotificationPermitted).mockReturnValue(true)
    vi.mocked(isWindowAway).mockReturnValue(true)
    sockets.length = 0
    testStore = createTestStore()
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.spyOn(globalStore, 'getState').mockImplementation(testStore.getState)
    vi.spyOn(globalStore, 'subscribe').mockImplementation(testStore.subscribe)
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(async () => {
    await act(async () => cleanup())
    queryClient.clear()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  async function connect() {
    const wrapper = ({ children }: { children: React.ReactNode }) => createElement(
      Provider, { store: testStore },
      createElement(QueryClientProvider, { client: queryClient }, children),
    )
    renderHook(() => useWebSocket(), { wrapper })
    await act(async () => sockets[0].open())
    return sockets[0]
  }

  it('a new unresolved row posts one silent toast tagged by slot and request', async () => {
    const ws = await connect()
    ws.frame('chat_message', permissionRow(SLOT, 'req-1', 'mid-1'))
    expect(postNativeNotification).toHaveBeenCalledTimes(1)
    const [title, opts] = vi.mocked(postNativeNotification).mock.calls[0]
    expect(title).toBe(SLOT)
    expect(opts?.body).toBe('Waiting for your approval: shell')
    expect(opts).toMatchObject({ silent: true, tag: `kirocrew-approval:${SLOT}:req-1` })
  })

  it('stays quiet while the window is focused', async () => {
    vi.mocked(isWindowAway).mockReturnValue(false)
    const ws = await connect()
    ws.frame('chat_message', permissionRow(SLOT, 'req-1', 'mid-1'))
    expect(postNativeNotification).not.toHaveBeenCalled()
  })

  it('stays quiet without the OS notification permission', async () => {
    vi.mocked(nativeNotificationPermitted).mockReturnValue(false)
    const ws = await connect()
    ws.frame('chat_message', permissionRow(SLOT, 'req-1', 'mid-1'))
    expect(postNativeNotification).not.toHaveBeenCalled()
  })

  it('a row that arrives resolved never toasts', async () => {
    const ws = await connect()
    ws.frame('chat_message', permissionRow(SLOT, 'req-1', 'mid-1', { resolved: 'rejected' }))
    expect(postNativeNotification).not.toHaveBeenCalled()
  })

  it('a row replayed during reconnect catch-up never toasts', async () => {
    const ws = await connect()
    let finishSync!: () => void
    vi.mocked(api.chatSlots).mockImplementationOnce(() => new Promise(resolve => {
      finishSync = () => resolve([])
    }))
    act(() => ws.open())
    ws.frame('chat_message', permissionRow(SLOT, 'req-2', 'mid-2'))
    expect(postNativeNotification).not.toHaveBeenCalled()
    await act(async () => finishSync())
  })

  it('a non-permission row never toasts', async () => {
    const ws = await connect()
    ws.frame('chat_message', { slot: SLOT, role: 'assistant', content: 'hi', ts: '2026-01-01T00:00:00Z', meta: { mid: 'mid-1' } })
    expect(postNativeNotification).not.toHaveBeenCalled()
  })
})
