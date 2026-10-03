/**
 * #16094: ChatSidebar lifts its authoritative board-lane state up via
 * `onBoardActiveChange` so the host's viewport-clamp reserve tracks the lane
 * actually rendered -- NOT the `tagColumnsEnabled` flag alone.
 *
 * The distinction is the bug the correction caught: the board feature can be ON
 * with ZERO columns (toggled on, then every column deleted). Then
 * `boardLaneActive = orderedColumns.length > 0` is false, the LIST lane renders,
 * and the chat pane sits beside it -- so the host must reserve CHAT_PANE_MIN_W,
 * not 0. These pin that the lifted signal is `boardLaneActive`, columns and all.
 *
 * loadChatConfig is mocked with tagColumnsEnabled: true for BOTH cases, so the
 * only thing that moves the signal is the presence of columns.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { TagColumn } from '../types'

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
// Feature ON for every case here: the signal must still swing on columns alone.
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const COLUMNS: TagColumn[] = [
  { id: 'c1', name: 'Col', tag_ids: [], mode: 'any', order: 0, source: 'tags' },
]

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'tagColumns') return () => Promise.resolve(COLUMNS)
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

const SLOTS = [{ key: 'k1', title: 'One', messages: 1, running: false, modified: 1000 }]

function renderSidebar(cols: TagColumn[]) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots: SLOTS, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {}, workflowRuns: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-tags'], [])
  qc.setQueryData(['tag-columns'], cols)
  qc.setQueryData(['chat-folders'], [])
  const onBoardActiveChange = vi.fn()
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
              onWidthChange={vi.fn()} onDragChange={vi.fn()}
              onBoardActiveChange={onBoardActiveChange}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { onBoardActiveChange }
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — onBoardActiveChange (lifted boardLaneActive)', () => {
  it('reports TRUE when the board feature is on AND columns exist', () => {
    const { onBoardActiveChange } = renderSidebar(COLUMNS)
    expect(onBoardActiveChange).toHaveBeenLastCalledWith(true)
  })

  it('reports FALSE when the feature is on but there are ZERO columns (list lane)', () => {
    // The correction's case: enabled + no columns renders the LIST lane, so the
    // host must reserve the chat pane. tagColumnsEnabled alone would wrongly
    // report board here.
    const { onBoardActiveChange } = renderSidebar([])
    expect(onBoardActiveChange).toHaveBeenLastCalledWith(false)
  })
})
