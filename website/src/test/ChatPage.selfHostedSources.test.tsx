/**
 * Regression test: ChatPage feeds the operator's self-managed GitLab allowlist
 * into the source-link index, so an allowlisted MR in the transcript becomes a
 * Changes tab.
 *
 * The gap this pins: `extractPullRequestLinks` / `PullRequestLinkIndex` default
 * to an EMPTY allowlist, which fails closed. Utility-level tests pass the hosts
 * explicitly, so they stay green even if ChatPage stops reading
 * `dashboardConfig().gitlab_hosts` — and self-hosted MRs would silently never
 * appear as source tabs. This asserts the wiring step itself.
 *
 * Harness mirrors ChatPage.surfaceUnread.test.tsx: mock everything ChatPage
 * pulls in that is irrelevant here, capture the props handed to the panel.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

const panelHistory: Array<Array<{ url: string; provider: string; number: number }>> = []
const indexHostArgs: Array<readonly string[] | undefined> = []
vi.mock('../utils/pullRequestLinks', async () => {
  const actual = await vi.importActual<typeof import('../utils/pullRequestLinks')>(
    '../utils/pullRequestLinks',
  )
  class SpyIndex extends actual.PullRequestLinkIndex {
    update(slot: string | null, messages: never, gitlabHosts?: readonly string[]) {
      indexHostArgs.push(gitlabHosts)
      const links = super.update(slot, messages, gitlabHosts)
      panelHistory.push(links.map(l => ({ url: l.url, provider: l.provider, number: l.number })))
      return links
    }
  }
  return { ...actual, PullRequestLinkIndex: SpyIndex }
})
vi.mock('../components/PullRequestPanel', () => ({ default: () => null }))
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
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
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../pages/chat', () => ({ ChatFooter: () => null, AssistantMessage: () => null, McpInfoButton: () => null }))
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

const SELF_HOSTED_MR = 'https://gitlab.acme.internal/team/api/-/merge_requests/7'

const renderWithHosts = (gitlabHosts: string[]) => {
  panelHistory.length = 0
  indexHostArgs.length = 0
  apiMocks.dashboardConfig = vi.fn().mockResolvedValue({ gitlab_hosts: gitlabHosts })
  apiMocks.chatSlots = vi.fn().mockResolvedValue([])
  const store = createTestStore({
    dashboard: {
      status: { platform: 'linux' }, connected: false, slots: [],
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0,
      unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as never,
    chat: {
      activeSlot: 'slot-1',
      messages: [{ role: 'assistant', content: `Opened ${SELF_HOSTED_MR}`, cls: '' }],
      slotRunning: false, slotStopping: false, slotState: 'idle',
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

const sawSelfHostedTab = () => panelHistory.some(sources => sources.some(s => s.url === SELF_HOSTED_MR))

describe('ChatPage self-hosted GitLab source tabs', () => {
  it('passes the configured allowlist to the index and surfaces the MR', async () => {
    renderWithHosts(['gitlab.acme.internal'])
    await waitFor(() =>
      expect(indexHostArgs.some(hosts => hosts?.includes('gitlab.acme.internal'))).toBe(true),
    )
    expect(sawSelfHostedTab()).toBe(true)
  })

  it('does not surface it when no host is allowlisted', async () => {
    renderWithHosts([])
    await waitFor(() => expect(indexHostArgs.length).toBeGreaterThan(0))
    expect(indexHostArgs.every(hosts => (hosts?.length ?? 0) === 0)).toBe(true)
    expect(sawSelfHostedTab()).toBe(false)
  })
})
