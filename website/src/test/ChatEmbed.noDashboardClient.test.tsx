/**
 * Invariant: an app-sdk embed's composer makes NO dashboard-client calls.
 *
 * ChatEmbed mounts the real ChatInput, which talks to the dashboard client
 * (`api/client`), not the embed's permission-scoped app wire. The embed
 * narrows the composer by omitting capability props, and each ambient call
 * ChatInput can make sits behind one of them (approvals behind
 * `slotApprovalChrome`, auto-compact behind the context chip, the skills
 * prefetch behind `typedCommandMenus`). A test per seam pins today's three
 * instances; this test pins the INVARIANT, so the next ambient effect added to
 * ChatInput -- a change that never touches ChatEmbed -- goes red here instead
 * of shipping undeclared API traffic out of every app embed.
 *
 * The dashboard client is replaced wholesale by a Proxy that records every
 * property access, so a new `api.whatever()` is caught without this file
 * knowing its name. Mount, focus, type and blur are exercised; the send itself
 * goes through the app wire (`useAppApi().post`) and is asserted separately in
 * ChatEmbed.sendReceipt.test.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'

const dashboardCalls = vi.hoisted(() => [] as string[])
vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get(_t, prop) {
      if (typeof prop === 'symbol') return undefined
      return (..._args: unknown[]) => {
        dashboardCalls.push(String(prop))
        return Promise.resolve({})
      }
    },
  }),
}))

const mockGet = vi.fn()
const mockPost = vi.fn()
vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
}))
vi.mock('../app-sdk/ChatMessageList', () => ({
  default: ({ messages }: { messages: unknown[] }) => <div data-testid="rows" data-count={messages.length} />,
}))

import ChatEmbed from '../app-sdk/ChatEmbed'

let queryClient: QueryClient

beforeEach(() => {
  dashboardCalls.length = 0
  vi.clearAllMocks()
  vi.useFakeTimers()
  Element.prototype.scrollIntoView = vi.fn()
  mockGet.mockResolvedValue({ messages: [], running: false, title: '' })
  mockPost.mockResolvedValue({ ok: true })
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('ChatEmbed composer makes no dashboard-client calls', () => {
  it('on mount, focus, typing, blur and a send, only the app wire is touched', async () => {
    await act(async () => {
      render(
        <Provider store={createTestStore()}>
          <QueryClientProvider client={queryClient}>
            <ChatEmbed slotKey="slot-1" agent="spec-builder" />
          </QueryClientProvider>
        </Provider>,
      )
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    const input = screen.getByLabelText('Chat message')
    await act(async () => {
      fireEvent.focus(input)
      fireEvent.change(input, { target: { value: 'hello $skill /cmd @file' } })
      fireEvent.blur(input)
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(500) })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Send' })) })
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })

    // The send went through the permission-scoped app wire...
    expect(mockPost).toHaveBeenCalledWith('/api/chat?ws=1', expect.objectContaining({ message: 'hello $skill /cmd @file' }))
    // ...and nothing reached the dashboard client. A non-empty list names the
    // new ambient call so the fix is a one-line gate, not a hunt.
    expect(dashboardCalls).toEqual([])
  })
})
