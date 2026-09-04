/**
 * Render-site pin for #8204: the disclosure identity ChatPage passes to
 * ToolCallLine must be the id-folded toolDisclosureKey, not the bare row key.
 *
 * The pure-helper suite (toolDisclosureKey.collision.test.ts) proves the
 * helper disambiguates; this file proves the RENDER SITE actually uses it.
 * Reverting `disclosure={toolDisclosure[dKey]}` / `disclosureKey={dKey}` back
 * to the bare `key` restores the defect (same-tick tool pills expand together)
 * while every helper-level test stays green — so the wiring needs its own pin.
 *
 * Arrangement note: exactly two tool rows are used on purpose — the
 * "Worked through N steps" turn fold requires items.length > 2, so two pills
 * render directly and the test needs no fold expansion.
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

// The mount fetch REPLACES chat.messages, so the mock must serve the same
// transcript the store preloads (see ChatPage.continueGate.test.tsx).
const detail = vi.hoisted(() => ({ messages: [] as { role: string; content: string; cls?: string; ts?: string; meta?: Record<string, unknown> }[] }))
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

if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

import ChatPage from '../pages/ChatPage'

type Msg = { role: string; content: string; cls?: string; ts?: string; meta?: Record<string, unknown> }

function makeStore(messages: Msg[]) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        slots: [{ key: 'slot-a', messages: messages.length, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages,
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
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

async function renderWith(messages: Msg[]) {
  detail.messages = messages
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={makeStore(messages)}>
          <ThemeProvider>
            <MemoryRouter><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
}

/** The tool pills' own disclosure buttons: aria-expanded buttons wrapping the
 *  tool-pill-label testid — page chrome (sidebar toggles, dropdowns) also
 *  carries aria-expanded, so the label anchor is what scopes this to pills. */
const pills = () => screen.getAllByRole('button').filter(
  b => b.hasAttribute('aria-expanded') && b.querySelector('[data-testid="tool-pill-label"]'),
)

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
})

describe('ChatPage tool-row disclosure identity (#8204 render site)', { timeout: 15_000 }, () => {
  it('expands only the clicked pill when two same-tick tool rows share a row key', async () => {
    // Same server tick ('tick'), no clientTs stamp, distinct tool_call_ids —
    // the exact shape #8204 measured. Their messageRowKey collides ('tool-tick');
    // only the id-folded disclosure key tells them apart.
    await renderWith([
      { role: 'user', content: 'do two things', cls: '', ts: 'u1' },
      { role: 'tool', content: '🔧 Running: alpha', cls: '', ts: 'tick', meta: { tool_call_id: 'tc1', purpose: 'first' } },
      { role: 'tool', content: '🔧 Running: bravo', cls: '', ts: 'tick', meta: { tool_call_id: 'tc2', purpose: 'second' } },
    ])

    await waitFor(() => expect(pills().length).toBe(2))
    const [a, b] = pills()
    expect(a.getAttribute('aria-expanded')).toBe('false')
    expect(b.getAttribute('aria-expanded')).toBe('false')

    fireEvent.click(a)

    // Before the fix both pills expanded here: the disclosure map was keyed by
    // the (colliding) row key, so one row's entry was every sibling's entry.
    const [a2, b2] = pills()
    expect(a2.getAttribute('aria-expanded')).toBe('true')
    expect(b2.getAttribute('aria-expanded')).toBe('false')

    // And the mirror: collapsing A must not collapse an expanded B.
    fireEvent.click(b2)
    fireEvent.click(pills()[0])
    const [a3, b3] = pills()
    expect(a3.getAttribute('aria-expanded')).toBe('false')
    expect(b3.getAttribute('aria-expanded')).toBe('true')
  })
})
