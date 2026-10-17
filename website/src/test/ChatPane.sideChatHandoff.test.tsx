import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setActiveSlot, stageToMainComposer } from '../store/chatSlice'
import { __resetPaneDraftsForTests } from '../utils/chatPaneDrafts'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* Side Chat → composer hand-off as a ChatPane consumes it. A pane is keyed by
 * `slotKey`, not by the active slot: a hand-off staged for its slot merges into
 * its composer, and one staged for any other slot is left in the store for
 * that slot's own consumer. */

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
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'

function makeStore(slotKeys: string[]) {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: slotKeys.map(key => ({ key, messages: 0, running: false, mode: 'member', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined })),
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  // The pane is a background slot (the Members page never makes a DM the
  // active chat slot), so the pane's own `slotKey` is the only key it has.
  store.dispatch(setActiveSlot('front'))
  return store
}

function renderPane(slotKey: string, store: ReturnType<typeof makeStore>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={slotKey} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
}

const composer = async () => (await screen.findAllByRole('textbox'))[0] as HTMLTextAreaElement

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  __resetPaneDraftsForTests()
})

describe('ChatPane Side Chat hand-off consumer', () => {
  it('appends a hand-off staged for its own slot to the pane composer and clears it from the store', async () => {
    const store = makeStore(['member-a'])
    renderPane('member-a', store)
    const box = await composer()
    fireEvent.change(box, { target: { value: 'typed so far' } })

    act(() => { store.dispatch(stageToMainComposer({ slot: 'member-a', text: 'Apply the narrow fix.' })) })

    await waitFor(() => expect(box.value).toBe('typed so far\n\nApply the narrow fix.'))
    expect(store.getState().chat.mainComposerAppend).toBeNull()
  })

  it('leaves a hand-off staged for another slot in the store, untouched', async () => {
    const store = makeStore(['member-a', 'member-b'])
    renderPane('member-a', store)
    const box = await composer()
    fireEvent.change(box, { target: { value: 'typed so far' } })

    act(() => { store.dispatch(stageToMainComposer({ slot: 'member-b', text: 'For B only.' })) })

    // Nothing for this pane to do: its composer is as the user left it and the
    // hand-off waits for member-b's own consumer.
    expect(box.value).toBe('typed so far')
    expect(store.getState().chat.mainComposerAppend).toEqual({ slot: 'member-b', text: 'For B only.' })
  })
})
