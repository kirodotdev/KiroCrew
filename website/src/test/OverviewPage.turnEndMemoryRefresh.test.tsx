/**
 * The Memory view at the end of a turn, inside the window that holds the
 * config caches. The real socket hook, the real Overview page and the real
 * Memory view share the real store; only the socket and the API are fakes.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { store } from '../store'
import { useWebSocket } from '../hooks/useWebSocket'
import OverviewPage from '../pages/OverviewPage'

const summary = (semantic: number) => ({ stores: [{ name: 'default', is_default: true, lineage: 'v1', exists: true, semantic_count: semantic }], active: 'default' })

const { api } = vi.hoisted(() => {
  const known: Record<string, ReturnType<typeof vi.fn>> = {
    lessons: vi.fn(), memorySettings: vi.fn(), sessions: vi.fn(), memoryStores: vi.fn(),
    memoryRetired: vi.fn(), memoryBackups: vi.fn(), memoryCarve: vi.fn(),
  }
  // Every other read (the socket's boot reads, the cards this test does not count) stays pending.
  return { api: new Proxy(known, { get: (target, name: string) => target[name] ?? (() => new Promise(() => {})) }) as Record<string, ReturnType<typeof vi.fn>> }
})
vi.mock('../api/client', () => ({ api }))
vi.mock('../pages/overview', () => ({ UsageTab: () => null, WakaTimeTab: () => null }))
vi.mock('../pages/overview/VectorMemoryCard', () => ({ SEMANTIC_CARD_ID: 'memory-semantic-card', default: () => <div /> }))
vi.mock('../pages/overview/EmbeddingModelCard', () => ({ default: () => <div /> }))
vi.mock('../pages/overview/MemoryRecordsEditor', () => ({ default: () => <div /> }))
// Loaded up front, so the page's lazy import resolves within `settle`.
await import('../pages/overview/MemoryTab')

class FakeSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  static last: FakeSocket | null = null
  readyState = FakeSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn()
  close = vi.fn(() => { this.readyState = FakeSocket.CLOSED })
  constructor() { FakeSocket.last = this }
  open() { this.readyState = FakeSocket.OPEN; this.onopen?.(new Event('open')) }
  frame(frame: object) { this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(frame) })) }
}

function Socket() { useWebSocket(); return null }

const turnEnd = { type: 'refresh', data: { kinds: ['turn_end'] } }
const calls = (name: string) => api[name].mock.calls.length

beforeEach(() => {
  // The turn-end window is a module timer; fake timers keep it from firing into
  // another test. `settle` runs the zero-delay timers React Query delivers on.
  vi.useFakeTimers()
  FakeSocket.last = null
  vi.stubGlobal('WebSocket', FakeSocket)
  for (const fn of Object.values(api)) fn.mockReset()
  api.lessons.mockResolvedValue({ lessons: [] })
  api.memorySettings.mockResolvedValue({ history_idle_hours: 3, history_max_days: 90, migrated: false })
  api.sessions.mockResolvedValue({ sessions: [] })
  api.memoryStores.mockImplementation(() => Promise.resolve(summary(1)))
  api.memoryRetired.mockResolvedValue({ retired: [] })
  api.memoryBackups.mockResolvedValue({ backups: [] })
  api.memoryCarve.mockResolvedValue({ counts: {} })
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

async function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <MemoryRouter initialEntries={['/?view=memory']}><Socket /><OverviewPage /></MemoryRouter>
      </Provider>
    </QueryClientProvider>,
  )
  const ws = FakeSocket.last!
  await act(async () => { ws.open() })
  await settle()
  expect(calls('memoryStores')).toBeGreaterThan(0)
  expect(calls('memoryBackups')).toBeGreaterThan(0)
  return { ws, view }
}

/** Resolve the reads and run what they wake, a millisecond of fake time per step:
 *  far short of the turn-end window. */
async function settle() {
  for (let i = 0; i < 20; i++) await act(async () => { await vi.advanceTimersByTimeAsync(1) })
}

/** Deliver one frame, 1.5 s after the last (the gateway's spacing); the store
 *  list must have been read for it. */
async function frameAndRead(ws: FakeSocket) {
  await act(async () => { await vi.advanceTimersByTimeAsync(1_500) })
  const before = calls('memoryStores')
  act(() => { ws.frame(turnEnd) })
  await settle()
  expect(calls('memoryStores')).toBeGreaterThan(before)
}

describe('the Memory view at the end of a turn', () => {
  it('re-reads its store list on every turn-end frame, and nothing else while that list is unchanged', async () => {
    const { ws, view } = await mount()
    await frameAndRead(ws)
    const backups = calls('memoryBackups')
    await frameAndRead(ws)
    await frameAndRead(ws)
    expect(calls('memoryBackups')).toBe(backups)
    view.unmount()
  })

  it('re-reads the rest of the view at once when a turn changed the store list', async () => {
    const { ws, view } = await mount()
    await frameAndRead(ws)
    const backups = calls('memoryBackups')
    api.memoryStores.mockImplementation(() => Promise.resolve(summary(2)))
    await frameAndRead(ws)
    expect(calls('memoryBackups')).toBeGreaterThan(backups)
    view.unmount()
  })

  it('acts on a changed answer even when the read a later frame owes starts right behind it', async () => {
    const { ws, view } = await mount()
    await frameAndRead(ws)
    const backups = calls('memoryBackups')
    let land: (value: unknown) => void = () => {}
    api.memoryStores.mockImplementationOnce(() => new Promise(resolve => { land = resolve }))
    api.memoryStores.mockImplementationOnce(() => new Promise(() => {}))
    // A frame starts a slow read; the next frame, a gap later, owes one more.
    await frameAndRead(ws)
    await act(async () => { await vi.advanceTimersByTimeAsync(1_500) })
    act(() => { ws.frame(turnEnd) })
    await settle()
    await act(async () => { land(summary(7)) })
    await settle()
    expect(calls('memoryBackups')).toBeGreaterThan(backups)
    view.unmount()
  })

  it('holds while the tab is hidden and re-reads once when it is shown again', async () => {
    const { ws, view } = await mount()
    await frameAndRead(ws)
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    const hiddenAt = calls('memoryStores')
    act(() => { ws.frame(turnEnd) })
    act(() => { ws.frame(turnEnd) })
    await settle()
    expect(calls('memoryStores')).toBe(hiddenAt)
    visibility.mockReturnValue('visible')
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    // At most one gap after the last read.
    await act(async () => { await vi.advanceTimersByTimeAsync(1_500) })
    await settle()
    expect(calls('memoryStores')).toBe(hiddenAt + 1)
    visibility.mockRestore()
    view.unmount()
  })
})
