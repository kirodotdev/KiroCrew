/**
 * /rewind's awaited fork can span a slot switch (#8067, GPT review on #17774).
 * Harness copied from SideSlashCommand.steer.test.tsx, which renders the REAL
 * composer inside ChatPage.
 *
 * Original header kept for the harness notes:
 * Regression test for /side typed while a turn is running.
 *
 * Slash-command interception lives in send(), but while a turn is running the
 * composer's default Enter action is steer() — which pipes the raw composer
 * text into the running turn without ever consulting interceptSlashCommand.
 * Net effect: `/side` (and `/onboarding`) are steered into the agent as
 * literal text instead of opening the side chat.
 *
 * These tests render the REAL ChatInput inside ChatPage (unlike the other
 * ChatPage suites, which mock it) because the bug is the routing decision
 * between onSend and onSteer — mocking the composer would hide it.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { switchSlot } from '../store/chatSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatSlot } from '../types'
import type { RootState } from '../store'

const { mockSideOpen, mockSideTurn, mockSendChat, mockRewind, mockSlotDetail } = vi.hoisted(() => ({
  mockRewind: vi.fn(),
  mockSlotDetail: vi.fn(),
  mockSideOpen: vi.fn().mockResolvedValue({ ok: true, open: true, messages: 0, last_run_id: '', created_at: '' }),
  mockSideTurn: vi.fn().mockResolvedValue({ ok: true, run_id: 'r1', messages: 1 }),
  mockSendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true, steered: true }) }),
}))

vi.mock('../api/client', () => ({
  api: new Proxy(
    { sideOpen: mockSideOpen, sideTurn: mockSideTurn, sendChat: mockSendChat, forkChatSlot: mockRewind },
    {
      get: (t, prop) => {
        if (prop in t) return (t as Record<string, unknown>)[prop as string]
        if (prop === 'chatSlotDetail') return mockSlotDetail
        // List-shaped endpoints: components .map() over these, so `{}` crashes
        // the render tree. slashCommands mirrors the backend list — an empty
        // array would leave the slash menu with zero rows, making Enter a
        // no-op in a way production never is.
        if (prop === 'slashCommands')
          return vi.fn().mockResolvedValue([{ name: '/side' }, { name: '/clear' }])
        if (prop === 'models' || prop === 'workspaces' || prop === 'notifications' || prop === 'pendingQuestions' || prop === 'approvals')
          return vi.fn().mockResolvedValue([])
        return vi.fn().mockResolvedValue({})
      },
    },
  ),
  SEARCH_MIN_CHARS: 2,
}))

// Heavy children that are irrelevant to composer routing — same set the other
// ChatPage suites stub, EXCEPT components/ChatInput which stays real.
vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({ default: () => null }))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
vi.mock('../components/OverlayDrawer', () => ({ default: () => null }))
vi.mock('../components/AgentDropdownList', () => ({ default: () => null }))
vi.mock('../components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/SegmentedControl', () => ({ default: () => null }))
vi.mock('../components/PendingQuestionCard', () => ({ default: () => null }))
vi.mock('../pages/chat/CollapsibleToolGroup', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/SidePanel', () => ({
  default: () => null,
  CHAT_PANE_MIN_W: 480,
  sidePanelFillWidth: () => 480,
}))
vi.mock('../pages/chat', () => ({
  ChatFooter: () => null,
  AssistantMessage: () => null,
  UserMessage: () => null,
  PinnedPrompt: () => null,
  McpInfoButton: () => null,
}))
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ contentWidth: 'compact' }),
  CONTENT_WIDTH: { compact: { messages: '900px', input: '916px' }, comfortable: { messages: '84%', input: '85%' }, full: { messages: '92%', input: '93%' } },
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch

import ChatPage from '../pages/ChatPage'
import { loadDrafts } from '../utils/chatDrafts'

const idleSlot = (key: string): ChatSlot => ({
  key, title: key, messages: 2, running: false, mode: '', created: '', last_ts: '',
  pending_approval: false, waiting_for_input: false, last_activity_ts: undefined,
})

function renderTwoChats() {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: true, slots: [idleSlot('chat-a'), idleSlot('chat-b')], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: 'chat-a',
      messages: [{ role: 'user', content: 'q1', cls: 'msg msg-u' }, { role: 'assistant', content: 'a1', cls: 'msg msg-a' }],
      slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: true, activityTab: 'tools', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat']}>
            <ChatPage />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return store
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  mockSlotDetail.mockReset()
  mockSlotDetail.mockResolvedValue({ messages: [], has_more: false })
})

describe('/rewind across a slot switch', () => {
  it('leaves the other chat\'s draft and selection alone when the fork lands late', async () => {
    let resolveFork!: (v: unknown) => void
    mockRewind.mockReturnValue(new Promise(r => { resolveFork = r }))
    const store = renderTwoChats()
    const input = await screen.findByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(input, { target: { value: '/rewind 1' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(mockRewind).toHaveBeenCalledWith('chat-a', undefined, undefined, undefined, undefined, undefined, 1))
    // The person moves to another chat and starts typing while the fork runs.
    await act(async () => { await store.dispatch(switchSlot('chat-b')) })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-b'))
    fireEvent.change(input, { target: { value: 'draft in b' } })
    await act(async () => { resolveFork({ ok: true, key: 'fork-a', title: 'fork', messages: 0 }) })
    await act(async () => { await Promise.resolve() })
    expect(input.value).toBe('draft in b')
    expect(store.getState().chat.activeSlot).toBe('chat-b')
  })

  it('keeps text typed in the same chat while the fork ran, and still follows the fork', async () => {
    let resolveFork!: (v: unknown) => void
    mockRewind.mockReturnValue(new Promise(r => { resolveFork = r }))
    const store = renderTwoChats()
    const input = await screen.findByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(input, { target: { value: '/rewind 1' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(mockRewind).toHaveBeenCalled())
    // A new message started in the same chat before the fork came back.
    fireEvent.change(input, { target: { value: 'next thought' } })
    await act(async () => { resolveFork({ ok: true, key: 'fork-a', title: 'fork', messages: 0 }) })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('fork-a'))
    // The switch parked it as chat-a's draft instead of erasing it.
    await waitFor(() => expect(loadDrafts()['chat-a']).toBe('next thought'))
  })

  // The fork exists but its transcript fails to load: the pane must say so
  // instead of opening blank (errors-use-error-notice).
  it('announces a failed load of the fork instead of opening a blank pane', async () => {
    mockRewind.mockResolvedValue({ ok: true, key: 'fork-a', title: 'fork', messages: 0 })
    mockSlotDetail.mockImplementation((key: string) => key === 'fork-a'
      ? Promise.reject(Object.assign(new Error('boom'), { status: 500 }))
      : Promise.resolve({ messages: [], has_more: false }))
    const store = renderTwoChats()
    const input = await screen.findByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(input, { target: { value: '/rewind 1' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(store.getState().chat.switchSlotGone).toMatchObject({ kind: 'failed' }))
  })
})

