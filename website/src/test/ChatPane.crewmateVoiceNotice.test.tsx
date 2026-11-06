import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, act, screen, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setActiveSlot } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { __resetPaneDraftsForTests } from '../utils/chatPaneDrafts'

/* A crewmate's reply offers Read aloud (#29689), so a playback failure for the
 * crewmate's slot shows in its own pane -- the Sessions page's notice is not
 * mounted on the Crewmates page. The pane holds an unsent draft, so the notice
 * offers no Ask the agent hand-off. A plain split pane offers no Read aloud and
 * mounts no notice. */

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
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Kiro Crew', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import CrewComposer from '../pages/members/CrewComposer'
import { api } from '../api/client'

const SLOT = 'member-radar-voice'

async function renderPane(crewmate: boolean) {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: 0, running: false, mode: crewmate ? 'member' : 'chat', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  // The Members page never makes the DM the active chat slot.
  store.dispatch(setActiveSlot('front'))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            {crewmate
              ? <ChatPane slotKey={SLOT} frameless agentLocked composerInput={CrewComposer} crewmate={{ name: 'Radar' }} />
              : <ChatPane slotKey={SLOT} />}
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
  // Let the pane's history load settle before the test acts.
  await waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalled())
  await act(async () => { await Promise.resolve() })
}

const fail = (slot: string) =>
  act(() => { window.dispatchEvent(new CustomEvent('voice-error', { detail: { slot, code: 'voice_synthesis_failed' } })) })

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  __resetPaneDraftsForTests()
})

describe("a mate's chat reports a Read aloud failure", () => {
  it("shows the failure for its own slot, without a hand-off beside the draft", async () => {
    await renderPane(true)
    fail('front')
    expect(screen.queryByTestId('voice-playback-error')).toBeNull()
    fail(SLOT)
    const notice = screen.getByTestId('voice-playback-error')
    expect(notice).toHaveTextContent('Read aloud failed')
    expect(screen.queryByRole('button', { name: /ask the agent/i })).toBeNull()
  })

  it('mounts no notice on a plain split pane', async () => {
    await renderPane(false)
    fail(SLOT)
    expect(screen.queryByTestId('voice-playback-error')).toBeNull()
  })
})
