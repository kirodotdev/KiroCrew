/**
 * ChatPage-level orchestration for the follow-up card's worktree action.
 *
 * The card component and the reducers are covered in FollowUpCard.test.tsx; what
 * is only reachable from the page is the ORDER and the failure handling of the
 * multi-step handoff (GPT review, PR #461 round 8): create the worktree, open a
 * session, scope it to the new directory, activate it, and only then prefill —
 * with the session deleted again if scoping fails, so a retry cannot accumulate
 * wrongly-scoped sessions.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    // Both slots: the created one must be listed or `switchSlot` cannot activate it.
    chatSlots: vi.fn().mockResolvedValue([
      { key: 'chat-1', messages: 1, running: false, mode: '', project: '/repo' },
      { key: 'chat-2', messages: 0, running: false, mode: '', project: '/repo-wt-limits' },
    ]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    createChatSlot: vi.fn().mockResolvedValue({ key: 'chat-2', title: 'chat-2', messages: 0, running: false }),
    deleteChatSlot: vi.fn().mockResolvedValue({ ok: true }),
    setSlotColor: vi.fn().mockResolvedValue({ ok: true }),
    setSlotFolder: vi.fn().mockResolvedValue({ ok: true }),
    chatSlotProject: vi.fn().mockResolvedValue({ ok: true }),
    createWorktree: vi.fn().mockResolvedValue({ ok: true, path: '/repo-wt-limits', branch: 'followup/add-rate-limits' }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'
import { api } from '../api/client'

const ITEM = {
  title: 'Add rate limits',
  description: 'The upload endpoint is unbounded.',
  prompt: 'Add a token-bucket limiter to POST /api/upload.',
}

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: 'chat-1', messages: 1, running: false, mode: '', project: '/repo', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'chat-1', messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
        followups: { 'chat-1': { items: [ITEM], ts: 100 } },
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

async function renderPage(store: ReturnType<typeof makeStore>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  await waitFor(() => expect(screen.getByText('Add rate limits')).toBeTruthy())
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  vi.clearAllMocks()
  ;(api.createWorktree as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, path: '/repo-wt-limits', branch: 'followup/add-rate-limits' })
  ;(api.createChatSlot as ReturnType<typeof vi.fn>).mockResolvedValue({ key: 'chat-2', title: 'chat-2', messages: 0, running: false })
  ;(api.chatSlotProject as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true })
})

describe('ChatPage follow-up worktree orchestration', () => {
  const composer = () => screen.getByLabelText('Message input') as HTMLTextAreaElement

  it('creates the worktree, scopes the new session, then hands the prompt to it', async () => {
    const store = makeStore()
    await renderPage(store)
    fireEvent.click(screen.getByRole('button', { name: /start in new worktree/i }))
    // Worktree BEFORE session: a git refusal must not leave an empty session.
    await waitFor(() => expect(api.createWorktree).toHaveBeenCalledWith('/repo', 'followup/add-rate-limits'))
    await waitFor(() => expect(api.chatSlotProject).toHaveBeenCalledWith('chat-2', '/repo-wt-limits'))
    expect((api.createWorktree as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0])
      .toBeLessThan((api.createChatSlot as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0])
    // The prompt is handed to the NEW slot through the prefill channel the
    // slot-restore effect reads — asserting the handoff target rather than a
    // rendered composer, which has several other drivers in this harness (the
    // rendered value is covered by the "Add to this session" case below and by
    // the Playwright capture harness). Activation is asserted separately, in the
    // scoping-order test above.
    // The prompt lands in the NEW session's composer: seeded into the prefill
    // channel before the switch, then applied (and cleared) by the slot-restore
    // effect when that slot activates.
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2'))
    await waitFor(() => expect(composer().value).toBe(ITEM.prompt))
    expect(sessionStorage.getItem('kirocrew_prefill')).toBeNull()
    // NOT asserted here: the final card-clear. It runs after `switchSlot(...)`
    // resolves, and that thunk does not settle under this harness (it wants
    // hydration machinery the page mock does not provide), so asserting it would
    // be asserting the harness. The clear IS asserted in the "Add to this
    // session" case below, which takes the same final branch.
  })

  it('does not activate the new session until scoping has completed', async () => {
    // Round 9 HIGH: createSlot used to activate immediately, so the composer went
    // live while chatSlotProject was still pending — a turn sent in that window
    // would run in the DEFAULT directory, not the worktree.
    let releaseScope: (() => void) | undefined
    ;(api.chatSlotProject as ReturnType<typeof vi.fn>).mockImplementation(
      () => new Promise<void>(res => { releaseScope = () => res() }),
    )
    const store = makeStore()
    await renderPage(store)
    fireEvent.click(screen.getByRole('button', { name: /start in new worktree/i }))
    await waitFor(() => expect(api.chatSlotProject).toHaveBeenCalled())
    // Scoping is still in flight: the ORIGIN session is still active AND the new
    // slot is not published yet, so it cannot be selected from the sidebar and
    // sent to while its CWD is still the default checkout (round 10 HIGH).
    expect(store.getState().chat.activeSlot).toBe('chat-1')
    expect(store.getState().dashboard.slots.map(s => s.key)).not.toContain('chat-2')
    releaseScope?.()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2'))
  })

  it('deletes the session it just made when scoping fails, and keeps the card', async () => {
    ;(api.chatSlotProject as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('nope'))
    const store = makeStore()
    await renderPage(store)
    fireEvent.click(screen.getByRole('button', { name: /start in new worktree/i }))
    await waitFor(() => expect(api.deleteChatSlot).toHaveBeenCalledWith('chat-2'))
    // The failed session was never published, so no unscoped slot is left behind.
    expect(store.getState().dashboard.slots.map(s => s.key)).not.toContain('chat-2')
    // No prefill into the wrong composer, and the suggestion survives for a retry.
    expect(composer().value).toBe('')
    expect(store.getState().chat.followups['chat-1']).toBeDefined()
  })

  it('surfaces a worktree failure without creating a session', async () => {
    ;(api.createWorktree as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('Branch already exists: followup/limits'))
    const store = makeStore()
    await renderPage(store)
    fireEvent.click(screen.getByRole('button', { name: /start in new worktree/i }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/Branch already exists/i))
    expect(api.createChatSlot).not.toHaveBeenCalled()
    expect(composer().value).toBe('')
    expect(store.getState().chat.followups['chat-1']).toBeDefined()
  })

  it('"Add to this session" prefills without touching git', async () => {
    const store = makeStore()
    await renderPage(store)
    fireEvent.click(screen.getByRole('button', { name: /add to this session/i }))
    await waitFor(() => expect(composer().value).toBe(ITEM.prompt))
    expect(api.createWorktree).not.toHaveBeenCalled()
    expect(store.getState().chat.followups['chat-1']).toBeUndefined()
  })

  it('appends to an unsent draft instead of destroying it', async () => {
    // Round 13 BLOCKING: the pending-input path replaces AND persists the draft,
    // so a plain set discarded whatever the user was mid-way through typing.
    const store = makeStore()
    await renderPage(store)
    fireEvent.change(composer(), { target: { value: 'half-written thought' } })
    fireEvent.click(screen.getByRole('button', { name: /add to this session/i }))
    await waitFor(() => expect(composer().value).toContain(ITEM.prompt))
    expect(composer().value).toBe(`half-written thought\n\n${ITEM.prompt}`)
  })
})
