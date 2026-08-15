/**
 * The sidebar folder header's geometry, locked as numbers.
 *
 * This file began as a guard on TWO alignment guides — folder glyph on the text x
 * of sibling sessions, folder name on the text x of the sessions inside it — after
 * #1211 changed three numbers at once and broke both. Those guides no longer hold:
 * a status gutter added 12px to where a session row's content starts (`px-3` 12 +
 * gutter `w-3` 12 + `gap-1.5` 6 = 30), and the header's pad is a symmetric
 * `px-2.5` (10px) so a folder reads as a HEADER over its sessions rather than a
 * peer opening the same column. Both glyph and name now sit well left of what they
 * used to track. That is a decision, not drift.
 *
 * What is still guarded, and why each number is not free:
 *   - the header's `px-2.5` pad with NO inline left-pad override, the 14px glyph
 *     and the 5px gap, so the folder row's own proportions cannot be changed by
 *     accident the way #1211 changed them;
 *   - the nested body's 15px indent step (`ml-2` + 1px border + `pl-1`) and its
 *     `border-l` connector line, which is what makes the nesting readable at all.
 *     Note this step no longer equals glyph 14 + gap 5 (19px): that equality
 *     existed only to serve the dead guide 2, so the gap is now just a gap;
 *   - the session row's `px-3` and `gap-1.5`, because they set the content offset
 *     that any future attempt to re-align the two must be computed from.
 *
 * jsdom has no layout engine, so this asserts the INPUTS to the geometry rather
 * than measured x's. That is a real limit, not a shortcut: an input-level
 * assertion stayed green through the very gutter change that broke both original
 * guides, because 16px was still 16px. Re-measure with
 * `website/scripts/capture-folder-glyph.mjs` under `MEASURE=1` whenever any of
 * these numbers moves.
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
    React.forwardRef((props: any, ref: any) => {
      const clean: any = {}
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
    AnimatePresence: ({ children }: any) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: any) => React.createElement(React.Fragment, null, children),
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

function renderSidebar(slots: any[], folders: any[]) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as any,
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {}, workflowRuns: {} } as any,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
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

const FOLDERS = [{ id: 'f1', name: 'Kiro', collapsed: false, order: 0 }]
const SLOTS = [
  { key: 'chat-1-100', title: 'inside the folder', running: false, messages: 2, folder_id: 'f1' },
  { key: 'chat-2-100', title: 'root lane', running: false, messages: 2 },
]

// Tree view (not flat): the guides are a tree-layout property.
beforeEach(() => { localStorage.clear() })
afterEach(() => vi.clearAllMocks())

/**
 * Exact class-token membership, not substring.
 *
 * `toContain('px-2')` is TRUE for `px-2.5`, and `toContain('gap-1')` is true for
 * `gap-1.5` — so a substring assertion on a Tailwind spacing class passes
 * vacuously the moment someone moves to the neighbouring fractional step, which
 * is exactly the kind of silent drift this file exists to catch.
 */
const hasClass = (el: HTMLElement, cls: string) =>
  el.className.split(/\s+/).includes(cls)

describe('chat sidebar — folder header alignment geometry', () => {
  it('keeps the px-2.5 pad / 14px glyph / 5px gap triple the folder row is built on', () => {
    const { getByTestId } = renderSidebar(SLOTS, FOLDERS)
    const glyph = getByTestId('folder-collapse-f1')

    // Symmetric `px-2.5` (10px) from the class, and NO inline left-pad override —
    // the 16px one that used to live here is gone. Both halves are asserted: a
    // reintroduced inline style would silently win over the class.
    const header = glyph.closest('[role="group"]') as HTMLElement
    expect(header).toBeTruthy()
    expect(hasClass(header, 'px-2.5')).toBe(true)
    expect(hasClass(header, 'pr-2')).toBe(false)
    expect(header.style.paddingLeft).toBe('')

    // glyph box + gap == the nested body's 19px indent step, so the glyph and
    // name columns stay exactly one indent step apart down the tree.
    expect(glyph.style.width).toBe('14px')
    expect(glyph.style.height).toBe('14px')
    const toggle = glyph.closest('button') as HTMLElement
    expect(toggle.className).toContain('gap-[5px]')
  })

  it('indents the nested folder body by 15px, and keeps its connector line', () => {
    const { getByText } = renderSidebar(SLOTS, FOLDERS)
    // ml-2 (8px) + 1px left border + pl-1 (4px) == 15px, measured per level by
    // capture-folder-glyph.mjs. The glyph→name step is 19px (glyph 14 + gap 5) and
    // no longer equals it: that equality existed to land the folder NAME on the
    // content x of the sessions inside it, and that guide is already gone (see
    // this file's header). The 5px gap is now just a gap.
    const row = getByText('inside the folder').closest('.session-row') as HTMLElement
    // The row is wrapped (sortable + motion shims), so walk up to the folder
    // body rather than assuming it is the immediate parent.
    const body = row.closest('[class*="border-l"]') as HTMLElement
    expect(body).toBeTruthy()
    expect(hasClass(body, 'ml-2')).toBe(true)
    expect(hasClass(body, 'pl-1')).toBe(true)
    // The connector line itself. Without the border the indent is just empty
    // space and the nesting stops being readable.
    expect(hasClass(body, 'border-l')).toBe(true)
    // The two row values that set the content offset (12 + gutter 12 + 6 = 30).
    // Pinned because any future attempt to re-align the folder glyph with session
    // content has to be computed from them, and jsdom cannot measure the result.
    // Token-exact: `px-3` as a substring would also match `px-3.5`.
    expect(hasClass(row, 'px-3')).toBe(true)
    expect(hasClass(row, 'gap-1.5')).toBe(true)
  })
})
