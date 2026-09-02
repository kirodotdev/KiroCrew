/**
 * The sidebar folder header's geometry, locked as numbers.
 *
 * THREE alignment guides, all live, all MEASURED on real renders rather than
 * derived from class names (see the last paragraph — that distinction is the whole
 * reason this file keeps having to be rewritten):
 *   1. a folder GLYPH sits on the x of the `border-l` connector line that runs
 *      down under it, so the glyph reads as the head of its own subtree;
 *   2. a folder NAME, and the agent label / title / tool-call subtitle of every
 *      session inside it, share ONE left edge;
 *   3. a NESTED folder's glyph sits on the content column of the sessions filed
 *      beside it — a subfolder reads as their peer, not as something floating a
 *      couple of px to their left.
 *
 * The algebra, with H = the folder header box's left edge and D =
 * `FOLDER_BODY_INSET_PX` — the nested body's own left inset, applied by
 * `FolderBody` so its collapse animation does not clip. D is invisible in the
 * class list, which is why every attempt to derive this geometry from Tailwind
 * classes alone has landed 2px out; it is imported from the component here rather
 * than restated, and the rendered padding is asserted against it:
 *
 *   P = header `px-3.5` 14   G = glyph 14    g = `gap-[5px]` 5
 *   M = body `ml-3` 12       B = 1px border  p = body `pl-1` 4
 *   R = session row `pl-3.5` 14
 *
 *   guide 1:  P = D + M                      14 = 2 + 12
 *   guide 2:  P + G + g = D + M + B + p + R  33 = 33
 *   guide 3:  P = R                          14 = 14
 *
 * Guide 3 collapses to "the folder header uses the same left pad as a session
 * row". That also makes the system scale-free: it holds in the root lane and at
 * every nesting depth, with no per-depth term anywhere.
 *
 * Captured from real renders, x in CSS px:
 *   pre-#3766 (d926ca569^), pod: connector 23, glyph 25, name 44, agent 44
 *   #3766, pod:                  connector 19, glyph 19, name 38, agent 54
 *   #3903, built SPA:            connector/glyph 263, name 285, agent/title 284
 *                                (guide 2 off by 1px); depth 2 glyph 282 vs
 *                                content 284 (guide 3 off by 2px)
 *   this branch, built SPA:      depth 1 connector/glyph 259, name/agent/title/
 *                                subtitle 278; depth 2 connector/glyph 278
 *                                (== depth 1's content column), name/content 297;
 *                                root lane content 259 (== the root folder glyph)
 *
 * jsdom has no layout engine, so this file asserts the INPUTS to the geometry,
 * never measured x's. That is a real limit: an input-level assertion stayed green
 * through the very status-gutter change that broke guide 2, because the pad it
 * checked had not moved. Hence the assertion that nothing occupies the row's left
 * pad in flow (that being the thing that actually moved the content), and hence
 * the rule — when any of
 * these numbers moves, re-measure with
 * `website/scripts/capture-folder-glyph.mjs` under `MEASURE=1`. Do not re-derive
 * on paper: #1211, #3766, #3903 and two paper estimates during this fix were all
 * off by 1-4px in exactly this way.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { FOLDER_BODY_INSET_PX } from '../pages/ChatSidebar'
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

// Guide 3 is only observable one level down: f1a is a folder that is itself a
// child of f1, so its glyph has to land on the content column of `sibling of the
// subfolder`, the session filed beside it in f1.
const NESTED_FOLDERS = [
  { id: 'f1', name: 'Kiro', collapsed: false, order: 0 },
  { id: 'f1a', name: 'Sidebar', collapsed: false, order: 0, parent_id: 'f1' },
]
const NESTED_SLOTS = [
  { key: 'chat-1-100', title: 'sibling of the subfolder', running: false, messages: 2, folder_id: 'f1' },
  { key: 'chat-3-100', title: 'inside the subfolder', running: false, messages: 2, folder_id: 'f1a' },
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
  it('gives the folder header the SAME left pad as a session row', () => {
    const { getByTestId } = renderSidebar(SLOTS, FOLDERS)
    const glyph = getByTestId('folder-collapse-f1')

    // Symmetric `px-3.5` (14px) from the class, and NO inline left-pad override:
    // a reintroduced inline style would silently win over the class, which is how
    // #1211 moved this unnoticed. 14 is not a taste choice — it equals the session
    // row's own pad, which IS guide 3. #3903's `pl-[18px]` (opened for an absolute
    // left-side unread dot) is asserted absent, because that pad broke guide 3.
    const header = glyph.closest('[role="group"]') as HTMLElement
    expect(header).toBeTruthy()
    expect(hasClass(header, 'px-3.5')).toBe(true)
    expect(hasClass(header, 'pl-[18px]')).toBe(false)
    expect(hasClass(header, 'px-2.5')).toBe(false)
    expect(header.style.paddingLeft).toBe('')

    // 14px glyph and a 5px glyph→name gap. NOT `gap-2` (8px): 8 overshoots the
    // content column by 3px and breaks guide 2.
    expect(glyph.style.width).toBe('14px')
    expect(glyph.style.height).toBe('14px')
    const toggle = glyph.closest('button') as HTMLElement
    expect(hasClass(toggle, 'gap-[5px]')).toBe(true)
    expect(hasClass(toggle, 'gap-2')).toBe(false)
  })

  it('lands child session content on the folder name x, and keeps the connector line', () => {
    const { getByText } = renderSidebar(SLOTS, FOLDERS)
    const row = getByText('inside the folder').closest('.session-row') as HTMLElement
    // The row is wrapped (sortable + motion shims), so walk up to the folder
    // body rather than assuming it is the immediate parent.
    const body = row.closest('[class*="border-l"]') as HTMLElement
    expect(body).toBeTruthy()
    // `ml-3` (12) + 1px border + `pl-1` (4). 2 + 12 == the header's 14px pad,
    // which is GUIDE 1.
    expect(hasClass(body, 'ml-3')).toBe(true)
    expect(hasClass(body, 'ml-4')).toBe(false)
    expect(hasClass(body, 'pl-1')).toBe(true)
    // The connector line itself. Without the border the indent is just empty
    // space, the nesting stops being readable, AND guides 1 and 3 lose the thing
    // the folder glyphs are aligned to.
    expect(hasClass(body, 'border-l')).toBe(true)

    // GUIDE 2 — the row's `pl-3.5` (14px) is its WHOLE content offset, which holds
    // only while NOTHING lives in that pad in flow. A status gutter as an in-flow
    // flex child is what added 12px + a gap and broke this in #3766; the gutter is
    // gone entirely now (the marker leads the secondary line — see
    // ChatSidebar.statusMarker.test.tsx, which pins that no absolutely-positioned
    // box returns to the row's left edge either).
    expect(hasClass(row, 'pl-3.5')).toBe(true)
    expect(hasClass(row, 'pr-3')).toBe(true)
    expect(hasClass(row, 'pl-1')).toBe(false)
    expect(hasClass(row, 'gap-1')).toBe(false)

    // D comes from the COMPONENT, not from a literal repeated here: it is the
    // nested body's own left inset, invisible in the class list, and the term four
    // revisions of this geometry each got wrong. Asserting the rendered padding
    // against the same constant is what makes the arithmetic below a live check
    // rather than three constants compared to each other — change the inset and
    // this test fails, in jsdom, without needing a layout engine.
    const wrapper = body.parentElement as HTMLElement
    expect(wrapper.style.paddingLeft).toBe(`${FOLDER_BODY_INSET_PX}px`)

    const D = FOLDER_BODY_INSET_PX, P = 14, G = 14, g = 5, M = 12, B = 1, p = 4, R = 14
    expect(P).toBe(D + M)                     // guide 1: glyph == connector
    expect(P + G + g).toBe(D + M + B + p + R) // guide 2: name == content
    expect(P).toBe(R)                         // guide 3: nested glyph == content
  })

  it('lands a NESTED folder glyph on the content column of its sibling sessions', () => {
    // GUIDE 3, at the depth where it is observable. jsdom cannot measure x, so
    // this asserts the two class chains that produce the equality: the nested
    // header reaches its glyph with the same pad a sibling session row reaches its
    // content with, from the same starting box.
    const { getByTestId, getByText } = renderSidebar(NESTED_SLOTS, NESTED_FOLDERS)
    const childGlyph = getByTestId('folder-collapse-f1a')
    const childHeader = childGlyph.closest('[role="group"]') as HTMLElement
    const siblingRow = getByText('sibling of the subfolder').closest('.session-row') as HTMLElement

    // Same body => the same starting box for both.
    const nestedBody = childHeader.closest('[class*="border-l"]') as HTMLElement
    expect(nestedBody).toBeTruthy()
    expect(siblingRow.closest('[class*="border-l"]')).toBe(nestedBody)

    // Same pad on both => the glyph lands on the sibling's content x.
    expect(hasClass(childHeader, 'px-3.5')).toBe(true)
    expect(hasClass(siblingRow, 'pl-3.5')).toBe(true)

    // And the subfolder's OWN connector stays under its own glyph (guide 1
    // recursively), which is what makes the algebra scale-free across depths.
    const grandchildRow = getByText('inside the subfolder').closest('.session-row') as HTMLElement
    const deepBody = grandchildRow.closest('[class*="border-l"]') as HTMLElement
    expect(deepBody).not.toBe(nestedBody)
    expect(hasClass(deepBody, 'ml-3')).toBe(true)
    expect(hasClass(deepBody, 'pl-1')).toBe(true)
  })
})
