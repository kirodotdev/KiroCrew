/**
 * Session-row projection window: at every total list size, only the first
 * SIDEBAR_DISPLACEMENT_WINDOW paint positions enroll in Framer layout
 * projection. This bounds group measurement without a 49-row motion cliff.
 *
 * The shared framer mock maps `layoutId` -> `data-layout-id`, which is what
 * these assertions read.
 */
import { StrictMode } from 'react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore, settleSidebarRowAnim } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { sseSlots } from '../store/dashboardSlice'

// Render framer-motion elements as plain DOM (jsdom can't run projection).
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown> & { children?: unknown }, ref: unknown) => {
      // Framer copies `layout`/`layoutId` into the projection node at mount and
      // never re-reads them, so what an element mounted with is what it keeps.
      const atMount = React.useRef({ layout: props.layout, layoutId: props.layoutId, layoutScroll: props.layoutScroll, initial: props.initial })
      const clean: Record<string, unknown> = {
        'data-mount-layout': String(atMount.current.layout),
        'data-mount-layout-id': atMount.current.layoutId,
        'data-mount-layout-scroll': String(atMount.current.layoutScroll),
        'data-mount-initial': JSON.stringify(atMount.current.initial),
      }
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (k === 'layout') { clean['data-layout'] = String(props[k]); continue }
        if (k === 'transition') { clean['data-layout-transition'] = JSON.stringify((props[k] as { layout?: unknown } | undefined)?.layout); continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref: ref as never }, props.children as never)
    })
  // One component per tag, as the real `motion.div` is: a fresh one per access
  // would remount every motion element whenever its parent re-renders.
  const components = new Map<string, ReturnType<typeof make>>()
  const motion = new Proxy({}, {
    get: (_t, tag: string) => {
      if (!components.has(tag)) components.set(tag, make(tag))
      return components.get(tag)
    },
  })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: unknown }) => React.createElement(React.Fragment, null, children as never),
    LayoutGroup: ({ children }: { children?: unknown }) => React.createElement(React.Fragment, null, children as never),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
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

import ChatSidebar, { SIDEBAR_DISPLACEMENT_WINDOW } from '../pages/ChatSidebar'

function mkSlots(n: number) {
  return Array.from({ length: n }, (_, i) => ({
    key: `slot-${i}`, title: `session ${i}`, running: false, messages: 1,
  }))
}

type FixtureSlot = ReturnType<typeof mkSlots>[number]

function renderSidebar(slots: FixtureSlot[], opts: { folders?: unknown[]; tagColumns?: unknown[]; strict?: boolean; dashboard?: Record<string, unknown> } = {}) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      ...opts.dashboard,
    } as never,
    chat: { activeSlot: null, slotStatusDetail: {} } as never,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], opts.folders ?? [])
  if (opts.tagColumns) qc.setQueryData(['tag-columns'], opts.tagColumns)
  const tree = (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots as never} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
  return { ...render(opts.strict ? <StrictMode>{tree}</StrictMode> : tree), store }
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

const rowWrappers = () => Array.from(document.querySelectorAll<HTMLElement>('[data-slot-key]'))

describe('sidebar layout-projection window', () => {
  // The first paint of a fresh lane is not movement: rows go from their
  // content-visibility placeholder to their real height and the reads that file
  // them land one by one. Projected, each of those commits springs the list into
  // place and every row plays its mount slide-in, so the list collapses and
  // re-expands as it loads. So the rows snap and skip the slide-in until the
  // population has painted -- but they mount WITH their projection, because a
  // row that mounted without `layout`/`layoutId` would never glide afterwards.
  it('mounts rows projected but snapping, with no slide-in, until the first population paints', async () => {
    renderSidebar(mkSlots(5))
    expect(document.querySelector('[data-row-anim]')?.getAttribute('data-row-anim')).toBe('holding')
    const held = rowWrappers()
    expect(held).toHaveLength(5)
    const lanes = Array.from(document.querySelectorAll('[data-testid$="-view-lane"]'))
    expect(lanes.length).toBeGreaterThan(0)
    for (const el of held) {
      expect(el.getAttribute('data-mount-layout')).toBe('position')
      expect(el.getAttribute('data-mount-layout-id')).toMatch(/^slot-/)
      expect(el.getAttribute('data-mount-initial')).toBe('false')
      expect(el.getAttribute('data-layout-transition')).toBe(JSON.stringify({ type: false }))
    }
    for (const lane of lanes) {
      expect(lane.getAttribute('data-mount-layout-scroll')).toBe('true')
    }
    await settleSidebarRowAnim()
    const settled = rowWrappers()
    // The same elements: settling re-renders the rows, it does not remount them.
    expect(settled).toHaveLength(held.length)
    settled.forEach((el, i) => expect(el).toBe(held[i]))
    for (const el of settled) {
      expect(el.getAttribute('data-layout')).toBe('position')
      expect(el.getAttribute('data-layout-id')).toMatch(/^slot-/)
      expect(JSON.parse(el.getAttribute('data-layout-transition')!)).toMatchObject({ type: 'spring' })
    }
  })
  // `slotsLoaded` is reset by every socket connect and an empty slot frame never
  // sets it again, so an empty gateway whose list already landed would hold for
  // the whole visit -- and the first session created there would lose its
  // slide-in. `slotsGeneration` is what says a list landed.
  it('settles an empty gateway whose slot list already landed', async () => {
    renderSidebar([], { dashboard: { slotsLoaded: false, slotsGeneration: 1 } })
    await settleSidebarRowAnim()
  })
  it('holds while no slot list has landed yet, and settles when one does', async () => {
    const { store } = renderSidebar([], { dashboard: { slotsLoaded: false, slotsGeneration: 0 }, tagColumns: [] })
    for (let i = 0; i < 4; i++) {
      await act(() => new Promise<void>(resolve => requestAnimationFrame(() => resolve())))
    }
    expect(document.querySelector('[data-row-anim]')?.getAttribute('data-row-anim')).toBe('holding')
    act(() => { store.dispatch(sseSlots(mkSlots(2) as never)) })
    await settleSidebarRowAnim()
  })
  it('enrolls only the first displacement window at every list size', async () => {
    const total = SIDEBAR_DISPLACEMENT_WINDOW + 40
    renderSidebar(mkSlots(total))
    await settleSidebarRowAnim()
    const rows = rowWrappers()
    expect(rows.length).toBe(total)
    for (const [index, el] of rows.entries()) {
      if (index < SIDEBAR_DISPLACEMENT_WINDOW) {
        expect(el.getAttribute('data-layout-id')).toMatch(/^slot-/)
        expect(el.getAttribute('data-layout')).toBe('position')
      } else {
        expect(el.getAttribute('data-layout-id')).toBeNull()
        expect(el.getAttribute('data-layout')).toBe('false')
      }
    }
  })

  // Folder rows are rendered by a CHILD (SortableFolderBlock calls
  // renderFolderBlock from its own render), which React may render again on
  // its own: dnd-kit re-renders it when it re-measures a droppable, and
  // StrictMode renders it twice. The repeat call must stamp the rows exactly as
  // the first did. A running per-render counter handed the repeat call stamps
  // past the window, so every row in the folder lost its projection (and, in
  // production, re-rendered twice for nothing). StrictMode is the deterministic
  // way to get that repeat call in jsdom.
  it('keeps folder rows inside the window when their block renders again', async () => {
    const total = SIDEBAR_DISPLACEMENT_WINDOW + 10
    const folder = { id: 'fbig', name: 'Big', order: 0, collapsed: false, created_at: 1 }
    const slots = mkSlots(total).map(s => ({ ...s, folder_id: 'fbig' }))
    renderSidebar(slots, { folders: [folder], strict: true })
    await settleSidebarRowAnim()
    const rows = rowWrappers()
    expect(rows.length).toBe(total)
    const enrolled = rows.filter(el => /^slot-/.test(el.getAttribute('data-layout-id') ?? ''))
    expect(enrolled).toHaveLength(SIDEBAR_DISPLACEMENT_WINDOW)
    expect(enrolled).toEqual(rows.slice(0, SIDEBAR_DISPLACEMENT_WINDOW))
  })
})

describe('lastActivityEpoch parse cache', () => {
  it('returns stable values for repeated ISO strings and still ranks garbage last', async () => {
    const { lastActivityEpoch } = await import('../pages/chat/sessionOrder')
    const a = { key: 'a', last_ts: '2026-08-29T00:00:00Z' }
    const first = lastActivityEpoch(a)
    expect(first).toBeGreaterThan(0)
    // Cache hit must return the identical value.
    expect(lastActivityEpoch(a)).toBe(first)
    // NaN handling survives the cache: unparseable -> 0, consistently.
    const bad = { key: 'b', last_ts: 'not-a-date' }
    expect(lastActivityEpoch(bad)).toBe(0)
    expect(lastActivityEpoch(bad)).toBe(0)
    // `modified` still bypasses the ladder entirely.
    expect(lastActivityEpoch({ key: 'c', modified: 123 })).toBe(123)
  })
})
