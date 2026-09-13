/**
 * ChatPage owns the multi-step slot REPLACEMENT behind the welcome-view mode
 * switches: create the replacement, carry the unsent drafts across, record the
 * succession, and release it again when the delete is rejected.
 *
 * The welcome view is stubbed to expose its two callbacks, because what is
 * reachable only from the page is the ORDER of those steps and what survives a
 * rejected delete.
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
    chatSlots: vi.fn().mockResolvedValue([
      { key: 'chat-1', messages: 0, running: false, mode: '', project: '/repo' },
      { key: 'chat-2', messages: 0, running: false, mode: '', project: '/repo' },
    ]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
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
    chatFolders: vi.fn().mockResolvedValue([]),
    tagColumns: vi.fn().mockResolvedValue([]),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: () => false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../components/WelcomeView', () => ({
  default: ({ onSwitchMode }: {
    onSwitchMode?: (m: 'persistent' | 'incognito' | 'temporary') => void
  }) => (
    <>
      <button data-testid="go-incognito" onClick={() => onSwitchMode?.('incognito')}>incognito</button>
    </>
  ),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'
import { api } from '../api/client'
import { DRAFTS_KEY } from '../utils/chatDrafts'
import { clearSlotSuccession, resolveSlotSuccession } from '../utils/slotSuccession'

const readDrafts = (): Record<string, string> => {
  try { return JSON.parse(localStorage.getItem(DRAFTS_KEY) || '{}') } catch { return {} }
}

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: 'chat-1', messages: 0, running: false, mode: '', project: '/repo', pending_approval: false, memory_mode: 'persistent' }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'chat-1', messages: [],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false, followups: {},
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

async function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={makeStore()}>
          <ThemeProvider>
            <MemoryRouter><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  await waitFor(() => expect(screen.getByTestId('go-incognito')).toBeTruthy())
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  vi.clearAllMocks()
  clearSlotSuccession()
  localStorage.setItem(DRAFTS_KEY, JSON.stringify({ 'chat-1': 'unsent words' }))
  ;(api.createChatSlot as ReturnType<typeof vi.fn>).mockResolvedValue({ key: 'chat-2', title: 'chat-2', messages: 0, running: false })
  ;(api.deleteChatSlot as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true })
})

describe('switching a slot to an ephemeral mode carries its unsent work', () => {
  it('copies the draft to the replacement and leaves the succession pinned once the delete succeeds', async () => {
    await renderPage()
    await act(async () => { fireEvent.click(screen.getByTestId('go-incognito')) })
    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalled())
    await waitFor(() => expect(readDrafts()['chat-2']).toBe('unsent words'))
    await waitFor(() => expect(api.deleteChatSlot).toHaveBeenCalledWith('chat-1'))
    // What separates this arm from the rejected one below: an upload that captured
    // the retired key still resolves onto the replacement.
    await waitFor(() => expect(resolveSlotSuccession('chat-1')).toBe('chat-2'))
  })

  it('keeps the old slot\'s draft when the delete is rejected, and stops the succession standing in', async () => {
    ;(api.deleteChatSlot as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('slot busy'))
    await renderPage()
    await act(async () => { fireEvent.click(screen.getByTestId('go-incognito')) })
    await waitFor(() => expect(readDrafts()['chat-2']).toBe('unsent words'))
    expect(readDrafts()['chat-1']).toBe('unsent words')
    expect(resolveSlotSuccession('chat-1')).toBe('chat-1')
  })
})
