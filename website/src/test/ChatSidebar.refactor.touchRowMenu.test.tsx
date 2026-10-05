/**
 * A session row on ANY touch screen — an iPad as much as a phone — offers the single ⋯
 * menu, not the hover-revealed Fork / Close cluster: a hover-only control sits
 * permanently over the row's timestamp and pin where nothing can hover. The touch flag
 * reaches the row through the row scene (`isMobile: isMobile || isTouchDevice`), so the
 * case that matters is a touch device that is NOT phone-width.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, within, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

const { device } = vi.hoisted(() => ({ device: { phoneWidth: false, touch: false } }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => device.phoneWidth }))
vi.mock('../hooks/useIsTouchDevice', () => ({ useIsTouchDevice: () => device.touch }))

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  type MockProps = Record<string, unknown> & { children?: React.ReactNode }
  const make = (tag: string) =>
    React.forwardRef((props: MockProps, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children' || FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children)
    })
  const cache = new Map<string, unknown>()
  const motion = new Proxy({}, {
    get: (_t, tag: string) => {
      if (!cache.has(tag)) cache.set(tag, make(tag))
      return cache.get(tag)
    },
  })
  return {
    motion,
    AnimatePresence: ({ children }: MockProps) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: MockProps) => React.createElement(React.Fragment, null, children),
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

const SLOTS = [{ key: 'k1', title: 'Only session', running: false, messages: 2 }] as unknown as ChatSlot[]

function rowControls() {
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: {}, connected: true, slots: SLOTS, approvalMode: 'normal', unreadSlots: [], slotsLoaded: true,
    } as unknown as RootState['dashboard'],
    chat: { ...defaults.chat, activeSlot: null, slotStatusDetail: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar slots={SLOTS} activeSlot={null} unreadSlots={[]} history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]} />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  const row = document.querySelector('[data-slot-key="k1"]') as HTMLElement
  expect(row, 'session row not rendered').not.toBeNull()
  return {
    more: within(row).queryAllByRole('button', { name: 'More options' }).length,
    close: within(row).queryAllByRole('button', { name: 'Close session' }).length,
    fork: within(row).queryAllByRole('button', { name: 'Fork chat' }).length,
  }
}

beforeEach(() => { localStorage.clear(); device.phoneWidth = false; device.touch = false })
afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('the session row menu on a touch screen', () => {
  it('a touch screen that is not phone-width gets the single ⋯ menu', () => {
    device.touch = true
    expect(rowControls()).toEqual({ more: 1, close: 0, fork: 0 })
  })

  it('a phone-width viewport gets it too', () => {
    device.phoneWidth = true
    expect(rowControls()).toEqual({ more: 1, close: 0, fork: 0 })
  })

  it('a pointer device keeps the hover cluster beside the ⋯ menu', () => {
    expect(rowControls()).toEqual({ more: 1, close: 1, fork: 1 })
  })
})
