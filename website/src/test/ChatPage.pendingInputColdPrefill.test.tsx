// A palette command invoked from a NON-chat page (an app page) seeds `pendingInput`
// and navigates to /chat, which mounts fresh. The pendingInput consumer's non-autoSend
// branch calls `setInput`, but the fresh slot's draft-restore runs AFTER it and would
// overwrite the composer with the slot's empty persisted draft — the seeded prompt
// vanished exactly when the command was invoked anywhere but /chat. The fix routes the
// seed through the keyed prefill channel too, which the restore consumes in preference
// to the stored draft. This pins that the branch writes that channel.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import type { RootState } from '../store'

vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (i: number, d: unknown) => React.ReactNode }) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running: false, has_more: false, total: 1 }),
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

// Spy the keyed prefill writer while keeping every other nav-intent export real. The
// spy still writes through, so the channel behaves normally; we only observe the call.
vi.mock('../utils/navIntent', async importOriginal => {
  const actual = await importOriginal<typeof import('../utils/navIntent')>()
  return { ...actual, writePrefill: vi.fn((...args: Parameters<typeof actual.writePrefill>) => actual.writePrefill(...args)) }
})

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'
import { writePrefill, PREFILL_STORAGE_KEY } from '../utils/navIntent'

const SEED = 'Summarise what shipped this week.'

function makeStore(activeSlot: string, pendingInput: string | null) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: activeSlot, messages: 1, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot, messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput,
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

async function renderAndWaitForInput(store: ReturnType<typeof makeStore>) {
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
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  vi.mocked(writePrefill).mockClear()
})

describe('ChatPage pendingInput cold-mount prefill', { timeout: 15_000 }, () => {
  it('routes a seeded prompt through the keyed prefill channel for the active slot', async () => {
    const store = makeStore('slot-p', SEED)
    await renderAndWaitForInput(store)

    // The branch that seeds the composer now also writes the keyed prefill, so the
    // fresh slot's draft-restore restores the seed rather than the empty draft.
    await waitFor(() => expect(writePrefill).toHaveBeenCalledWith('slot-p', SEED))
  })

  it('does not touch the prefill channel when nothing was seeded', async () => {
    const store = makeStore('slot-p', null)
    await renderAndWaitForInput(store)
    // Give the pendingInput effect a tick; with no pending input it must not fire.
    await new Promise(r => setTimeout(r, 50))
    expect(writePrefill).not.toHaveBeenCalled()
  })

  it('does not clobber a prefill a hand-off already staged for a different slot', async () => {
    // The worktree follow-up keys the NEW slot's prefill and then sets pendingInput;
    // this effect's `activeSlot` can still be render-lagged to the origin slot. Re-keying
    // it here would send the seed to the wrong session's restore.
    sessionStorage.setItem(
      PREFILL_STORAGE_KEY,
      JSON.stringify({ slotKey: 'slot-worktree', prompt: SEED, ts: Date.now() }),
    )
    const store = makeStore('slot-p', SEED)
    await renderAndWaitForInput(store)
    await new Promise(r => setTimeout(r, 50))
    expect(writePrefill).not.toHaveBeenCalled()
    // The hand-off's target survives untouched.
    expect(JSON.parse(sessionStorage.getItem(PREFILL_STORAGE_KEY) || '{}')).toMatchObject({
      slotKey: 'slot-worktree',
      prompt: SEED,
    })
  })
})
