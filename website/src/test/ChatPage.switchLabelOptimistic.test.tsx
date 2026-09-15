/**
 * Regression test for #4523: the composer chips must reflect a model or
 * project switch as soon as the API call resolves, WITHOUT waiting for a
 * full slot-list round trip.
 *
 * The switch endpoints do trigger a server-side slots rebroadcast, but the
 * acting tab must not depend on that push channel to see its own pick: the
 * push is coalesced (a visible beat behind the click), and when the
 * websocket is down — the desktop app after sleep/wake — it never arrives at
 * all, leaving the chip on the pre-switch value until a page refresh. The
 * fix dispatches `updateSlot` on API success.
 *
 * This harness is that scenario by construction: `useWebSocket` is mocked
 * away, so NO slots frame can ever arrive, and the label can only update if
 * the switch callback itself writes the store. Pre-fix, every assertion
 * below fails with the chip stuck on its pre-switch value.
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
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([
      { model_name: 'auto', description: 'Models chosen by task' },
      { model_name: 'claude-opus-5', description: 'Claude Opus 5' },
      { model_name: 'claude-sonnet-5', description: 'Claude Sonnet 5' },
    ]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({ model: 'claude-opus-5' }),
    agentResolvedModel: vi.fn().mockResolvedValue({ model: 'claude-opus-5' }),
    // The switch endpoints answer with the STORED value (deprecated ids are
    // remapped, project paths realpath-normalized server-side).
    chatSlotModel: vi.fn().mockResolvedValue({ ok: true, model: 'claude-sonnet-5' }),
    chatSlotProject: vi.fn().mockResolvedValue({ ok: true, project: '/home/user/proj-x' }),
    // The agent switch also names the re-resolved workspace binding.
    chatSlotAgent: vi.fn().mockResolvedValue({ ok: true, agent: 'researcher', workspace: 'research-ws' }),
    recentProjects: vi.fn().mockResolvedValue({ dirs: ['/home/user/proj-x'] }),
    browseDirs: vi.fn().mockResolvedValue({ path: '/home/user', parent: '/home', dirs: [] }),
    projectGit: vi.fn().mockRejectedValue(new Error('not a repo')),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'kirocrew' }, { name: 'researcher' }], defaultAgent: 'kirocrew' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
// No websocket: no `slots` frame can arrive, so the chips can only move if
// the switch callbacks write the store themselves — the property under test.
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'
import { api } from '../api/client'

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        slots: [{ key: 'slot-a', messages: 0, running: false, mode: '', agent: 'kirocrew', model: 'claude-opus-5', project: '/home/user/old-proj', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages: [],
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

async function renderChat() {
  const store = makeStore()
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

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  vi.clearAllMocks()
})

describe('ChatPage — switch labels update without a slot-list round trip (#4523)', { timeout: 15_000 }, () => {
  it('model chip shows the new model as soon as the switch call resolves', async () => {
    const store = await renderChat()
    expect(screen.getByTitle('Model: claude-opus-5')).toBeTruthy()
    const slotsFetchesBeforePick = vi.mocked(api.chatSlots).mock.calls.length

    const chip = screen.getByTitle('Model: claude-opus-5')
    await act(async () => { fireEvent.click(chip) })
    const option = await waitFor(() => screen.getByRole('option', { name: /claude-sonnet-5/ }))
    await act(async () => { fireEvent.click(option) })

    // Label reflects the pick…
    expect(await waitFor(() => screen.getByTitle('Model: claude-sonnet-5'))).toBeTruthy()
    // …because the store was written on API success…
    expect(store.getState().dashboard.slots.find(s => s.key === 'slot-a')?.model).toBe('claude-sonnet-5')
    // …not because anything re-fetched the slot list (and no websocket exists
    // in this harness to push one).
    expect(vi.mocked(api.chatSlots).mock.calls.length).toBe(slotsFetchesBeforePick)
  })

  it('stores the server-normalized model, not the raw request', async () => {
    // A deprecated id is remapped server-side; the chip must show the stored
    // value the response names, not the spelling the picker sent. The
    // discriminator: the pick below sends 'auto' while the mocked response
    // (factory default) answers 'claude-sonnet-5' — an implementation that
    // dispatches the REQUEST value writes 'auto' and fails the assertion.
    const store = await renderChat()
    const chip = screen.getByTitle('Model: claude-opus-5')
    await act(async () => { fireEvent.click(chip) })
    // Pick the auto row; the (mocked) server answers with its stored value.
    const option = await waitFor(() => screen.getByRole('option', { name: /auto/ }))
    await act(async () => { fireEvent.click(option) })
    await waitFor(() => expect(store.getState().dashboard.slots.find(s => s.key === 'slot-a')?.model).toBe('claude-sonnet-5'))
  })

  it('does not relabel the chip when the switch call fails', async () => {
    vi.mocked(api.chatSlotModel).mockRejectedValueOnce(new Error('boom'))
    const store = await renderChat()
    const chip = screen.getByTitle('Model: claude-opus-5')
    await act(async () => { fireEvent.click(chip) })
    const option = await waitFor(() => screen.getByRole('option', { name: /claude-sonnet-5/ }))
    await act(async () => { fireEvent.click(option) })

    // The pick failed: the store keeps the pre-switch model and the chip
    // keeps naming it.
    expect(store.getState().dashboard.slots.find(s => s.key === 'slot-a')?.model).toBe('claude-opus-5')
    expect(screen.getByTitle('Model: claude-opus-5')).toBeTruthy()
  })

  it('serializes two rapid picks and ends on the LATEST one', async () => {
    // The dropdown deliberately stays open after a pick, so two quick picks
    // race. The registry serializes the wire calls (the second must not START
    // until the first settles — send order is server processing order), and
    // the store must end on the latest pick.
    let releaseFirst: (v: { ok: boolean; model: string }) => void = () => {}
    vi.mocked(api.chatSlotModel)
      .mockImplementationOnce(() => new Promise(res => { releaseFirst = res }))
      .mockImplementationOnce(async () => ({ ok: true, model: 'auto' }))
    const store = await renderChat()

    const chip = screen.getByTitle('Model: claude-opus-5')
    await act(async () => { fireEvent.click(chip) })
    const first = await waitFor(() => screen.getByRole('option', { name: /claude-sonnet-5/ }))
    await act(async () => { fireEvent.click(first) })
    const second = await waitFor(() => screen.getByRole('option', { name: /auto/ }))
    await act(async () => { fireEvent.click(second) })

    // Serialization: the second pick's wire call is queued, not in flight.
    expect(api.chatSlotModel).toHaveBeenCalledTimes(1)

    await act(async () => { releaseFirst({ ok: true, model: 'claude-sonnet-5' }) })

    // The queued second call fires after the first settles and, being the
    // newest ticket, its value is what the store must end on.
    await waitFor(() => expect(api.chatSlotModel).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(store.getState().dashboard.slots.find(s => s.key === 'slot-a')?.model).toBe('auto'))
  })

  it('does not relabel the project chip when the project switch fails', async () => {
    vi.mocked(api.chatSlotProject).mockRejectedValueOnce(new Error('boom'))
    const store = await renderChat()
    const chip = screen.getByTitle('Project: /home/user/old-proj')
    await act(async () => { fireEvent.click(chip) })
    const row = await waitFor(() => screen.getByRole('option', { name: /proj-x/ }))
    await act(async () => { fireEvent.mouseDown(row) })

    await waitFor(() => expect(api.chatSlotProject).toHaveBeenCalled())
    expect(store.getState().dashboard.slots.find(s => s.key === 'slot-a')?.project).toBe('/home/user/old-proj')
    expect(screen.getByTitle('Project: /home/user/old-proj')).toBeTruthy()
  })

  it('project chip shows the server-normalized path as soon as the pick resolves', async () => {
    const store = await renderChat()
    const chip = screen.getByTitle('Project: /home/user/old-proj')
    await act(async () => { fireEvent.click(chip) })
    // ProjectPicker recent tab: rows are role=option buttons acting on mousedown.
    const row = await waitFor(() => screen.getByRole('option', { name: /proj-x/ }))
    await act(async () => { fireEvent.mouseDown(row) })

    await waitFor(() => expect(api.chatSlotProject).toHaveBeenCalledWith('slot-a', '/home/user/proj-x'))
    // Store carries the response's realpath-normalized spelling; the chip
    // follows it with no slot-list refetch involved.
    await waitFor(() => expect(store.getState().dashboard.slots.find(s => s.key === 'slot-a')?.project).toBe('/home/user/proj-x'))
    expect(await waitFor(() => screen.getByTitle('Project: /home/user/proj-x'))).toBeTruthy()
  })

  it('agent pick writes agent AND workspace to the store without a slot-list round trip (#5120)', async () => {
    const store = await renderChat()
    const slotsFetchesBeforePick = vi.mocked(api.chatSlots).mock.calls.length
    const chip = screen.getByTitle('Agent: kirocrew')
    await act(async () => { fireEvent.click(chip) })
    const option = await waitFor(() => screen.getByRole('option', { name: /researcher/ }))
    await act(async () => { fireEvent.click(option) })

    await waitFor(() => expect(api.chatSlotAgent).toHaveBeenCalledWith('slot-a', 'researcher'))
    // The write mirrors exactly what the response names: the stored agent
    // plus the re-resolved workspace binding, as one pair.
    const slot = () => store.getState().dashboard.slots.find(s => s.key === 'slot-a')
    await waitFor(() => expect(slot()?.agent).toBe('researcher'))
    expect(slot()?.workspace).toBe('research-ws')
    // …and not because anything re-fetched the slot list (no websocket
    // exists in this harness to push one either).
    expect(vi.mocked(api.chatSlots).mock.calls.length).toBe(slotsFetchesBeforePick)
    expect(await waitFor(() => screen.getByTitle('Agent: researcher'))).toBeTruthy()
  })

  it('keeps the pre-switch agent when the agent switch fails (#5120)', async () => {
    vi.mocked(api.chatSlotAgent).mockRejectedValueOnce(new Error('boom'))
    const store = await renderChat()
    const chip = screen.getByTitle('Agent: kirocrew')
    await act(async () => { fireEvent.click(chip) })
    const option = await waitFor(() => screen.getByRole('option', { name: /researcher/ }))
    await act(async () => { fireEvent.click(option) })

    await waitFor(() => expect(api.chatSlotAgent).toHaveBeenCalled())
    expect(store.getState().dashboard.slots.find(s => s.key === 'slot-a')?.agent).toBe('kirocrew')
    expect(screen.getByTitle('Agent: kirocrew')).toBeTruthy()
  })
})
