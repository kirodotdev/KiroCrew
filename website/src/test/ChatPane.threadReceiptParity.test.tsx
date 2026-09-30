/**
 * A message queued in a THREAD gets the same receipt as one queued in the parent
 * chat.
 *
 * The defect this pins: in the QA recording a message typed into a fresh thread
 * looked like it needed re-sending. The thread's seed had started a turn, so the
 * typed message went to the queue -- correct behaviour with no visible receipt to
 * say so. The seed turn is being removed, but the receipt is the part that has to
 * be true regardless: a thread is an ordinary session, so its queue is drawn by the
 * same `QueueStack` the main chat uses, and `threadSurface` must change nothing but
 * wording.
 *
 * Rendered through the REAL ChatPane on both surfaces rather than asserted against
 * the prop, because "same receipt" is a claim about what the person sees.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { act, render } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

const engine = {
  recording: false, transcribing: false, sessionOwner: null as string | null, streamEnabled: false,
  toggle: vi.fn(), start: vi.fn().mockResolvedValue(undefined), stop: vi.fn(), cancel: vi.fn(), prewarm: vi.fn(),
  error: null as string | null, level: 0, deviceLabel: '', deviceId: '', clearError: vi.fn(), partial: '',
  download: null, sampleRef: { current: {} }, switchDevice: vi.fn(), deviceSwitchIsLive: false,
}
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => engine, voiceInputSupported: true }))
vi.mock('../hooks/usePushToTalk', () => ({ usePushToTalk: () => undefined }))
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
    planAction: vi.fn().mockResolvedValue({ ok: true }),
    sttConfig: vi.fn().mockResolvedValue({ enabled: true, available: true, streaming: false, dictation_panel: true, provider: 'local' }),
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
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'

const SLOT = 'chat-9-receipt'
const QUEUED = 'wait, different question'

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: 1, running: true, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        ...chatReducer(undefined, { type: '@@INIT' }),
        // A message the slot accepted while its turn was running: the row the
        // receipt is drawn from, on a slot that is NOT the active one (which is
        // what a thread drawer always is).
        slotMessages: { [SLOT]: [{ role: 'queued', content: QUEUED, cls: '', meta: { queue_id: 'q-1' } }] },
        slotRun: { [SLOT]: { state: 'streaming' } },
      } as unknown as RootState['chat'],
    } as Partial<RootState>,
  })
}

async function renderPane(threadSurface?: 'open' | 'closed') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(['sttConfig'], { enabled: true, available: true, streaming: false, dictation_panel: true, provider: 'local' })
  let result!: ReturnType<typeof render>
  await act(async () => {
    result = render(
      <Provider store={makeStore()}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey={SLOT} threadSurface={threadSurface} />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    )
  })
  return result
}

beforeEach(() => { vi.clearAllMocks() })

describe('a queued message in a thread', () => {
  it('draws the same receipt as the parent chat', async () => {
    const plain = await renderPane(undefined)
    const plainCards = plain.container.querySelectorAll('.queue-card').length
    const plainText = plain.container.textContent ?? ''
    plain.unmount()

    const thread = await renderPane('open')
    const threadCards = thread.container.querySelectorAll('.queue-card').length
    const threadText = thread.container.textContent ?? ''

    // The receipt exists at all -- a queued message with nothing on screen is the
    // defect from the recording.
    expect(plainCards).toBeGreaterThan(0)
    // And it is the SAME receipt, not a thread-flavoured one.
    expect(threadCards).toBe(plainCards)
    expect(plainText).toContain(QUEUED)
    expect(threadText).toContain(QUEUED)
  })

  it('is drawn on an ENDED thread too, because ending releases the anchor and not the session', async () => {
    const closed = await renderPane('closed')
    expect(closed.container.querySelectorAll('.queue-card').length).toBeGreaterThan(0)
    expect(closed.container.textContent ?? '').toContain(QUEUED)
  })
})
