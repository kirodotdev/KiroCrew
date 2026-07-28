/**
 * Chat sidebar flat view respects collapsed folders.
 * In flat view ("explode chats out of folders"), a session is dropped when its
 * folder — or any ancestor folder — is collapsed, so "flat view" means
 * "flatten the folders I currently have expanded". Each topmost collapsed
 * folder that still hides a session surfaces as a pill in the reveal strip
 * ("Show collapsed folders: <folder>") whose click expands it. Tree view is
 * unaffected; searching bypasses the collapse-hiding (not exercised here).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render } from '@testing-library/react'
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
    React.forwardRef((props: any, ref: any) => {
      const clean: any = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: any) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: any) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, { get: () => vi.fn().mockResolvedValue([]) }),
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

function renderSidebar(slots: any[], folders: any[]) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as any,
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {}, workflowRuns: {} } as any,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

// Flat view on from the first render.
beforeEach(() => { localStorage.clear(); localStorage.setItem('mc-sidebar-flat-view', '1') })
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — flat view respects collapsed folders', () => {
  const cronInFolder = { key: 'cron-abc123', title: 'nightly report', running: false, messages: 2, folder_id: 'cronsF' }
  const looseChat = { key: 'chat-1-100', title: 'loose chat', running: false, messages: 2 }

  it('hides a collapsed folder\'s session in flat view and shows a reveal pill', () => {
    const folders = [{ id: 'cronsF', name: 'crons', collapsed: true, order: 0 }]
    const { getByText, queryByText, getByTestId } = renderSidebar([cronInFolder, looseChat], folders)
    expect(queryByText('nightly report')).toBeNull() // in collapsed folder → hidden
    expect(getByText('loose chat')).toBeTruthy()       // un-foldered → shown
    // Reveal strip present, listing the collapsed folder.
    expect(getByTestId('flat-collapsed-strip')).toBeTruthy()
    expect(getByText('Show collapsed folders:')).toBeTruthy()
    expect(getByText('crons')).toBeTruthy()
  })

  it('shows the session and no reveal strip when the folder is expanded', () => {
    const folders = [{ id: 'cronsF', name: 'crons', collapsed: false, order: 0 }]
    const { getByText, queryByTestId } = renderSidebar([cronInFolder, looseChat], folders)
    expect(getByText('nightly report')).toBeTruthy() // expanded folder → shown
    expect(getByText('loose chat')).toBeTruthy()
    expect(queryByTestId('flat-collapsed-strip')).toBeNull() // nothing hidden → no strip
  })

  it('lists the TOPMOST collapsed ancestor (parent), not an expanded child', () => {
    // Parent 'p' collapsed, child 'c' expanded, session filed in the child:
    // the session is hidden (ancestor collapsed) and the pill names the parent,
    // since expanding the parent is what reveals it.
    const folders = [
      { id: 'p', name: 'parent-fold', collapsed: true, order: 0 },
      { id: 'c', name: 'child-fold', collapsed: false, order: 1, parent_id: 'p' },
    ]
    const nested = { key: 'chat-9-900', title: 'nested chat', running: false, messages: 2, folder_id: 'c' }
    const { queryByText, getByText } = renderSidebar([nested, looseChat], folders)
    expect(queryByText('nested chat')).toBeNull()   // ancestor collapsed → hidden
    expect(getByText('parent-fold')).toBeTruthy()   // pill names the topmost collapsed folder
    expect(queryByText('child-fold')).toBeNull()    // not the expanded child
  })

  it('does not hang on cyclic folder ancestry (visited-set guard)', () => {
    // A hand-edited folders.json can contain a parent_id cycle. The ancestry
    // walks must terminate (visited-set guard) rather than freeze the tab.
    const folders = [
      { id: 'a', name: 'Aye', collapsed: true, order: 0, parent_id: 'b' },
      { id: 'b', name: 'Bee', collapsed: false, order: 1, parent_id: 'a' },
    ]
    const inCycle = { key: 'chat-7-700', title: 'cycle chat', running: false, messages: 2, folder_id: 'a' }
    const { queryByText, getByText } = renderSidebar([inCycle, looseChat], folders)
    // Render completed (no infinite loop): the collapsed cycle member hides its
    // session and the reveal pill names that collapsed folder.
    expect(queryByText('cycle chat')).toBeNull()
    expect(getByText('loose chat')).toBeTruthy()
    expect(getByText('Aye')).toBeTruthy()
  })
})
