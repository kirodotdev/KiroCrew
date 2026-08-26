/**
 * ChatEmbed approval rollback — integration through the REAL ChatMessageList
 * and CollapsibleToolGroup (unlike ChatEmbed.test.tsx, which mocks the list).
 *
 * The chain under test: ChatEmbed's approve handler must RETURN the approval
 * POST's promise (mutateAsync), ChatMessageListProps.onApprove must let that
 * promise through its type boundary, and CollapsibleToolGroup.submitDecision's
 * .catch must roll the optimistic "Approved" state back to answerable buttons
 * when the POST fails. A fire-and-forget handler (mutate) breaks the first
 * link: the row renders Approved while the agent stays parked on the
 * undelivered decision (#5524).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ChatMessage } from '../types'

const mockGet = vi.fn()
const mockPost = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
}))

import ChatEmbed from '../app-sdk/ChatEmbed'

// One unresolved permission message: ChatMessageList groups it and renders the
// CollapsibleToolGroup approval row (collapsed — running=false, so no
// autoExpand) with live Approve/Trust/Reject buttons.
const PENDING_APPROVAL: ChatMessage[] = [
  {
    role: 'permission',
    content: '',
    cls: '',
    ts: '1',
    meta: { approval_id: 'appr-1', tool_input: 'echo hi' },
  },
]

let queryClient: QueryClient

function renderEmbed() {
  return render(
    React.createElement(
      QueryClientProvider,
      { client: queryClient },
      <ChatEmbed slotKey="slot-1" />,
    ),
  )
}

/** Advance the fake timers in small steps until `predicate` holds (or the
 *  bounded budget runs out — the caller's assertions then fail closed). This
 *  avoids coupling the test to how many microtask/timer ticks React Query's
 *  commit currently needs; RTL's waitFor cannot be used here because it does
 *  not advance vitest fake timers. */
async function settleUntil(predicate: () => boolean, maxTicks = 50) {
  for (let i = 0; i < maxTicks && !predicate(); i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10)
    })
  }
}

/** Mount the embed and flush until the pending approval row is on screen. */
async function mountWithPendingApproval() {
  await act(async () => {
    renderEmbed()
    await vi.advanceTimersByTimeAsync(0)
  })
  await settleUntil(() => screen.queryByRole('button', { name: 'Approve' }) !== null)
  expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument()
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers()
  Element.prototype.scrollIntoView = vi.fn()
  mockGet.mockResolvedValue({ messages: PENDING_APPROVAL, running: false, title: '' })
  mockPost.mockResolvedValue({})
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
})

afterEach(() => {
  // Restore the console.error spy (and any other spy) so a later test's real
  // render errors are not silently swallowed.
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('ChatEmbed approval rollback (#5524)', () => {
  it('a failed approval POST rolls back to an answerable card', async () => {
    mockPost.mockRejectedValue(new Error('boom'))
    // The rollback path intentionally logs the failure; keep the test log clean.
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})

    await mountWithPendingApproval()

    await act(async () => {
      screen.getByRole('button', { name: 'Approve' }).click()
    })
    // Flush until the rejection has travelled the whole chain (the rollback
    // logs it), never a fixed tick count.
    await settleUntil(() => consoleError.mock.calls.some(c => c[0] === 'Approval failed:'))

    // The POST was attempted...
    expect(mockPost).toHaveBeenCalledWith('/api/chat/slots/slot-1/approve', {
      action: 'approved',
      request_id: 'appr-1',
    })
    expect(consoleError).toHaveBeenCalledWith('Approval failed:', expect.any(Error))
    // ...and failed, so the optimistic "Approved" state must be rolled back:
    // the row still needs attention and every decision button is back and
    // enabled. With a fire-and-forget handler the rejection never reaches
    // submitDecision's .catch and this row would read "Approved" forever.
    expect(screen.getByText('Approval needed')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeEnabled()
  })

  it('a successful approval POST keeps the resolved state (no rollback)', async () => {
    await mountWithPendingApproval()

    await act(async () => {
      screen.getByRole('button', { name: 'Approve' }).click()
    })
    await settleUntil(() => screen.queryByText('Approved') !== null)

    expect(mockPost).toHaveBeenCalledWith('/api/chat/slots/slot-1/approve', {
      action: 'approved',
      request_id: 'appr-1',
    })
    // Resolved for good: the label settles on "Approved"...
    expect(screen.getByText('Approved')).toBeInTheDocument()
    // ...and after letting any straggling settlement (refetch scheduling) run,
    // the decision buttons never come back.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200)
    })
    expect(screen.getByText('Approved')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Reject' })).toBeNull()
  })
})
