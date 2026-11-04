/**
 * Regression test: an `/artifacts/<slug>` link inside a NOTE opens the side
 * Artifacts panel instead of a new browser tab.
 *
 * An `inject` row renders through `MarkdownRenderer` directly, not through
 * `AssistantMessage`, so it needs its own `onArtifactOpen`. Without it the
 * renderer leaves the link a plain anchor and the browser follows it.
 *
 * Two halves, as in ChatPage.noteSessionLink: what ChatPage's note branch hands
 * over, then the real renderer driven with exactly those props.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

type MdProps = { content?: string; onArtifactOpen?: (slug: string) => void }
const mdProps: MdProps[] = []

import { render, screen, act, fireEvent, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('../components/MarkdownRenderer', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../components/MarkdownRenderer')>()),
  default: (props: MdProps) => {
    mdProps.push(props)
    return null
  },
}))

vi.mock('../pages/chat', () => ({
  ChatFooter: () => null,
  McpInfoButton: () => null,
  UserMessage: () => null,
  AssistantMessage: () => null,
}))

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

const HERE = 'chat-1-1788000000'
const SLOT_HERE = { key: HERE, title: 'this one', messages: 1, running: false, mode: '', created: '', last_ts: '' }

/** A note carrying an artifact link, as `/note` persists it. */
const NOTE = { role: 'inject', content: 'Queue updated: [cr-queue](/artifacts/cr-queue)', ts: '2026-10-29T20:00:00Z' }

const renderChatPage = () => {
  const slots = [SLOT_HERE]
  apiMocks.chatSlots = vi.fn().mockResolvedValue(slots)
  apiMocks.chatSlotDetail = vi.fn().mockResolvedValue({ messages: [NOTE], has_more: false, total: 1 })
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: true,
      slots, approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0,
      unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as never,
    chat: {
      activeSlot: HERE,
      messages: [NOTE], slotRunning: false, slotStopping: false, slotState: 'idle',
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
          <MemoryRouter initialEntries={[`/chat/${HERE}`]}>
            <Routes>
              <Route path="/chat/:slug?" element={<ChatPage mode="" />} />
            </Routes>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  act(() => { store.dispatch({ type: 'chat/replaceMessages', payload: [NOTE] }) })
}

const noteRender = () => mdProps.filter(p => p.content === NOTE.content).at(-1)

describe('a note opens its artifact link in the side panel', () => {
  beforeEach(() => {
    for (const k of Object.keys(apiMocks)) delete apiMocks[k]
    mdProps.length = 0
  })

  it('hands the note renderer the artifact opener', async () => {
    renderChatPage()
    await act(async () => {})
    const props = noteRender()
    expect(props).toBeDefined()
    expect(typeof props!.onArtifactOpen).toBe('function')
  })

  it('a click on the link opens the artifact in place and does not navigate', async () => {
    renderChatPage()
    await act(async () => {})
    const props = noteRender()!

    const { default: RealMarkdownRenderer } = await vi.importActual<
      typeof import('../components/MarkdownRenderer')
    >('../components/MarkdownRenderer')
    render(<RealMarkdownRenderer content={props.content!} onArtifactOpen={props.onArtifactOpen} />)

    const anchor = screen.getByText('cr-queue').closest('a')!
    expect(anchor).toHaveAttribute('href', '/artifacts/cr-queue')
    const followed = fireEvent.click(anchor)
    expect(followed).toBe(false)
    await waitFor(() => expect(apiMocks.recordArtifactReference).toHaveBeenCalledWith('cr-queue', HERE))
  })
})
