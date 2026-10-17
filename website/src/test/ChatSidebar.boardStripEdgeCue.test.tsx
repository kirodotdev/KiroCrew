/**
 * The board's lane strip paints an edge cue where lanes continue past a
 * clipped edge. On a window too narrow for every lane the strip ends at the
 * sidebar's edge, and an overlay scrollbar leaves no standing sign that more
 * lanes sit past it, so without the cue a clipped lane reads as the end of
 * the board. jsdom has no layout, so the strip's scroll geometry is stubbed.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatTag, TagColumn } from '../types'
import type { RootState } from '../store'

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef<HTMLElement, Record<string, unknown> & { children?: React.ReactNode }>(
      (props, ref) => {
        const clean: Record<string, unknown> = {}
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
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false }),
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

const A = '11111111-1111-1111-1111-111111111111'
const B = '22222222-2222-2222-2222-222222222222'
const C = '33333333-3333-3333-3333-333333333333'
const D = '44444444-4444-4444-4444-444444444444'
const tags: ChatTag[] = [
  { id: A, name: 'Planned', color: '#e11', order: 0, status: true },
  { id: B, name: 'Review', color: '#1a1', order: 1, status: true },
  { id: C, name: 'Done', color: '#11e', order: 2, status: true },
  { id: D, name: 'Shipped', color: '#e1e', order: 3, status: true },
]
const columns: TagColumn[] = [
  { id: 'col-a', name: 'Planned', tag_ids: [A], mode: 'any', order: 0 },
  { id: 'col-b', name: 'Review', tag_ids: [B], mode: 'any', order: 1 },
  { id: 'col-c', name: 'Done', tag_ids: [C], mode: 'any', order: 2 },
]

function renderBoard() {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots: [], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as RootState['dashboard'],
    chat: { activeSlot: null } as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  qc.setQueryData(['chat-tags'], tags)
  qc.setQueryData(['tag-columns'], columns)
  qc.setQueryData(['chat-folders'], [])
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={[]} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...view, qc }
}

/** Gives the strip a scroll geometry and re-measures it through a scroll. */
function setStripGeometry(strip: HTMLElement, { scrollWidth, clientWidth, scrollLeft }: { scrollWidth: number; clientWidth: number; scrollLeft: number }) {
  Object.defineProperty(strip, 'scrollWidth', { configurable: true, get: () => scrollWidth })
  Object.defineProperty(strip, 'clientWidth', { configurable: true, get: () => clientWidth })
  Object.defineProperty(strip, 'scrollLeft', { configurable: true, writable: true, value: scrollLeft })
  act(() => { fireEvent.scroll(strip) })
}

afterEach(() => { localStorage.clear() })

describe('chat sidebar — board lane strip edge cues', () => {
  it('shows no cue while every lane fits', () => {
    renderBoard()
    const strip = screen.getByTestId('column-strip')
    setStripGeometry(strip, { scrollWidth: 644, clientWidth: 644, scrollLeft: 0 })
    expect(screen.queryByTestId('column-strip-cue-left')).toBeNull()
    expect(screen.queryByTestId('column-strip-cue-right')).toBeNull()
  })

  it('marks the right edge while a lane is clipped past it', () => {
    renderBoard()
    const strip = screen.getByTestId('column-strip')
    // Three 220 px lanes plus gaps and padding in a 644 px strip.
    setStripGeometry(strip, { scrollWidth: 692, clientWidth: 644, scrollLeft: 0 })
    expect(screen.getByTestId('column-strip-cue-right')).toBeInTheDocument()
    expect(screen.queryByTestId('column-strip-cue-left')).toBeNull()
  })

  // The cue is fade-only: the strip scrolls natively, so the cue draws no
  // arrow that looks clickable, holds no control and passes pointer events
  // through to the lanes under it.
  it('is a fade only: no arrow, no control, pointer events pass through', () => {
    renderBoard()
    const strip = screen.getByTestId('column-strip')
    setStripGeometry(strip, { scrollWidth: 692, clientWidth: 644, scrollLeft: 0 })
    const cue = screen.getByTestId('column-strip-cue-right')
    expect(cue.className).toContain('pointer-events-none')
    expect(cue.getAttribute('aria-hidden')).toBe('true')
    expect(cue.querySelector('button, a, [tabindex]')).toBeNull()
    expect(cue.querySelector('svg')).toBeNull()
    // The band is the session tab strip's 24 px fade, narrow enough that the
    // lane controls it overlaps stay legible, so none of them needs guarding.
    expect(cue.className).toMatch(/\bw-6\b/)
    expect(cue.className).not.toMatch(/\bfrom-\d+%/)
    // The lanes are cards, and in light theme the card and the page
    // background are both near white, so a fade from bg would show nothing
    // over them. The fade starts from the foreground, which contrasts with
    // the card in every theme.
    expect(cue.className).toMatch(/\bfrom-text-strong\/\d+\b/)
    expect(cue.className).not.toMatch(/\bfrom-bg\b/)
  })

  // The strip pads its cards by p-2, so a full-height band would also tint
  // the page gutter above and below the lanes. Both cues sit inside that
  // padding, over the cards' own vertical extent.
  it.each([['right', 0], ['left', 48]] as const)('insets the %s cue to the cards, not the strip padding', (side, scrollLeft) => {
    renderBoard()
    const strip = screen.getByTestId('column-strip')
    expect(strip.className).toMatch(/\bp-2\b/)
    setStripGeometry(strip, { scrollWidth: 692, clientWidth: 644, scrollLeft })
    const cue = screen.getByTestId(`column-strip-cue-${side}`)
    expect(cue.className).toMatch(/\btop-2\b/)
    expect(cue.className).toMatch(/\bbottom-2\b/)
    expect(cue.className).not.toMatch(/\b(top|bottom)-0\b/)
  })

  it('moves the cue to the left edge once the strip is scrolled to its end', () => {
    renderBoard()
    const strip = screen.getByTestId('column-strip')
    setStripGeometry(strip, { scrollWidth: 692, clientWidth: 644, scrollLeft: 48 })
    const cue = screen.getByTestId('column-strip-cue-left')
    expect(screen.queryByTestId('column-strip-cue-right')).toBeNull()
    // The left cue is the same fade as the right one: it holds no control
    // and passes pointer events through to the first lane under it.
    expect(cue.className).toContain('pointer-events-none')
    expect(cue.getAttribute('aria-hidden')).toBe('true')
    expect(cue.className).toMatch(/\bfrom-text-strong\/\d+\b/)
  })

  // A lane added while the strip keeps its box changes only the content
  // width, so no scroll event and no box resize reports it. The column
  // count re-measures the strip.
  it('marks the right edge when a new lane overflows the strip, with no scroll or resize', async () => {
    const { qc } = renderBoard()
    const strip = screen.getByTestId('column-strip')
    let scrollWidth = 644
    Object.defineProperty(strip, 'scrollWidth', { configurable: true, get: () => scrollWidth })
    Object.defineProperty(strip, 'clientWidth', { configurable: true, get: () => 644 })
    Object.defineProperty(strip, 'scrollLeft', { configurable: true, writable: true, value: 0 })
    act(() => { fireEvent.scroll(strip) })
    expect(screen.queryByTestId('column-strip-cue-right')).toBeNull()

    // One more 220 px lane plus its 8 px gap, and no event on the strip.
    scrollWidth = 644 + 228
    // The query cache notifies its observers on a timer, so the update is
    // flushed inside an async act.
    await act(async () => {
      qc.setQueryData(['tag-columns'], [
        ...columns,
        { id: 'col-d', name: 'Shipped', tag_ids: [D], mode: 'any', order: 3 },
      ])
    })
    // The same strip node, now holding the fourth lane.
    expect(screen.getByTestId('column-strip')).toBe(strip)
    expect(strip.children).toHaveLength(4)
    expect(screen.getByTestId('column-strip-cue-right')).toBeInTheDocument()
    expect(screen.queryByTestId('column-strip-cue-left')).toBeNull()
  })
})
