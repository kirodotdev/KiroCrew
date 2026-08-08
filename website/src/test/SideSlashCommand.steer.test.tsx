/**
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
import { syncSlotRunningFromServer } from '../store/chatSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatSlot } from '../types'
import type { RootState } from '../store'

const { mockSideOpen, mockSideTurn, mockSteerChat, mockSendChat } = vi.hoisted(() => ({
  mockSideOpen: vi.fn().mockResolvedValue({ ok: true, open: true, messages: 0, last_run_id: '', created_at: '' }),
  mockSideTurn: vi.fn().mockResolvedValue({ ok: true, run_id: 'r1', messages: 1 }),
  mockSteerChat: vi.fn().mockResolvedValue({ ok: true }),
  mockSendChat: vi.fn().mockResolvedValue({ ok: true }),
}))

vi.mock('../api/client', () => ({
  api: new Proxy(
    { sideOpen: mockSideOpen, sideTurn: mockSideTurn, steerChat: mockSteerChat, sendChat: mockSendChat },
    {
      get: (t, prop) => {
        if (prop in t) return (t as Record<string, unknown>)[prop as string]
        if (prop === 'chatSlotDetail') return vi.fn().mockResolvedValue({ messages: [], has_more: false })
        // List-shaped endpoints: components .map() over these, so `{}` crashes
        // the render tree. slashCommands mirrors the backend list — an empty
        // array would leave the slash menu with zero rows, making Enter a
        // no-op in a way production never is.
        if (prop === 'slashCommands')
          return vi.fn().mockResolvedValue([{ name: '/side' }, { name: '/clear' }])
        if (prop === 'models' || prop === 'workspaces' || prop === 'notifications')
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

const SLOT = 'chat-1'

const runningSlot = (key: string): ChatSlot => ({
  key, title: key, messages: 1, running: true, mode: '', created: '', last_ts: '',
  pending_approval: false, waiting_for_input: false, last_activity_ts: undefined,
})

function renderRunningChatPage() {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: true, slots: [runningSlot(SLOT)], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: SLOT,
      messages: [{ role: 'user', content: 'long running task', cls: 'msg msg-u' }],
      slotRunning: true, slotStopping: false, slotState: 'running',
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
  // Slot hydration on mount runs clearSlotState, which wipes slotRunning; in
  // production recurring WS slot updates re-sync it. Hydration is async here
  // (mocked chatSlotDetail), so tests re-dispatch the sync until the composer
  // reflects the running state — see armRunning().
  return store
}

/** Re-sync running=true until the composer shows the steer split button.
 *  The button needs pending text, so call AFTER typing into the input. */
async function armRunning(store: ReturnType<typeof createTestStore>) {
  await waitFor(() => {
    act(() => {
      store.dispatch(syncSlotRunningFromServer({ slot: SLOT, running: true, stopping: false }))
    })
    expect(screen.getByTestId('busy-send-button')).toHaveAttribute('aria-label', 'Steer')
  })
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
})

describe('/side while a turn is running', () => {
  it('Enter routes to steer while running (precondition for the bypass)', async () => {
    const store = renderRunningChatPage()
    const input = await screen.findByLabelText('Message input')
    fireEvent.change(input, { target: { value: 'plain steer text' } })
    await armRunning(store)
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(mockSteerChat).toHaveBeenCalledWith('plain steer text', SLOT))
    expect(mockSendChat).not.toHaveBeenCalled()
  })

  it('/side opens the side chat instead of being steered into the turn', async () => {
    const store = renderRunningChatPage()
    const input = await screen.findByLabelText('Message input')
    fireEvent.change(input, { target: { value: '/side' } })
    await armRunning(store)
    // Bare "/side" keeps the slash menu open, so the first Enter is the menu
    // selection (autocompletes to "/side ") and the second Enter fires the
    // composer — same two-Enter sequence a real user produces.
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect((input as HTMLTextAreaElement).value).toBe('/side '))
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(mockSideOpen).toHaveBeenCalledWith(SLOT))
    expect(mockSteerChat).not.toHaveBeenCalled()
    expect(mockSendChat).not.toHaveBeenCalled()
    expect(store.getState().chat.activityTab).toBe('side')
  })

  it('/side <message> forwards the body to sideTurn, not to steer', async () => {
    const store = renderRunningChatPage()
    const input = await screen.findByLabelText('Message input')
    fireEvent.change(input, { target: { value: '/side what is this error about' } })
    await armRunning(store)
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(mockSideTurn).toHaveBeenCalledWith(SLOT, 'what is this error about'))
    expect(mockSteerChat).not.toHaveBeenCalled()
  })

  it('restores the composer text when the side turn is rejected', async () => {
    mockSideTurn.mockRejectedValueOnce(new Error('409: side turn already in flight'))
    const store = renderRunningChatPage()
    const input = await screen.findByLabelText('Message input')
    fireEvent.change(input, { target: { value: '/side my precious question' } })
    await armRunning(store)
    fireEvent.keyDown(input, { key: 'Enter' })
    // Cleared optimistically, then restored once the rejection lands.
    await waitFor(() => expect((input as HTMLTextAreaElement).value).toBe('/side my precious question'))
    expect(mockSteerChat).not.toHaveBeenCalled()
  })

  it('merges the rejected question below text typed while the rejection was in flight', async () => {
    let rejectTurn: (e: Error) => void = () => {}
    mockSideTurn.mockImplementationOnce(
      () => new Promise((_, rej) => { rejectTurn = rej }),
    )
    const store = renderRunningChatPage()
    const input = await screen.findByLabelText('Message input')
    fireEvent.change(input, { target: { value: '/side my question' } })
    await armRunning(store)
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(mockSideTurn).toHaveBeenCalled())
    // User starts typing something new before the 409 lands.
    fireEvent.change(input, { target: { value: 'fresh thought' } })
    act(() => rejectTurn(new Error('409: side turn already in flight')))
    // mergeIntoDraft contract: new typing survives on top, the recovered
    // question appends after a paragraph break — nothing is lost.
    await waitFor(() =>
      expect((input as HTMLTextAreaElement).value).toBe('fresh thought\n\n/side my question'),
    )
  })
})
