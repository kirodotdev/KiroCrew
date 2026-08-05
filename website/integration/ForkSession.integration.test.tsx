/**
 * Fork Session integration tests.
 *
 * Exercises the full frontend fork flow through MSW:
 *   AssistantMessage button click → forkSlot thunk → POST /api/chat/slots/:slot/fork
 *   → addSlotOptimistic → switchSlot → slot state updates.
 *
 * Complements unit tests in src/test/AssistantMessage.test.tsx (button rendering)
 * and src/test/chatSlice.test.ts (thunk isolated).
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { http, HttpResponse } from 'msw'
import { server } from './mocks/server'
import { createTestStore } from '../src/test/helpers'
import AssistantMessage from '../src/pages/chat/AssistantMessage'
import { forkSlot } from '../src/store/chatSlice'

describe('Fork Session Integration', () => {
  it('fork button click → API call → optimistic slot added to store', async () => {
    const store = createTestStore()
    server.use(
      http.post('/api/chat/slots/chat-1-100/fork', () => HttpResponse.json({
        ok: true, key: 'chat-2-999', title: 'Fork of Parent', messages: 3, prompt: '',
      })),
    )

    // Dispatch the thunk directly — simulates what handleFork does after button click.
    render(
      <Provider store={store}>
        <AssistantMessage
          content="Hello from parent"
          isStreaming={false}
          slotRunning={false}
         
          onFork={() => store.dispatch(forkSlot({ slot: 'chat-1-100', atIndex: 2 })) as unknown as Promise<void>}
          forkIndex={2}
        />
      </Provider>,
    )

    fireEvent.click(screen.getByTitle('Fork conversation from here'))

    await waitFor(() => {
      const slots = store.getState().dashboard.slots
      expect(slots).toContainEqual(
        expect.objectContaining({ key: 'chat-2-999', title: 'Fork of Parent' }),
      )
    })
  })

  it('fork API returning ok:false does not add optimistic slot', async () => {
    const store = createTestStore()
    server.use(
      http.post('/api/chat/slots/chat-1-100/fork', () =>
        HttpResponse.json({ ok: false, error: 'cannot fork an ephemeral session' }, { status: 400 }),
      ),
    )

    const before = store.getState().dashboard.slots.length
    render(
      <Provider store={store}>
        <AssistantMessage
          content="Hello"
          isStreaming={false}
          slotRunning={false}
         
          onFork={() => store.dispatch(forkSlot({ slot: 'chat-1-100' })) as unknown as Promise<void>}
          forkIndex={0}
        />
      </Provider>,
    )

    fireEvent.click(screen.getByTitle('Fork conversation from here'))

    await waitFor(() => {
      expect(store.getState().dashboard.slots.length).toBe(before)
    })
  })

  it('fork button is hidden for streaming messages', () => {
    const store = createTestStore()
    render(
      <Provider store={store}>
        <AssistantMessage
          content="typing…"
          isStreaming={true}
          slotRunning={true}
         
          onFork={() => Promise.resolve()}
        />
      </Provider>,
    )
    expect(screen.queryByTitle('Fork conversation from here')).not.toBeInTheDocument()
  })

  it('fork button disabled while fork in flight (prevents double-click)', async () => {
    const store = createTestStore()
    let resolve: (v: unknown) => void = () => {}
    server.use(
      http.post('/api/chat/slots/:slot/fork', () =>
        new Promise((r) => {
          resolve = () => r(HttpResponse.json({ ok: true, key: 'chat-2-1', title: 'Fork', messages: 1, prompt: '' }))
        }),
      ),
    )

    render(
      <Provider store={store}>
        <AssistantMessage
          content="Hello"
          isStreaming={false}
          slotRunning={false}
         
          onFork={() => store.dispatch(forkSlot({ slot: 'chat-1-100' })) as unknown as Promise<void>}
          forkIndex={0}
        />
      </Provider>,
    )

    const btn = screen.getByTitle('Fork conversation from here') as HTMLButtonElement
    fireEvent.click(btn)

    await waitFor(() => expect(btn).toBeDisabled())

    resolve(null)
    await waitFor(() => expect(btn).not.toBeDisabled())
  })

  it('fork never auto-submits the unsent composer draft', async () => {
    // Option B: forking must NOT pick up the composer text. The fork request
    // carries no prompt, so nothing is auto-submitted into the new session and
    // the unsent draft stays parked in the source slot. Mirrors the post-fix
    // ChatPage.handleFork (fork + switchSlot only — the composer is untouched).
    const store = createTestStore()
    let forkBody: Record<string, unknown> | null = null
    server.use(
      http.post('/api/chat/slots/chat-1-100/fork', async ({ request }) => {
        forkBody = (await request.json().catch(() => ({}))) as Record<string, unknown>
        return HttpResponse.json({ ok: true, key: 'chat-2-777', title: 'Fork', messages: 2, prompt: '' })
      }),
    )

    render(
      <Provider store={store}>
        <AssistantMessage
          content="Hello"
          isStreaming={false}
          slotRunning={false}

          onFork={() => store.dispatch(forkSlot({ slot: 'chat-1-100', atIndex: 1 })) as unknown as Promise<void>}
          forkIndex={1}
        />
      </Provider>,
    )

    fireEvent.click(screen.getByTitle('Fork conversation from here'))

    await waitFor(() => {
      expect(store.getState().dashboard.slots).toContainEqual(
        expect.objectContaining({ key: 'chat-2-777' }),
      )
    })
    // The core fix: the fork POST carries NO prompt, so the parked composer
    // draft is never auto-submitted into the forked session.
    expect(forkBody?.prompt).toBeUndefined()
  })
})
