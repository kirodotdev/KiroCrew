/**
 * A mate's home folder (`mate_home`, no parent) and everything under it is
 * managed by the gateway: a person may open and collapse it, and the backend
 * refuses every other write (403 mate_home_locked). The sidebar must therefore
 * not OFFER those writes anywhere, not only in the list view's folder row.
 *
 * Load-bearing assertions:
 *   (1) the header's "New chat in folder" picker omits a mate home and its
 *       child folder, and keeps an ordinary folder;
 *   (2) with only mate folders, the picker is absent rather than empty;
 *   (3) a board column's copy of a mate home renders no ⋯ menu, no header
 *       new-chat button and no empty-folder new-chat row, while an ordinary
 *       folder in the same column keeps all three.
 *
 * Radix DropdownMenu cannot be opened by mouse in jsdom (needs PointerEvent),
 * so the create menu is opened by keyboard. The phone branch lists folder rows
 * inline, so they are queryable without opening a flyout.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatFolder, ChatTag, TagColumn } from '../types'

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

const cfg = vi.hoisted(() => ({ value: {} as Record<string, unknown> }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => cfg.value,
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({ createChatSlot: vi.fn(), chatFolders: vi.fn() }))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
  }),
}))

// `useIsMobile` resolves its media query at module load, so the hook itself is
// mocked: the phone branch lists the picker's folder rows inline.
const mobile = vi.hoisted(() => ({ value: false }))
vi.mock('../hooks/useIsMobile', () => ({
  MOBILE_BREAKPOINT: 768,
  useIsMobile: () => mobile.value,
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

const HOME = 'f-home'
const HOME_CHILD = 'f-home-child'
const PLAIN = 'f-plain'
const COL = 'col-aaaa'
const TAG = '11111111-1111-1111-1111-111111111111'

const mateHome: ChatFolder = { id: HOME, name: 'Radar', order: 0, mate_home: true, owner_app: 'member:radar' }
const mateChild: ChatFolder = { id: HOME_CHILD, name: 'Radar inbox', order: 0, parent_id: HOME }
const plain: ChatFolder = { id: PLAIN, name: 'Research', order: 1 }

const tags: ChatTag[] = [{ id: TAG, name: 'Review', color: '#1a1', order: 0, status: true }]
const columns: TagColumn[] = [{ id: COL, name: 'Review', tag_ids: [TAG], mode: 'any', order: 0 }]

function renderSidebar(folders: ChatFolder[]) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots: [], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  qc.setQueryData(['chat-tags'], tags)
  qc.setQueryData(['tag-columns'], columns)
  mocks.chatFolders.mockResolvedValue(folders)
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

function openCreateMenu() {
  fireEvent.keyDown(screen.getByLabelText('More create options'), { key: 'Enter' })
}

beforeEach(() => {
  localStorage.clear()
  mobile.value = false
  cfg.value = { tagColumnsEnabled: false, confirmCloseSession: false, hideEmptyFolderBody: false }
  mocks.createChatSlot.mockResolvedValue({ key: 'chat-new-1' })
})
afterEach(() => vi.clearAllMocks())

describe('mate-managed folders offer no person writes', () => {
  it('the header "New chat in folder" picker omits a mate home and its child', async () => {
    mobile.value = true
    renderSidebar([mateHome, mateChild, plain])
    openCreateMenu()
    await screen.findByTestId('new-chat-in-folder-caption')
    const menu = within(screen.getByRole('menu'))
    expect(menu.getByText('Research').closest('[role="menuitem"]')).not.toBeNull()
    expect(menu.queryByText('Radar')).toBeNull()
    expect(menu.queryByText('Radar inbox')).toBeNull()
  })

  it('drops the picker entirely when only mate folders exist', async () => {
    mobile.value = true
    renderSidebar([mateHome, mateChild])
    openCreateMenu()
    // The menu is open (its fixed rows render) but carries no folder section.
    await screen.findByTestId('new-temporary-chat')
    expect(screen.queryByTestId('new-chat-in-folder-caption')).toBeNull()
  })

  it('a board column renders no menu or new-chat control for a mate home', () => {
    cfg.value = { ...cfg.value, tagColumnsEnabled: true }
    const { container } = renderSidebar([mateHome, plain])
    const q = (id: string) => container.querySelector(`[data-testid="${id}"]`)
    // Both folders render in the column: the mate home stays openable.
    expect(q(`col-${COL}-folder-${HOME}`)).not.toBeNull()
    expect(q(`col-${COL}-folder-${PLAIN}`)).not.toBeNull()
    // The ordinary folder keeps its controls, so the absences below are real.
    expect(q(`col-${COL}-folder-${PLAIN}-menu`)).not.toBeNull()
    expect(q(`col-${COL}-folder-${PLAIN}-new-chat`)).not.toBeNull()
    expect(q(`col-${COL}-folder-${PLAIN}-empty-new-chat`)).not.toBeNull()
    expect(q(`col-${COL}-folder-${HOME}-menu`)).toBeNull()
    expect(q(`col-${COL}-folder-${HOME}-new-chat`)).toBeNull()
    expect(q(`col-${COL}-folder-${HOME}-empty-new-chat`)).toBeNull()
  })
})
