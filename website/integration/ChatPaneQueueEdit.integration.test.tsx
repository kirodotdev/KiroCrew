/**
 * ChatPane queue edit wiring — regression test for
 * https://github.com/kirodotdev/KiroCrew/issues/2240
 *
 * QueueStack's inline edit (Pencil -> EditInput -> commit) was fully built and
 * unit-tested, and ChatPage passes `onEdit` — but ChatPane (split view, ⌘D)
 * did not, so the Pencil never rendered in split panes. These tests pin the
 * ChatPane-level wiring end to end: the affordance renders, a commit PATCHes
 * the backend, and the store is left to the authoritative `queue_edit` WS
 * echo (a rejected PATCH restores the typed text into the composer).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import React from 'react'
import { http, HttpResponse } from 'msw'
import { renderWithProviders, createTestStore } from './helpers'
import { server } from './mocks/server'
import ChatPane from '../src/components/ChatPane'
import type { ChatMessage } from '../src/types'

// Mock framer-motion so QueueStack renders its children directly (same
// pattern as QueueStackInterrupt.integration.test.tsx).
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

const SLOT = 'pane-edit-test'

function queuedMsg(queueId: string, content: string): ChatMessage {
  return { role: 'queued', content, cls: 'msg msg-queued', meta: { queueId } } as ChatMessage
}

/** Seed the slot's hydration endpoint with one queued message. */
function mockSlotDetail(messages: ChatMessage[]) {
  server.use(
    http.get('/api/chat/slots/' + SLOT, () => HttpResponse.json({ messages })),
  )
}

describe('ChatPane — queued message inline edit (issue #2240)', () => {
  beforeEach(() => {
    mockSlotDetail([queuedMsg('q1', 'original queued text')])
  })

  it('renders the edit affordance on a queued message (regression: onEdit was not passed)', async () => {
    renderWithProviders(<ChatPane slotKey={SLOT} />)
    await waitFor(() => {
      expect(screen.getByLabelText('Edit queued message')).toBeInTheDocument()
    })
  })

  it('commits an edit: PATCHes the queue item; the store is left to the WS echo', async () => {
    let patched: { url: string; body: any } | null = null
    server.use(
      http.patch('/api/chat/slots/' + SLOT + '/queue/q1', async ({ request }) => {
        patched = { url: request.url, body: await request.json() }
        return HttpResponse.json({ ok: true })
      }),
    )

    const store = createTestStore()
    const user = userEvent.setup()
    renderWithProviders(<ChatPane slotKey={SLOT} />, { store })

    await waitFor(() => {
      expect(screen.getByLabelText('Edit queued message')).toBeInTheDocument()
    })
    await user.click(screen.getByLabelText('Edit queued message'))

    const input = await screen.findByRole('textbox', { name: 'Edit queued message' })
    await user.clear(input)
    await user.type(input, 'rewritten before running{Enter}')

    await waitFor(() => {
      expect(patched).not.toBeNull()
      expect(patched!.body).toEqual({ content: 'rewritten before running' })
    })

    // No client-side dispatch: the authoritative queue_edit WS broadcast is
    // the only store writer (no WS in this harness, so content is unchanged).
    // A local dispatch here could race a concurrent edit with stale text.
    const msgs = store.getState().chat.slotMessages[SLOT] || []
    const edited = msgs.find(m => m.role === 'queued' && (m.meta as any)?.queueId === 'q1')
    expect(edited?.content).toBe('original queued text')
  })

  it('does not update the store when the PATCH fails (no stale-rollback race)', async () => {
    server.use(
      http.patch('/api/chat/slots/' + SLOT + '/queue/q1', () =>
        HttpResponse.json({ error: 'boom' }, { status: 500 }),
      ),
    )

    const store = createTestStore()
    const user = userEvent.setup()
    renderWithProviders(<ChatPane slotKey={SLOT} />, { store })

    await waitFor(() => {
      expect(screen.getByLabelText('Edit queued message')).toBeInTheDocument()
    })
    await user.click(screen.getByLabelText('Edit queued message'))

    const input = await screen.findByRole('textbox', { name: 'Edit queued message' })
    await user.clear(input)
    await user.type(input, 'edit that will fail{Enter}')

    // The store is only written after a successful PATCH, so a failure leaves
    // the queued message untouched - the card keeps showing what the agent
    // will actually receive, and a delayed rejection can never clobber a
    // later successful edit.
    await waitFor(() => {
      const msgs = store.getState().chat.slotMessages[SLOT] || []
      const m = msgs.find(x => x.role === 'queued' && (x.meta as any)?.queueId === 'q1')
      expect(m?.content).toBe('original queued text')
    })

    // The rejection must not swallow the typed edit: the editor is already
    // closed, so the replacement text is restored into the pane's composer.
    await waitFor(() => {
      const composer = screen.getByRole('textbox', { name: 'Message input' }) as HTMLTextAreaElement
      expect(composer.value).toBe('edit that will fail')
    })
  })


  it('does not restore into the composer on a transport-ambiguous failure', async () => {
    // A network-level failure (request never sent, or response lost after the
    // server committed) is ambiguous: restoring could duplicate a committed
    // edit. Only a real HTTP rejection proves the edit was not committed, so
    // the transport case skips the restore - the card keeps showing the old
    // text, which is the user's cue to retry.
    server.use(
      http.patch('/api/chat/slots/' + SLOT + '/queue/q1', () => HttpResponse.error()),
    )

    const store = createTestStore()
    const user = userEvent.setup()
    renderWithProviders(<ChatPane slotKey={SLOT} />, { store })

    await waitFor(() => {
      expect(screen.getByLabelText('Edit queued message')).toBeInTheDocument()
    })
    await user.click(screen.getByLabelText('Edit queued message'))
    const input = await screen.findByRole('textbox', { name: 'Edit queued message' })
    await user.clear(input)
    await user.type(input, 'transport limbo edit{Enter}')

    // Give the rejection time to propagate, then assert the composer stayed empty.
    await new Promise(r => setTimeout(r, 200))
    const composer = screen.getByRole('textbox', { name: 'Message input' }) as HTMLTextAreaElement
    expect(composer.value).toBe('')
  })

  it('does not PATCH on an empty edit (trim guard)', async () => {
    const patchSpy = vi.fn()
    server.use(
      http.patch('/api/chat/slots/' + SLOT + '/queue/q1', () => {
        patchSpy()
        return HttpResponse.json({ ok: true })
      }),
    )

    const user = userEvent.setup()
    renderWithProviders(<ChatPane slotKey={SLOT} />)

    await waitFor(() => {
      expect(screen.getByLabelText('Edit queued message')).toBeInTheDocument()
    })
    await user.click(screen.getByLabelText('Edit queued message'))

    const input = await screen.findByRole('textbox', { name: 'Edit queued message' })
    await user.clear(input)
    await user.keyboard('{Enter}')

    expect(patchSpy).not.toHaveBeenCalled()
  })
})
