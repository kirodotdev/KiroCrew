/**
 * The unresumable-resume notice becomes pixels on the chat pane (#5925).
 *
 * `api_chat_slot_resume` succeeds whether or not the resumed session's surface
 * is one the chat page can display, so `ok` alone cannot tell a usable resume
 * from one that will bounce (#3624). PR #3640 taught ONE call site -- the
 * sidebar's history row -- to read the returned `surface`; the four siblings
 * (this page's "Continue a previous chat" list, the notification panel's Resume
 * button, and the `recents` / `sessions` command-palette providers) stayed
 * blind, and two of them are plain modules with no component to render into.
 *
 * The check now lives in `resumeFromHistory`'s own cases and the notice renders
 * HERE rather than in the sidebar, because the sidebar's Older Sessions pane
 * starts closed (`historyOpen` defaults to false): a notice inside it can only
 * be seen by someone who had already opened it, which is nobody arriving from
 * the other three paths. All four of them end on /chat, so this is the one
 * surface that can answer for all of them -- and it sits with the pane-level
 * banners, outside the split / no-slot / transcript ternary, because a resume
 * can arrive with no active slot at all.
 *
 * This file pins the half the store-level tests cannot see -- that ChatPage
 * reads the slice field, localizes the sentence from the raw facts stored
 * there, and dismisses through the slice. It mounts the real page, so a notice
 * rendered into a collapsed or unmounted branch would fail here.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'

interface VirtuosoMockProps {
  data?: unknown[]
  itemContent: (index: number, item: unknown) => ReactNode
}
vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: VirtuosoMockProps) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))

type Msg = { role: string; content: string }
const detail = vi.hoisted(() => ({ messages: [] as Msg[] }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn(async () => ({ messages: detail.messages, running: false, has_more: false, total: detail.messages.length })),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
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
// The surface registry is populated by module side effect, and only `App.tsx`
// imports it in production -- so a harness that mounts ChatPage directly starts
// with an EMPTY registry and every surface lookup misses. Import it here for the
// same reason the app does. (The miss degrades safely to the surface-free
// sentence, which is what the unregistered-surface case below asserts.)
import '../surfaces/builtins'

const plainTurn: Msg[] = [
  { role: 'user', content: 'hello' },
  { role: 'assistant', content: 'first answer' },
]

function makeStore(unresumableResume: { key: string; title: string; surface: string; reason: 'surface' | 'failed' } | null, activeSlot: string | null = 'slot-a') {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        slots: activeSlot ? [{ key: 'slot-a', messages: plainTurn.length, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }] : [],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot, messages: activeSlot ? plainTurn : [],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        unresumableResume, lastResumeRequestId: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

async function renderWith(unresumableResume: { key: string; title: string; surface: string; reason: 'surface' | 'failed' } | null, activeSlot: string | null = 'slot-a') {
  detail.messages = plainTurn
  const store = makeStore(unresumableResume, activeSlot)
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
  return store
}

beforeEach(() => {
  detail.messages = plainTurn
})

describe('unresumable-resume notice on the chat pane (#5925)', () => {
  it('renders nothing while the slice holds no unresumable resume', async () => {
    await renderWith(null)
    expect(screen.queryByTestId('unresumable-resume-error')).toBeNull()
  })

  it('names the session and its surface when the slice records one', async () => {
    // A `dashboard`-prefixed key resolves to the localized dashboard label
    // instead of interpolating the raw wire surface -- the case the key-prefix
    // heuristic exists for ("a Session session" is what interpolating it gives).
    await renderWith({ key: 'dashboard_ops', title: 'Ops board', surface: 'dashboard', reason: 'surface' })

    const notice = await screen.findByTestId('unresumable-resume-error')
    expect(notice.textContent).toContain('Ops board')
    expect(notice.textContent).toContain("can't be opened in chat")
    // The sentence must NOT name the sidebar: three of the four entry points
    // never touch it, and the notice no longer renders there.
    expect(notice.textContent).not.toContain('sidebar')
  })

  it('dismissing clears the slice field, so a re-render cannot resurrect it', async () => {
    const store = await renderWith({ key: 'dashboard_ops', title: 'Ops board', surface: 'dashboard', reason: 'surface' })

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))

    await waitFor(() => expect(screen.queryByTestId('unresumable-resume-error')).toBeNull())
    expect((store.getState() as RootState).chat.unresumableResume).toBeNull()
  })

  it('renders with NO active slot, which is how a palette or notification resume arrives', async () => {
    // The reachable case that a composer-adjacent notice missed entirely: with no
    // tab open, ChatPage renders its empty state, so a notice nested inside the
    // transcript branch is not in the tree at all -- and both palette providers
    // plus the notification panel navigate here in exactly that condition. The
    // notice therefore lives with the pane-level banners, outside the
    // split / no-slot / transcript ternary.
    await renderWith({ key: 'dashboard_ops', title: 'Ops board', surface: 'dashboard', reason: 'surface' }, null)

    const notice = await screen.findByTestId('unresumable-resume-error')
    expect(notice.textContent).toContain('Ops board')
    // Confirm we really are in the no-slot state, so this is not silently
    // asserting the transcript branch again.
    expect(screen.getByText('What can I do for you?')).toBeTruthy()
  })

  it('a FAILED resume gets its own sentence and names no surface', async () => {
    // Nothing was resumed, so there is no surface to name -- claiming one would
    // be a guess.
    await renderWith({ key: 'chat-9', title: 'Older chat', surface: '', reason: 'failed' })

    const notice = await screen.findByTestId('unresumable-resume-error')
    expect(notice.textContent).toContain('Older chat')
    expect(notice.textContent).toContain("Couldn't open")
    // No restatement: "Couldn't open" already says the resume did not work.
    expect(notice.textContent).not.toContain('resume failed')
    expect(notice.textContent).not.toContain('chat session')
  })

  it('an unregistered surface gets a surface-free sentence, not raw machine vocabulary', async () => {
    // The wire `surface` is a machine value. Interpolating it renders lowercase
    // machine words mid-sentence ("it's a subagent session"), and its empty case
    // reads "it's a Session session".
    await renderWith({ key: 'chat-9', title: 'Older chat', surface: 'subagent', reason: 'surface' })

    const notice = await screen.findByTestId('unresumable-resume-error')
    expect(notice.textContent).toContain("isn't a chat session")
    expect(notice.textContent).not.toContain('subagent')
    // "surface" is internal vocabulary; it must not reach user copy.
    expect(notice.textContent).not.toContain('surface')
  })

  it('a registered surface is named by the registry label, not its slot mode', async () => {
    await renderWith({ key: 'member-ada', title: 'Ada', surface: 'member', reason: 'surface' })

    const notice = await screen.findByTestId('unresumable-resume-error')
    expect(notice.textContent).toContain('Crew Members')
    expect(notice.textContent).not.toContain('member session')
  })
})
