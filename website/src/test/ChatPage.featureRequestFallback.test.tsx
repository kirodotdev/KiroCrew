/**
 * Integration pin for #13342: the ChatPage hands the transcript the
 * feature-request form route ONLY for a slot the header's "Request a Feature"
 * action created, and only a `usage_limit` error row in it renders the link.
 *
 * Why at this layer: `ErrorCard` renders whatever route it is given, and the
 * shared row set forwards whatever option it is given -- both are pinned in
 * their own files. The composition (store marker -> renderer option -> card)
 * happens once, here, and a refactor that dropped the selector would leave
 * every unit test green while the fallback silently stopped appearing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { ThemeProvider } from '../hooks/useTheme'
import { i18nT } from '../i18n/t'
import { FEATURE_REQUEST_FORM_URL } from '../prompts/featureRequest'
import type { RootState } from '../store'

interface VirtuosoMockProps {
  data?: unknown[]
  itemContent: (index: number, item: unknown) => ReactNode
}
vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: VirtuosoMockProps) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))

// ChatPage refetches the active slot on mount and the response REPLACES
// `chat.messages`; the holder keeps the fetch and the preloaded store in step.
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
    continueSlot: vi.fn().mockResolvedValue({ ok: true }),
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

type Msg = { role: string; content: string; cls?: string; kind?: string; meta?: Record<string, unknown> }

// The row the runner appends for a capped account: the provider's own sentence
// (which limit, request id) plus the structural kind `_terminal_error_meta`
// stamps from the raw frame -- never from this prose.
const LIMIT_PROSE = '❌ The monthly usage limit has been reached. Retrying will not help until the limit resets. Check your plan\'s usage allowance, or switch to a model or account tier with remaining capacity. (request_id: be06fbe8-3341-4c00-9cac-ff34774e1ec4)'
const requestRow: Msg = { role: 'user', content: 'I’d like to request a feature!', cls: '' }
const limitRow: Msg = { role: 'error', content: LIMIT_PROSE, cls: 'msg msg-err', meta: { kind: 'usage_limit' } }

function makeStore(messages: Msg[], featureRequestSlots: string[]) {
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
        activeSlot: 'slot-a', messages, featureRequestSlots,
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

async function renderWith(messages: Msg[], featureRequestSlots: string[]) {
  detail.messages = messages
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={makeStore(messages, featureRequestSlots)}>
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
})

describe('ChatPage — a feature request refused for a usage limit offers the issue form (#13342)', { timeout: 15_000 }, () => {
  it('renders the form link with its explanation on the usage-limit row of the pill\'s slot, and no Resume', async () => {
    await renderWith([requestRow, limitRow], ['slot-a'])

    const card = screen.getByTestId('error-card')
    expect(card).toHaveAttribute('data-usage-limit-fallback', 'true')
    // The provider's own sentence is kept -- it names WHICH limit and carries the request id.
    expect(card).toHaveTextContent('The monthly usage limit has been reached.')
    expect(card).toHaveTextContent(i18nT('pages.chat.errorCard.feature_request_form_hint'))
    const link = screen.getByTestId('error-card-feature-request-form')
    expect(link).toHaveAttribute('href', FEATURE_REQUEST_FORM_URL)
    expect(link).toHaveAttribute('target', '_blank')
    // Resume would replay the very rejection this row reports.
    expect(screen.queryByTestId('error-card-continue')).toBeNull()
  })

  it('renders today\'s row for the same error in a slot the pill did not create', async () => {
    await renderWith([{ role: 'user', content: 'refactor the parser', cls: '' }, limitRow], [])

    expect(screen.queryByTestId('error-card-feature-request-form')).toBeNull()
    expect(screen.getByTestId('error-card')).not.toHaveAttribute('data-usage-limit-fallback')
    // Unchanged: the newest error row of an interrupted turn still offers Resume.
    expect(screen.getByTestId('error-card-continue')).toBeInTheDocument()
  })

  it('leaves the #4198 refused-send row alone even in the pill\'s slot', async () => {
    // No structural kind: the send never went out, so this is not a capacity
    // problem and the retry affordance (the pill itself) is the right one.
    const refused: Msg = { role: 'error', content: i18nT('pages.chatPage.send_failed_with_error', { error: 'slot agent mismatch' }), cls: '' }
    await renderWith([requestRow, refused], ['slot-a'])

    expect(screen.queryByTestId('error-card-feature-request-form')).toBeNull()
    expect(screen.getByTestId('error-card')).toHaveTextContent('slot agent mismatch')
  })
})
