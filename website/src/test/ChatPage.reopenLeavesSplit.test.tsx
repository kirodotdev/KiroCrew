/**
 * The reopen-closed-session shortcut resumes through `resumeFromHistory` with
 * `leaveSplit`, so the reopened chat is what the page shows; a resume that does
 * not ask keeps Split View. Driven through a real `render(<ChatPage />)` with
 * the split grid stubbed to a prop recorder (split from
 * ChatPageMoreCoverage.test.tsx, whose setup this copies the minimum of).
 */
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { resumeFromHistory } from '../store/chatSlice'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'

let assistantMounted = false
vi.mock('../pages/chat', async () => {
  const React = await import('react')
  return {
    ChatFooter: () => null,
    McpInfoButton: () => null,
    PinnedPrompt: () => null,
    UserMessage: ({ content }: { content: string }) =>
      React.createElement('div', { 'data-testid': 'user-msg' }, content),
    AssistantMessage: (props: { content: string }) => {
      assistantMounted = true
      return React.createElement('div', { 'data-testid': 'assistant-msg' }, props.content)
    },
  }
})

vi.mock('../components/ChatInput', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../components/ChatInput')>()
  const React = await import('react')
  return { ...actual, default: () => React.createElement('textarea', { 'aria-label': 'Message input' }) }
})

interface GridProps { openSideChat?: (slot: string) => boolean | void | Promise<boolean | void> }
let gridProps: GridProps | null = null
vi.mock('../components/SessionGridView', async () => {
  const React = await import('react')
  return {
    default: (props: GridProps) => {
      gridProps = props
      return React.createElement('div', { 'data-testid': 'session-grid' })
    },
  }
})

vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content?: string }) => content ?? null,
}))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
vi.mock('../components/OverlayDrawer', () => ({ default: ({ children }: { children?: ReactNode }) => children }))
vi.mock('../components/AgentDropdownList', () => ({ default: () => null, DefaultAgentRow: () => null, ManageAgentsFooter: () => null }))
vi.mock('../components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/SegmentedControl', () => ({ default: () => null }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MemoryModeChip', () => ({ MemoryModeChip: () => null }))
vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ contentWidth: 'compact' }),
  CONTENT_WIDTH: {
    compact: { messages: '900px', input: '916px' },
    comfortable: { messages: '84%', input: '85%' },
    full: { messages: '92%', input: '93%' },
  },
}))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({
  useFilteredDropdown: () => ({
    filtered: [], query: '', setQuery: vi.fn(),
    selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn(),
  }),
}))
vi.mock('../hooks/useVoiceInput', () => ({
  useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }),
  voiceInputSupported: false,
}))

// Mounts every display item so ChatPage's own row renderer runs for real.
vi.mock('../hooks/virtualizer/useVirtualChat', () => ({
  useVirtualChat: (opts: { items?: unknown[]; getKey?: (it: unknown, i: number) => string }) => {
    const items = opts.items ?? []
    return {
      virtualItems: items.map((data, index) => ({
        key: opts.getKey ? opts.getKey(data, index) : String(index),
        index,
        mounted: true,
        data,
      })),
      isAtBottom: true,
      getFollow: () => true,
      scrollToBottom: vi.fn(),
      mountIndex: vi.fn(() => false),
      retainRange: vi.fn(),
      farmIsMeasured: () => true,
      farmRecord: vi.fn(() => true),
      measureRef: () => () => {},
      topSentinelRef: { current: null },
      bottomSentinelRef: { current: null },
      offsetBefore: 0,
      offsetAfter: 0,
      totalHeight: 0,
    }
  },
}))

vi.mock('../api/pins', () => ({
  PIN_PREVIEW_INPUT_MAX_CHARS: 4096,
  pinsApi: {
    list: vi.fn().mockResolvedValue({ pins: [] }),
    create: vi.fn().mockResolvedValue({}),
    remove: vi.fn().mockResolvedValue({ ok: true }),
  },
}))

const apiMocks: Record<string, ReturnType<typeof vi.fn>> = {}
const apiSpy = (name: string) => {
  if (!(name in apiMocks)) apiMocks[name] = vi.fn().mockResolvedValue({})
  return apiMocks[name]
}
vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_t, prop: string) => {
      if (!(prop in apiMocks)) {
        apiMocks[prop] = vi.fn().mockResolvedValue(
          prop === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 }
            : prop === 'pendingQuestions' || prop === 'approvals' ? []
              : prop === 'chatSlotEnvironment' ? { profile: null, profiles: [] } : {},
        )
      }
      return apiMocks[prop]
    },
  }),
  fileReadUrl: (p: string) => `/api/file?path=${encodeURIComponent(p)}`,
  SEARCH_MIN_CHARS: 2,
}))

// stubGlobal, not assignment, so a sibling in the same shard is not affected.
vi.stubGlobal(
  'matchMedia',
  vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
)
vi.stubGlobal(
  'fetch',
  vi.fn().mockResolvedValue({
    ok: true, status: 200, text: () => Promise.resolve(''), json: () => Promise.resolve({}),
  }),
)

afterAll(() => {
  vi.unstubAllGlobals()
})

import ChatPage from '../pages/ChatPage'

const SLOT = {
  key: 'chat-1', title: 'chat-1', messages: 0, running: false,
  mode: '', created: '', last_ts: '',
}
const OTHER_SLOT = { ...SLOT, key: 'chat-2', title: 'chat-2' }

const msg = (role: string, content: string, extra: Partial<ChatMessage> = {}): ChatMessage => ({
  role, content, cls: '', ...extra,
})

/** Renders one user + one assistant turn over two known slots, then enters
 *  split view through the header toggle. */
async function renderSplit() {
  const slots = [SLOT, OTHER_SLOT]
  const messages = [
    msg('user', 'what changed?', { ts: '2026-08-12T07:00:00Z' }),
    msg('assistant', 'two files changed', { ts: '2026-08-12T07:00:05Z' }),
  ]
  apiSpy('dashboardConfig').mockResolvedValue({ session_grid: true })
  apiMocks.chatSlots = vi.fn().mockResolvedValue(slots)
  apiMocks.chatSlotDetail = vi.fn().mockResolvedValue({
    messages, has_more: false, total: messages.length,
  })
  apiMocks.sessions = vi.fn().mockResolvedValue({ sessions: [], has_more: false })
  // RTK's preloadedState REPLACES a slice, so spread the reducers' own state.
  const base = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...base.dashboard,
      status: { platform: 'darwin' } as unknown as RootState['dashboard']['status'],
      connected: true,
      slots: slots as unknown as RootState['dashboard']['slots'],
    },
    chat: { ...base.chat, activeSlot: 'chat-1' } as unknown as RootState['chat'],
  })
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
  act(() => { store.dispatch({ type: 'chat/replaceMessages', payload: messages }) })
  await waitFor(() => expect(assistantMounted).toBe(true))
  fireEvent.click(await screen.findByRole('button', { name: 'Enter split view' }))
  await waitFor(() => expect(gridProps?.openSideChat).toBeTypeOf('function'))
  return { store }
}

beforeEach(() => {
  assistantMounted = false
  gridProps = null
  localStorage.clear()
  sessionStorage.clear()
  for (const k of Object.keys(apiMocks)) delete apiMocks[k]
  vi.useFakeTimers({ shouldAdvanceTime: true })
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
})

describe('ChatPage resume from history in Split View', () => {
  it('a resume that asks to leave Split View shows the resumed chat', async () => {
    const { store } = await renderSplit()
    apiSpy('resumeChatSlot').mockResolvedValue({ ok: true, key: 'chat-3', mode: '', messages: [] })
    await act(async () => { await store.dispatch(resumeFromHistory({ key: 'dashboard:chat-3', title: 'three', leaveSplit: true })).unwrap() })
    expect(store.getState().chat.activeSlot).toBe('chat-3')
    await waitFor(() => expect(screen.queryByTestId('session-grid')).toBeNull())
  })

  it('a resume that does not ask keeps Split View', async () => {
    const { store } = await renderSplit()
    apiSpy('resumeChatSlot').mockResolvedValue({ ok: true, key: 'chat-3', mode: '', messages: [] })
    await act(async () => { await store.dispatch(resumeFromHistory({ key: 'dashboard:chat-3', title: 'three' })).unwrap() })
    expect(store.getState().chat.activeSlot).toBe('chat-3')
    expect(screen.getByTestId('session-grid')).toBeTruthy()
  })
})
