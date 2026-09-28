/**
 * Pane-level pin for #13342: a split-view pane draws the SAME rows as the
 * single-chat surface, so a `usage_limit` error row in the slot the header's
 * "Request a Feature" action created must offer the issue form there too.
 *
 * Why a second pin beside ChatPage.featureRequestFallback: the shared row set
 * only forwards the route it is HANDED, and ChatPane builds its own registry
 * from the same factory. A pane that forgot the option would leave every
 * ChatPage test green while the pane drew the plain card -- with no Resume
 * (panes never pass one) and no way out.
 */
import { describe, it, expect, vi } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { i18nT } from '../i18n/t'
import { FEATURE_REQUEST_FORM_URL, FEATURE_REQUEST_ROW_META_KEY } from '../prompts/featureRequest'

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
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'

const SLOT = 'chat-1-feature'

type Msg = { role: string; content: string; ts: string; cls?: string; meta?: Record<string, unknown> }

// The row the runner appends for a capped account: the provider's own sentence
// plus the structural kind `_terminal_error_meta` stamps from the raw frame.
const LIMIT_PROSE = '❌ The monthly usage limit has been reached. Retrying will not help until the limit resets. (request_id: 3c1f0c1a-7d1e-4a1c-9d3a-1b2c3d4e5f60)'
// The row the pill's flow sent, with the flow's stamp in its `meta` -- the pane
// reads the rows alone, exactly as the main surface does.
const requestRow: Msg = { role: 'user', content: 'I’d like to request a feature!', ts: '2026-09-01T00:00:00Z', meta: { sendId: 's-fr-seed', mid: 'u-1', [FEATURE_REQUEST_ROW_META_KEY]: true } }
const limitRow: Msg = { role: 'error', content: LIMIT_PROSE, ts: '2026-09-01T00:00:01Z', cls: 'msg msg-err', meta: { kind: 'usage_limit' } }

function makeStore(messages: Msg[]) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: messages.length, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        ...chatReducer(undefined, { type: '@@INIT' }),
        activeSlot: SLOT,
        slotState: 'idle',
        messages,
      } as unknown as RootState['chat'],
    } as Partial<RootState>,
  })
}

function mount(messages: Msg[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={makeStore(messages)}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ChatPane slotKey={SLOT} />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

describe('ChatPane — a feature request refused for a usage limit offers the issue form (#13342)', () => {
  it('renders the form link with its explanation on the usage-limit row of the pill\'s turn', () => {
    mount([requestRow, limitRow])

    const card = screen.getByTestId('error-card')
    expect(card).toHaveAttribute('data-usage-limit-fallback', 'true')
    expect(card).toHaveTextContent('The monthly usage limit has been reached.')
    expect(card).toHaveTextContent(i18nT('pages.chat.errorCard.feature_request_form_hint'))
    const link = screen.getByTestId('error-card-feature-request-form')
    expect(link).toHaveAttribute('href', FEATURE_REQUEST_FORM_URL)
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('renders today\'s row for the same error under a user row the pill did not stamp', () => {
    mount([{ role: 'user', content: 'refactor the parser', ts: '2026-09-01T00:00:00Z', meta: { sendId: 's-typed' } }, limitRow])

    expect(screen.queryByTestId('error-card-feature-request-form')).toBeNull()
    expect(screen.getByTestId('error-card')).not.toHaveAttribute('data-usage-limit-fallback')
  })

  it('renders today\'s row for a later limit after the user typed on in the pill\'s slot', () => {
    // Same turn rule as ChatPage: the form belongs to the stamped turn's own
    // refusal, not to every limit the slot ever hits.
    mount([
      requestRow,
      { role: 'assistant', content: 'Filed as #13342.', ts: '2026-09-01T00:00:01Z' },
      { role: 'user', content: 'now refactor the parser', ts: '2026-09-01T00:00:02Z', meta: { sendId: 's-typed', mid: 'u-2' } },
      { ...limitRow, ts: '2026-09-01T00:00:03Z' },
    ])

    expect(screen.queryByTestId('error-card-feature-request-form')).toBeNull()
    expect(screen.getByTestId('error-card')).not.toHaveAttribute('data-usage-limit-fallback')
  })
})
