/**
 * A collapsed folder must not contribute LAYOUT height to the sidebar's scroll
 * lane.
 *
 * `FolderBody` collapses with a grid `1fr`/`0fr` animation plus
 * `overflow: hidden`. That clips the rows for paint, but it does NOT stop them
 * contributing scrollable overflow to the lane: measured on 0.5.0rc7 and on
 * main, a collapsed folder holding a dormant split put 2984px of scrollHeight
 * above the lane's clientHeight, so the sidebar scrolled that far into pure
 * emptiness under the last visible row. `content-visibility: hidden` skips the
 * subtree's layout and removes it.
 *
 * jsdom performs no layout, so the height itself is unassertable here — the
 * browser-side proof is `website/scripts/measure-sidebar-gap.mjs`, which reports
 * lane scrollHeight against clientHeight for this exact fixture and has an
 * `EMPTY_FOLDER=1` control. What this file pins is the style contract that fix
 * rests on, in BOTH directions:
 *
 *   - collapsed  -> `content-visibility: hidden` (no layout, hence no height),
 *   - collapsed  -> rows still MOUNTED, because sessionRowNav.ts walks them to
 *     skip over collapsed folders during keyboard navigation; swapping the fix
 *     to `display: none`/unmounting would silently break that,
 *   - expanded   -> `content-visibility: visible`, so opening still lays out.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, cleanup, act } from '@testing-library/react'
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
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<HTMLElement>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const FOLDER = 'f-autofix'
// Folders must come back from the api mock, not only the seeded query cache:
// the seeded entry is stale on mount so react-query refetches it.
const fixtures: { chatFolders: unknown[] } = { chatFolders: [] }

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop in fixtures) return vi.fn().mockResolvedValue(fixtures[prop as keyof typeof fixtures])
      return vi.fn().mockResolvedValue([])
    },
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
import { FolderBody, FOLDER_BODY_COLLAPSE_MS } from '../components/FolderBody'

const SLOTS = [
  { key: 'in-folder', title: 'autofix session', running: false, messages: 4, folder_id: FOLDER },
  { key: 'ungrouped', title: 'Cron: gh-issue-triage', running: false, messages: 4 },
]

async function renderSidebar(collapsed: boolean) {
  fixtures.chatFolders = [{ id: FOLDER, name: 'kirocrew-github-autofix', order: 0, collapsed }]
  // Cast through the factory's own parameter type rather than `any`: the
  // preloaded slices are partial on purpose (this suite only needs the sidebar's
  // inputs), and naming the type keeps the cast checked against the real shape.
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots: SLOTS, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint',
      sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    },
    chat: { activeSlot: null, slotStatusDetail: {} },
  } as Parameters<typeof createTestStore>[0])
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS as React.ComponentProps<typeof ChatSidebar>['slots']}
              activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  await utils.findByText('kirocrew-github-autofix')
  return utils
}

/** The FolderBody inner box — the element carrying the collapse styles. */
function folderBodyInner(): HTMLElement {
  // The grid wrapper is the aria-hidden/inert host; its only child is the box
  // that carries overflow + visibility + content-visibility.
  const grid = document.querySelector<HTMLElement>('[aria-hidden="true"][style*="grid"]')
  if (!grid) throw new Error('no FolderBody grid wrapper found')
  const inner = grid.firstElementChild as HTMLElement | null
  if (!inner) throw new Error('FolderBody grid has no inner box')
  return inner
}

describe('collapsed folder contributes no layout height', () => {
  it('skips the collapsed subtree layout with content-visibility: hidden', async () => {
    await renderSidebar(true)
    const style = folderBodyInner().getAttribute('style') || ''
    // Explicitly `content-visibility`, not just `visibility`: `visibility:
    // hidden` is what the buggy version already had, and it keeps layout.
    expect(style).toContain('content-visibility: hidden')
    expect(style).toContain('overflow: hidden')
  })

  it('keeps the collapsed rows mounted for keyboard navigation', async () => {
    await renderSidebar(true)
    // sessionRowNav walks these to skip past a collapsed folder; a fix that
    // unmounted them (or used display:none) would strand ArrowDown.
    expect(folderBodyInner().querySelectorAll('[data-session-row]').length).toBeGreaterThan(0)
  })

  it('lays the subtree out again when the folder is expanded', async () => {
    await renderSidebar(false)
    const grid = document.querySelector<HTMLElement>('[aria-hidden="false"][style*="grid"]')
    const inner = grid?.firstElementChild as HTMLElement | null
    expect(inner).toBeTruthy()
    expect(inner!.getAttribute('style') || '').toContain('content-visibility: visible')
  })
})

describe('FolderBody defers layout suppression until the collapse has animated', () => {
  // The layout-suppressing property cannot apply the instant `open` flips false:
  // zeroing the subtree's height leaves `grid-template-rows: 1fr` nothing to
  // animate away from, so the folder snaps shut instead of closing. Opening is
  // the mirror case — layout must come back BEFORE the track animates open.
  const inner = () =>
    document.querySelector<HTMLElement>('[style*="grid-template-rows"]')!
      .firstElementChild as HTMLElement

  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers(); cleanup() })

  it('keeps layout through the collapse, then suppresses it', () => {
    const { rerender } = render(<FolderBody open><div>rows</div></FolderBody>)
    expect(inner().getAttribute('style')).toContain('content-visibility: visible')

    rerender(<FolderBody open={false}><div>rows</div></FolderBody>)
    // Mid-transition: still laid out, so the track has a height to animate from.
    act(() => { vi.advanceTimersByTime(FOLDER_BODY_COLLAPSE_MS - 1) })
    expect(inner().getAttribute('style')).toContain('content-visibility: visible')

    act(() => { vi.advanceTimersByTime(1) })
    expect(inner().getAttribute('style')).toContain('content-visibility: hidden')
  })

  it('restores layout immediately on open, and suppresses on a closed mount', () => {
    const { rerender } = render(<FolderBody open={false}><div>rows</div></FolderBody>)
    // A body that mounts closed must not reserve height for even one frame.
    expect(inner().getAttribute('style')).toContain('content-visibility: hidden')

    rerender(<FolderBody open><div>rows</div></FolderBody>)
    expect(inner().getAttribute('style')).toContain('content-visibility: visible')
  })

  it('cancels a pending suppression when reopened mid-collapse', () => {
    const { rerender } = render(<FolderBody open><div>rows</div></FolderBody>)
    rerender(<FolderBody open={false}><div>rows</div></FolderBody>)
    act(() => { vi.advanceTimersByTime(FOLDER_BODY_COLLAPSE_MS / 2) })
    rerender(<FolderBody open><div>rows</div></FolderBody>)
    // The timer armed by the collapse must not fire against the reopened body.
    act(() => { vi.advanceTimersByTime(FOLDER_BODY_COLLAPSE_MS) })
    expect(inner().getAttribute('style')).toContain('content-visibility: visible')
  })
})
