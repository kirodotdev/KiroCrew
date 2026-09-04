/**
 * Regression: clicking inside the inline session-rename input
 * did not move the caret to the click point. Root cause: the session row is
 * draggable, and a draggable ancestor makes the browser treat a mousedown in a
 * descendant text input as a drag-gesture start, suppressing native caret
 * placement. Fix: the row is non-draggable while that row is being renamed
 * (draggable={renamingSlot !== s.key}).
 *
 * jsdom can't exercise real caret placement, so the load-bearing assertion is
 * the invariant the fix turns on: the row's draggable attribute is "false"
 * while its rename input is open, and "true" otherwise.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { fireEvent, render, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import type { RootState } from '../store'
import { ThemeProvider } from '../hooks/useTheme'

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
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({}, { get: () => vi.fn().mockResolvedValue([]) }),
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

const SLOT_KEY = 'chat-rename-1'
const slot = { key: SLOT_KEY, title: 'My Session Title', running: false, tags: [], created: '', last_ts: '' }

function renderSidebar(connected = false, onSelectSlot?: (key: string) => void) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected, slots: [slot], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(['chat-tags'], [])
  qc.setQueryData(['tag-columns'], [])
  qc.setQueryData(['chat-folders'], [])
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={[slot]} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
              onSelectSlot={onSelectSlot}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

function rowFor(container: HTMLElement): HTMLElement {
  const wrap = container.querySelector(`[data-slot-key="${SLOT_KEY}"]`)
  expect(wrap).toBeTruthy()
  const row = (wrap as HTMLElement).querySelector('.session-row')
  expect(row).toBeTruthy()
  return row as HTMLElement
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

describe('session row drag is disabled during inline rename', () => {
  // The legacy lane uses dnd-kit drag; the logical drag-enabled state is
  // surfaced via data-draggable (dnd-kit's `disabled` prop is otherwise not
  // reflected as a DOM attribute). Drag must be off during rename so the input
  // gets native click-to-place-caret.
  it('row is draggable before rename', () => {
    const { container } = renderSidebar()
    expect(rowFor(container).getAttribute('data-draggable')).toBe('true')
  })

  it('double-clicking the title selects once, opens rename, and disables dragging', () => {
    const onSelectSlot = vi.fn()
    const { container } = renderSidebar(true, onSelectSlot)
    const row = rowFor(container)
    expect(row.getAttribute('data-draggable')).toBe('true')

    const title = within(row).getByTitle('My Session Title')
    fireEvent.click(title, { detail: 1 })
    fireEvent.click(title, { detail: 2 })
    fireEvent.doubleClick(title, { detail: 2 })

    expect(onSelectSlot).toHaveBeenCalledTimes(1)
    expect(onSelectSlot).toHaveBeenCalledWith(SLOT_KEY)
    // Same row node, now rendering the existing inline editor; drag must be off
    // so the browser gives the textarea native click-to-place-caret behavior.
    expect(rowFor(container).getAttribute('data-draggable')).toBe('false')
    const input = within(rowFor(container)).getByRole('textbox') as HTMLTextAreaElement
    expect(input.value).toBe('My Session Title')
    // select-text overrides the row's inherited select-none so click-drag
    // selection works in the field — the other half of the fix.
    expect(input.className).toContain('select-text')
  })
})
