import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* ChatPane sends must follow ChatPage's wire/bubble split for folder tokens
 * (issue #743 review finding): the API payload carries `[attached_dir N] path`
 * markers plus meta.dirs, while the optimistic bubble keeps the raw `@path/`
 * token for the chip. Without this, a split-pane send ships the display token
 * verbatim and history replay has no meta.dirs to resolve. */

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
    chatSlotAgent: vi.fn().mockResolvedValue(undefined),
  },
  SEARCH_MIN_CHARS: 2,
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  },
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }, { name: 'reviewer' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

function makeStore(slotKey: string) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slotKey, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

function renderPane(slotKey: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore(slotKey)
  return renderWithStore(store, qc, slotKey)
}

function renderWithStore(store: ReturnType<typeof makeStore>, qc: QueryClient, slotKey: string) {
  return Object.assign(render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={slotKey} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  ), { store })
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('ChatPane send — folder token serialization', () => {
  it('sends [attached_dir N] wire text with meta.dirs; bubble keeps the raw token', async () => {
    renderPane('pane-1')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'please review @/home/user/design-assets/ thanks' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const [wireText, slot, , , meta] = (api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(slot).toBe('pane-1')
    expect(wireText).toBe('please review [attached_dir 1] /home/user/design-assets thanks')
    expect(meta).toEqual({ dirs: ['/home/user/design-assets'], sendId: expect.stringMatching(/^s-/) })
  })

  it('sends plain text untouched (sendId only) when there are no folder tokens', async () => {
    renderPane('pane-2')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'just words' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const [wireText, , , , meta] = (api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(wireText).toBe('just words')
    // sendId always rides meta (same contract as ChatPage) so the server echo
    // reconciles against the optimistic bubble even when wire text diverges.
    expect(meta).toEqual({ sendId: expect.stringMatching(/^s-/) })
  })
})

/* #4131: the pane's optimistic bubble is confirmed by the send's OWN response.
 * No `chat_message` user echo is coming — `DashboardState.append` suppresses it
 * for dashboard sends because the composer already rendered the bubble — so an
 * accepted response is the only thing that can retire the pending state before
 * the 30s sweep renders "may not have been delivered" on a delivered message. */
describe('ChatPane send — the response confirms the optimistic bubble', () => {
  const userRow = (store: ReturnType<typeof makeStore>, slot: string) =>
    store.getState().chat.slotMessages[slot]?.find(m => m.role === 'user')

  it('retires the pending-confirmation flags when the server accepts', async () => {
    const { store } = renderPane('pane-confirm')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'confirm me' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(userRow(store, 'pane-confirm')?.meta?.optimistic).toBeUndefined())
    // The correlation id stays so a late echo updates this row in place.
    expect(userRow(store, 'pane-confirm')?.meta?.sendId).toMatch(/^s-/)
  })

  it('leaves the bubble pending when the server rejects the send', async () => {
    ;(api.sendChat as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }) })
    const { store } = renderPane('pane-reject')
    const box = (await screen.findAllByRole('textbox'))[0]
    fireEvent.change(box, { target: { value: 'refuse me' } })
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    // A rejected send is exactly what the indicator exists for — the flags must
    // survive so the sweep can flag it.
    expect(userRow(store, 'pane-reject')?.meta?.optimistic).toBe(true)
  })
})

/* The split-view pane is the third dashboard caller of `chatSlotAgent`. It used
 * to swallow failures with `console.error`, so a switch that never happened
 * looked identical to one that did. It now feeds the same shared notice the
 * chat picker and the cycle shortcuts use. */
describe('ChatPane agent switch — failures reach the shared notice', () => {
  async function openAgentPicker() {
    const { store } = renderPane('pane-agent')
    const trigger = await screen.findByLabelText(/agent/i)
    fireEvent.click(trigger)
    return store
  }

  it('publishes the failure message instead of only logging it', async () => {
    const { ApiError } = await import('../api/client') as unknown as {
      ApiError: new (s: number, m: string, b?: string) => Error
    }
    ;(api.chatSlotAgent as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new ApiError(400, 'invalid agent name', JSON.stringify({ error: 'invalid agent name' })),
    )
    const store = await openAgentPicker()
    fireEvent.click(await screen.findByText('reviewer'))

    await waitFor(() => expect(api.chatSlotAgent).toHaveBeenCalledWith('pane-agent', 'reviewer'))
    await waitFor(() =>
      expect(store.getState().chat.agentSwitchNotice?.message).toBe('invalid agent name'),
    )
  })

  it('leaves no notice behind when the switch succeeds', async () => {
    ;(api.chatSlotAgent as ReturnType<typeof vi.fn>).mockResolvedValueOnce(undefined)
    const store = await openAgentPicker()
    fireEvent.click(await screen.findByText('reviewer'))

    await waitFor(() => expect(api.chatSlotAgent).toHaveBeenCalledWith('pane-agent', 'reviewer'))
    expect(store.getState().chat.agentSwitchNotice).toBeNull()
  })
})

/* Producer side of the split-view focus contract: `queryComposer()` scopes its
 * lookup to the `[data-chat-pane]` ancestor of the focused element, falling
 * back to the pane marked `data-chat-pane="focused"` when focus sits in a
 * portal (the pane's own pickers render under document.body). The REAL pane
 * wrapper must carry the attribute — with value "focused" exactly when the
 * grid marks the pane focused — and contain the pane's composer. Losing
 * either would not fail any focus test that mounts fake panes; it would only
 * silently degrade split-view shortcuts back to first-pane-wins in
 * production. */
describe('ChatPane pane boundary — data-chat-pane contract', () => {
  it('the pane wrapper carries data-chat-pane and contains the pane composer', async () => {
    const { container } = renderPane('pane-focus')
    const pane = container.querySelector('[data-chat-pane]')
    expect(pane).not.toBeNull()
    const composer = await screen.findAllByRole('textbox')
    expect(pane!.contains(composer[0])).toBe(true)
    expect(pane!.querySelector('textarea[data-composer-input]')).not.toBeNull()
  })

  it('the wrapper marks the grid-focused pane with the "focused" value', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = makeStore('pane-marked')
    const { container } = render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey="pane-marked" focused />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    )
    expect(container.querySelector('[data-chat-pane="focused"]')).not.toBeNull()
  })

  it('keyboard focus into the pane claims grid focus, not just mousedown', async () => {
    // Tab into a pane (no mousedown) must move the grid's focused marker,
    // or the "focused" fallback would name a pane the user already left and
    // route Alt+Enter from a portaled picker to the wrong session.
    const onFocus = vi.fn()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = makeStore('pane-kbd')
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey="pane-kbd" onFocus={onFocus} />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    )
    const box = (await screen.findAllByRole('textbox'))[0]
    box.focus()
    expect(onFocus).toHaveBeenCalled()
  })
})
