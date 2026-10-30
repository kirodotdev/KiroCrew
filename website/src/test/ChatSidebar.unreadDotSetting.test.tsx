/**
 * Chat sidebar: the `dashboard.show_unread_dot` setting.
 *
 * The per-row unread dot ("Agent finished — your turn") is shown by default.
 * Only a literal `false` hides it; an absent key, `true` or a hand-edited
 * non-boolean keeps today's behavior.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
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
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
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

const slots = [{ key: 'k1', title: 'a-session', running: false, messages: 2, agent: 'kirocrew', last_message: 'done' } as ChatSlot]

function renderSidebar() {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: ['k1'], updateProgress: null,
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
              slots={slots} activeSlot={null} unreadSlots={['k1']}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

const unreadDot = (container: HTMLElement) =>
  container.querySelector('.session-row span[role="img"][aria-label="Agent finished — your turn"]')

afterEach(() => {
  vi.clearAllMocks()
  kirocrewConfigMock.mockImplementation(() => Promise.resolve({}))
})

describe('chat sidebar — dashboard.show_unread_dot', () => {
  it('shows the unread dot by default, when the key is absent', async () => {
    const { container } = renderSidebar()
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalled())
    await waitFor(() => expect(unreadDot(container)).toBeTruthy())
  })

  it.each([[true], ['false'], [null]])('shows the unread dot when the setting is %j', async value => {
    kirocrewConfigMock.mockImplementation(() => Promise.resolve({ dashboard: { show_unread_dot: value } }))
    const { container } = renderSidebar()
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalled())
    await waitFor(() => expect(unreadDot(container)).toBeTruthy())
  })

  it('hides the unread dot, and keeps the last message, when the setting is false', async () => {
    kirocrewConfigMock.mockImplementation(() => Promise.resolve({ dashboard: { show_unread_dot: false } }))
    const { container } = renderSidebar()
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalled())
    await waitFor(() => expect(unreadDot(container)).toBeNull())
    expect(container.querySelector('.session-row')!.textContent).toContain('done')
  })
})
