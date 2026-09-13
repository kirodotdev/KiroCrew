/**
 * Chat sidebar session-row STATUS MARKER — the single glyph (spinner, bot, shield,
 * question, loop, unread dot) that LEADS the row's secondary line, immediately in
 * front of the words naming the same state ("Thinking…", "3 agents running",
 * "Needs approval").
 *
 * What is pinned here, and why each would regress silently:
 *   1. exactly ONE marker per row, built inside the branch that also writes the
 *      words, so glyph and secondary line can never name different states,
 *   2. an owed decision (approval, question) outranks every "working" signal — a
 *      decision rendered as work in progress is how an owed approval is missed,
 *   3. `running` draws a SPINNER, not a dot,
 *   4. the unread "your turn" dot leads the secondary line too — NOT absolutely
 *      positioned at the row's right edge — and yields to any more specific state,
 *   5. there is NO absolute status gutter at the row's left edge. The marker used to
 *      live there, inside the row's `pl-3.5`, occupying x 1..13. That band is shared
 *      with two decorations that paint over it: the recency tint (an opaque accent
 *      stripe up to 7px wide, `recencyTintShadow`) and the session-colour bar (2px,
 *      `.session-colored::before`). An accent spinner on an accent stripe is a 1:1
 *      contrast, so a recent session's glyph lost its left half and read as clipped
 *      and mis-placed. Inline, the marker starts at the content column and clears
 *      both by construction. The alignment guides are unaffected either way — the
 *      gutter was out of flow (ChatSidebar.folderAlignment.test.tsx),
 *   6. a marker sitting in front of its own visible label is DECORATIVE
 *      (`aria-hidden`), so the state is announced once, not twice. The unread dot is
 *      the exception: the words it leads are `last_message`, which do not name it, so
 *      it keeps a real accessible name.
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

/** With no gutter, the content column is the row's FIRST child. */
function colOf(container: HTMLElement): HTMLElement {
  const row = container.querySelector('.session-row')
  if (!row) throw new Error('no .session-row rendered')
  return row.firstElementChild as HTMLElement
}

/** The secondary line. The column's children are [meta, headline, secondary]. */
function lineOf(container: HTMLElement): HTMLElement | null {
  return (colOf(container).children[2] as HTMLElement | undefined) ?? null
}

/** The marker itself — only when it really is the line's FIRST child.
 *
 *  `firstElementChild` alone is NOT enough and this was proven by mutation: the
 *  `running` branch renders its label as a bare text node, so moving the glyph
 *  AFTER the text left `firstElementChild` pointing at it and the assertion green.
 *  The marker must be the first child NODE, text included. */
function markerOf(container: HTMLElement): HTMLElement | null {
  const line = lineOf(container)
  const first = line?.firstElementChild as HTMLElement | null
  if (!line || !first || line.firstChild !== first) return null
  const isGlyph = first.tagName.toLowerCase() === 'svg'
    || first.classList.contains('rounded-full')
  return isGlyph ? first : null
}

const slot = (over: Partial<ChatSlot> = {}): ChatSlot =>
  ({ key: 'k1', title: 'a-session', running: false, messages: 2, agent: 'kirocrew', ...over }) as ChatSlot

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — status marker leads the secondary line', () => {
  it('draws a spinner, not a dot, while the agent is working', () => {
    const { container } = renderSidebar([slot({ running: true })])
    const line = lineOf(container)!
    expect(line.querySelector('.animate-spin')).toBeTruthy()
    // A pulsing dot is what the spinner replaces; it must not come back.
    expect(line.querySelector('.rounded-full')).toBeNull()
  })

  it('puts the marker FIRST in the line, in front of the words it marks', () => {
    // This ordering is the change: the glyph reads as the lead-in to "Thinking…",
    // not as a mark floating somewhere else in the row.
    const { container } = renderSidebar([slot({ running: true })])
    const line = lineOf(container)!
    expect(markerOf(container)).toBeTruthy()
    expect(line.firstElementChild!.classList.contains('animate-spin')).toBe(true)
    // Nothing at all before it — not even a text node. The running branch writes
    // its label as bare text, so an element-only check passes with the glyph
    // trailing the words (verified by mutation).
    expect(line.firstChild).toBe(line.firstElementChild)
    // It must not be squeezed out when the words are long: a 10px glyph in a flex
    // row without `shrink-0` collapses toward zero width.
    expect(line.firstElementChild!.className).toMatch(/\bshrink-0\b/)
    // The line lays its children out as a row, which is also what gives the unread
    // dot (`w-2 h-2`) its box — as an inline child both dimensions are dropped.
    expect(line.className).toMatch(/\bflex\b/)
    expect(line.className).toMatch(/\bitems-center\b/)
  })

  it('puts the unread "your turn" dot in the same lead position', () => {
    const { container } = renderSidebar([slot({ last_message: 'done' })], ['k1'])
    const line = lineOf(container)!
    expect(line.firstElementChild!.className).toMatch(/\brounded-full\b/)
    expect(line.textContent).toContain('done')
    // The old treatment was an absolutely-positioned dot pinned to the right edge.
    expect(container.querySelector('.session-row .absolute.right-1\\.5.rounded-full')).toBeNull()
  })

  it('renders the line for an unread row that has said nothing yet', () => {
    // The dot IS the content then; dropping the line would drop the only signal.
    const { container } = renderSidebar([slot()], ['k1'])
    expect(markerOf(container)).toBeTruthy()
  })

  it('leaves NO absolute status gutter at the row left edge', () => {
    const { container } = renderSidebar([slot({ running: true })])
    const row = container.querySelector('.session-row') as HTMLElement
    // The gutter was `absolute left-px w-3 h-3` with an inline `top`. Nothing may
    // sit in that band again: the recency tint and the session-colour bar paint
    // there, and an accent glyph over an accent stripe is invisible.
    expect(row.querySelector('.absolute.left-px')).toBeNull()
    for (const el of Array.from(row.children)) {
      expect(el.className.includes('left-px')).toBe(false)
    }
    // The content column is the row's first child now, and it is in flow.
    expect(colOf(container).className).toMatch(/\bflex-1\b/)
    expect(colOf(container).className).not.toMatch(/\babsolute\b/)
  })

  it('keeps the row left pad as the WHOLE content offset, so the guides hold', () => {
    // Removing an out-of-flow box moves no x — but a replacement in FLOW would,
    // which is exactly what #3766 did (12px + a gap onto the content column).
    // Asserted here as well as in folderAlignment so this move cannot later be
    // "fixed" by putting a spacer back at the row's left edge.
    const { container } = renderSidebar([slot({ running: true })])
    const row = container.querySelector('.session-row') as HTMLElement
    expect(row.className).toMatch(/\bpl-3\.5\b/)
    expect(row.children[0]).toBe(colOf(container))
  })

  it('keeps every line box on the 4px grid', () => {
    // The row's type scale is an ARITHMETIC contract, not a taste setting, so it
    // is asserted as arithmetic rather than as three remembered class names.
    // Read off the rendered classes: a future edit that reaches for a ratio
    // (`leading-snug`) instead of an explicit box fails here rather than shipping
    // a row that silently drifts off the grid again.
    const { container } = renderSidebar([slot({ running: true })])
    const row = container.querySelector('.session-row') as HTMLElement
    const col = colOf(container)
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

    // 3. The marker lives INSIDE the secondary line's box, so it cannot change any
    //    height: 10px of glyph in a 16px line. That is what replaced the gutter's
    //    derived `top` offset — there is no independent y left to keep in sync.
    expect(boxOf(status)).toBeGreaterThan(10)

    // 4. The headline outranks both neighbours by enough to READ as a headline.
    //    The previous 11/13/12 scale sat within 2px, and CJK glyphs fill their em
    //    box, so the secondary line competed with the title instead of yielding.
    expect(sizeOf(title)).toBeGreaterThanOrEqual(sizeOf(meta) + 3)
    expect(sizeOf(title)).toBeGreaterThan(sizeOf(status))

    // 5. And it never wraps, which is what keeps the row height a constant.
    expect(title.className).toMatch(/\btruncate\b/)
    expect(title.className).not.toMatch(/line-clamp/)
  })

  it('renders exactly one status marker, never two', () => {
    // Running AND unread: the old right-edge dot coexisted with the running
    // glyph. One row cannot show both.
    const { container } = renderSidebar([slot({ running: true })], ['k1'])
    const row = container.querySelector('.session-row')!
    expect(row.querySelectorAll('.animate-spin, .rounded-full')).toHaveLength(1)
  })

  it('ranks an owed approval above every working signal', () => {
    const { container } = renderSidebar([slot({ running: true, pending_approval: true })], ['k1'])
    const line = lineOf(container)!
    expect(line.textContent).toContain('Needs approval')
    expect(line.querySelector('.animate-spin')).toBeNull()   // not "working"
    // A shield, not a bare dot: an owed decision earns a shape of its own, and the
    // accent "your turn" dot is the only bare dot left. A shield also reads as
    // "permission" rather than "message", which is what an approval actually is —
    // it gates a tool call, it is not something to reply to.
    expect(line.querySelector('.lucide-shield-check')).toBeTruthy()
    expect(line.querySelector('.rounded-full')).toBeNull()
  })

  it('ranks an unanswered question above working, and below an approval', () => {
    const { container: q } = renderSidebar([slot({ running: true, needs_input: true })])
    expect(lineOf(q)!.textContent).toContain('Needs your answer')
    // A question mark, not the plain speech bubble the channel-origin glyph uses
    // elsewhere in this row — an owed answer must not look like provenance.
    expect(lineOf(q)!.querySelector('.lucide-message-circle-question-mark')).toBeTruthy()

    const { container: both } = renderSidebar([slot({ needs_input: true, pending_approval: true })])
    expect(lineOf(both)!.textContent).toContain('Needs approval')
  })

  it('marks a goal loop with the Goal icon, not a bare dot', () => {
    // A loop is a distinct MODE, so it earns a distinct mark; the pulsing dot it
    // replaced was indistinguishable from the unread dot at a glance.
    const { container } = renderSidebar(
      [slot()],
      [],
      { goalLoops: { k1: { active: true, cycle_count: 7, max_cycles: 24 } } },
    )
    const line = lineOf(container)!
    expect(line.textContent).toContain('Loop 7/24')
    expect(line.querySelector('.lucide-goal')).toBeTruthy()
    expect(line.querySelector('.rounded-full')).toBeNull()
  })

  it('hides a state glyph from the a11y tree, since its own label is right beside it', () => {
    // The gutter needed `role="img"` + `aria-label` because the glyph was alone in
    // it. In front of its own words, that name would be announced twice.
    const { container } = renderSidebar([slot({ running: true })])
    const marker = markerOf(container)!
    expect(marker.getAttribute('aria-hidden')).toBe('true')
    expect(marker.getAttribute('role')).not.toBe('img')
  })

  it('keeps a real accessible name on the unread dot, which leads no label', () => {
    // Its line is `last_message` — those words say what the agent said, not that
    // the turn came back to you, so this marker is the only thing carrying that.
    const { container } = renderSidebar([slot({ last_message: 'done' })], ['k1'])
    const dot = markerOf(container)!
    expect(dot.getAttribute('role')).toBe('img')
    expect(dot.getAttribute('aria-label')).toBe('Agent finished — your turn')
    expect(dot.getAttribute('title')).toBe('Agent finished — your turn')
  })

  it('keeps the coloured text label in the secondary line', () => {
    // The marker joining the line must not swallow its words — "Needs approval"
    // has to stay readable as a phrase.
    const { getByText } = renderSidebar([slot({ pending_approval: true })])
    expect(getByText('Needs approval')).toBeTruthy()
  })

  // ── Marker and words are ONE node per branch (#3830) ──────────────────
  //
  // The two used to be independent ternary chains a few hundred lines apart, with
  // comments asserting they "can never disagree" and nothing enforcing it. Then
  // they became one resolver with two fields; now they are one JSX node, so a
  // branch cannot ship a glyph without its phrase. These drive every branch of the
  // shared precedence and check that the marker and the words agree.

  it.each([
    // [name, slot overrides, expected line fragment, marker must be a spinner?]
    ['pending approval', { pending_approval: true, running: true }, 'Needs approval', false],
    ['needs input', { needs_input: true, running: true }, 'Needs your answer', false],
    ['running', { running: true }, 'Thinking', true],
  ] as const)(
    'marker and words name the same state: %s',
    (_name, over, fragment, spinner) => {
      const { container } = renderSidebar([slot(over)])
      const line = lineOf(container)!
      expect(line.textContent).toContain(fragment)
      // Exactly one glyph, and it is the one this state owns.
      expect(line.querySelectorAll('svg, .rounded-full')).toHaveLength(1)
      expect(!!line.querySelector('.animate-spin')).toBe(spinner)
      expect(markerOf(container)).toBeTruthy()
    },
  )

  it('shows the last message with no marker on an idle, read row', () => {
    // The tail: an idle row that has been read carries words and nothing else.
    const { container } = renderSidebar([slot({ last_message: 'done' })])
    const line = lineOf(container)!
    expect(line.textContent).toContain('done')
    expect(line.querySelectorAll('svg, .rounded-full')).toHaveLength(0)
  })
})
