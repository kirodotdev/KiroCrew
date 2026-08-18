/**
 * Chat sidebar session-row STATUS GUTTER — the fixed-width slot left of the
 * headline that holds the row's single status glyph.
 *
 * What is pinned here, and why each would regress silently:
 *   1. exactly ONE glyph per row, drawn from the subtitle chain's own precedence,
 *      so glyph and secondary line can never name different states,
 *   2. an owed decision (approval, question) outranks every "working" signal — a
 *      decision rendered as work in progress is how an owed approval is missed,
 *   3. `running` draws a SPINNER, not a dot,
 *   4. the unread "your turn" dot lives in the gutter, NOT absolutely positioned
 *      at the row's right edge, and yields to any more specific state,
 *   5. the gutter is OUT OF FLOW — absolutely positioned inside the row's own
 *      `pl-3.5`, and anchored to the headline at a fixed offset. In flow it added
 *      its 12px width plus a gap to where the content column starts, which is what
 *      broke the sidebar's two left-edge guides
 *      (ChatSidebar.folderAlignment.test.tsx); centred on the ROW instead, it fell
 *      below the headline on any row carrying a chip row,
 *   6. the glyph carries an accessible name — it is the gutter's only content.
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
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

function renderSidebar(slots: ChatSlot[], unread: string[] = [], chat: Record<string, unknown> = {}) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: unread, updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, ...chat } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={null} unreadSlots={unread}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

/** The gutter is the row's FIRST child; the content column is its sibling. */
function gutterOf(container: HTMLElement): HTMLElement {
  const row = container.querySelector('.session-row')
  if (!row) throw new Error('no .session-row rendered')
  return row.firstElementChild as HTMLElement
}

const slot = (over: Partial<ChatSlot> = {}): ChatSlot =>
  ({ key: 'k1', title: 'a-session', running: false, messages: 2, agent: 'kirocrew', ...over }) as ChatSlot

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — status gutter', () => {
  it('draws a spinner, not a dot, while the agent is working', () => {
    const { container } = renderSidebar([slot({ running: true })])
    const g = gutterOf(container)
    expect(g.querySelector('.animate-spin')).toBeTruthy()
    // A pulsing dot is what the spinner replaces; it must not come back.
    expect(g.querySelector('.rounded-full')).toBeNull()
  })

  it('puts the unread "your turn" dot in the gutter, not at the row\'s right edge', () => {
    const { container } = renderSidebar([slot()], ['k1'])
    expect(gutterOf(container).querySelector('.rounded-full')).toBeTruthy()
    // The old treatment was an absolutely-positioned dot pinned right.
    expect(container.querySelector('.session-row .absolute.right-1\\.5.rounded-full')).toBeNull()
  })

  it('keeps the gutter out of the content flow, anchored to the headline, so the left edges hold', () => {
    const { container } = renderSidebar([slot({ last_message: 'done' })])
    const g = gutterOf(container)
    expect(g).toBeTruthy()
    expect(g.className).toMatch(/\bw-3\b/)        // width still declared
    expect(g.querySelector('svg, .rounded-full')).toBeNull()   // but nothing drawn
    expect(g.getAttribute('aria-hidden')).toBe('true')
    // OUT OF FLOW. As a flex child this box cost the content column 12px + a gap
    // and pushed every session's text off the folder name's x; the row's own
    // `pl-3.5` is what reserves the space now. Asserted as classes because jsdom
    // has no layout engine to measure the offset with.
    expect(g.className).toMatch(/\babsolute\b/)
    expect(g.className).toMatch(/\bleft-px\b/)
    expect(g.className).not.toMatch(/\bshrink-0\b/)
    // Anchored to the HEADLINE at a fixed offset, NOT centred on the row. Row
    // centring put the glyph under the headline on any row taller than three
    // lines (a `source_links` chip row adds ~18px), and was never exact even
    // without one: headline-centre equals row-centre only when the meta and
    // secondary line boxes match. It is a literal because the row's type scale
    // is fixed and the headline no longer wraps.
    expect(g.className).not.toMatch(/\btop-1\/2\b/)
    expect(g.className).not.toMatch(/-translate-y-1\/2/)
    expect((g as HTMLElement).style.top).toBe('24px')
  })

  it('keeps every line box on the 4px grid, and derives the gutter offset from them', () => {
    // The row's type scale is an ARITHMETIC contract, not a taste setting, so it
    // is asserted as arithmetic rather than as three remembered class names.
    // Read off the rendered classes: a future edit that reaches for a ratio
    // (`leading-snug`) instead of an explicit box fails here rather than shipping
    // a row that silently drifts off the grid again.
    const { container } = renderSidebar([slot({ running: true })])
    const row = container.querySelector('.session-row') as HTMLElement
    const col = row.children[1] as HTMLElement
    const boxOf = (el: Element) => {
      const m = /leading-\[(\d+)px\]/.exec(el.className)
      if (!m) throw new Error(`no explicit line box on: ${el.className}`)
      return Number(m[1])
    }
    const sizeOf = (el: Element) => {
      const m = /text-\[(\d+)px\]/.exec(el.className)
      if (!m) throw new Error(`no explicit font size on: ${el.className}`)
      return Number(m[1])
    }
    const [meta, title, status] = [col.children[0], col.children[1], col.children[2]]

    // `py-2` — the row's own vertical padding, the only term not read off a line
    // box, and a grid multiple itself so the FIRST line starts on a grid line too.
    // `py-1.5` (6px) kept the row height a multiple of 4 while putting every edge
    // inside it 2px off, which is a grid on paper only.
    expect(row.className).toMatch(/\bpy-2\b/)
    const PAD = 8

    // 1. Every line box is a whole number of grid units — AND so is the padding,
    //    which is what puts each line's own top edge on a grid line rather than
    //    merely making the rows stack correctly.
    expect(PAD % 4).toBe(0)
    for (const el of [meta, title, status]) expect(boxOf(el) % 4).toBe(0)

    // 2. So is the row, which is what makes consecutive rows stack on the grid
    //    instead of accumulating fractional drift.
    const rowH = PAD * 2 + boxOf(meta) + boxOf(title) + boxOf(status)
    expect(rowH % 4).toBe(0)
    // Every interior edge, cumulatively — the check that `py-1.5` failed 56 times
    // out of 64 while the row height alone still looked correct.
    let y = PAD
    for (const el of [meta, title, status]) { expect(y % 4).toBe(0); y += boxOf(el) }
    expect(y % 4).toBe(0)
    expect(rowH).toBe(64)

    // 3. The gutter's offset is that geometry, not an independent guess: its 12px
    //    box centred on the headline's box. Note what is NOT asserted — that the
    //    first and last boxes match. Row-centring needed that equality (it is the
    //    only way headline-centre coincides with row-centre); anchoring to the
    //    headline does not, which is what lets the meta line take the tightest box.
    const GUTTER_BOX = 12
    expect(row.children[0].className).toMatch(/\bw-3\b/)
    expect((row.children[0] as HTMLElement).style.top)
      .toBe(`${PAD + boxOf(meta) + (boxOf(title) - GUTTER_BOX) / 2}px`)

    // 4. The headline outranks both neighbours by enough to READ as a headline.
    //    The previous 11/13/12 scale sat within 2px, and CJK glyphs fill their em
    //    box, so the secondary line competed with the title instead of yielding.
    expect(sizeOf(title)).toBeGreaterThanOrEqual(sizeOf(meta) + 3)
    expect(sizeOf(title)).toBeGreaterThan(sizeOf(status))

    // 5. And it never wraps, which is what keeps the row height a constant and
    //    the offset in (3) valid for every row.
    expect(title.className).toMatch(/\btruncate\b/)
    expect(title.className).not.toMatch(/line-clamp/)
  })

  it('renders exactly one status glyph, never two', () => {
    // Running AND unread: the old right-edge dot coexisted with the running
    // subtitle glyph. One slot cannot show both.
    const { container } = renderSidebar([slot({ running: true })], ['k1'])
    const row = container.querySelector('.session-row')!
    expect(row.querySelectorAll('.animate-spin, .rounded-full')).toHaveLength(1)
  })

  it('ranks an owed approval above every working signal', () => {
    const { container } = renderSidebar([slot({ running: true, pending_approval: true })], ['k1'])
    const g = gutterOf(container)
    expect(g.getAttribute('aria-label')).toBe('Needs approval')
    expect(g.querySelector('.animate-spin')).toBeNull()   // not "working"
    // A shield, not a bare dot: an owed decision earns a shape of its own, and
    // the accent "your turn" dot is now the only bare dot in the gutter. A shield
    // also reads as "permission" rather than "message", which is what an approval
    // actually is — it gates a tool call, it is not something to reply to.
    expect(g.querySelector('.lucide-shield-check')).toBeTruthy()
    expect(g.querySelector('.rounded-full')).toBeNull()
  })

  it('ranks an unanswered question above working, and below an approval', () => {
    const { container: q } = renderSidebar([slot({ running: true, needs_input: true })])
    expect(gutterOf(q).getAttribute('aria-label')).toBe('Needs your answer')
    // A question mark, not the plain speech bubble the channel-origin glyph uses
    // elsewhere in this row — an owed answer must not look like provenance.
    expect(gutterOf(q).querySelector('.lucide-message-circle-question-mark')).toBeTruthy()

    const { container: both } = renderSidebar([slot({ needs_input: true, pending_approval: true })])
    expect(gutterOf(both).getAttribute('aria-label')).toBe('Needs approval')
  })

  it('marks a goal loop with the Goal icon, not a bare dot', () => {
    // A loop is a distinct MODE, so it earns a distinct mark; the pulsing dot it
    // replaced was indistinguishable from the unread dot at a glance.
    const { container } = renderSidebar(
      [slot()],
      [],
      { goalLoops: { k1: { active: true, cycle_count: 7, max_cycles: 24 } } },
    )
    const g = gutterOf(container)
    expect(g.getAttribute('aria-label')).toBe('Loop 7/24')
    expect(g.querySelector('.lucide-goal')).toBeTruthy()
    expect(g.querySelector('.rounded-full')).toBeNull()
  })

  it('gives the glyph an accessible name, since it is the gutter\'s only content', () => {
    const { container } = renderSidebar([slot()], ['k1'])
    const g = gutterOf(container)
    expect(g.getAttribute('role')).toBe('img')
    expect(g.getAttribute('aria-label')).toBe('Agent finished — your turn')
  })

  it('keeps the coloured text label in the secondary line', () => {
    // The glyph moving to the gutter must not take its label with it — "Needs
    // approval" has to stay readable as a phrase.
    const { getByText } = renderSidebar([slot({ pending_approval: true })])
    expect(getByText('Needs approval')).toBeTruthy()
  })

  // ── Glyph and subtitle are derived from ONE resolver (#3830) ──────────
  //
  // The two used to be independent ternary chains a few hundred lines apart,
  // with comments asserting they "can never disagree" and nothing enforcing
  // it. These drive every branch of the shared precedence and check that the
  // gutter and the secondary line name the SAME state — the property the
  // comments only claimed.

  const subtitleOf = (container: HTMLElement): string => {
    const col = gutterOf(container).nextElementSibling as HTMLElement
    // children: [meta line, headline, secondary line]
    return (col.children[2] as HTMLElement | undefined)?.textContent ?? ''
  }

  it.each([
    // [name, slot overrides, expected subtitle fragment, gutter must be a spinner?]
    ['pending approval', { pending_approval: true, running: true }, 'Needs approval', false],
    ['needs input', { needs_input: true, running: true }, 'Needs your answer', false],
    ['running', { running: true }, 'Thinking', true],
  ] as const)(
    'gutter and subtitle name the same state: %s',
    (_name, over, fragment, spinner) => {
      const { container } = renderSidebar([slot(over)])
      const g = gutterOf(container)
      expect(subtitleOf(container)).toContain(fragment)
      // The gutter drew exactly one glyph, and it is the one this state owns.
      expect(g.querySelectorAll('svg, .rounded-full')).toHaveLength(1)
      expect(!!g.querySelector('.animate-spin')).toBe(spinner)
      // The gutter's accessible name is the state's own label, not a
      // lower-precedence one.
      expect(g.getAttribute('aria-label')).toBeTruthy()
    },
  )

  it('an owed approval outranks running in BOTH representations at once', () => {
    // The desync this guards: a branch reordered in one chain but not the
    // other would show a spinner beside "Needs approval", or vice versa.
    const { container } = renderSidebar([slot({ running: true, pending_approval: true })], ['k1'])
    expect(subtitleOf(container)).toContain('Needs approval')
    expect(gutterOf(container).querySelector('.animate-spin')).toBeNull()
  })

  it('the gutter tail is unread while the subtitle tail is last_message', () => {
    // The one place the two legitimately differ, so collapsing them onto a
    // single chain would have been wrong: an idle row with a last message
    // shows that message and NO gutter glyph; an idle unread row shows the
    // dot. Both tails are consumer-owned and must stay reachable.
    const { container: idle } = renderSidebar([slot({ last_message: 'done' })])
    expect(subtitleOf(idle)).toContain('done')
    expect(gutterOf(idle).querySelectorAll('svg, .rounded-full')).toHaveLength(0)

    const { container: unread } = renderSidebar([slot({ last_message: 'done' })], ['k1'])
    expect(gutterOf(unread).querySelector('.rounded-full')).toBeTruthy()
    expect(subtitleOf(unread)).toContain('done')
  })

  it('leaves no inline glyph beside the secondary line', () => {
    const { container } = renderSidebar([slot({ running: true })])
    const col = gutterOf(container).nextElementSibling as HTMLElement
    // children: [meta line, headline, secondary line]
    const sub = col.children[2]
    expect(sub?.textContent).toContain('Thinking')
    expect(sub?.querySelector('svg, .rounded-full')).toBeNull()
  })
})
