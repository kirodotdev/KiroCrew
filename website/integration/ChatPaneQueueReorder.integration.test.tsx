/**
 * ChatPane reorder wiring - locks the full-order invariant from the GPT 5.6
 * review of https://github.com/kirodotdev/KiroCrew/pull/2250:
 *
 * The slot's queue can contain HIDDEN queued messages (sub-agent completion
 * deliveries, recovery continuations) that QueueStack does not render. A
 * reorder triggered from the visible cards must submit the COMPLETE id
 * sequence - visible ids swapped in place, hidden ids keeping their
 * positions - otherwise the backend appends the omitted ids at the tail,
 * silently demoting automation messages.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import React from 'react'
import { http, HttpResponse } from 'msw'
import { renderWithProviders } from './helpers'
import { server } from './mocks/server'
import ChatPane from '../src/components/ChatPane'
import type { ChatMessage } from '../src/types'

vi.mock('framer-motion', () => ({
  AnimatePresence: ({ children }: any) => <>{children}</>,
  motion: {
    div: React.forwardRef(({ children, ...props }: any, ref: any) => (
      <div ref={ref} {...props}>{children}</div>
    )),
  },
  useMotionValue: () => ({ set: vi.fn(), get: () => 0, jump: vi.fn() }),
  useSpring: () => ({ set: vi.fn(), get: () => 0, jump: vi.fn() }),
}))

const SLOT = 'pane-reorder-test'

function queued(queueId: string, content: string): ChatMessage {
  return { role: 'queued', content, cls: 'msg msg-queued', meta: { queueId } } as ChatMessage
}

/** A queued system delivery: excluded from the interactive stack by
 *  isNonInteractiveQueued via its completion-announce prefix. */
function hiddenDelivery(queueId: string): ChatMessage {
  return {
    role: 'queued',
    content: '[Subagent completion event] Agent X completed',
    cls: 'msg msg-queued',
    meta: { queueId },
  } as ChatMessage
}

describe('ChatPane - reorder submits the complete queue order', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/chat/slots/' + SLOT, () =>
        HttpResponse.json({
          messages: [
            queued('q1', 'first visible'),
            hiddenDelivery('sys1'),
            queued('q2', 'second visible'),
            queued('q3', 'third visible'),
          ],
        }),
      ),
    )
  })

  it('keeps hidden queued ids in place and swaps only the visible pair', async () => {
    let submitted: string[] | null = null
    server.use(
      http.put('/api/chat/slots/' + SLOT + '/queue/order', async ({ request }) => {
        submitted = ((await request.json()) as { order: string[] }).order
        return HttpResponse.json({ ok: true })
      }),
    )

    const user = userEvent.setup()
    const { container } = renderWithProviders(<ChatPane slotKey={SLOT} />)

    // 3 visible cards -> stack starts collapsed; expand it.
    await waitFor(() => {
      expect(container.querySelector('[role="button"][aria-expanded]')).not.toBeNull()
    })
    await user.click(container.querySelector('[role="button"][aria-expanded]')!)

    // Move the second visible card (q2) one step sooner (swap with q1).
    const sooner = await screen.findAllByLabelText('Run sooner')
    await user.click(sooner[1])

    await waitFor(() => expect(submitted).not.toBeNull())
    // Full order submitted: sys1 keeps its slot between the swapped pair's
    // positions; q1 and q2 swapped; q3 untouched.
    expect(submitted).toEqual(['q2', 'sys1', 'q1', 'q3'])
  })
})
