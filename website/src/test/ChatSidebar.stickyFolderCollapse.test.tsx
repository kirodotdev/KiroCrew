/**
 * Collapsing a folder whose header is PINNED (sticky, its block's top scrolled
 * above the lane) must leave that header where it is painted, as the top row of
 * the list, instead of letting it fall to the block's natural top above the lane
 * while every row below jumps up.
 *
 * jsdom performs no layout, so this stubs the two rects the sidebar reads (the
 * folder block and its header row) and gives the lane a real `scrollTop`. The
 * geometry is the reported shape: the lane has scrolled 500px; the folder block
 * starts 300px above the header's pinned position, so the header is stuck 300px
 * below its natural place. Keeping it in place means the lane must scroll up by
 * exactly those 300px, in the commit that hides the body: scrolling earlier
 * would paint the folder's first rows under the header for a frame.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore, settleSidebarRowAnim } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

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
      // Recorded, so a test can see whether a row is in layout projection.
      if ('layout' in props) clean['data-layout'] = String(props.layout)
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  // One component per tag, so `motion.div` is the same type on every render: a
  // fresh type per access would remount the lane on each commit and drop the
  // stubbed geometry before the collapse commit reads it.
  const byTag = new Map<string, ReturnType<typeof make>>()
  const motion = new Proxy({}, {
    get: (_t, tag: string) => {
      if (!byTag.has(tag)) byTag.set(tag, make(tag))
      return byTag.get(tag)
    },
  })
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

const FOLDER = 'f-long'
const fixtures: { chatFolders: unknown[] } = { chatFolders: [] }
// The server keeps what it was sent, so the refetch after the PATCH agrees with
// the optimistic collapse instead of reverting it before it ever renders.
const updateChatFolder = vi.fn(async (id: string, body: object) => {
  fixtures.chatFolders = fixtures.chatFolders.map(f => (f as { id: string }).id === id ? { ...(f as object), ...body } : f)
  return {}
})

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'updateChatFolder') return updateChatFolder
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

const SLOTS = Array.from({ length: 12 }, (_, i) => ({
  key: `s${i}`, title: `long folder session ${i}`, running: false, messages: 4, folder_id: FOLDER,
}))

async function renderSidebar(collapsed: boolean) {
  fixtures.chatFolders = [{ id: FOLDER, name: 'long folder', order: 0, collapsed }]
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
  await utils.findByText('long folder')
  await settleSidebarRowAnim()
  return utils
}

const LANE_TOP = 100
/** Where the header is painted: pinned at the lane's top edge. */
const HEADER_PAINTED_TOP = LANE_TOP
/** How far the block's top has scrolled above the pinned header. */
const SCROLLED_INTO_FOLDER = 300

/**
 * Give the lane a writable scrollTop and the block + header the pinned rects.
 * With `reducedMotion`, the body closes with no transition, as it does under
 * `prefers-reduced-motion`: once the folder renders collapsed the block holds
 * only its header, so the header measures at the block's top.
 */
function stubPinnedGeometry(
  laneScrollTop: number,
  blockTop = HEADER_PAINTED_TOP - SCROLLED_INTO_FOLDER,
  { reducedMotion = false } = {},
) {
  const block = document.querySelector<HTMLElement>(`[data-folder-drop="${FOLDER}"]`)
  const header = document.querySelector<HTMLElement>(`[data-folder-row="${FOLDER}"]`)
  // Whichever list lane rendered (tree is the default), it is the block's scroller.
  const lane = block?.closest<HTMLElement>('[data-testid$="-view-lane"]')
  if (!block || !header || !lane) throw new Error('folder block, header or lane not rendered')
  let top = laneScrollTop
  // The folder body's own aria-hidden. The rail inside it is aria-hidden itself,
  // so the search starts at the rail's parent.
  const bodyHidden = () =>
    block.querySelector(`[data-testid="folder-rail-${FOLDER}"]`)?.parentElement?.closest('[aria-hidden]')?.getAttribute('aria-hidden') ?? null
  /** The folder body's aria-hidden at each scrollTop write: "true" = rows already hidden. */
  const bodyHiddenAtWrite: (string | null)[] = []
  /** The body's transition at each write, i.e. in the collapsed commit itself. */
  const transitionAtWrite: string[] = []
  /** Each session row's `layout` prop at each write ("false" = not projected). */
  const rowLayoutAtWrite: string[][] = []
  Object.defineProperty(lane, 'scrollTop', {
    configurable: true,
    get: () => top,
    set: (v: number) => {
      bodyHiddenAtWrite.push(bodyHidden())
      transitionAtWrite.push(folderBodyBox().style.transition)
      rowLayoutAtWrite.push(rowLayouts())
      top = Math.max(0, v)
    },
  })
  const r = (y: number, h: number) => ({ top: y, bottom: y + h, left: 0, right: 300, width: 300, height: h, x: 0, y, toJSON: () => ({}) }) as DOMRect
  block.getBoundingClientRect = () => r(blockTop, 1200)
  header.getBoundingClientRect = () => r(reducedMotion && bodyHidden() === 'true' ? blockTop : HEADER_PAINTED_TOP, 32)
  return { lane, header, bodyHiddenAtWrite, transitionAtWrite, rowLayoutAtWrite }
}

/** Every session row's recorded `layout` prop, in DOM order. */
function rowLayouts(): string[] {
  return [...document.querySelectorAll('[data-layout]')].map(el => el.getAttribute('data-layout') ?? '')
}

/** The folder body's grid box, which carries the close transition. */
function folderBodyBox(): HTMLElement {
  const rail = document.querySelector<HTMLElement>(`[data-testid="folder-rail-${FOLDER}"]`)
  const box = rail?.parentElement?.closest<HTMLElement>('[aria-hidden]')
  if (!box) throw new Error('folder body not rendered')
  return box
}

describe('collapsing a folder whose sticky header is pinned keeps the header in place', () => {
  it('scrolls the lane so the collapsed header stays at its pinned position (header click)', async () => {
    const { getByRole } = await renderSidebar(false)
    const { lane } = stubPinnedGeometry(500)
    fireEvent.click(getByRole('button', { name: /collapse folder long folder/i }))
    // The block's top now sits where the header was painted: 500 - 300.
    await waitFor(() => expect(lane.scrollTop).toBe(500 - SCROLLED_INTO_FOLDER))
    await waitFor(() => expect(updateChatFolder).toHaveBeenCalledWith(FOLDER, { collapsed: true }))
  })

  it('scrolls only once the body is hidden, so the folder\'s first rows never paint under the header', async () => {
    const { getByRole } = await renderSidebar(false)
    const { lane, bodyHiddenAtWrite } = stubPinnedGeometry(500)
    fireEvent.click(getByRole('button', { name: /collapse folder long folder/i }))
    // Nothing moves in the click itself: the open rows are still painted then.
    expect(lane.scrollTop).toBe(500)
    await waitFor(() => expect(lane.scrollTop).toBe(500 - SCROLLED_INTO_FOLDER))
    expect(bodyHiddenAtWrite).toEqual(['true'])
  })

  it('holds the header under prefers-reduced-motion, where the body is already closed when the hold runs', async () => {
    const { getByRole } = await renderSidebar(false)
    const { lane } = stubPinnedGeometry(500, undefined, { reducedMotion: true })
    fireEvent.click(getByRole('button', { name: /collapse folder long folder/i }))
    await waitFor(() => expect(lane.scrollTop).toBe(500 - SCROLLED_INTO_FOLDER))
  })

  it('does the same when the folder is collapsed from its left-edge line', async () => {
    await renderSidebar(false)
    const { lane } = stubPinnedGeometry(500)
    fireEvent.click(document.querySelector<HTMLElement>(`[data-testid="folder-rail-${FOLDER}"]`)!)
    await waitFor(() => expect(lane.scrollTop).toBe(500 - SCROLLED_INTO_FOLDER))
  })

  it('closes the body with no transition when the collapse starts from the pinned header', async () => {
    // An animated close under a held header slides the rows up past it and
    // leaves the lane empty until the next folder arrives (the blank lane).
    const { getByRole } = await renderSidebar(false)
    const { lane, transitionAtWrite } = stubPinnedGeometry(500)
    fireEvent.click(getByRole('button', { name: /collapse folder long folder/i }))
    await waitFor(() => expect(lane.scrollTop).toBe(500 - SCROLLED_INTO_FOLDER))
    expect(transitionAtWrite).toEqual(['none'])
    const box = folderBodyBox()
    expect(box.getAttribute('aria-hidden')).toBe('true')
    expect(box.style.gridTemplateRows).toBe('0fr')
  })

  it('takes the rows out of layout projection for that commit, then puts them back', async () => {
    // Projected, the next folder's rows would slide up from where they sat
    // below the open body, leaving the lane under its header empty meanwhile.
    const { getByRole } = await renderSidebar(false)
    expect(rowLayouts().length).toBeGreaterThan(0)
    expect(new Set(rowLayouts())).toEqual(new Set(['position']))
    const { lane, rowLayoutAtWrite } = stubPinnedGeometry(500)
    fireEvent.click(getByRole('button', { name: /collapse folder long folder/i }))
    await waitFor(() => expect(lane.scrollTop).toBe(500 - SCROLLED_INTO_FOLDER))
    expect(rowLayoutAtWrite.length).toBe(1)
    expect(new Set(rowLayoutAtWrite[0])).toEqual(new Set(['false']))
    await waitFor(() => expect(new Set(rowLayouts())).toEqual(new Set(['position'])))
  })

  it('closes with no transition under prefers-reduced-motion too', async () => {
    const { getByRole } = await renderSidebar(false)
    const { lane, transitionAtWrite } = stubPinnedGeometry(500, undefined, { reducedMotion: true })
    fireEvent.click(getByRole('button', { name: /collapse folder long folder/i }))
    await waitFor(() => expect(lane.scrollTop).toBe(500 - SCROLLED_INTO_FOLDER))
    expect(transitionAtWrite).toEqual(['none'])
  })

  it('keeps the animated close, and animates the expand, when the header is at its natural place', async () => {
    const { getByRole } = await renderSidebar(false)
    stubPinnedGeometry(40, HEADER_PAINTED_TOP)
    // Read the transition in the commit that hides the body, before any frame
    // runs: a later read could see a released instant close and pass anyway.
    const box = folderBodyBox()
    const transitionWhenHidden: string[] = []
    const seen = new MutationObserver(() => {
      if (box.getAttribute('aria-hidden') === 'true' && transitionWhenHidden.length === 0) {
        transitionWhenHidden.push(box.style.transition)
      }
    })
    seen.observe(box, { attributes: true, attributeFilter: ['aria-hidden'] })
    fireEvent.click(getByRole('button', { name: /collapse folder long folder/i }))
    await waitFor(() => expect(folderBodyBox().getAttribute('aria-hidden')).toBe('true'))
    seen.disconnect()
    expect(transitionWhenHidden).toHaveLength(1)
    expect(transitionWhenHidden[0]).toMatch(/grid-template-rows 150ms/)
    fireEvent.click(getByRole('button', { name: /expand folder long folder/i }))
    await waitFor(() => expect(folderBodyBox().getAttribute('aria-hidden')).toBe('false'))
    expect(folderBodyBox().style.transition).toMatch(/grid-template-rows 150ms/)
  })

  it('leaves the lane alone when the header sits at its natural place', async () => {
    const { getByRole } = await renderSidebar(false)
    // Block top on screen and equal to the header's: nothing is scrolled under it.
    const { lane } = stubPinnedGeometry(40, HEADER_PAINTED_TOP)
    fireEvent.click(getByRole('button', { name: /collapse folder long folder/i }))
    await waitFor(() => expect(getByRole('button', { name: /expand folder long folder/i })).toBeTruthy())
    expect(lane.scrollTop).toBe(40)
  })

  it('does not scroll when EXPANDING a collapsed folder', async () => {
    const { getByRole } = await renderSidebar(true)
    const { lane } = stubPinnedGeometry(500)
    fireEvent.click(getByRole('button', { name: /expand folder long folder/i }))
    await waitFor(() => expect(getByRole('button', { name: /collapse folder long folder/i })).toBeTruthy())
    expect(lane.scrollTop).toBe(500)
  })
})
