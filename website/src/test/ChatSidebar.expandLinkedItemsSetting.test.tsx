/**
 * Chat sidebar: the `dashboard.expand_linked_items` setting.
 *
 * A row's linked-items strip (the count plus its PR chips) is folded by
 * default. Only a literal `true` starts it open; the count still folds and
 * opens each row on its own.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

const { kirocrewConfigMock } = vi.hoisted(() => ({
  kirocrewConfigMock: vi.fn((): Promise<unknown> => Promise.resolve({})),
}))

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
        if (k === 'children' || FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  // One component per tag: a fresh type on every read would remount the subtree.
  const byTag = new Map<string, ReturnType<typeof make>>()
  const motion = new Proxy({}, {
    get: (_t, tag: string) => {
      if (!byTag.has(tag)) byTag.set(tag, make(tag))
      return byTag.get(tag)
    },
  })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, name: string) => (name === 'kirocrewConfig' ? kirocrewConfigMock : vi.fn().mockResolvedValue([])),
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
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

const slots = [{
  key: 'k1', title: 'a-session', running: false, messages: 2, agent: 'kirocrew', last_message: 'done',
  source_links: [{ provider: 'github', number: 7, label: '#7', kind: 'change', state: 'open', url: 'https://github.com/acme/app/pull/7' }],
} as ChatSlot]

function renderSidebar() {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: null, slotStatusDetail: {}, workflowRuns: {}, subagents: {}, slotActivity: {}, automations: {},
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
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

afterEach(() => {
  vi.clearAllMocks()
  kirocrewConfigMock.mockImplementation(() => Promise.resolve({}))
})

describe('chat sidebar — dashboard.expand_linked_items', () => {
  it.each([[undefined], [false], ['true']])('keeps the strip folded when the setting is %j', async value => {
    kirocrewConfigMock.mockImplementation(() => Promise.resolve({ dashboard: value === undefined ? {} : { expand_linked_items: value } }))
    renderSidebar()
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalled())
    const count = await screen.findByTestId('session-links-count')
    expect(count).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('session-links-expanded')).toBeNull()
  })

  it('starts the strip open when the setting is true, and the count still folds it', async () => {
    kirocrewConfigMock.mockImplementation(() => Promise.resolve({ dashboard: { expand_linked_items: true } }))
    renderSidebar()
    await waitFor(() => expect(screen.getByTestId('session-links-expanded')).toBeInTheDocument())
    const count = screen.getByTestId('session-links-count')
    expect(count).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(count)
    expect(count).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('session-links-expanded')).toBeNull()
    fireEvent.click(count)
    expect(screen.getByTestId('session-links-expanded')).toBeInTheDocument()
  })
})
