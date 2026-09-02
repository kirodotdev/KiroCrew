/**
 * Board view (tag-columns) folder DragOverlay portal: the drag ghost must be
 * mounted under document.body, NOT inside the sidebar subtree.
 *
 * Why: the sidebar renders inside OverlayDrawer's morph clip-path, and a
 * clip-path clips every descendant INCLUDING fixed-position ones — so an
 * un-portaled overlay disappears the moment the ghost strays past the drawer
 * edge. The list lane's shared overlay has the same defect and is being fixed
 * separately; this suite pins the per-column board overlay only.
 *
 * dnd-kit's pointer-drag lifecycle can't run in this DOM environment (it
 * needs real PointerEvents + layout measurement), so the DndContext is
 * stubbed to hand the test its onDragStart, and the real react-dom
 * createPortal does the mounting under test. Mutation check: removing the
 * createPortal wrapper around the board DragOverlay re-parents the ghost into
 * the sidebar subtree and turns both assertions red.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatTag, TagColumn, ChatFolder } from '../types'

// Render framer-motion elements as plain DOM (happy-dom can't run projection).
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
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({ chatFolders: vi.fn(), tagColumns: vi.fn(), chatTags: vi.fn() }))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
  }),
}))

// Capture each DndContext's onDragStart so the test can put a folder drag in
// flight without pointer events. DragOverlay is presentational and reads the
// active item off dnd-kit's internal store (which the stub doesn't provide),
// so it passes children through — the mounting under test is done by the REAL
// createPortal in the component, not by this stub.
const dnd = vi.hoisted(() => ({ starts: [] as Array<(e: unknown) => void> }))
vi.mock('@dnd-kit/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@dnd-kit/core')>()
  return {
    ...actual,
    DndContext: (props: { children?: unknown; onDragStart?: (e: unknown) => void }) => {
      // Every DndContext is handed the same memoised callback and re-renders
      // re-register it, so dedupe by reference — this collapses to a single
      // entry, not one per context.
      if (props.onDragStart && !dnd.starts.includes(props.onDragStart)) dnd.starts.push(props.onDragStart)
      return props.children as never
    },
    DragOverlay: (props: { children?: unknown }) => props.children as never,
  }
})

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'

const REVIEW = '22222222-2222-2222-2222-222222222222'
const COL_A = 'col-aaaa'
const FOLDER_A = 'folder-aaaa'
const FOLDER_B = 'folder-bbbb'

const tags: ChatTag[] = [{ id: REVIEW, name: 'Review', color: '#1a1', order: 0, status: true }]
const columns: TagColumn[] = [{ id: COL_A, name: 'Review', tag_ids: [REVIEW], mode: 'any', order: 0 }]
const folders: ChatFolder[] = [
  { id: FOLDER_A, name: 'Alpha', order: 0 },
  { id: FOLDER_B, name: 'Bravo', order: 1 },
]

function renderSidebar() {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots: [], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as any,
    chat: { activeSlot: null } as any,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-tags'], tags)
  qc.setQueryData(['tag-columns'], columns)
  qc.setQueryData(['chat-folders'], folders)
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={[]} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  localStorage.clear()
  dnd.starts.length = 0
  mocks.chatFolders.mockResolvedValue(folders)
  mocks.tagColumns.mockResolvedValue(columns)
  mocks.chatTags.mockResolvedValue(tags)
})
afterEach(() => vi.clearAllMocks())

/** Put a folder drag in flight through the column DndContext's own handler. */
function startFolderDrag() {
  expect(dnd.starts.length).toBeGreaterThan(0)
  act(() => {
    for (const start of dnd.starts) {
      start({ active: { id: FOLDER_A, data: { current: { type: 'folder' } } } })
    }
  })
}

describe('board view: folder DragOverlay is portaled out of the clipped drawer', () => {
  it('mounts the drag ghost directly under document.body', () => {
    renderSidebar()
    expect(document.body.querySelector('[data-testid="folder-drag-ghost"]')).toBeNull()
    startFolderDrag()
    const ghost = document.body.querySelector('[data-testid="folder-drag-ghost"]') as HTMLElement
    expect(ghost).toBeTruthy()
    // Portaled: the ghost's parent chain must reach document.body WITHOUT
    // passing through the board column strip. Walking parents (rather than
    // asserting parentElement === body) keeps the test honest if a wrapper
    // element is ever added around the overlay inside the portal.
    let inColumnStrip = false
    for (let el = ghost.parentElement; el; el = el.parentElement) {
      if (el.getAttribute?.('data-testid') === 'column-strip') inColumnStrip = true
    }
    expect(inColumnStrip).toBe(false)
  })

  it('keeps the ghost outside the sidebar render subtree (the clipped region)', () => {
    const { container } = renderSidebar()
    startFolderDrag()
    const ghost = document.body.querySelector('[data-testid="folder-drag-ghost"]') as HTMLElement
    expect(ghost).toBeTruthy()
    // The render container stands in for the OverlayDrawer clip-path region:
    // everything inside it is clipped, so the ghost must not be a descendant.
    expect(container.contains(ghost)).toBe(false)
  })
})
