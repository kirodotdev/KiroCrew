import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { store as appStore } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { appendSlotMessage, selectSlotMessages, setActiveSlot } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { __resetPaneDraftsForTests } from '../utils/chatPaneDrafts'

/* A message sent while a crewmate works lands in its chat AT ONCE.
 *
 * A crewmate's DM is a background slot: this tab's stream state for it turns
 * busy only on a chunk or tool frame. While the crewmate's turn is thinking
 * (or was already running when the DM opened) the stream state still reads
 * idle, though the slot's server flag says the turn is running and the
 * working indicator shows. With sub-agents in flight the composer is busy, so
 * its send is a steer — and the steer path read only the stream state, took
 * the "no turn" branch, and sent without a bubble: the message appeared only
 * when the server echoed it back.
 *
 * Mutation check: drop `&& !paneSlot?.running` from doSteer's guard and the
 * first test goes RED (no bubble, `drawn` 0). */

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: true, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true, steered: true }) }),
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
  ApiError: class ApiError extends Error {},
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

const SLOT = 'member-radar'

/** The server says the turn runs and sub-agents are out; no live frame has
 *  reached this tab yet, so the slot's stream state is still idle. */
function renderCrewmate(serverRunning: boolean) {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: 0, running: serverRunning, subagents_running: true, mode: 'member', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  // The Members page never makes the DM the active chat slot.
  store.dispatch(setActiveSlot('front'))
  vi.spyOn(appStore, 'getState').mockImplementation(store.getState)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={SLOT} crewmate={{ name: 'Radar' }} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
  return store
}

async function send(text: string) {
  const box = (await screen.findAllByRole('textbox'))[0]
  fireEvent.change(box, { target: { value: text } })
  fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
  await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
}

const userRows = (store: ReturnType<typeof renderCrewmate>) =>
  selectSlotMessages(store.getState() as RootState, SLOT).filter(m => m.role === 'user')

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  __resetPaneDraftsForTests()
  vi.mocked(api.sendChat).mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true, steered: true }) } as unknown as Response)
})
afterEach(() => vi.restoreAllMocks())

describe("a send while a crewmate works", () => {
  it('shows in the chat at once, as a steer into the running turn, before any echo', async () => {
    const store = renderCrewmate(true)
    await send('also check the logs')
    expect(vi.mocked(api.sendChat).mock.calls[0][5]).toBe(true)
    const rows = userRows(store)
    expect(rows).toHaveLength(1)
    expect(rows[0].meta).toMatchObject({ steer: true, optimistic: true })
    expect(screen.getAllByText('also check the logs')).toHaveLength(1)
  })

  it("the server's steer echo settles that bubble in place, no second copy", async () => {
    const store = renderCrewmate(true)
    await send('also check the logs')
    const sendId = (vi.mocked(api.sendChat).mock.calls[0][4] as { sendId: string }).sendId
    act(() => {
      store.dispatch(appendSlotMessage({
        slot: SLOT,
        message: { role: 'user', content: 'also check the logs', cls: 'msg msg-u', ts: '2026-10-09T00:00:10Z', meta: { steer: true, steerState: 'consumed', sendId } },
      }))
    })
    const rows = userRows(store)
    expect(rows).toHaveLength(1)
    expect(rows[0].meta?.optimistic).toBeUndefined()
    expect(screen.getAllByText('also check the logs')).toHaveLength(1)
  })

  it('busy only with sub-agents (no turn on the server) still starts a fresh turn, no steer bubble', async () => {
    const store = renderCrewmate(false)
    await send('start again')
    // The send path's steer flag asks the server to skip the sub-agent hold.
    expect(vi.mocked(api.sendChat).mock.calls[0][5]).toBe(true)
    expect(userRows(store).some(m => m.meta?.steer)).toBe(false)
  })
})
