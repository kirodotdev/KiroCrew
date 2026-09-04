/**
 * Test: a sidebar chip reveals its pull request / issue in the side panel, even
 * when the panel's own transcript scan does not list that link.
 *
 * The gap this pins. Two different scans produce the chips and the panel tabs:
 *  - the chips come from the BACKEND scan (state.py), which keeps every provider
 *    url in the whole server-side transcript, whoever wrote it;
 *  - the panel tabs come from the frontend extractor, which emits only links the
 *    AGENT surfaced (a pull request the USER pasted is deliberately a Resource,
 *    not a Change) and sees only the messages this window has loaded.
 * So a chip routinely names a link the panel has never heard of. Without the
 * injection asserted here the panel would normalise the selection straight back
 * to the first link it does know, and the chip would be a dead end — visibly a
 * bug, since the chip is what the user just clicked.
 *
 * Harness mirrors ChatPage.selfHostedSources.test.tsx: stub everything ChatPage
 * pulls in that is irrelevant here, and capture the props handed to the panel.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { __resetPanelTabs } from '../hooks/usePanelTabs'

const SLOT = 'slot-1'
/** Mentioned by the AGENT: the extractor emits it, so the panel knows it. */
const AGENT_PR = 'https://github.com/acme/widgets/pull/10'
/** Pasted by the USER: a chip, but NOT a panel tab. */
const USER_PR = 'https://github.com/acme/widgets/pull/99'
const USER_ISSUE = 'https://github.com/acme/widgets/issues/55'

interface PanelSnapshot {
  sources: string[]
  issues: string[]
  selectedSourceUrl: string
  selectedIssueUrl: string
  activeTabId: string | null
}
const panelSnapshots: PanelSnapshot[] = []

vi.mock('../pages/chat/SidePanel', () => ({
  default: (props: {
    sources?: { url: string }[]
    issues?: { url: string }[]
    selectedSourceUrl?: string
    selectedIssueUrl?: string
    tabsCtl: { activeId: string | null }
  }) => {
    panelSnapshots.push({
      sources: (props.sources ?? []).map(s => s.url),
      issues: (props.issues ?? []).map(i => i.url),
      selectedSourceUrl: props.selectedSourceUrl ?? '',
      selectedIssueUrl: props.selectedIssueUrl ?? '',
      activeTabId: props.tabsCtl.activeId,
    })
    return <div data-testid="side-panel" />
  },
  CHAT_PANE_MIN_W: 360,
  sidePanelFillWidth: () => false,
  shouldMountSidePanel: ({ activityOpen }: { activityOpen: boolean }) => activityOpen,
}))

// Drive the reveal through the real prop ChatPage passes down, so this asserts
// the wiring rather than a hand-called callback.
vi.mock('../pages/ChatSidebar', () => ({
  default: ({ onOpenSource }: {
    onOpenSource?: (slot: string, link: { url: string; kind: 'change' | 'issue' }) => void
  }) => (
    <>
      <button type="button" onClick={() => onOpenSource?.(SLOT, { url: USER_PR, kind: 'change' })}>reveal-pr</button>
      <button type="button" onClick={() => onOpenSource?.(SLOT, { url: USER_ISSUE, kind: 'issue' })}>reveal-issue</button>
    </>
  ),
  SIDEBAR_MIN: 200,
  SIDEBAR_MAX: 500,
}))

vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../components/ChatInput', () => ({ default: () => null }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({ default: () => null }))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
vi.mock('../components/OverlayDrawer', () => ({ default: ({ children }: { children?: ReactNode }) => children }))
vi.mock('../components/AgentDropdownList', () => ({ default: () => null }))
vi.mock('../components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/SegmentedControl', () => ({ default: () => null }))
vi.mock('../pages/chat/CollapsibleToolGroup', () => ({ default: () => null }))
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../pages/chat', () => ({ ChatFooter: () => null, AssistantMessage: () => null, UserMessage: () => null, McpInfoButton: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ contentWidth: 'compact' }),
  CONTENT_WIDTH: { compact: { messages: '900px', input: '916px' }, comfortable: { messages: '84%', input: '85%' }, full: { messages: '92%', input: '93%' } },
}))
vi.mock('../hooks/usePanelState', () => ({ usePanelState: () => ({ isOpen: false, openPanel: vi.fn(), closePanel: vi.fn() }), useDiffPanel: () => ({ isOpen: false, filePath: '', original: '', modified: '', openDiff: vi.fn(), closeDiff: vi.fn() }) }))
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
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as never

import ChatPage from '../pages/ChatPage'

const TRANSCRIPT = [
  { role: 'assistant', content: `Opened ${AGENT_PR}`, cls: '' },
  { role: 'user', content: `See also ${USER_PR} and ${USER_ISSUE}`, cls: '' },
]

const renderChatPage = () => {
  panelSnapshots.length = 0
  apiMocks.dashboardConfig = vi.fn().mockResolvedValue({})
  apiMocks.chatSlots = vi.fn().mockResolvedValue([])
  const store = createTestStore({
    dashboard: {
      // Deliberately EMPTY, as in ChatPage.selfHostedSources.test.tsx. A
      // populated slot list makes ChatPage re-fetch the active slot on mount,
      // which replaces the preloaded transcript with this harness's stub reply
      // and empties the very link list under test. The sidebar is stubbed here,
      // so it needs no slot data of its own.
      status: { platform: 'linux' }, connected: true, slots: [],
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0,
      unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as never,
    chat: {
      activeSlot: SLOT,
      messages: TRANSCRIPT,
      slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'files', slotActivity: {}, slotHistory: [],
    } as never,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat']}>
            <Routes>
              <Route path="/chat/:slug?" element={<ChatPage mode="" />} />
            </Routes>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

const latest = () => panelSnapshots[panelSnapshots.length - 1]

describe('ChatPage – sidebar chip reveals a source in the panel', () => {
  beforeEach(() => {
    __resetPanelTabs()
    localStorage.clear()
  })

  it('opens the Changes tab on the clicked pull request, even one the extractor excludes', async () => {
    renderChatPage()
    // Negative control: the panel is closed and the user-pasted PR is not a tab.
    expect(panelSnapshots).toHaveLength(0)

    fireEvent.click(screen.getByText('reveal-pr'))

    await waitFor(() => expect(screen.getByTestId('side-panel')).toBeInTheDocument())
    await waitFor(() => expect(latest().selectedSourceUrl).toBe(USER_PR))
    // Injected AHEAD of the scanned links, which are otherwise left intact.
    expect(latest().sources).toEqual([USER_PR, AGENT_PR])
    expect(latest().activeTabId).toBe('changes')
  })

  it('opens the Issues tab on the clicked issue', async () => {
    renderChatPage()
    fireEvent.click(screen.getByText('reveal-issue'))

    await waitFor(() => expect(screen.getByTestId('side-panel')).toBeInTheDocument())
    await waitFor(() => expect(latest().selectedIssueUrl).toBe(USER_ISSUE))
    expect(latest().issues).toEqual([USER_ISSUE])
    expect(latest().activeTabId).toBe('issues')
    // The reveal is per kind: the Changes list is untouched.
    expect(latest().sources).toEqual([AGENT_PR])
  })

  it('keeps a revealed pull request AND a revealed issue on the same session', () => {
    // Regression: a single last-one-wins reveal record meant revealing the issue
    // evicted the pull request, its injection vanished from `sources`, and the
    // Changes reconciliation normalised the selection onto a DIFFERENT pull
    // request behind the user's back.
    renderChatPage()
    fireEvent.click(screen.getByText('reveal-pr'))
    fireEvent.click(screen.getByText('reveal-issue'))

    expect(latest().issues).toEqual([USER_ISSUE])
    expect(latest().selectedIssueUrl).toBe(USER_ISSUE)
    // The pull request survived the second reveal, still selected.
    expect(latest().sources).toContain(USER_PR)
    expect(latest().selectedSourceUrl).toBe(USER_PR)
  })

  it('the injection is what surfaces it — an unrevealed user-pasted PR stays out', async () => {    // Bidirectional proof for the first case: with the panel open but NOTHING
    // revealed, the extractor's own list is all the panel gets. Without this the
    // first test could pass on a panel that simply lists every url it sees.
    renderChatPage()
    fireEvent.click(screen.getByText('reveal-issue'))
    await waitFor(() => expect(screen.getByTestId('side-panel')).toBeInTheDocument())

    expect(latest().sources).not.toContain(USER_PR)
    expect(latest().selectedSourceUrl).toBe(AGENT_PR)
  })

  it('the reveal survives a reload instead of silently swapping the panel', async () => {
    // The selection is durable but the extractor cannot reproduce a user-pasted
    // link, so holding the reveal in memory only meant the panel came back on a
    // DIFFERENT pull request with no signal it had changed.
    renderChatPage()
    fireEvent.click(screen.getByText('reveal-pr'))
    await waitFor(() => expect(latest().selectedSourceUrl).toBe(USER_PR))

    // Second mount = a reload: same localStorage, fresh React state.
    cleanup()
    renderChatPage()
    // Open the panel the way a restored session does, without another chip click.
    fireEvent.click(screen.getByText('reveal-issue'))
    await waitFor(() => expect(screen.getByTestId('side-panel')).toBeInTheDocument())

    expect(latest().sources).toContain(USER_PR)
    expect(latest().selectedSourceUrl).toBe(USER_PR)
  })
})
