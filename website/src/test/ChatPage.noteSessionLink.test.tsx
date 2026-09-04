/**
 * Regression test: a `/chat?sid=…` link inside a NOTE switches session in place.
 *
 * An `inject` row renders through `MarkdownRenderer` directly, not through
 * `AssistantMessage`, so it needed its own copy of the three session props.
 * Without them `resolveSessionChip` refuses at its first guard and the link
 * falls through to the external branch — `ALLOWED_PROTOCOLS` holds only the
 * vscode schemes, so a root-relative href counts as external and gains
 * `target="_blank"`.
 *
 * Two halves, because either alone passes on broken code: the first asserts what
 * ChatPage's note branch HANDS OVER, the second feeds those very props to the
 * real renderer and asserts the anchor they buy.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

/** Props each `MarkdownRenderer` render received, in order. */
type MdProps = { content?: string; onSessionOpen?: (key: string) => void; sessions?: ReadonlyMap<string, string>; activeSession?: string }
const mdProps: MdProps[] = []

import { render, screen, act } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// Capture-only stub. The note branch is the sole caller here: `../pages/chat` is
// stubbed below, so no assistant row reaches the renderer.
vi.mock('../components/MarkdownRenderer', () => ({
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
          prop === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {},
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

/** Real slot-key shape (`chat-<n>-<unix-ts>`); `sessionKeyFrom` refuses anything else. */
const HERE = 'chat-1-1788000000'
const THERE = 'chat-2-1788000001'
const SLOT_HERE = { key: HERE, title: 'this one', messages: 1, running: false, mode: '', created: '', last_ts: '' }
const SLOT_THERE = { key: THERE, title: 'next: the other one', messages: 1, running: false, mode: '', created: '', last_ts: '' }

/** A note carrying the hand-off link, as `/note` persists it. */
const NOTE = { role: 'inject', content: `[next](/chat?sid=${THERE})`, ts: '2026-09-02T20:00:00Z' }

/** A FAILED sub-agent completion: that card opens expanded, so its body renders. */
const SUBAGENT_ROW = {
  role: 'subagent',
  content: [
    '[Subagent completion event]',
    'Agent `53e3e5eb` (kirocrew) failed ❌',
    'Task: Draft the release notes',
    '',
    `Handed off. Next: [next](/chat?sid=${THERE})`,
  ].join('\n'),
  ts: '2026-09-02T20:01:00Z',
}

const renderChatPage = (connected: boolean) => {
  const slots = [SLOT_HERE, SLOT_THERE]
  apiMocks.chatSlots = vi.fn().mockResolvedValue(slots)
  apiMocks.chatSlotDetail = vi.fn().mockResolvedValue({ messages: [NOTE], has_more: false, total: 1 })
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected,
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
  return { store }
}

/** Re-seed through the store so the row is present after mount. */
const seedNote = (store: ReturnType<typeof createTestStore>) => {
  act(() => { store.dispatch({ type: 'chat/replaceMessages', payload: [NOTE] }) })
}

/** The note row's own render, identified by the content it was handed. */
const noteRender = () => mdProps.filter(p => p.content === NOTE.content).at(-1)

describe('a note hands the renderer its session wiring', () => {
  beforeEach(() => {
    for (const k of Object.keys(apiMocks)) delete apiMocks[k]
    mdProps.length = 0
  })

  it('passes the handler, the roster and the active key', async () => {
    const { store } = renderChatPage(true)
    seedNote(store)
    await act(async () => {})

    const props = noteRender()
    expect(props).toBeDefined()
    expect(typeof props!.onSessionOpen).toBe('function')
    expect(props!.sessions?.get(THERE)).toBe(SLOT_THERE.title)
    expect(props!.activeSession).toBe(HERE)
  })

  it('withholds the roster while disconnected, as the assistant row does', async () => {
    // Negative control: pins the assertion above to `connected` rather than to a
    // roster that is simply always handed over.
    const { store } = renderChatPage(false)
    seedNote(store)
    await act(async () => {})

    expect(noteRender()?.sessions).toBeUndefined()
  })

  it('hands the same triple to a completion card, which renders the payload itself', async () => {
    // The cards call MarkdownRenderer directly too, so ChatPage must wire them
    // as well; forwarding inside the card is useless if nothing passes them.
    const { store } = renderChatPage(true)
    act(() => { store.dispatch({ type: 'chat/replaceMessages', payload: [SUBAGENT_ROW] }) })
    await act(async () => {})

    const props = mdProps.filter(p => p.content?.includes(`/chat?sid=${THERE}`)).at(-1)
    expect(props).toBeDefined()
    expect(typeof props!.onSessionOpen).toBe('function')
    expect(props!.sessions?.get(THERE)).toBe(SLOT_THERE.title)
    expect(props!.activeSession).toBe(HERE)
  })
})

describe('the anchor those props buy', () => {
  beforeEach(() => { mdProps.length = 0 })

  it('drops _blank and gains the switch tooltip', async () => {
    const { store } = renderChatPage(true)
    seedNote(store)
    await act(async () => {})
    const props = noteRender()!

    // The REAL renderer, handed exactly what ChatPage's note branch passed.
    const { default: RealMarkdownRenderer } = await vi.importActual<
      typeof import('../components/MarkdownRenderer')
    >('../components/MarkdownRenderer')
    render(
      <RealMarkdownRenderer
        content={props.content!}
        onSessionOpen={props.onSessionOpen}
        sessions={props.sessions}
        activeSession={props.activeSession}
      />,
    )

    const anchor = screen.getByText('next').closest('a')!
    expect(anchor).not.toHaveAttribute('target')
    expect(anchor).toHaveAttribute('href', `/chat?sid=${THERE}`)
    expect(anchor.getAttribute('title')).toContain(SLOT_THERE.title)
  })
})
