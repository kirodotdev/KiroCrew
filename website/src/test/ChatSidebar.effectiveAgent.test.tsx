/**
 * Chat sidebar session row — the EFFECTIVE-AGENT marker.
 *
 * The row names the agent a session is bound to, and that name is stored
 * verbatim on purpose: it is the user's intent, and rewriting it on disk was
 * destructive (the resolution behind such a rewrite can be momentarily stale
 * while the overwrite is permanent). The consequence is that the row can
 * advertise an agent that is not running — an app agent that was removed, or one
 * whose registration has not landed — and the user only discovers it turns later,
 * when none of that agent's tools are there.
 *
 * `effective_agent` closes that gap without touching the stored binding. It is a
 * POSITIVE CLAIM and nothing else, which is what these tests pin:
 *
 *   1. a non-empty value that differs from the displayed agent renders a marker,
 *   2. absent / "" / equal renders NOTHING — because the backend also reports ""
 *      when resolution is simply unsettled (a cold snapshot during boot), so
 *      inequality alone would flash "your agent was substituted" on a healthy
 *      install, and rows arriving from persisted or optimistically-added state
 *      predate the field entirely,
 *   3. the marker is plain text, not a colour or an icon: legible with colour
 *      vision ignored, present in the meta line's accessible name, and readable
 *      without hovering (the `title` repeats the visible string rather than being
 *      the only place the meaning lives).
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

// RELATIVE, not a literal date: the sidebar's dormant-session collapse
// (staleCollapse.ts, default threshold 2 days) hides any row whose last
// activity is older than the threshold, and these tests address rows by
// title, so a hardcoded date turns into a time bomb — the fixture aged past
// the threshold two days after it was written and all ten tests started
// failing on every PR at once. A minute ago is always fresh.
const LAST_TS = new Date(Date.now() - 60_000).toISOString()

const SLOTS: ChatSlot[] = [
  // The divergence: bound to a `mochi` that nothing dispatches, so `kirocrew`
  // takes the turns.
  { key: 'k-diverged', title: 'diverged', running: false, messages: 2, agent: 'mochi', effective_agent: 'kirocrew', tags: [], last_ts: LAST_TS },
  // Honored: the backend reports "" rather than repeating the agent name.
  { key: 'k-honored', title: 'honored', running: false, messages: 2, agent: 'mochi', effective_agent: '', tags: [], last_ts: LAST_TS },
  // Field absent entirely — a row from persisted state, or a backend predating it.
  { key: 'k-absent', title: 'absent', running: false, messages: 2, agent: 'mochi', tags: [], last_ts: LAST_TS },
  // Present and in agreement. Redundant on the wire, but it must not render a
  // marker saying a session is answered by the agent it already names.
  { key: 'k-same', title: 'same', running: false, messages: 2, agent: 'mochi', effective_agent: 'mochi', tags: [], last_ts: LAST_TS },
] as unknown as ChatSlot[]

function renderSidebar(slots: ChatSlot[] = SLOTS) {
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
  qc.setQueryData(['chat-tags'], [])
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

/** The marker inside a row's meta line, or null. */
function markerFor(container: HTMLElement, title: string): HTMLElement | null {
  return metaLineFor(container, title).querySelector('[data-testid="session-effective-agent"]')
}

// `LAST_TS` is a fixed wall-clock instant while the stale-session collapse
// measures age against `Date.now()`, so this file's rows drift into dormancy on
// their own: they were 1.6 days old when the collapse shipped and passed, then
// crossed the 2-day default at 2026-08-28T18:00Z and every row-by-title lookup
// below started throwing `no row titled ...` with no code change in between.
// Pinning the threshold off keeps the rows queryable and makes the file's age
// irrelevant; the collapse's own behavior is pinned in
// ChatSidebar.staleCollapse.test.tsx, which is where it belongs.
beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
})
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — effective-agent marker', () => {
  it('names the agent that actually answers when it differs', () => {
    const { container } = renderSidebar()
    const marker = markerFor(container, 'diverged')
    expect(marker).not.toBeNull()
    expect(marker!.textContent).toContain('kirocrew')
  })

  it('keeps showing the requested agent alongside it', () => {
    // The marker ADDS a fact; it must not replace the binding the user chose,
    // which is what the row is still bound to and what a rebind would target.
    const { container } = renderSidebar()
    expect(metaLineFor(container, 'diverged').textContent).toContain('mochi')
  })

  it.each([
    ['the backend reports no divergence', 'honored'],
    ['the field is absent entirely', 'absent'],
    ['the effective agent equals the requested one', 'same'],
  ])('renders no marker when %s', (_label, title) => {
    const { container } = renderSidebar()
    expect(markerFor(container, title)).toBeNull()
  })

  it('carries the meaning in text, not in colour or an icon', () => {
    // The accessibility contract. Colour-blind and screen-reader users get the
    // same information as everyone else because the information IS the text: it
    // sits in the meta line's accessible name, in document order, and the row
    // renders no svg to convey it.
    const { container } = renderSidebar()
    const marker = markerFor(container, 'diverged')!
    expect(marker.textContent?.trim().length).toBeGreaterThan(0)
    expect(marker.querySelector('svg')).toBeNull()
    // No inline colour: it inherits the line's muted tone rather than encoding
    // the state in a hue.
    expect(marker.getAttribute('style')).toBeNull()
  })

  it('is readable without hovering — the title only repeats the visible text', () => {
    // A `title` alone would be hover-only, unreachable by keyboard and skipped
    // by most screen readers. Here it is a convenience for a truncated row, so
    // the two must agree; if they ever diverge, the visible half is the one users
    // actually get.
    const { container } = renderSidebar()
    const marker = markerFor(container, 'diverged')!
    const title = marker.getAttribute('title')
    expect(title).toBeTruthy()
    expect(marker.textContent).toContain(title!)
  })

  it('yields the line rather than clipping the trailing meta', () => {
    // The trailing timestamp + channel glyphs are `ml-auto … shrink-0`, so an
    // unbounded marker in front of them pushes them off a minimum-width sidebar.
    // This is the row's least important fact, so it has to be the one that
    // shrinks: `min-w-0` makes it shrinkable at all, `max-w-*` stops it claiming
    // the line before shrinking starts, and `truncate` ellipsizes the remainder.
    // Asserted as classes because the geometry itself is unobservable in jsdom
    // (no layout), and the failure mode is invisible until a real narrow sidebar.
    const { container } = renderSidebar()
    const cls = markerFor(container, 'diverged')!.className
    expect(cls).toContain('min-w-0')
    expect(cls).toContain('truncate')
    expect(cls).toMatch(/max-w-\[/)
    expect(cls).not.toContain('shrink-0')
  })

  it('bounds the agent name too once the marker shares the line', () => {
    // The name's own bound was gated on tags alone. A diverged row is the other
    // case where the line has a second claimant, so a long agent name would
    // otherwise consume it before the marker ever got to shrink.
    const { container } = renderSidebar()
    const name = metaLineFor(container, 'diverged').querySelector('span')
    expect(name?.className).toContain('max-w-[50%]')
  })

  it('stays quiet on a row with no agent at all', () => {
    // `defaultAgent` is '' here, so the row's displayed agent is ''. An empty
    // `effective_agent` must not be read as "differs from ''".
    const { container } = renderSidebar([
      { key: 'k-bare', title: 'bare', running: false, messages: 1, agent: '', tags: [], last_ts: LAST_TS },
    ] as unknown as ChatSlot[])
    expect(markerFor(container, 'bare')).toBeNull()
  })
})
