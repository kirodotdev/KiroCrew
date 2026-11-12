/**
 * Settings → Chat → Sessions → Quick Reply Card on Hover gates the sidebar's
 * needs-me hover card. On (the default) a needs-me row opens its card on hover
 * and on →; off neither does, and the row itself is unchanged.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act, fireEvent, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'

// Render framer-motion elements as plain DOM (jsdom can't run projection).
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
vi.mock('../api/client', async importActual => {
  const actual = await importActual<typeof import('../api/client')>()
  return {
    ...actual,
    SEARCH_MIN_CHARS: 2,
    api: new Proxy({} as Record<string, unknown>, { get: () => vi.fn().mockResolvedValue([]) }),
  }
})

import ChatSidebar from '../pages/ChatSidebar'
import { FINE_POINTER_QUERY, OPEN_DELAY_MS } from '../pages/chat-sidebar/NeedsMeHoverCard'
import { loadChatConfig } from '../pages/chat/ChatSettings'

// One clock for the whole file: the row's activity is a minute before it, so
// the stale-collapse threshold never folds it away.
const NOW = new Date('2026-01-01T12:00:00.000Z')
const SLOTS = [{
  key: 'choice', title: 'Pick a plan', messages: 4, running: false, mode: '', created: '', pinned: false,
  has_options: true, options: ['Upgrade both', 'Pin until next week'], last_message: 'Upgrade both now, or pin until next week?',
  last_ts: '2026-01-01T11:59:00.000Z', folder_id: null,
}]

async function renderSidebar() {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots: SLOTS, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  // Let the mocked reads settle inside act before the test drives the row.
  await act(async () => {})
  return view
}

function row(container: HTMLElement): HTMLElement {
  const el = container.querySelector<HTMLElement>('[data-slot-key="choice"] [data-session-row]')
  expect(el).toBeTruthy()
  return el!
}

function hover(el: HTMLElement) {
  fireEvent.pointerOver(el)
  act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS + 10) })
}

beforeEach(() => {
  localStorage.clear()
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(NOW)
  Object.defineProperty(window, 'matchMedia', {
    configurable: true, writable: true,
    value: (q: string) => ({
      matches: q === FINE_POINTER_QUERY, media: q, onchange: null,
      addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
    }),
  })
})
afterEach(() => { vi.useRealTimers(); vi.clearAllMocks() })

describe('Quick Reply Card on Hover setting', () => {
  it('is on for a client with no stored choice', () => {
    expect(loadChatConfig().needsMeHoverCard).toBe(true)
  })

  it('on: hovering a needs-me row opens its card', async () => {
    const { container } = await renderSidebar()
    hover(row(container))
    expect(await screen.findByTestId('needs-me-card')).toBeTruthy()
  })

  it('off: neither hover nor → opens a card, and the row still renders', async () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ needsMeHoverCard: false }))
    const { container } = await renderSidebar()
    const el = row(container)
    hover(el)
    el.focus()
    fireEvent.keyDown(el, { key: 'ArrowRight' })
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS + 10) })
    expect(screen.queryByTestId('needs-me-card')).toBeNull()
    expect(el.textContent).toContain('Pick a plan')
  })

  it('turning it off live closes the layer without a reload', async () => {
    const { container } = await renderSidebar()
    localStorage.setItem('mc-chat-config', JSON.stringify({ needsMeHoverCard: false }))
    act(() => { window.dispatchEvent(new Event('mc-config-changed')) })
    hover(row(container))
    expect(screen.queryByTestId('needs-me-card')).toBeNull()
  })
})
