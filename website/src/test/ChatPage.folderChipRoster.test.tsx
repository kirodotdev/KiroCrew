/**
 * The folder chip's roster on the chat page is the folders the Sessions list
 * draws. A Bot's home (and the folders inside it) leaves that list while none of
 * its sessions is listed there (`foldersForSessionList`), so a chip naming one
 * would reveal a row that is not drawn. Such a folder stays plain text, as a
 * path no folder renders to does.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatFolder } from '../types'

// Stub AssistantMessage: print the roster the page hands the folder chip.
vi.mock('../pages/chat', async () => {
  const React = await import('react')
  const { SidebarFolderCtx } = await import('../components/markdown/contexts')
  function Probe() {
    const ctx = React.useContext(SidebarFolderCtx)
    return React.createElement('span', { 'data-testid': 'roster' },
      ctx.folders ? ctx.folders.map(f => f.id).join(',') : 'none')
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
      retainRange: vi.fn(),
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

const MSG = { role: 'assistant', content: 'see `Radar/Radar inbox`', ts: '2026-06-23T20:00:00Z' }

/** A person's folder, and a Bot's home with one folder inside it. */
const FOLDERS: ChatFolder[] = [
  { id: 'f-work', name: 'Work', order: 0 },
  { id: 'f-radar', name: 'Radar', order: 1, mate_home: true, owner_app: 'member:radar' },
  { id: 'f-radar-inbox', name: 'Radar inbox', order: 0, parent_id: 'f-radar' },
]

const SLOT_A = { key: 'chat-1', title: 'one', messages: 1, running: false, mode: '', created: '', last_ts: '' }

const renderChatPage = (slots: Array<typeof SLOT_A & { folder_id?: string }>) => {
  apiMocks.chatSlots = vi.fn().mockResolvedValue(slots)
  apiMocks.chatFolders = vi.fn().mockResolvedValue(FOLDERS)
  apiMocks.chatSlotDetail = vi.fn().mockResolvedValue({ messages: [MSG], has_more: false, total: 1 })
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: true,
      slots, approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0,
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
  act(() => { store.dispatch({ type: 'chat/replaceMessages', payload: [MSG] }) })
}

describe('ChatPage folder chip roster', () => {
  beforeEach(() => {
    for (const k of Object.keys(apiMocks)) delete apiMocks[k]
  })

  it('leaves out a Bot home and its folders while none of its sessions is listed', async () => {
    renderChatPage([SLOT_A])
    await vi.waitFor(() => expect(screen.getByTestId('roster').textContent).toBe('f-work'))
  })

  it('keeps a Bot home and its folders while one of its sessions is listed', async () => {
    renderChatPage([SLOT_A, { ...SLOT_A, key: 'chat-2', title: 'two', folder_id: 'f-radar-inbox' }])
    await vi.waitFor(() => expect(screen.getByTestId('roster').textContent).toBe('f-work,f-radar,f-radar-inbox'))
  })
})
