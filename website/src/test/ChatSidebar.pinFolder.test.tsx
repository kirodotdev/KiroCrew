/**
 * Chat sidebar — the folder pin.
 *
 * A pinned folder's sessions (subfolders included) stay listed while the status
 * chips, the tag chips or the folder checkboxes narrow the list. Text search is
 * NOT stepped over: a query names what the person wants to see, a chip only
 * trims the view. The pin lives on the folder (`PATCH /api/chat/folders/:id
 * {pinned}`), the rows under it are not pinned individually, and the controls
 * that the pin overrides read inert rather than offering a tick that would
 * change nothing on screen.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// Render framer-motion elements as plain DOM (happy-dom can't run projection).
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
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({
  chatFolders: vi.fn(),
  updateChatFolder: vi.fn(),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (target, prop: string) => {
      if (prop in target) return target[prop]
      if (prop === 'chatTags') return vi.fn().mockResolvedValue([
        { id: 't1', name: 'Alpha', color: '#ff0000', order: 0 },
      ])
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
import type { ChatFolder, ChatSlot } from '../types'

const RUNNING_ONLY_LS_KEY = 'mc-session-running-only'
const TAG_FILTER_LS_KEY = 'mc-session-tag-filter'
const HIDDEN_FOLDERS_LS_KEY = 'mc-flat-hidden-folders'

/** Two folders of one idle, untagged session each; `pinnedF` is pinned, `plainF` is not. */
const FOLDERS: ChatFolder[] = [
  { id: 'pinnedF', name: 'oncall', collapsed: false, order: 0, pinned: true },
  { id: 'plainF', name: 'research', collapsed: false, order: 1 },
]
const SLOTS = [
  { key: 'chat-1-100', title: 'oncall session', running: false, messages: 2, folder_id: 'pinnedF' },
  { key: 'chat-2-200', title: 'research session', running: false, messages: 2, folder_id: 'plainF' },
] as unknown as ChatSlot[]

function renderSidebar(slots: ChatSlot[] = SLOTS, folders: ChatFolder[] = FOLDERS) {
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      ...defaults.chat,
      activeSlot: null, slotStatusDetail: {}, revealRequest: null, revealNonce: 0,
    } as unknown as RootState['chat'],
  })
  // staleTime keeps the seeded folder list authoritative: the blanket api mock
  // resolves every call to [], so an on-mount refetch would wipe the folders.
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

/** Open a folder header's ⋯ menu. Keyboard activation is the path happy-dom
 *  handles, and the menu lives only for the current tick, so callers drive the
 *  item they want synchronously (see ChatSidebarW3Coverage for the full note). */
function openFolderMenu(folderId: string) {
  fireEvent.keyDown(screen.getByTestId(`folder-menu-${folderId}`), { key: 'Enter' })
  expect(screen.getByTestId(`folder-settings-${folderId}`)).toBeTruthy()
}

beforeEach(() => {
  localStorage.clear()
  mocks.chatFolders.mockReset().mockResolvedValue([])
  mocks.updateChatFolder.mockReset().mockResolvedValue({})
})
afterEach(() => vi.clearAllMocks())

describe('a pinned folder steps over the chips and the folder checkboxes', () => {
  it('status chip: an idle session in a pinned folder stays under Running, one in a plain folder goes', async () => {
    localStorage.setItem(RUNNING_ONLY_LS_KEY, '1')
    const { queryByText } = renderSidebar()
    await waitFor(() => expect(queryByText('oncall session')).not.toBeNull())
    expect(queryByText('research session')).toBeNull()
  })

  it('tag chip: an untagged session in a pinned folder stays, one in a plain folder goes', async () => {
    localStorage.setItem(TAG_FILTER_LS_KEY, JSON.stringify(['t1']))
    const { queryByText } = renderSidebar()
    await waitFor(() => expect(queryByText('oncall session')).not.toBeNull())
    // The vocabulary resolves asynchronously; the plain row goes once it has.
    await waitFor(() => expect(queryByText('research session')).toBeNull())
  })

  it('folder checkbox: unchecking a pinned folder hides nothing, in the tree and in the flat lane', async () => {
    localStorage.setItem(HIDDEN_FOLDERS_LS_KEY, JSON.stringify(['pinnedF', 'plainF']))
    const tree = renderSidebar()
    await waitFor(() => expect(tree.queryByText('oncall session')).not.toBeNull())
    expect(tree.queryByText('research session')).toBeNull()
    tree.unmount()
    localStorage.setItem('mc-sidebar-flat-view', '1')
    const flat = renderSidebar()
    await waitFor(() => expect(flat.queryByText('oncall session')).not.toBeNull())
    expect(flat.queryByText('research session')).toBeNull()
  })

  it('the pin covers the whole subtree: a session in a subfolder of a pinned folder stays', async () => {
    localStorage.setItem(RUNNING_ONLY_LS_KEY, '1')
    const folders: ChatFolder[] = [
      ...FOLDERS,
      { id: 'childF', name: 'pages', collapsed: false, order: 0, parent_id: 'pinnedF' },
      { id: 'plainChildF', name: 'notes', collapsed: false, order: 0, parent_id: 'plainF' },
    ]
    const slots = [
      { key: 'chat-3-300', title: 'oncall pages session', running: false, messages: 2, folder_id: 'childF' },
      { key: 'chat-4-400', title: 'research notes session', running: false, messages: 2, folder_id: 'plainChildF' },
    ] as unknown as ChatSlot[]
    const { queryByText } = renderSidebar(slots, folders)
    await waitFor(() => expect(queryByText('oncall pages session')).not.toBeNull())
    expect(queryByText('research notes session')).toBeNull()
  })

  it('an unchecked ancestor keeps its block as the container of a pinned subfolder, but its own sessions go, in the tree and in the flat lane', async () => {
    // `parent` is unchecked, its child `oncallF` is pinned. The parent's block
    // renders, because dropping it would drop the pinned child's sessions in
    // the tree and the board while the flat lane still listed them. The pin
    // covers the subtree, not the chain: the parent's OWN session answers to
    // the parent's checkbox, in every lane the same way.
    const folders: ChatFolder[] = [
      { id: 'parentF', name: 'ops', collapsed: false, order: 0 },
      { id: 'pinnedF', name: 'oncall', collapsed: false, order: 0, parent_id: 'parentF', pinned: true },
      { id: 'plainF', name: 'research', collapsed: false, order: 1 },
    ]
    const slots = [
      ...SLOTS,
      { key: 'chat-6-600', title: 'ops rota', running: false, messages: 2, folder_id: 'parentF' },
    ] as unknown as ChatSlot[]
    localStorage.setItem(HIDDEN_FOLDERS_LS_KEY, JSON.stringify(['parentF', 'plainF']))
    const tree = renderSidebar(slots, folders)
    await waitFor(() => expect(tree.queryByText('oncall session')).not.toBeNull())
    // The parent's header is on screen, and the pinned child's block sits in it.
    const parentBlock = tree.getByTestId('folder-menu-parentF').closest('[data-folder-drop="parentF"]')
    expect(parentBlock).not.toBeNull()
    expect(parentBlock!.contains(tree.getByText('oncall session'))).toBe(true)
    expect(tree.queryByText('ops rota')).toBeNull()
    expect(tree.queryByText('research session')).toBeNull()
    // No reveal row offers the parent: its block is already on screen, so a peek
    // would draw it twice. The root row announces the plain folder alone.
    expect(tree.getByTestId('hidden-reveal-root').textContent).toContain('1 hidden folder')
    fireEvent.click(tree.getByTestId('hidden-reveal-root').querySelector('button')!)
    expect(tree.getAllByTestId('folder-menu-parentF')).toHaveLength(1)
    expect(tree.queryByText('research session')).not.toBeNull()
    tree.unmount()
    localStorage.setItem('mc-sidebar-flat-view', '1')
    const flat = renderSidebar(slots, folders)
    await waitFor(() => expect(flat.queryByText('oncall session')).not.toBeNull())
    expect(flat.queryByText('ops rota')).toBeNull()
    expect(flat.queryByText('research session')).toBeNull()
  })

  it('the chips still narrow an ancestor of a pinned folder: the pin covers the subtree, not the chain', async () => {
    const folders: ChatFolder[] = [
      { id: 'parentF', name: 'ops', collapsed: false, order: 0 },
      { id: 'pinnedF', name: 'oncall', collapsed: false, order: 0, parent_id: 'parentF', pinned: true },
    ]
    const slots = [
      { key: 'chat-1-100', title: 'oncall session', running: false, messages: 2, folder_id: 'pinnedF' },
      { key: 'chat-6-600', title: 'ops rota', running: false, messages: 2, folder_id: 'parentF' },
    ] as unknown as ChatSlot[]
    localStorage.setItem(RUNNING_ONLY_LS_KEY, '1')
    const { queryByText } = renderSidebar(slots, folders)
    await waitFor(() => expect(queryByText('oncall session')).not.toBeNull())
    expect(queryByText('ops rota')).toBeNull()
  })

  it('text search still narrows a pinned folder: a non-matching session goes', async () => {
    const slots = [
      ...SLOTS,
      { key: 'chat-5-500', title: 'oncall runbook', running: false, messages: 2, folder_id: 'pinnedF' },
    ] as unknown as ChatSlot[]
    const utils = renderSidebar(slots)
    await waitFor(() => expect(utils.queryByText('oncall session')).not.toBeNull())
    fireEvent.change(utils.getByPlaceholderText('Search sessions…'), { target: { value: 'runbook' } })
    await waitFor(() => expect(utils.queryByText('oncall session')).toBeNull())
    expect(utils.queryByText('oncall runbook')).not.toBeNull()
  })

  it('the chip still shows as narrowing: other rows are narrowed, the list is not "unfiltered"', async () => {
    localStorage.setItem(RUNNING_ONLY_LS_KEY, '1')
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('oncall session')).not.toBeNull())
    // The plain folder's session is gone, so the list IS narrowed; the pinned
    // row surviving must not read as "nothing is filtered".
    expect(utils.queryByText('research session')).toBeNull()
  })
})

describe('the pin on the folder, not on its rows', () => {
  it('the pinned folder header carries the pin glyph; a plain one does not', async () => {
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('oncall session')).not.toBeNull())
    expect(utils.getByTestId('folder-pinned-pinnedF')).toBeTruthy()
    expect(utils.queryByTestId('folder-pinned-plainF')).toBeNull()
  })

  it('a session in a pinned folder is not itself pinned: no pinned-session marker on its row', async () => {
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('oncall session')).not.toBeNull())
    const row = utils.getByText('oncall session').closest('[data-session-row]')
    expect(row).not.toBeNull()
    // The session pin renders its glyph on the row, titled "Pinned"; a folder
    // pin leaves the row exactly as an unpinned session's.
    expect(row!.querySelector('[title="Pinned"]')).toBeNull()
    expect(utils.queryByTestId('pinned-session-divider')).toBeNull()
  })
})

describe('the folder menu', () => {
  it('offers Pin folder on a plain folder and PATCHes pinned: true', async () => {
    renderSidebar()
    openFolderMenu('plainF')
    expect(screen.getByTestId('folder-pin-plainF').textContent).toContain('Pin folder')
    fireEvent.click(screen.getByTestId('folder-pin-plainF'))
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith('plainF', { pinned: true }))
  })

  it('offers Unpin folder on a pinned folder and PATCHes pinned: false', async () => {
    renderSidebar()
    openFolderMenu('pinnedF')
    expect(screen.getByTestId('folder-pin-pinnedF').textContent).toContain('Unpin folder')
    fireEvent.click(screen.getByTestId('folder-pin-pinnedF'))
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith('pinnedF', { pinned: false }))
  })

  it('keeps Hide folder under a pin but disabled, saying why; a plain folder gets the working item', async () => {
    const first = renderSidebar()
    openFolderMenu('pinnedF')
    const locked = screen.getByTestId('folder-visibility-pinnedF')
    expect(locked.getAttribute('data-disabled')).not.toBeNull()
    expect(locked.textContent).toContain('Hide folder')
    // The reason is a line in the item, not only a title: a disabled item takes
    // no pointer events, so a hover tooltip would never show.
    const reason = screen.getByTestId('folder-visibility-reason-pinnedF')
    expect(locked.contains(reason)).toBe(true)
    expect(reason.textContent).toBe('oncall is pinned: its sessions stay visible when filters are on. Search still applies.')
    fireEvent.click(locked)
    expect(JSON.parse(localStorage.getItem(HIDDEN_FOLDERS_LS_KEY) ?? '[]')).toEqual([])
    first.unmount()
    renderSidebar()
    openFolderMenu('plainF')
    const plain = screen.getByTestId('folder-visibility-plainF')
    expect(plain.getAttribute('data-disabled')).toBeNull()
    expect(screen.queryByTestId('folder-visibility-reason-plainF')).toBeNull()
  })

  it('keeps Hide folder disabled on an ancestor of a pinned folder, with the holds-a-pin reason', async () => {
    const folders: ChatFolder[] = [
      { id: 'parentF', name: 'ops', collapsed: false, order: 0 },
      { id: 'pinnedF', name: 'oncall', collapsed: false, order: 0, parent_id: 'parentF', pinned: true },
    ]
    renderSidebar(SLOTS, folders)
    openFolderMenu('parentF')
    const locked = screen.getByTestId('folder-visibility-parentF')
    expect(locked.getAttribute('data-disabled')).not.toBeNull()
    expect(screen.getByTestId('folder-visibility-reason-parentF').textContent).toBe('ops holds a pinned folder, so it stays listed')
  })

  it('carries the behaviour sentence as the title of the enabled Pin folder and Unpin folder items', async () => {
    const TITLE = "A pinned folder's sessions stay listed while filters are on, subfolders included. Search still applies, and the folder keeps its place in the list."
    const first = renderSidebar()
    openFolderMenu('plainF')
    expect(screen.getByTestId('folder-pin-plainF').getAttribute('title')).toBe(TITLE)
    first.unmount()
    renderSidebar()
    openFolderMenu('pinnedF')
    expect(screen.getByTestId('folder-pin-pinnedF').getAttribute('title')).toBe(TITLE)
  })

  it('pinning a hidden folder leaves the saved hide alone: the pin steps over it while it holds', async () => {
    localStorage.setItem(HIDDEN_FOLDERS_LS_KEY, JSON.stringify(['plainF']))
    const utils = renderSidebar()
    expect(utils.queryByText('research session')).toBeNull()
    // A hidden folder's block is gone from the tree, header and all; the way to
    // its menu is the reveal row that peeks the hidden folders open in place.
    fireEvent.click(utils.getByTestId('hidden-reveal-root').querySelector('button')!)
    openFolderMenu('plainF')
    fireEvent.click(screen.getByTestId('folder-pin-plainF'))
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith('plainF', { pinned: true }))
    // The optimistic cache update already carries `pinned`, so the row is back,
    // and the preference the person set is still saved for when it is unpinned.
    await waitFor(() => expect(utils.queryByText('research session')).not.toBeNull())
    expect(JSON.parse(localStorage.getItem(HIDDEN_FOLDERS_LS_KEY) ?? '[]')).toEqual(['plainF'])
  })
})

describe('the filter menu folder row', () => {
  const HOLDS = 'ops holds a pinned folder, so it stays listed'
  const PINNED = 'oncall is pinned: its sessions stay visible when filters are on. Search still applies.'

  it('stays a working checkbox for an ancestor of a pinned folder, reading its real state', async () => {
    // The ancestor's checkbox still does something (it hides the ancestor's own
    // sessions; the pinned block stays as its container), so the row is neither
    // inert nor forced to read checked: it reads the stored uncheck, carries no
    // reason line and no pin in the slot, and re-checking it brings the
    // ancestor's own sessions back.
    const folders: ChatFolder[] = [
      { id: 'parentF', name: 'ops', collapsed: false, order: 0 },
      { id: 'pinnedF', name: 'oncall', collapsed: false, order: 0, parent_id: 'parentF', pinned: true },
    ]
    const slots = [
      ...SLOTS,
      { key: 'chat-6-600', title: 'ops rota', running: false, messages: 2, folder_id: 'parentF' },
    ] as unknown as ChatSlot[]
    localStorage.setItem(HIDDEN_FOLDERS_LS_KEY, JSON.stringify(['parentF']))
    const utils = renderSidebar(slots, folders)
    await waitFor(() => expect(utils.queryByText('oncall session')).not.toBeNull())
    expect(utils.queryByText('ops rota')).toBeNull()
    fireEvent.keyDown(utils.getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    const row = await screen.findByTestId('folder-filter-parentF')
    expect(row.getAttribute('aria-checked')).toBe('false')
    expect(row.getAttribute('data-disabled')).toBeNull()
    expect(row.getAttribute('title')).not.toBe(HOLDS)
    expect(screen.queryByTestId('folder-filter-reason-parentF')).toBeNull()
    expect(screen.queryByTestId('folder-filter-lock-parentF')).toBeNull()
    // The uncheck is announced here, not by the hidden-folders count: the block
    // is on screen, so no reveal row could peek it.
    expect(utils.getByLabelText('Sort and filter sessions').getAttribute('data-folder-hide-active')).toBeNull()
    fireEvent.click(row)
    await waitFor(() => expect(utils.queryByText('ops rota')).not.toBeNull())
    expect(row.getAttribute('aria-checked')).toBe('true')
  })

  it('reads checked and inert for a pinned folder, and says why under its name', async () => {
    localStorage.setItem(HIDDEN_FOLDERS_LS_KEY, JSON.stringify(['pinnedF']))
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('oncall session')).not.toBeNull())
    fireEvent.keyDown(utils.getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    const row = await screen.findByTestId('folder-filter-pinnedF')
    expect(row.getAttribute('aria-checked')).toBe('true')
    expect(row.getAttribute('data-disabled')).not.toBeNull()
    expect(row.getAttribute('title')).toBe(PINNED)
    const reason = screen.getByTestId('folder-filter-reason-pinnedF')
    expect(row.contains(reason)).toBe(true)
    expect(reason.textContent).toBe(PINNED)
    expect(row.contains(screen.getByTestId('folder-filter-lock-pinnedF'))).toBe(true)
    // A plain row stays a working checkbox: a tick, no pin in the slot, no reason line.
    const plain = screen.getByTestId('folder-filter-plainF')
    expect(plain.getAttribute('aria-checked')).toBe('true')
    expect(plain.getAttribute('data-disabled')).toBeNull()
    expect(screen.queryByTestId('folder-filter-reason-plainF')).toBeNull()
    expect(screen.queryByTestId('folder-filter-lock-plainF')).toBeNull()
  })
})
