/**
 * A failed session-create must not lose the knowledge context the user picked.
 *
 * `send()` consumes the pending selection (`clearPending()`) before creating the
 * session, so the create-failure path has to put it back. Skipping that when a NEWER
 * selection exists drops the failed turn's context; replacing drops what the user
 * picked since. Both must survive the merge, newer winning on an id collision.
 *
 * The knowledge hook is mocked HERE (and only here) so the selection is
 * controllable; the rest of the create-failure suite exercises the real hook.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setActiveSlot } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import type { KnowledgeBlock, KnowledgeResult } from '../pages/chat/useKnowledgeFetch'
import { changeLanguage } from '../i18n/all'
import { i18nT } from '../i18n/t'
import { createFailReason } from '../pages/chat/useChatPageComposerController'

const item = (id: string, content = `body-${id}`): KnowledgeResult => ({
  id, title: `T-${id}`, source: null, match_type: 'fts', tokens: 5, summary: '', content,
})

/** Mutable stand-in for the knowledge hook's state. */
const knowledge: { pendingKnowledge: KnowledgeBlock | null; injected: KnowledgeResult[][] } = {
  pendingKnowledge: null,
  injected: [],
}

vi.mock('../pages/chat/useKnowledgeFetch', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../pages/chat/useKnowledgeFetch')>()
  return {
    ...actual,
    useKnowledgeFetch: () => ({
      results: [],
      query: '',
      loading: false,
      get pendingKnowledge() { return knowledge.pendingKnowledge },
      searchKnowledge: vi.fn(),
      inject: (items: KnowledgeResult[]) => {
        knowledge.injected.push(items)
        knowledge.pendingKnowledge = { items, totalTokens: items.length }
      },
      clearPending: () => { knowledge.pendingKnowledge = null },
      clearResults: vi.fn(),
    }),
  }
})

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
const createChatSlot = vi.fn()
const sendChat = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: (...a: unknown[]) => sendChat(...a),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    createChatSlot: (...a: unknown[]) => createChatSlot(...a),
    setSlotColor: vi.fn().mockResolvedValue({ ok: true }),
    setSlotFolder: vi.fn().mockResolvedValue({ ok: true }),
    chatSlotProject: vi.fn().mockResolvedValue({ ok: true }),
    suggestions: vi.fn().mockResolvedValue({ suggestions: [] }),
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

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slotsLoaded: true,
        slots: [
          { key: 'slot-a', messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined },
          { key: 'slot-b', messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined },
        ],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages: [],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: 'context-bearing message',
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

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  createChatSlot.mockReset()
  sendChat.mockReset()
  sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) })
  knowledge.pendingKnowledge = null
  knowledge.injected = []
})

afterEach(async () => {
  await changeLanguage('en')
})

describe('create-failure knowledge recovery', { timeout: 20_000 }, () => {
  it('merges the failed selection with a newer one, newer winning on id collision', async () => {
    // Selection A is pending when the auto-send fires; B (sharing one id) is chosen
    // while the create is still in flight.
    knowledge.pendingKnowledge = { items: [item('a'), item('shared', 'OLD-shared')], totalTokens: 10 }
    let rejectCreate: (e: Error) => void = () => {}
    createChatSlot.mockImplementation(() => new Promise((_res, rej) => { rejectCreate = rej }))

    const store = makeStore()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    await act(async () => {
      render(
        <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter initialEntries={['/chat?autoSend=1&newSession=1']}><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
        </QueryClientProvider>,
      )
    })
    await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
    // send() consumed A via clearPending(); the user then picks B mid-flight.
    knowledge.pendingKnowledge = { items: [item('b'), item('shared', 'NEW-shared')], totalTokens: 10 }

    await act(async () => {
      rejectCreate(new Error('gateway unavailable'))
      await Promise.resolve()
    })

    await waitFor(() => expect(knowledge.injected.length).toBeGreaterThan(0))
    const merged = knowledge.injected[knowledge.injected.length - 1]
    const ids = merged.map(i => i.id)
    // Both selections survive...
    expect(ids).toContain('a')
    expect(ids).toContain('b')
    // ...and the colliding id appears once, taking the NEWER item.
    expect(ids.filter(id => id === 'shared')).toHaveLength(1)
    // Distinct content on each side, so reversing precedence fails this.
    expect(merged.find(i => i.id === 'shared')?.content).toBe('NEW-shared')
    expect(sendChat).not.toHaveBeenCalled()
  })

  it('preserves the legacy knowledge suffix through the pseudolocale template', async () => {
    // Off-screen, `inject` would attach the failed turn's context to whatever session
    // the user is now viewing, so it is deliberately not restored — but the
    // notification must say so rather than leaving the retry quietly context-free.
    knowledge.pendingKnowledge = { items: [item('a')], totalTokens: 5 }
    let rejectCreate: (e: Error) => void = () => {}
    createChatSlot.mockImplementation(() => new Promise((_res, rej) => { rejectCreate = rej }))

    const store = makeStore()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    await act(async () => {
      render(
        <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter initialEntries={['/chat?autoSend=1&newSession=1']}><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
        </QueryClientProvider>,
      )
    })
    await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
    await changeLanguage('en-XA')

    await act(async () => {
      store.dispatch(setActiveSlot('slot-b'))
      rejectCreate(new Error('gateway unavailable'))
      await Promise.resolve()
    })

    await waitFor(() => expect(store.getState().notifications.items).toHaveLength(1))
    const note = store.getState().notifications.items[0]
    expect(note.slot).toBe('slot-a')
    expect(note.body).toBe(i18nT('pages.chatPage.message_saved_as_draft', {
      error: 'gateway unavailable',
      extra: ' Its knowledge context was not kept — re-pick it before you resend.',
    }))
    // And it was NOT injected into slot-b's selection.
    expect(knowledge.injected).toHaveLength(0)
  })

  it('keeps the legacy rejected-send fallback byte-identical under a non-English locale', async () => {
    await changeLanguage('de')
    expect(createFailReason({})).toBe('the server did not respond')
  })
})
