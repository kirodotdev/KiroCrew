/**
 * The chat page's closed-session provider (#9915). The background lookup a
 * chip runs never raises a notice: the reader asked for nothing yet. The click
 * checks again and resumes a session that still exists. A session gone since is
 * said through the page's error notice; a failed check is answered `failed`,
 * which the chip shows inline at the spot clicked.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const CLOSED = 'chat-7-1784661951'
import { render, screen, fireEvent, act } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// Stub AssistantMessage: buttons that drive the page's ClosedSessionCtx.
vi.mock('../pages/chat', async () => {
  const React = await import('react')
  const { ClosedSessionCtx } = await import('../components/markdown/contexts')
  function Probe() {
    const ctx = React.useContext(ClosedSessionCtx)
    const [answer, setAnswer] = React.useState('none')
    return React.createElement('div', null,
      React.createElement('button', {
        'data-testid': 'lookup',
        onClick: () => { void ctx.lookup?.(CLOSED).then(r => setAnswer(r ? r.title : 'null'), () => setAnswer('rejected')) },
      }, 'lookup'),
      React.createElement('button', {
        'data-testid': 'open',
        onClick: () => { void ctx.open?.({ key: `dashboard_${CLOSED}`, title: 'Earlier work' }).then(setAnswer) },
      }, 'open'),
      React.createElement('span', { 'data-testid': 'answer' }, answer),
    )
  }
  return {
    ChatFooter: () => null,
    McpInfoButton: () => null,
    UserMessage: () => null,
    AssistantMessage: () => React.createElement(Probe),
  }
})

vi.mock('../components/MarkdownPanel', async () => {
  const React = await import('react')
  return { default: () => React.createElement('div', { 'data-testid': 'md-panel' }) }
})
vi.mock('../components/DiffPanel', async () => {
  const React = await import('react')
  return { default: () => React.createElement('div', { 'data-testid': 'diff-panel' }) }
})

vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../hooks/virtualizer/useVirtualChat', () => ({
  useVirtualChat: (opts: { items?: unknown[]; getKey?: (it: unknown, i: number) => string }) => {
    const items = opts.items ?? []
    return {
      virtualItems: items.map((data, index) => ({
        key: opts.getKey ? opts.getKey(data, index) : String(index),
        index,
        mounted: true,
        data,
      farmIsMeasured: () => true,
      farmRecord: () => true,
      })),
      isAtBottom: true,
      getFollow: () => true,
      scrollToBottom: vi.fn(),
      mountIndex: vi.fn(),
      measureRef: () => () => {},
      topSentinelRef: { current: null },
      bottomSentinelRef: { current: null },
      offsetBefore: 0,
      offsetAfter: 0,
    }
  },
}))
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
vi.mock('../components/ChatInput', () => ({ default: () => null }))
vi.mock('../components/WelcomeView', async () => {
  const React = await import('react')
  return { default: () => React.createElement('div', { 'data-testid': 'welcome' }) }
})
vi.mock('../components/MarkdownRenderer', () => ({ default: () => null }))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
vi.mock('../components/OverlayDrawer', () => ({ default: ({ children }: { children?: ReactNode }) => children }))
vi.mock('../components/AgentDropdownList', () => ({ default: () => null }))
vi.mock('../components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/SegmentedControl', () => ({ default: () => null }))
vi.mock('../pages/chat/CollapsibleToolGroup', () => ({ default: ({ children }: { children?: ReactNode }) => children }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ contentWidth: 'compact' }),
  CONTENT_WIDTH: { compact: { messages: '900px', input: '916px' }, comfortable: { messages: '84%', input: '85%' }, full: { messages: '92%', input: '93%' } },
}))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))

const apiMocks: Record<string, ReturnType<typeof vi.fn>> = {}
vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_t, prop: string) => {
      if (!(prop in apiMocks)) {
        apiMocks[prop] = vi.fn().mockResolvedValue(
          prop === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 }
            : prop === 'pendingQuestions' || prop === 'approvals' ? [] : {},
        )
      }
      return apiMocks[prop]
    },
  }),
  fileReadUrl: (p: string) => `/api/file?path=${encodeURIComponent(p)}`,
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({
  ok: true, status: 200,
  text: () => Promise.resolve('file content'),
  json: () => Promise.resolve({}),
}) as never

import ChatPage from '../pages/ChatPage'

const MSG = { role: 'assistant', content: 'painted transcript', ts: '2026-06-23T20:00:00Z' }

const SLOT_A = { key: 'chat-1', title: 'one', messages: 1, running: false, mode: '', created: '', last_ts: '' }
const SLOT_B = { key: 'chat-2', title: 'two', messages: 1, running: false, mode: '', created: '', last_ts: '' }

const renderChatPage = (connected: boolean, extraSlots: typeof SLOT_A[] = []) => {
  const allSlots = [SLOT_A, SLOT_B, ...extraSlots]
  apiMocks.chatSlots = vi.fn().mockResolvedValue(allSlots)
  apiMocks.chatSlotDetail = vi.fn().mockResolvedValue({ messages: [MSG], has_more: false, total: 1 })
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected,
      slots: allSlots, approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0,
      unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as never,
    chat: {
      activeSlot: 'chat-1',
      messages: [MSG], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
    } as never,
  })
  // Wrapped BEFORE render: `useDispatch` captures `store.dispatch` by reference
  // during render, so a wrapper installed afterwards is simply bypassed.
  const seen: string[] = []
  const real = store.dispatch
  store.dispatch = ((action: unknown) => {
    // A thunk dispatches its own `pending` through the middleware's internal
    // dispatch, so the thunk itself is the observable the guard actually gates.
    seen.push(typeof action === 'function' ? 'thunk' : ((action as { type?: string } | null)?.type ?? 'unknown'))
    return (real as (a: unknown) => unknown)(action)
  }) as typeof store.dispatch
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat/chat-1']}>
            <Routes>
              <Route path="/chat/:slug?" element={<ChatPage mode="" />} />
            </Routes>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { store, seen }
}

const seedMessage = (store: ReturnType<typeof createTestStore>) => {
  act(() => { store.dispatch({ type: 'chat/replaceMessages', payload: [MSG] }) })
}

describe('ChatPage closed-session provider', () => {
  beforeEach(() => {
    for (const k of Object.keys(apiMocks)) delete apiMocks[k]
  })

  it('answers a found session with its stem and title', async () => {
    const { store } = renderChatPage(true)
    seedMessage(store)
    apiMocks.sessionMeta = vi.fn().mockResolvedValue({ key: `dashboard_${CLOSED}`, title: 'Earlier work' })
    await act(async () => { fireEvent.click(await screen.findByTestId('lookup')) })
    await vi.waitFor(() => expect(screen.getByTestId('answer').textContent).toBe('Earlier work'))
    expect(apiMocks.sessionMeta).toHaveBeenCalledWith(CLOSED)
  })

  it('a missing session is a quiet null, with no error notice', async () => {
    const { store } = renderChatPage(true)
    seedMessage(store)
    apiMocks.sessionMeta = vi.fn().mockResolvedValue(null)
    await act(async () => { fireEvent.click(await screen.findByTestId('lookup')) })
    await vi.waitFor(() => expect(screen.getByTestId('answer').textContent).toBe('null'))
    expect(screen.queryByText('Could not open the target session.')).toBeNull()
  })

  it('a failed background lookup raises no notice', async () => {
    const { store } = renderChatPage(true)
    seedMessage(store)
    apiMocks.sessionMeta = vi.fn().mockRejectedValue(new Error('gateway unreachable'))
    await act(async () => { fireEvent.click(await screen.findByTestId('lookup')) })
    await vi.waitFor(() => expect(screen.getByTestId('answer').textContent).toBe('rejected'))
    expect(screen.queryByText('Could not open the target session.')).toBeNull()
  })

  it('a click whose check fails answers failed, raises no banner, and does not resume', async () => {
    // The chip shows the failure inline at the spot clicked (MarkdownRenderer.closedSessionChip.test.tsx).
    const { store } = renderChatPage(true)
    seedMessage(store)
    apiMocks.sessionMeta = vi.fn().mockRejectedValue(new Error('gateway unreachable'))
    apiMocks.resumeChatSlot = vi.fn().mockResolvedValue({ ok: false })
    await act(async () => { fireEvent.click(await screen.findByTestId('open')) })
    await vi.waitFor(() => expect(screen.getByTestId('answer').textContent).toBe('failed'))
    expect(screen.queryByText('Could not open the target session.')).toBeNull()
    expect(screen.queryByText(/gateway unreachable/)).toBeNull()
    expect(apiMocks.resumeChatSlot).not.toHaveBeenCalled()
  })

  it('a click on a session deleted since says it was deleted', async () => {
    const { store } = renderChatPage(true)
    seedMessage(store)
    apiMocks.sessionMeta = vi.fn().mockResolvedValue(null)
    apiMocks.resumeChatSlot = vi.fn().mockResolvedValue({ ok: false })
    await act(async () => { fireEvent.click(await screen.findByTestId('open')) })
    await vi.waitFor(() => expect(screen.getByTestId('answer').textContent).toBe('gone'))
    expect(await screen.findByText(/Earlier work.*was deleted and cannot be opened/)).toBeTruthy()
    expect(apiMocks.resumeChatSlot).not.toHaveBeenCalled()
  })

  it('a click on a session that still exists resumes it', async () => {
    const { store } = renderChatPage(true)
    seedMessage(store)
    apiMocks.sessionMeta = vi.fn().mockResolvedValue({ key: `dashboard_${CLOSED}`, title: 'Earlier work' })
    apiMocks.resumeChatSlot = vi.fn().mockResolvedValue({ ok: false })
    await act(async () => { fireEvent.click(await screen.findByTestId('open')) })
    await vi.waitFor(() => expect(screen.getByTestId('answer').textContent).toBe('opened'))
    expect(apiMocks.sessionMeta).toHaveBeenCalledWith(`dashboard_${CLOSED}`)
    expect(apiMocks.resumeChatSlot).toHaveBeenCalledWith(`dashboard_${CLOSED}`, 'Earlier work')
  })
})
