/**
 * Each silenced in-place notice announces through the floating toast instead, RENDERED.
 *
 * The in-place copies pass `announce={false}`, so they carry no live region and the toast is the
 * only surface that announces. That is safe only while the toast is actually mounted beside them,
 * which a scan of source text cannot show: it reads the call, not the output. These tests mount
 * the real pair for each silenced call site, drive the real failure through the real API layer,
 * and count the live regions the DOM ends up with. One is correct; zero means the failure is
 * announced nowhere, and two means it is read out twice.
 *
 * The toast is mounted from store state here exactly as `App.tsx` mounts it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useRef, type ReactNode } from 'react'
import { render, screen, act, fireEvent, waitFor } from '@testing-library/react'
import { Provider, useSelector } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider, useQueryClient } from '@tanstack/react-query'
import type { RootState } from '../store'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setAgentSwitchNotice } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { initI18n } from '../i18n/all'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([
      { model_name: 'claude-opus-5', description: 'Claude Opus 5' },
      { model_name: 'claude-sonnet-5', description: 'Claude Sonnet 5' },
    ]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({ ok: true, model: 'claude-sonnet-5' }),
    chatSlotAgent: vi.fn().mockResolvedValue({ ok: true, agent: 'writer' }),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    chatSlotAutocompact: vi.fn().mockResolvedValue({ pct: 70, global_pct: 70, agent_pct: null, agent: null, min: 10, max: 95 }),
    setChatSlotAutocompact: vi.fn().mockResolvedValue({ pct: 80, global_pct: 70, agent_pct: null, agent: null }),
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
import AgentSwitchNoticeBanner from '../components/AgentSwitchNoticeBanner'
import { ContextUsageControl } from '../components/chat-input/ContextShelf'
import { useAutoCompactThreshold } from '../components/chat-input/autoCompact'
import { useAppDispatch } from '../store'
import { api } from '../api/client'

initI18n('en')

/** The floating toast, mounted from store state exactly as the App shell mounts it. */
function Toast() {
  const notice = useSelector((s: RootState) => s.chat.agentSwitchNotice)
  if (!notice) return null
  return <AgentSwitchNoticeBanner notice={notice} />
}

function makeStore(slotKey: string) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slotKey, messages: 0, running: false, mode: '', agent: 'default', model: 'claude-opus-5', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

function mount(slotKey: string, body: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore(slotKey)
  return Object.assign(render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            {body}
            <Toast />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  ), { store })
}

/** Every live region in the rendered tree, toast and in-place copies alike. */
function liveRegions(): Element[] {
  return Array.from(document.querySelectorAll('[role="alert"],[role="status"],[aria-live]'))
}

/**
 * The shared assertion for one silenced call site: the pair is mounted together, both carry the
 * same text, and the DOM announces it exactly once -- from the toast, not the silenced copy.
 *
 * Scoped to the live regions carrying THIS failure's text, because a whole pane mounts unrelated
 * ones (an empty status for the stream, a polite region for the composer). The count still fails
 * in both directions that matter: nothing carrying the message means it is announced nowhere, and
 * two means a screen reader reads it out twice, which is the defect the silencing removed.
 */
function expectAnnouncedOnceByTheToast(inPlace: HTMLElement, message: string) {
  const toast = screen.getByTestId('agent-switch-notice')
  expect(inPlace.textContent).toContain(message)
  expect(toast.textContent).toContain(message)

  const announcing = liveRegions().filter(r => r.textContent?.includes(message))
  expect(
    announcing.map(r => `${r.getAttribute('role') ?? r.getAttribute('aria-live')}: ${r.textContent?.trim()}`),
    'the silenced in-place copy and the floating toast must leave exactly one live region carrying this failure: zero announces it nowhere, two reads it out twice',
  ).toHaveLength(1)
  expect(toast.contains(announcing[0]), 'the surviving live region must be the toast, not the copy that passed announce={false}').toBe(true)
  expect(inPlace.contains(announcing[0])).toBe(false)
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('ChatPane: the in-pane switch error is silenced and the toast announces for it', () => {
  it('a refused model switch mounts both surfaces and announces exactly once', async () => {
    const message = 'the model switch did not persist'
    vi.mocked(api.chatSlotModel).mockRejectedValue(new Error(message))
    mount('pane-1', <ChatPane slotKey="pane-1" />)

    const chip = await waitFor(() => screen.getByTitle(/^Model: claude-opus-5(?: ·|$)/))
    await act(async () => { fireEvent.click(chip) })
    const option = await waitFor(() => screen.getByRole('option', { name: /claude-sonnet-5/ }))
    await act(async () => { fireEvent.click(option) })

    const inPlace = await waitFor(() => screen.getByTestId('chat-pane-switch-error'))
    expectAnnouncedOnceByTheToast(inPlace, message)
  })
})

/** The popover's slider, driven through the real hook that owns the write. */
function AutoCompactHost() {
  const dispatch = useAppDispatch()
  const queryClient = useQueryClient()
  const ctxWrapRef = useRef<HTMLDivElement>(null) as React.RefObject<HTMLDivElement>
  const autoCompactThreshold = useAutoCompactThreshold({
    activeSlot: 'slot-a', ctxPopoverOpen: true, queryClient, dispatch,
  })
  return (
    <>
      <ContextUsageControl
        contextPct={42}
        shelfCompact={false}
        ctxPopoverOpen
        setCtxPopoverOpen={() => {}}
        ctxWrapRef={ctxWrapRef}
        autoCompactThreshold={autoCompactThreshold}
      />
      <button data-testid="drag" onClick={() => autoCompactThreshold.pushAutoCompact(80)}>drag</button>
    </>
  )
}

describe('ContextShelf: the in-popover auto-compact error is silenced and the toast announces for it', () => {
  it('a refused threshold write mounts both surfaces and announces exactly once', async () => {
    const message = 'the threshold did not persist'
    vi.mocked(api.setChatSlotAutocompact).mockRejectedValue(new Error(message))
    mount('slot-a', <AutoCompactHost />)

    await waitFor(() => expect(api.chatSlotAutocompact).toHaveBeenCalled())
    await act(async () => { fireEvent.click(screen.getByTestId('drag')) })

    const inPlace = await waitFor(
      () => screen.getByTestId('auto-compact-write-error'),
      { timeout: 4000 },
    )
    expectAnnouncedOnceByTheToast(inPlace, message)
  })
})

describe('ContextShelf: a new slider drag clears only the toast this slider raised', () => {
  it('leaves an unrelated failure on the shared toast in place', async () => {
    vi.mocked(api.setChatSlotAutocompact).mockResolvedValue({ pct: 80, global_pct: 70, agent_pct: null, agent: null, min: 10, max: 95 })
    const { store } = mount('slot-a', <AutoCompactHost />)
    await waitFor(() => expect(api.chatSlotAutocompact).toHaveBeenCalled())

    act(() => { store.dispatch(setAgentSwitchNotice('the session folder is unavailable')) })
    await act(async () => { fireEvent.click(screen.getByTestId('drag')) })

    expect(store.getState().chat.agentSwitchNotice?.message).toBe('the session folder is unavailable')
  })

  it('clears its own stale failure even after the in-popover copy was dismissed', async () => {
    const message = 'the threshold did not persist'
    vi.mocked(api.setChatSlotAutocompact).mockRejectedValueOnce(new Error(message))
    const { store } = mount('slot-a', <AutoCompactHost />)
    await waitFor(() => expect(api.chatSlotAutocompact).toHaveBeenCalled())
    await act(async () => { fireEvent.click(screen.getByTestId('drag')) })
    await waitFor(() => expect(store.getState().chat.agentSwitchNotice?.message).toBe(message), { timeout: 4000 })

    vi.mocked(api.setChatSlotAutocompact).mockResolvedValue({ pct: 80, global_pct: 70, agent_pct: null, agent: null, min: 10, max: 95 })
    await act(async () => { fireEvent.click(screen.getByTestId('drag')) })

    expect(store.getState().chat.agentSwitchNotice).toBeNull()
  })
})
