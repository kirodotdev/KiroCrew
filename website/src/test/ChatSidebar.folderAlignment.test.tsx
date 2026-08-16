/**
 * The sidebar folder header's TWO alignment guides, locked as numbers.
 *
 * A folder row and the session rows around it share two left guides:
 *   1. the folder GLYPH's left edge sits on the text x of the session rows at
 *      the folder's OWN level (both reach it via `px-4`), so a folder and its
 *      siblings open the same column;
 *   2. the folder NAME's left edge sits on the text x of the sessions INSIDE
 *      it, because the nested body indents by 19px (`ml-3` + 1px border +
 *      `pl-1`) and glyph 14px + 5px gap == 19px.
 *
 * Both hold only for the exact triple (16px pad, 14px glyph, 5px gap) — change
 * any one and one guide breaks. #1211 changed all three (9px / 17px / 7px) and
 * broke both, which is what this file exists to prevent recurring.
 *
 * jsdom has no layout engine, so this asserts the INPUTS to the geometry (the
 * inline paddings/sizes and the indent classes) rather than measured x's. The
 * measured-pixel counterpart is `website/scripts/capture-folder-glyph.mjs`
 * under `MEASURE=1`.
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

describe('chat sidebar — folder header alignment geometry', () => {
  it('keeps the 16px pad / 14px glyph / 5px gap triple that lands both guides', () => {
    const { getByTestId } = renderSidebar(SLOTS, FOLDERS)
    const glyph = getByTestId('folder-collapse-f1')

    // Guide 1: the glyph's left edge == sibling session text x. The session
    // rows reach that x with `px-4` (16px), so the header's pad must match.
    const header = glyph.closest('[role="group"]') as HTMLElement
    expect(header).toBeTruthy()
    expect(header.style.paddingLeft).toBe('16px')

    // Guide 2: glyph box + gap == the nested body's 19px indent step, which
    // puts the NAME on the child sessions' text x.
    expect(glyph.style.width).toBe('14px')
    expect(glyph.style.height).toBe('14px')
    const toggle = glyph.closest('button') as HTMLElement
    expect(toggle.className).toContain('gap-[5px]')
  })

  it('indents the folder body by the 19px step the gap is derived from', () => {
    const { getByText } = renderSidebar(SLOTS, FOLDERS)
    // ml-3 (12px) + 1px left border + pl-1 (4px) == 19px == glyph 14 + gap 5.
    const row = getByText('inside the folder').closest('.session-row') as HTMLElement
    // The row is wrapped (sortable + motion shims), so walk up to the folder
    // body rather than assuming it is the immediate parent.
    const body = row.closest('[class*="border-l"]') as HTMLElement
    expect(body).toBeTruthy()
    expect(body.className).toContain('ml-3')
    expect(body.className).toContain('pl-1')
    expect(row.className).toContain('px-4')
  })
})
