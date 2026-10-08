/**
 * The folder header's hover toolbar (⋯ / new chat) shows on hover, on a touch
 * screen, while its menu is open, and on KEYBOARD focus only. The session row's
 * toolbar shares the same reveal and is pinned in ChatSidebar.sourceLinkChip.
 *
 * It used to also show on `focus-within`. A mouse click focuses the header's own
 * button, so after clicking a folder the toolbar stayed pinned open once the
 * pointer had left. `:focus-visible` is not matched by a
 * mouse-focused button, so a keyboard user still gets the toolbar and a mouse
 * user no longer has it stuck.
 *
 * jsdom does not evaluate `:hover` / `:focus-visible`, so the class list is the
 * observable contract here.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatFolder } from '../types'

// Render framer-motion elements as plain DOM because jsdom cannot run projection.
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

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: () => vi.fn().mockResolvedValue({}),
  }),
}))

vi.mock('../hooks/useIsMobile', () => ({
  MOBILE_BREAKPOINT: 768,
  useIsMobile: () => false,
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

const FOLDER_ID = 'folder-reveal'
const folders: ChatFolder[] = [{ id: FOLDER_ID, name: 'PR drive-to-green', order: 0, collapsed: true }]

function renderSidebar() {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: true, slots: [], slotsLoaded: true, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: null,
      messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={[]} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="kirocrew" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

function expectHoverAndKeyboardOnly(bar: HTMLElement) {
  const cls = bar.className
  expect(cls).toContain('opacity-0')
  expect(cls).toContain('group-hover:opacity-100')
  expect(cls).toContain('[@media(hover:none)]:opacity-100')
  expect(cls).toContain('group-focus-visible:opacity-100')
  expect(cls).toContain('group-has-[:focus-visible]:opacity-100')
  expect(cls).toContain('has-[[data-state=open]]:opacity-100')
  expect(cls).not.toContain('focus-within')
}

describe('folder header toolbar', () => {
  it('reveals on hover and keyboard focus, not after a mouse click', () => {
    renderSidebar()
    const bar = screen.getByTestId(`folder-menu-${FOLDER_ID}`).parentElement as HTMLElement
    expectHoverAndKeyboardOnly(bar)
  })
})
