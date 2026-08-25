import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setQuestionCard, clearQuestionCard } from '../store/chatSlice'
import dashboardReducer, { updateSlot } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { FOLLOWUP_CHIP_DEBOUNCE_MS } from '../components/FollowUpBar'

/* A grid pane must surface the agent's follow-up [OPTIONS:] choices
 * (issue #5870): ChatMessageList strips the marker from the transcript, so a
 * ChatPane that never passes followUpOptions to ChatInput silently drops the
 * choices — the user has to retype them by hand. These tests pin the ChatPage
 * wiring mirrored into ChatPane: pills render from the last assistant message,
 * are suppressed while the pane is busy or a question card is up, and a pick
 * routes through the pane's own send path. */


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
    dashboardConfig: vi.fn().mockResolvedValue({ quick_send: false }),
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
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

/** The marker has to close its own line for OPTION_MARKER_RE to match. */
const ASSISTANT_WITH_OPTIONS = 'Ready to proceed.\n\n[OPTIONS: Alpha | Beta]'

const PANE_MESSAGES = [
  { role: 'user', content: 'hi', ts: '2026-08-25T00:00:00Z' },
  { role: 'assistant', content: ASSISTANT_WITH_OPTIONS, ts: '2026-08-25T00:00:01Z' },
]

function makeStore(slotKey: string, slotExtra: Record<string, unknown> = {}) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slotKey, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined, ...slotExtra }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

async function renderPane(slotKey: string, slotExtra: Record<string, unknown> = {}) {
  ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ messages: PANE_MESSAGES, running: false, has_more: false, total: PANE_MESSAGES.length })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore(slotKey, slotExtra)
  await act(async () => {
    render(
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
  })
  // Hydration is settled once the transcript shows the assistant's prose.
  await waitFor(() => expect(screen.getByText(/Ready to proceed/)).toBeTruthy())
  return store
}

const composer = () => (screen.getAllByRole('textbox')[0]) as HTMLTextAreaElement
const chip = (option: string) => screen.getByRole('button', { name: option })

/** Fire one debounced chip click and let its onSelect run (fake timers active). */
function clickOption(option: string, opts: { shiftKey?: boolean } = {}) {
  fireEvent.click(chip(option), opts)
  vi.advanceTimersByTime(FOLLOWUP_CHIP_DEBOUNCE_MS + 10)
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  vi.clearAllMocks()
})
afterEach(() => { vi.useRealTimers() })

describe('ChatPane follow-up options (issue #5870)', () => {
  it('renders the last assistant message\'s [OPTIONS:] choices as pills', async () => {
    await renderPane('pane-1')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
    expect(screen.getByRole('button', { name: 'Beta' })).toBeTruthy()
  })

  it('clicking a pill fills the composer, and Enter sends through the pane\'s send path', async () => {
    await renderPane('pane-2')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
    vi.useFakeTimers()
    await act(async () => { clickOption('Alpha') })
    expect(composer().value).toBe('Alpha')
    vi.useRealTimers()
    fireEvent.keyDown(composer(), { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const [wireText, slot] = (api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(wireText).toBe('Alpha')
    expect(slot).toBe('pane-2')
  })

  it('double-click sends the option label directly through the pane\'s send path', async () => {
    await renderPane('pane-3')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Beta' })).toBeTruthy())
    fireEvent.doubleClick(chip('Beta'))
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const [wireText, slot] = (api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(wireText).toBe('Beta')
    expect(slot).toBe('pane-3')
  })

  it('an option send never consumes the composer draft (clear-without-send guard)', async () => {
    // ChatPage.send gates its clear cluster on `if (!optionText)` — the pane
    // must hold the same invariant: a direct-send of an option label supplies
    // its own text, so the user's typed draft stays in the composer instead of
    // being wiped by a message they never composed.
    await renderPane('pane-6')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
    fireEvent.change(composer(), { target: { value: 'my unsent draft' } })
    fireEvent.doubleClick(chip('Alpha'))
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const [wireText] = (api.sendChat as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(wireText).toBe('Alpha')
    expect(composer().value).toBe('my unsent draft')
  })

  it('unselecting an option splices its own appended text, never a matching substring of the draft', async () => {
    // Regression: `indexOf(', ' + option)` can match INSIDE the draft — draft
    // "Please, Alphabet" + option "Alpha" would splice mid-word on unselect.
    // The handler appends at the END, so it must remove the LAST occurrence.
    await renderPane('pane-7')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
    fireEvent.change(composer(), { target: { value: 'Please, Alphabet' } })
    vi.useFakeTimers()
    await act(async () => { clickOption('Alpha') })
    expect(composer().value).toBe('Please, Alphabet, Alpha')
    await act(async () => { clickOption('Alpha') })
    expect(composer().value).toBe('Please, Alphabet')
  })

  it('offers no pills while the pane is busy, and offers them once busy clears', async () => {
    // selectComposerBusy reads the dashboard slot's subagents_running flag —
    // the same composer-busy rule that queues sends — so the derive gate must
    // suppress the pills for the whole busy window, mirroring ChatPage's
    // isStreaming argument to deriveFollowUpOptions.
    const store = await renderPane('pane-4', { subagents_running: true })
    expect(screen.queryByRole('button', { name: 'Alpha' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Beta' })).toBeNull()
    // Positive control in the same test: flipping busy off makes the pills
    // appear, so the nulls above prove the gate rather than a render break.
    await act(async () => { store.dispatch(updateSlot({ key: 'pane-4', subagents_running: false })) })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
  })

  it('suppresses pills while a pending question card is up, and restores them when it clears', async () => {
    const store = await renderPane('pane-5')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
    await act(async () => {
      store.dispatch(setQuestionCard({ slot: 'pane-5', questions: [{ question: 'Which one?', options: [{ label: 'Card-X' }] }] }))
    })
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Alpha' })).toBeNull())
    // Positive control in the same test: the pills return once the card is
    // gone, so the null above proves the gate, not an unrelated render break.
    await act(async () => { store.dispatch(clearQuestionCard({ slot: 'pane-5' })) })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Alpha' })).toBeTruthy())
  })
})
