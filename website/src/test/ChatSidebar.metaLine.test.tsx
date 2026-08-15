/**
 * Chat sidebar session-row META LINE — the one-line strip above the session
 * title, reading `agent-name · tag · tag … timestamp [pin]`.
 *
 * Properties pinned here, each a deliberate choice a refactor could undo:
 *   1. the second slot shows the session's TAGS, not a value derived from the
 *      project path — every tag, each `· name` tinted with the tag's colour and
 *      NO border, because a bordered pill would read as an actionable filter,
 *   2. tags render in tag `order` so the sequence is stable,
 *   3. the "·" run appears only when there ARE tags, so an untagged session never
 *      shows a dangling dot after the agent name,
 *   4. the pin glyph is the last node in the line, after the timestamp,
 *   5. there is no separate chip row — a tag is printed exactly once, in the meta
 *      line.
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
// Legacy single-lane list (no tag columns) keeps the rows flat + easy to query.
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

const LAST_TS = '2026-08-13T18:00:00Z'

// Tag vocabulary: two plain (identity) tags plus one status tag, so the
// two plain tags exercise the multi-tag run; the status tag exercises ordering.
const TAGS = [
  { id: 'kirocrew', name: 'backend', color: '#6b7280', order: 5, status: false },
  { id: 'themes', name: 'kc-themes', color: '#6b7280', order: 6, status: false },
  { id: 'review', name: 'Review', color: '#f59e0b', order: 3, status: true },
]

const SLOTS: ChatSlot[] = [
  { key: 'k-tag', title: 'has-a-tag', running: false, messages: 2, agent: 'kirocrew', tags: ['kirocrew'], last_ts: LAST_TS },
  { key: 'k-bare', title: 'no-tags', running: false, messages: 2, agent: 'kirocrew', tags: [], last_ts: LAST_TS },
  { key: 'k-pin', title: 'is-pinned', running: false, messages: 2, agent: 'kirocrew', tags: ['themes'], pinned: true, last_ts: LAST_TS },
  // A status tag alongside a plain tag: both render in the meta line, ordered by
  // tag order (status Review at order 3 sorts ahead of the plain tag at 5).
  { key: 'k-mixed', title: 'status-plus-identity', running: false, messages: 2, agent: 'kirocrew', tags: ['review', 'kirocrew'], last_ts: LAST_TS },
] as unknown as ChatSlot[]

function renderSidebar(slots: ChatSlot[] = SLOTS, tags: Array<{ id: string; name: string; color: string; order: number; status: boolean }> = TAGS) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  qc.setQueryData(['chat-tags'], tags)
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

/** The meta line for a row, found via the title text that sits beneath it. */
function metaLineFor(container: HTMLElement, title: string): HTMLElement {
  const titleEl = [...container.querySelectorAll('div')].find(
    el => el.getAttribute('title') === title && el.className.includes('text-[13px]'),
  )
  if (!titleEl) throw new Error(`no row titled "${title}"`)
  const line = titleEl.previousElementSibling as HTMLElement | null
  if (!line?.className.includes('session-agent-label')) throw new Error(`no meta line above "${title}"`)
  return line
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — session row meta line', () => {
  it('renders the session\'s tag after the agent name, separated by "·"', () => {
    const { container } = renderSidebar()
    const line = metaLineFor(container, 'has-a-tag')
    expect(line.textContent).toContain('kirocrew')   // agent
    expect(line.textContent).toContain('·')
    expect(line.textContent).toContain('backend')    // tag name
  })

  it('renders every tag in the meta line, in tag order, tinted with the tag colour', () => {
    const { container } = renderSidebar()
    // 'status-plus-identity' carries Review (order 3) + backend (order 5). Both
    // show in the meta line, ordered by tag order (Review first), each tinted.
    const line = metaLineFor(container, 'status-plus-identity')
    expect(line.textContent).toContain('Review')
    expect(line.textContent).toContain('backend')
    expect(line.textContent!.indexOf('Review')).toBeLessThan(line.textContent!.indexOf('backend'))
    const review = [...line.querySelectorAll('span')].find(el => el.textContent === 'Review')
    expect(review!.getAttribute('style')).toContain('color')
  })

  it('gives the meta tags no border, so they read as text and not as chips', () => {
    const { container } = renderSidebar()
    const tag = [...metaLineFor(container, 'has-a-tag').querySelectorAll('span')]
      .find(el => el.textContent === 'backend')
    expect(tag).toBeTruthy()
    // The tag id wrapper carries the testid; neither it nor the tinted name span
    // may look like the old bordered chip.
    const wrapper = tag!.closest('[data-testid^="slot-tag-"]') as HTMLElement
    expect(wrapper.className).not.toMatch(/\bborder\b|\bborder-|\brounded/)
  })

  it('omits the "·" entirely when the session has no tags', () => {
    const { container } = renderSidebar()
    expect(metaLineFor(container, 'no-tags').textContent).not.toContain('·')
  })

  it('renders no separate chip row — tags live only in the meta line', () => {
    // Every tag is meta-line text now; the old bordered chip row is gone. The tag
    // must appear inside this row's meta line and NOT a second time below it.
    const { container } = renderSidebar()
    const line = metaLineFor(container, 'has-a-tag')
    expect(line.querySelector('[data-testid="slot-tag-kirocrew"]')).toBeTruthy()
    // The row's content column is the meta line's parent; scan it for a second
    // copy of the tag (a chip row would put one outside the meta line).
    const content = line.parentElement as HTMLElement
    expect(content.querySelectorAll('[data-testid="slot-tag-kirocrew"]')).toHaveLength(1)
  })

  it('places the pin glyph last in the line, after the timestamp', () => {
    const { container } = renderSidebar()
    const line = metaLineFor(container, 'is-pinned')
    const pin = line.querySelector('[title="Pinned"]')
    expect(pin).toBeTruthy()
    // Walk the line's text-bearing and glyph nodes in DOM order; the pin's
    // wrapper must be the final one, and the timestamp must precede it.
    const trailing = pin!.parentElement!
    expect(trailing.lastElementChild).toBe(pin)
    const before = [...trailing.children].slice(0, -1).map(el => el.textContent).join(' ')
    expect(before).toMatch(/\d/)   // the timestamp sits ahead of the pin
  })

  it('leaves an unpinned row with no pin glyph', () => {
    const { container } = renderSidebar()
    expect(metaLineFor(container, 'has-a-tag').querySelector('[title="Pinned"]')).toBeNull()
  })
})
