/**
 * Regression test: an `/artifacts/<slug>` link inside a sub-agent completion
 * card, a skill load card (its single body and a multi-skill row) or a session
 * message card on the main chat page opens the side Artifacts panel instead of
 * a new browser tab.
 *
 * Each card renders its markdown through `MarkdownRenderer` itself, so it needs
 * the page's artifact opener from the row render context. Without it the
 * renderer leaves the link a plain anchor and the browser follows it.
 *
 * Two halves, as in ChatPage.noteArtifactLink: what each card hands its
 * renderer on this page, then the real renderer driven with exactly those props.
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

const LINK = '[Release checklist](/artifacts/release-checklist)'

const SUBAGENT = {
  role: 'subagent', ts: '2026-10-29T20:00:01Z',
  content: `[Subagent completion event]\nAgent \`53e3e5eb\` (kirocrew) failed ❌\nTask: Draft the release checklist\n\nSubagent result: ${LINK}`,
}
const SKILL = {
  role: 'system', ts: '2026-10-29T20:00:02Z', content: 'Loaded skill: release',
  meta: { kind: 'skill_load', skills: [{ name: 'release', body: `Skill body links ${LINK}` }] },
}
const SKILLS = {
  role: 'system', ts: '2026-10-29T20:00:03Z', content: 'Loaded skills: alpha, beta',
  meta: { kind: 'skill_load', skills: [{ name: 'alpha', body: `Alpha links ${LINK}` }, { name: 'beta', body: 'Beta body' }] },
}
const SEND = {
  role: 'tool', ts: '2026-10-29T20:00:04Z', content: '🔧 Session send',
  meta: {
    tool_name: 'session_send', mcp_server: 'kirocrew-dashboard',
    input: JSON.stringify({ target: 'chat-9', message: `Session message links ${LINK}` }),
    sessionSend: { outcome: 'delivered', target: 'chat-9' },
  },
}
const ROWS = [SUBAGENT, SKILL, SKILLS, SEND]

/** Opens every card whose markdown waits behind a disclosure. */
const openCards = () => {
  const group = screen.queryByText(/^(Worked through \d+ steps?|\d+ tool calls?)$/)
  if (group) fireEvent.click(group)
  for (const toggle of screen.getAllByTestId('skill-load-card-toggle')) fireEvent.click(toggle)
  fireEvent.click(screen.getByRole('button', { name: 'alpha' }))
}

const CARDS = [
  { card: 'sub-agent completion card', marker: 'Subagent result' },
  { card: 'skill load card', marker: 'Skill body links' },
  { card: 'skill load card, multi-skill row', marker: 'Alpha links' },
  { card: 'session message card', marker: 'Session message links' },
]

const renderChatPage = () => {
  const slots = [SLOT_HERE]
  apiMocks.chatSlots = vi.fn().mockResolvedValue(slots)
  apiMocks.chatSlotDetail = vi.fn().mockResolvedValue({ messages: ROWS, has_more: false, total: ROWS.length })
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
      messages: ROWS, slotRunning: false, slotStopping: false, slotState: 'idle',
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
  act(() => { store.dispatch({ type: 'chat/replaceMessages', payload: ROWS }) })
}

const cardRender = (marker: string) => mdProps.filter(p => p.content?.includes(marker)).at(-1)

describe('agent and skill cards open their artifact link in the side panel', () => {
  beforeEach(() => {
    for (const k of Object.keys(apiMocks)) delete apiMocks[k]
    mdProps.length = 0
  })

  it.each(CARDS)('hands the $card renderer the artifact opener', async ({ marker }) => {
    renderChatPage()
    await act(async () => {})
    openCards()
    const props = cardRender(marker)
    expect(props).toBeDefined()
    expect(typeof props!.onArtifactOpen).toBe('function')
  })

  it.each(CARDS)('a click on the $card link opens the artifact in place and does not navigate', async ({ marker }) => {
    renderChatPage()
    await act(async () => {})
    openCards()
    const props = cardRender(marker)!

    const { default: RealMarkdownRenderer } = await vi.importActual<
      typeof import('../components/MarkdownRenderer')
    >('../components/MarkdownRenderer')
    render(<RealMarkdownRenderer content={props.content!} onArtifactOpen={props.onArtifactOpen} />)

    const anchor = screen.getByText('Release checklist').closest('a')!
    expect(anchor).toHaveAttribute('href', '/artifacts/release-checklist')
    const followed = fireEvent.click(anchor)
    expect(followed).toBe(false)
    await waitFor(() => expect(apiMocks.recordArtifactReference).toHaveBeenCalledWith('release-checklist', HERE))
  })
})
