/**
 * The "Older Sessions" pane hides sub-agent/sub-task sessions by default and
 * surfaces each session's folder + tags inline on its row.
 *
 * Pins: subagent rows (a subagent:/subagent_ key
 * key) are absent by default while normal chats show; a "Show sub-agent
 * sessions" checkbox reveals them and persists the choice to localStorage; the
 * toggle only appears when the loaded history actually contains a subagent row;
 * and a row's folder glyph+name and tinted tag names render from folder_id/tags.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// Render framer-motion elements as plain DOM (jsdom can't run projection).
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown> & { children?: unknown }, ref: unknown) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as never)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: unknown }) => React.createElement(React.Fragment, null, children as never),
    LayoutGroup: ({ children }: { children?: unknown }) => React.createElement(React.Fragment, null, children as never),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))

const apiMocks = vi.hoisted(() => ({
  chatFolders: vi.fn().mockResolvedValue([]),
  chatTags: vi.fn().mockResolvedValue([]),
  sessions: vi.fn().mockResolvedValue({ sessions: [], has_more: false }),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop in apiMocks) return apiMocks[prop as keyof typeof apiMocks]
      return vi.fn().mockResolvedValue([])
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

import ChatSidebar from '../pages/ChatSidebar'

type HistoryRow = Record<string, unknown>

function renderSidebar(
  history: HistoryRow[],
  folders: Record<string, unknown>[] = [],
  tags: Record<string, unknown>[] = [],
) {
  apiMocks.chatFolders.mockResolvedValue(folders)
  apiMocks.chatTags.mockResolvedValue(tags)
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: {}, connected: true, slots: [], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as never,
    chat: {
      ...defaults.chat,
      activeSlot: null, slotStatusDetail: {},
      revealRequest: null, revealNonce: 0,
    } as never,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  qc.setQueryData(['chat-tags'], tags)
  qc.setQueryData(['tag-columns'], [])
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={[] as never} activeSlot={null} unreadSlots={[]}
              history={history as never} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

/** The footer toggle opens the Older Sessions pane; its aria-label is stable. */
const footer = () => screen.getByLabelText('Older sessions')
const openPane = () => fireEvent.click(footer())

const HIDE_KEY = 'mc-older-hide-subagents'

const REAL_A = { key: 'chat-1-100', title: 'Investigating security event log probes', messages: 3, modified: 3000 }
const REAL_B = { key: 'chat-2-200', title: 'Meeting Recording to Transcription', messages: 3, modified: 2500 }
const SUB_FLAG = { key: 'subagent_899ccce8', title: 'underscore subagent run', messages: 1, modified: 2800 }
const SUB_KEY = { key: 'subagent:df09562c', title: 'prefixed subagent run', messages: 1, modified: 2700 }

beforeEach(() => {
  localStorage.clear()
  apiMocks.sessions.mockClear()
})
afterEach(() => vi.clearAllMocks())

describe('Older Sessions — sub-agent filtering', () => {
  it('hides sub-agent sessions by default while showing normal chats', () => {
    renderSidebar([REAL_A, REAL_B, SUB_FLAG, SUB_KEY])
    openPane()
    expect(screen.getByText('Investigating security event log probes')).toBeInTheDocument()
    expect(screen.getByText('Meeting Recording to Transcription')).toBeInTheDocument()
    // Both an underscore-keyed and a colon-keyed subagent row are excluded.
    expect(screen.queryByText('underscore subagent run')).toBeNull()
    expect(screen.queryByText('prefixed subagent run')).toBeNull()
  })

  it('reveals sub-agent sessions when the toggle is switched on, and persists the choice', () => {
    renderSidebar([REAL_A, SUB_FLAG, SUB_KEY])
    openPane()
    const toggle = screen.getByTestId('older-show-subagents-toggle') as HTMLInputElement
    // Default: hidden, so the box is unchecked.
    expect(toggle.checked).toBe(false)
    fireEvent.click(toggle)
    expect(screen.getByText('underscore subagent run')).toBeInTheDocument()
    expect(screen.getByText('prefixed subagent run')).toBeInTheDocument()
    // '0' means "do not hide" — the value that survives a reload.
    expect(localStorage.getItem(HIDE_KEY)).toBe('0')
  })

  it('honors a persisted "show" preference on first render', () => {
    localStorage.setItem(HIDE_KEY, '0')
    renderSidebar([REAL_A, SUB_FLAG])
    openPane()
    expect(screen.getByText('underscore subagent run')).toBeInTheDocument()
    expect((screen.getByTestId('older-show-subagents-toggle') as HTMLInputElement).checked).toBe(true)
  })

  it('omits the toggle entirely when no sub-agent sessions are present', () => {
    renderSidebar([REAL_A, REAL_B])
    openPane()
    expect(screen.queryByTestId('older-show-subagents-toggle')).toBeNull()
  })

  it('shows a hidden-count hint (not a bare empty state) when every row is a hidden sub-agent', () => {
    renderSidebar([SUB_FLAG, SUB_KEY])
    openPane()
    // The list filters to empty, but the pane must explain WHY and point at the
    // toggle rather than reading as "no sessions".
    expect(screen.getByText(/sub-agent sessions hidden/i)).toBeInTheDocument()
    expect(screen.getByTestId('older-show-subagents-toggle')).toBeInTheDocument()
  })

  it('does not treat the _bg background session as a sub-agent', () => {
    const bg = { key: '_bg', title: 'background session', messages: 1, modified: 2600 }
    renderSidebar([REAL_A, bg])
    openPane()
    // _bg is not a sub-agent, so it stays visible and no toggle appears.
    expect(screen.getByText('background session')).toBeInTheDocument()
    expect(screen.queryByTestId('older-show-subagents-toggle')).toBeNull()
  })
})

describe('Older Sessions — inline folder and tags', () => {
  it('renders the folder glyph + name and tinted tag names on a row', () => {
    const folders = [{ id: 'f1', name: 'Kiro Factory', color: '#8b5cf6', order: 0 }]
    const tags = [{ id: 't1', name: 'review', color: '#22c55e', order: 0 }]
    const row = { key: 'chat-9-900', title: 'foldered tagged chat', messages: 2, modified: 3000, folder_id: 'f1', tags: ['t1'] }
    renderSidebar([row], folders, tags)
    openPane()
    const folderBadge = screen.getByTestId('history-folder-f1')
    expect(within(folderBadge).getByText('Kiro Factory')).toBeInTheDocument()
    const tagBadge = screen.getByTestId('history-tag-t1')
    expect(within(tagBadge).getByText('review')).toBeInTheDocument()
    expect(within(tagBadge).getByText('review')).toHaveStyle({ color: '#22c55e' })
  })

  it('renders no folder/tag line when a row has neither', () => {
    renderSidebar([REAL_A])
    openPane()
    expect(screen.queryByTestId('history-folder-f1')).toBeNull()
    expect(screen.queryByTestId('history-tag-t1')).toBeNull()
  })
})
