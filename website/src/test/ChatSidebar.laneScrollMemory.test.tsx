/**
 * The session lane remembers WHICH ROW was at its top across a sidebar remount.
 *
 * Collapsing the sessions sidebar (desktop toggle) or closing the mobile
 * drawer UNMOUNTS ChatSidebar — OverlayDrawer gates its children on `open` —
 * so a user who had scrolled deep into a long list came back to the top on
 * every reopen. The lane records the top visible session row plus its offset
 * (`useLaneScrollMemory`) and, on the next mount, scrolls that row back to the
 * same spot — a row anchor rather than a pixel offset, because rows are
 * `content-visibility: auto` and a fresh mount lays never-rendered rows out
 * at a placeholder height, so the same scrollTop would land on a different
 * session.
 *
 * happy-dom has no layout, so row geometry is stubbed: each row reports a
 * rect derived from a fixed row height and the lane's current scrollTop.
 *
 * Locks the contract:
 *  (1) Tree lane: unmount + remount puts the recorded anchor row back at the
 *      recorded offset — even when rows are laid out at a DIFFERENT height on
 *      the second mount.
 *  (2) Flat lane: same, and a SEPARATE entry from the tree lane, so toggling
 *      views does not bleed one lane's anchor into the other.
 *  (3) Anchor row gone (closed while collapsed): falls back to the pixel
 *      offset rather than staying at the top.
 *  (4) Nothing recorded: a fresh mount stays at the top.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, fireEvent, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatFolder, ChatSlot } from '../types'
import { _resetLaneScrollMemory, _laneScrollAnchor } from '../hooks/useLaneScrollMemory'

// Render framer-motion elements as plain DOM (happy-dom can't run projection).
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef<HTMLElement, Record<string, unknown>>((props, ref) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  // Cache per tag: a fresh component type per property access would remount
  // every motion element on each render — the real `motion.div` is stable,
  // and this test observes a DOM node's scrollTop across renders.
  const cache = new Map<string, unknown>()
  const motion = new Proxy({}, { get: (_t, tag: string) => {
    if (!cache.has(tag)) cache.set(tag, make(tag))
    return cache.get(tag)
  } })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    useReducedMotion: () => false,
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
// Legacy single-lane list (no tag columns): the lane under test is the tree /
// flat lane, not a board column.
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({ folders: [] as unknown[] }))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, p: string) => {
      if (p === 'chatFolders') return vi.fn().mockImplementation(() => Promise.resolve(mocks.folders))
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
import type { RootState } from '../store'

const FLAT_VIEW_LS_KEY = 'mc-sidebar-flat-view'

// Recent activity, one minute apart: rows older than the stale-collapse
// threshold (7 days) fold into the collapsed stale section and would not be
// rendered as rows at all.
const slot = (key: string, title: string, folderId: string | null, minutesAgo: number): ChatSlot => ({
  key, title, messages: 1, running: false, mode: '', created: '', pinned: false,
  last_ts: new Date(Date.now() - minutesAgo * 60_000).toISOString(),
  folder_id: folderId,
} as unknown as ChatSlot)

const FOLDER: ChatFolder = { id: 'f1', name: 'Work', order: 0, collapsed: false } as unknown as ChatFolder
// Half the sessions live in a folder: the tree lane renders the root rows and
// the folder's rows in one scroller, which is the lane under test.
const SLOTS = Array.from({ length: 40 }, (_, i) => slot(`chat-${i}`, `Session ${i}`, i % 2 ? 'f1' : null, i + 1))

function renderSidebar(folders: ChatFolder[], slots: ChatSlot[] = SLOTS) {
  mocks.folders = folders
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  const tree = (
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
    </QueryClientProvider>
  )
  return render(tree)
}

const lane = (testId: 'tree-view-lane' | 'flat-view-lane') => {
  const el = document.querySelector(`[data-testid="${testId}"]`) as HTMLElement | null
  if (!el) throw new Error(`no ${testId} rendered`)
  return el
}

const rect = (top: number, h: number) =>
  ({ top, bottom: top + h, left: 0, right: 240, width: 240, height: h, x: 0, y: top, toJSON: () => ({}) }) as DOMRect

/** Geometry model: the lane's top edge at y=0 and 715px tall; its session rows
 * stacked `rowH` px each in DOM order, shifted up by the lane's current
 * scrollTop. Installed on the prototype so rows that mount AFTER `layout()` is
 * called (the tree lane fills in once the folders query settles) are measured
 * too. `scrollTop` is a plain settable property in happy-dom, so the restore's
 * corrections land. */
let rowH = 48
const realRect = Element.prototype.getBoundingClientRect
Element.prototype.getBoundingClientRect = function (this: Element) {
  const laneEl = this.closest('[data-testid$="-view-lane"]') as HTMLElement | null
  if (!laneEl) return realRect.call(this)
  if (laneEl === this) return rect(0, 715)
  if (!this.hasAttribute('data-session-row')) return realRect.call(this)
  const i = Array.from(laneEl.querySelectorAll('[data-session-row]')).indexOf(this)
  return rect(i * rowH - laneEl.scrollTop, rowH)
}
function layout(_laneEl: HTMLElement, h: number) { rowH = h }

const scrollTo = (el: HTMLElement, top: number) => {
  el.scrollTop = top
  fireEvent.scroll(el)
}

/** Flush the rAF-coalesced measurement and the restore's settle frames. */
const settle = async () => {
  for (let i = 0; i < 16; i++) {
    await act(async () => { await new Promise(r => requestAnimationFrame(() => r(null))) })
  }
}

/** Row identities in DOM order (the tree lists the folder's rows first). */
const rowKeys = (el: HTMLElement) =>
  Array.from(el.querySelectorAll('[data-session-row]')).map(r => r.getAttribute('data-session-row'))

const laneTopRow = (el: HTMLElement) => {
  const laneTop = el.getBoundingClientRect().top
  return Array.from(el.querySelectorAll<HTMLElement>('[data-session-row]'))
    .find(r => r.getBoundingClientRect().bottom > laneTop + 1)?.getAttribute('data-session-row')
}

beforeEach(() => {
  _resetLaneScrollMemory()
  localStorage.clear()
})

describe('ChatSidebar session lane scroll memory', () => {
  it('tree lane: remount restores the anchor row even when row heights changed', async () => {
    const first = renderSidebar([FOLDER])
    const before = lane('tree-view-lane')
    layout(before, 48)
    // 20 rows of 48px scrolled out, plus 10px into the 21st row: it sits at -10.
    scrollTo(before, 20 * 48 + 10)
    await settle()
    const anchorRow = rowKeys(before)[20]
    expect(laneTopRow(before)).toBe(anchorRow)
    expect(_laneScrollAnchor('chat-sidebar-lane:tree')).toMatchObject({ row: anchorRow, offset: -10 })
    first.unmount()

    renderSidebar([FOLDER])
    const after = lane('tree-view-lane')
    // Fresh mount: rows are placeholders at 60px, so a pixel restore (970)
    // would land four rows short. The anchor must put the same row back at -10.
    layout(after, 60)
    await settle()
    expect(after.scrollTop).toBe(20 * 60 + 10)
    expect(laneTopRow(after)).toBe(anchorRow)
  })

  it('flat lane: restores its own anchor, independent of the tree lane', async () => {
    const treeMount = renderSidebar([FOLDER])
    const tree = lane('tree-view-lane')
    layout(tree, 48)
    scrollTo(tree, 5 * 48)
    await settle()
    treeMount.unmount()

    // Flat view is a persisted preference; the lane only exists with folders.
    localStorage.setItem(FLAT_VIEW_LS_KEY, '1')
    const flatMount = renderSidebar([FOLDER])
    const flat = lane('flat-view-lane')
    layout(flat, 48)
    await settle()
    // Nothing recorded for the flat lane yet: it must NOT inherit the tree's.
    expect(flat.scrollTop).toBe(0)
    scrollTo(flat, 30 * 48)
    await settle()
    const flatTop = laneTopRow(flat)
    expect(flatTop).toBeTruthy()
    flatMount.unmount()

    renderSidebar([FOLDER])
    const again = lane('flat-view-lane')
    layout(again, 48)
    await settle()
    expect(again.scrollTop).toBe(30 * 48)
    expect(laneTopRow(again)).toBe(flatTop)
  })

  it('anchor row closed while collapsed: falls back to the pixel offset', async () => {
    const first = renderSidebar([FOLDER])
    const before = lane('tree-view-lane')
    layout(before, 48)
    scrollTo(before, 12 * 48)
    await settle()
    const closed = rowKeys(before)[12]!
    expect(_laneScrollAnchor('chat-sidebar-lane:tree')?.row).toBe(closed)
    first.unmount()

    renderSidebar([FOLDER], SLOTS.filter(s => s.key !== closed))
    const after = lane('tree-view-lane')
    layout(after, 48)
    await settle()
    expect(after.scrollTop).toBe(12 * 48)
  })

  it('nothing recorded: a fresh mount stays at the top', async () => {
    renderSidebar([FOLDER])
    const el = lane('tree-view-lane')
    layout(el, 48)
    await settle()
    expect(el.scrollTop).toBe(0)
  })
})
