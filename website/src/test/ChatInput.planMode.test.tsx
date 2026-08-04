/**
 * Plan mode toggle — the composer's read-only switch.
 *
 * Plan mode differs from the neighbouring Browse toggle in one load-bearing
 * way: it is SERVER-owned. The backend gate keys on a persisted slot flag, so
 * the switch must render from `slot.plan_mode` and every flip must reach
 * `api.setSlotPlanMode`. A client-only copy (browse mode's shape) would show
 * "on" while the gate was off — the failure mode this file guards.
 *
 * The toggle lives in the ChatInput "+" drop-up, next to the browser switch.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import type { RootState } from '../store'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (i: number, d: unknown) => React.ReactNode }) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running: false, has_more: false, total: 1 }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    setSlotPlanMode: vi.fn().mockResolvedValue({ ok: true, plan_mode: true }),
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
import { api } from '../api/client'

const MSG = [{ role: 'assistant', content: 'hi', cls: '' }]

function makeStore(activeSlot: string, planModeBySlot: Record<string, boolean> = {}) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        slots: [
          { key: 'slot-a', messages: 1, running: false, stopping: false, stop_state: 'idle', mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined, plan_mode: planModeBySlot['slot-a'] ?? false },
          { key: 'slot-b', messages: 1, running: false, stopping: false, stop_state: 'idle', mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined, plan_mode: planModeBySlot['slot-b'] ?? false },
        ],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot, messages: MSG,
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

async function renderChat(activeSlot: string, planModeBySlot?: Record<string, boolean>) {
  const store = makeStore(activeSlot, planModeBySlot)
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
  return store
}

const LABEL = 'Have the agent plan before it changes anything'
const CHIP_TIP = 'Plan mode is on — click to turn it off without starting the work'
const openMenu = () => fireEvent.click(screen.getByTitle('Add files & options'))
const planToggle = () => screen.getByRole('switch', { name: LABEL })
const planChip = () => screen.queryByRole('button', { name: CHIP_TIP })
const isOn = () => planToggle().getAttribute('aria-checked') === 'true'

async function switchSlot(store: ReturnType<typeof makeStore>, slot: string) {
  await act(async () => { store.dispatch({ type: 'chat/setActiveSlot', payload: slot }) })
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  vi.mocked(api.setSlotPlanMode).mockClear()
  vi.mocked(api.setSlotPlanMode).mockResolvedValue({ ok: true, plan_mode: true })
})

describe('ChatInput — plan mode toggle', { timeout: 15_000 }, () => {
  it('renders off for a session with the flag unset', async () => {
    await renderChat('slot-a')
    await act(async () => { openMenu() })
    expect(isOn()).toBe(false)
  })

  it('renders on from the slot flag, not from local state', async () => {
    // Restart survival lives here: the server restored plan_mode, so the switch
    // must already read on without the user touching it.
    await renderChat('slot-a', { 'slot-a': true })
    await act(async () => { openMenu() })
    expect(isOn()).toBe(true)
  })

  it('persists a flip through the api', async () => {
    await renderChat('slot-a')
    await act(async () => { openMenu() })
    await act(async () => { fireEvent.click(planToggle()) })
    expect(api.setSlotPlanMode).toHaveBeenCalledWith('slot-a', true)
    expect(isOn()).toBe(true)
  })

  it('persists turning it back off', async () => {
    vi.mocked(api.setSlotPlanMode).mockResolvedValue({ ok: true, plan_mode: false })
    await renderChat('slot-a', { 'slot-a': true })
    await act(async () => { openMenu() })
    await act(async () => { fireEvent.click(planToggle()) })
    expect(api.setSlotPlanMode).toHaveBeenCalledWith('slot-a', false)
    expect(isOn()).toBe(false)
  })

  it('rolls back when the server refuses', async () => {
    // The endpoint answers 409 while a turn is running. Leaving the switch on
    // after a refusal would promise a gate that is not armed.
    vi.mocked(api.setSlotPlanMode).mockRejectedValue(new Error('409'))
    await renderChat('slot-a')
    await act(async () => { openMenu() })
    await act(async () => { fireEvent.click(planToggle()) })
    await waitFor(() => expect(isOn()).toBe(false))
  })

  it('honours the server echo over the optimistic flip', async () => {
    vi.mocked(api.setSlotPlanMode).mockResolvedValue({ ok: true, plan_mode: false })
    await renderChat('slot-a')
    await act(async () => { openMenu() })
    await act(async () => { fireEvent.click(planToggle()) })
    await waitFor(() => expect(isOn()).toBe(false))
  })

  it('is per-session and does not bleed across slots', async () => {
    const store = await renderChat('slot-a', { 'slot-a': true })
    await act(async () => { openMenu() })
    expect(isOn()).toBe(true)
    await switchSlot(store, 'slot-b')
    expect(isOn()).toBe(false)
  })
})

describe('ChatInput — plan mode chip', { timeout: 15_000 }, () => {
  it('is absent when plan mode is off', async () => {
    await renderChat('slot-a')
    expect(planChip()).toBeNull()
  })

  it('is a labelled chip in the composer footer when armed', async () => {
    // The usability review rated an unlabelled glyph + coloured border as not
    // legible: with the menu closed the words "plan mode" appeared nowhere.
    await renderChat('slot-a', { 'slot-a': true })
    const chip = planChip()
    expect(chip).not.toBeNull()
    expect(chip?.textContent).toContain('Plan mode')
  })

  it('turns plan mode off from where the user already is', async () => {
    // The review's other blocker: no exit from the armed state without
    // remembering an attachment menu.
    vi.mocked(api.setSlotPlanMode).mockResolvedValue({ ok: true, plan_mode: false })
    await renderChat('slot-a', { 'slot-a': true })
    await act(async () => { fireEvent.click(planChip() as HTMLElement) })
    expect(api.setSlotPlanMode).toHaveBeenCalledWith('slot-a', false)
    await waitFor(() => expect(planChip()).toBeNull())
  })
})
