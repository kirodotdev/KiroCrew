/**
 * Batch selection in the real Sessions sidebar: Shift-click picks a range of rows,
 * the picked rows say so to sight and to assistive tech, a click on a picked list
 * toggles instead of opening, and the bar counts what is picked.
 *
 * The gesture rules themselves are pinned in pages/chat-sidebar/selection.test.tsx;
 * this file pins that the sidebar's rows, its root handlers and its bar are wired
 * to them.
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'

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
// Legacy single-lane list (no tag columns) keeps the rows flat and easy to query.
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: () => vi.fn().mockResolvedValue([]),
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

const KEYS = ['k-a', 'k-b', 'k-c', 'k-d']
const SLOTS = KEYS.map(key => ({ key, title: `Session ${key}`, running: false }))
const NO_UNREAD: string[] = []

function renderSidebar(activeSlot: string | null = null) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots: SLOTS, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot, slotStatusDetail: {}, subagents: {}, slotActivity: {},
      subagentQueued: {}, goalLoops: {}, workflowRuns: {},
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  const onSelectSlot = vi.fn()
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS} activeSlot={activeSlot} unreadSlots={NO_UNREAD} onSelectSlot={onSelectSlot}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { onSelectSlot }
}

/** The rows in the order the lane draws them. */
const drawnRows = () => Array.from(document.querySelectorAll<HTMLElement>('[data-session-row]'))
const rowOf = (key: string) => document.querySelector<HTMLElement>(`[data-session-row="${key}"]`) as HTMLElement
const pressed = () => drawnRows().filter(r => r.getAttribute('aria-pressed') === 'true').map(r => r.dataset.sessionRow)

describe('batch selection in the Sessions sidebar', () => {
  it('shows no bar and marks no row at rest', () => {
    renderSidebar()
    expect(drawnRows().map(r => r.dataset.sessionRow).sort()).toEqual(KEYS)
    expect(screen.queryByTestId('session-selection-bar')).toBeNull()
    expect(drawnRows().every(r => !r.hasAttribute('aria-pressed'))).toBe(true)
  })

  it('picks the drawn range between two Shift-clicks, marks it and counts it', () => {
    const { onSelectSlot } = renderSidebar()
    const order = drawnRows().map(r => r.dataset.sessionRow as string)
    fireEvent.click(rowOf(order[0]), { shiftKey: true })
    fireEvent.click(rowOf(order[2]), { shiftKey: true })

    expect(pressed()).toEqual(order.slice(0, 3))
    expect(rowOf(order[1]).className).toContain('session-selected')
    // The unpicked row is a toggle too while rows are picked, and says it is off.
    expect(rowOf(order[3]).getAttribute('aria-pressed')).toBe('false')
    expect(rowOf(order[3]).className).not.toContain('session-selected')
    expect(screen.getByTestId('session-selection-count')).toHaveTextContent('3 selected')
    expect(onSelectSlot).not.toHaveBeenCalled()
  })

  it('starts a first range at the open session', () => {
    renderSidebar('k-b')
    const order = drawnRows().map(r => r.dataset.sessionRow as string)
    const open = order.indexOf('k-b')
    const far = open === order.length - 1 ? 0 : order.length - 1
    fireEvent.click(rowOf(order[far]), { shiftKey: true })
    expect(pressed()).toEqual(order.slice(Math.min(open, far), Math.max(open, far) + 1))
    expect(screen.getByTestId('session-selection-status')).toHaveTextContent(`${Math.abs(far - open) + 1} selected`)
  })

  it('toggles on a click while rows are picked, and opens again once none is', () => {
    const { onSelectSlot } = renderSidebar()
    const order = drawnRows().map(r => r.dataset.sessionRow as string)
    fireEvent.click(rowOf(order[1]), { shiftKey: true })
    fireEvent.click(rowOf(order[3]))
    expect(pressed()).toEqual([order[1], order[3]])
    expect(screen.getByTestId('session-selection-count')).toHaveTextContent('2 selected')

    fireEvent.click(screen.getByTestId('session-selection-clear'))
    expect(screen.queryByTestId('session-selection-bar')).toBeNull()
    expect(onSelectSlot).not.toHaveBeenCalled()
    fireEvent.click(rowOf(order[3]))
    expect(onSelectSlot).toHaveBeenCalledWith(order[3])
  })

  it('drops the selection on Escape from a row', () => {
    renderSidebar()
    const first = drawnRows()[0]
    fireEvent.click(first, { shiftKey: true })
    expect(screen.getByTestId('session-selection-bar')).toBeInTheDocument()
    fireEvent.keyDown(first, { key: 'Escape' })
    expect(screen.queryByTestId('session-selection-bar')).toBeNull()
  })

  it('picks a row from its menu, the entry a touch screen has', () => {
    renderSidebar()
    const target = drawnRows()[1]
    fireEvent.keyDown(within(target).getByRole('button', { name: 'More options' }), { key: 'Enter' })
    fireEvent.click(screen.getByRole('menuitem', { name: 'Select' }))
    expect(pressed()).toEqual([target.dataset.sessionRow])
    expect(screen.getByTestId('session-selection-count')).toHaveTextContent('1 selected')
  })
})
