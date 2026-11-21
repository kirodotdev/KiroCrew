/**
 * ChatEmbed's read signal for hosts: `onRead` fires once for each read that
 * lands, from a push signal or a poll, never for a failed read, and the root
 * carries the same time as `data-read-at`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, act, fireEvent, screen } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider, focusManager } from '@tanstack/react-query'

import type { ChatMessage } from '../types'
import { resetEmbedRecoveryForTests } from '../app-sdk/embedSendRecovery'
import { AppSdkContext, type AppSdkContextValue } from '../app-sdk/scopedApi'
import type { SlotPush, SlotPushWatcher } from '../app-sdk/slotPush'

const mockGet = vi.fn()
const mockPost = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: (p: string, ...a: unknown[]) => (p.endsWith('/question') ? Promise.resolve({ pending: [] }) : mockGet(p, ...a)), post: mockPost }),
  useAppInfo: () => ({ name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } }),
}))

vi.mock('../app-sdk/ChatMessageList', () => ({
  default: React.forwardRef(function MockList(
    { transcript }: { messages: ChatMessage[]; transcript?: { earlier?: { hasMore: boolean; onLoad: () => void } } },
    ref: React.Ref<unknown>,
  ) {
    React.useImperativeHandle(ref, () => ({ scrollToBottom: vi.fn() }), [])
    return (
      <div data-testid="chat-message-list">
        {transcript?.earlier?.hasMore && (
          <button data-testid="load-earlier-messages" onClick={() => transcript.earlier!.onLoad()}>earlier</button>
        )}
      </div>
    )
  }),
}))

import ChatEmbed, { EMBED_PUSH_MIN_GAP_MS, type ChatEmbedRead } from '../app-sdk/ChatEmbed'

const IDLE_PAGE = { messages: [{ role: 'user', content: 'hi', ts: 1 }], running: false, title: 'Demo', has_more: false }
const RUNNING_PAGE = { ...IDLE_PAGE, running: true }

function fakePush() {
  const watchers = new Set<SlotPushWatcher>()
  const push: SlotPush = {
    connected: () => true,
    watch(_slotKey, watcher) {
      watchers.add(watcher)
      return () => { watchers.delete(watcher) }
    },
  }
  return { push, signal: () => { for (const w of [...watchers]) w.onChange() } }
}

let queryClient: QueryClient

async function mount(onRead: (read: ChatEmbedRead) => void, push: SlotPush | null = null) {
  const ctx: AppSdkContextValue = {
    api: { get: mockGet, post: mockPost } as unknown as AppSdkContextValue['api'],
    info: { name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } },
    subscribe: () => () => {},
    navigate: () => {},
    notify: () => {},
    ...(push ? { slotPush: push } : {}),
  }
  const tree = (cb: (read: ChatEmbedRead) => void) => (
    <QueryClientProvider client={queryClient}>
      <AppSdkContext.Provider value={ctx}>
        <ChatEmbed slotKey="slot-1" onRead={cb} />
      </AppSdkContext.Provider>
    </QueryClientProvider>
  )
  let view!: ReturnType<typeof render>
  await act(async () => { view = render(tree(onRead)) })
  await act(async () => { await vi.advanceTimersByTimeAsync(100) })
  return {
    root: () => view.container.firstElementChild as HTMLElement,
    rerender: (cb: (read: ChatEmbedRead) => void) => act(async () => { view.rerender(tree(cb)) }),
  }
}

const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms) })

beforeEach(() => {
  vi.clearAllMocks()
  resetEmbedRecoveryForTests()
  mockPost.mockResolvedValue({})
  Element.prototype.scrollIntoView = vi.fn()
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-16T12:00:00Z'))
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
})

afterEach(() => {
  focusManager.setFocused(undefined)
  queryClient.clear()
  vi.useRealTimers()
})

describe('ChatEmbed onRead', () => {
  it('reports the first read with its landing time and running flag, on the root too', async () => {
    mockGet.mockResolvedValue(RUNNING_PAGE)
    const onRead = vi.fn()
    const view = await mount(onRead)
    expect(onRead).toHaveBeenCalledTimes(1)
    const [{ at, running }] = onRead.mock.calls[0] as [ChatEmbedRead]
    expect(running).toBe(true)
    expect(at).toBe(Date.parse('2026-10-16T12:00:00Z'))
    expect(view.root().getAttribute('data-read-at')).toBe(String(at))
  })

  it('reports a push read and a poll read once each', async () => {
    mockGet.mockResolvedValue(IDLE_PAGE)
    const host = fakePush()
    const onRead = vi.fn()
    await mount(onRead, host.push)
    expect(onRead).toHaveBeenCalledTimes(1)
    host.signal()
    await advance(EMBED_PUSH_MIN_GAP_MS + 50)
    expect(mockGet).toHaveBeenCalledTimes(2)
    expect(onRead).toHaveBeenCalledTimes(2)
    const reads = mockGet.mock.calls.length
    await advance(16_000)
    expect(mockGet.mock.calls.length).toBeGreaterThan(reads)
    expect(onRead).toHaveBeenCalledTimes(mockGet.mock.calls.length)
    const ats = onRead.mock.calls.map(([r]) => (r as ChatEmbedRead).at)
    expect(new Set(ats).size).toBe(ats.length)
  })

  it('never reports a failed read, and reports the next read that lands', async () => {
    mockGet.mockResolvedValueOnce(IDLE_PAGE).mockRejectedValueOnce(new Error('down')).mockResolvedValue(IDLE_PAGE)
    const onRead = vi.fn()
    const view = await mount(onRead)
    const first = view.root().getAttribute('data-read-at')
    await advance(5_100)
    expect(mockGet).toHaveBeenCalledTimes(2)
    expect(onRead).toHaveBeenCalledTimes(1)
    expect(view.root().getAttribute('data-read-at')).toBe(first)
    await advance(5_100)
    expect(mockGet).toHaveBeenCalledTimes(3)
    expect(onRead).toHaveBeenCalledTimes(2)
  })

  it('does not report the same read again when the host passes a new callback', async () => {
    mockGet.mockResolvedValue(IDLE_PAGE)
    const first = vi.fn()
    const view = await mount(first)
    const second = vi.fn()
    await view.rerender(second)
    expect(first).toHaveBeenCalledTimes(1)
    expect(second).not.toHaveBeenCalled()
  })

  it('keeps the read time of the page on screen while a wider page loads and after it fails', async () => {
    let rejectWide: (e: Error) => void = () => {}
    mockGet.mockImplementation((u: string) => (u.includes('?limit=200&')
      ? Promise.resolve({ ...IDLE_PAGE, has_more: true })
      : new Promise((_, reject) => { rejectWide = reject })))
    const onRead = vi.fn()
    const view = await mount(onRead)
    const first = view.root().getAttribute('data-read-at')
    expect(first).not.toBeNull()
    await act(async () => { fireEvent.click(screen.getByTestId('load-earlier-messages')) })
    expect(view.root().getAttribute('data-read-at')).toBe(first)
    await act(async () => { rejectWide(new Error('boom')) })
    await advance(10)
    expect(view.root().getAttribute('data-read-at')).toBe(first)
    expect(onRead).toHaveBeenCalledTimes(1)
  })

  it('has no read time before the first read lands', async () => {
    mockGet.mockImplementation(() => new Promise(() => {}))
    const onRead = vi.fn()
    const view = await mount(onRead)
    expect(onRead).not.toHaveBeenCalled()
    expect(view.root().hasAttribute('data-read-at')).toBe(false)
  })
})
