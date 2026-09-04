/**
 * Coverage for two ChatSidebar surfaces no sibling suite drives end to end:
 *
 *  1. The LIST-VIEW folder header — its collapse toggle, double-click rename,
 *     and every item of its ⋯ menu (Rename, New subfolder, Move folder to,
 *     Folder settings, Hide when empty, Delete folder) plus the row's
 *     "new chat in folder" button. The sibling suites assert the menu's items
 *     are PRESENT; here they are activated, so the folder mutations behind them
 *     (create / delete / optimistic PATCH + rollback) run.
 *  2. The sort-and-filter menu's write paths: toggling a filter on and clearing
 *     it from its chip, choosing a sort order, "Show all folders", and the
 *     Recent submenu's preset buttons + custom amount field.
 *
 * `FolderConfigModal` is stubbed to a submit/close pair: the modal itself is
 * covered by its own suite, and what is under test here is the sidebar's
 * `onSubmit` — which builds the create payload, and for edit mode builds a PATCH
 * from `draft.touched` only and rethrows so a rejected save keeps the modal open.
 *
 * Radix menus open on keyboard activation (Enter) — the path happy-dom handles;
 * their pointer path needs real PointerEvents. Submenus open on ArrowRight,
 * which is the one key the Recent row's own onKeyDown lets fall through.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ApiError } from '../api/apiError'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatFolder } from '../types'

// Render framer-motion elements as plain DOM because happy-dom cannot run projection.
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

/** What the stubbed modal hands back, and what the sidebar handed it. */
const modal = vi.hoisted(() => ({
  draft: {
    name: 'Gamma', color: '', projectDir: '', defaultAgent: '',
    touched: [] as string[],
  },
  props: null as Record<string, unknown> | null,
  submitError: null as unknown,
}))
vi.mock('../components/FolderConfigModal', async () => {
  const React = await import('react')
  return {
    default: (props: Record<string, unknown>) => {
      modal.props = props
      const folder = props.folder as ChatFolder | undefined
      // One button per wrapper: three sibling <button>s in one parent trip the
      // max-two-buttons-per-row review rule, test files included.
      return React.createElement('div', {
        'data-testid': 'folder-modal',
        'data-mode': String(props.mode),
        'data-parent': String(props.parentId ?? ''),
        'data-folder': folder?.id ?? '',
      }, [
        React.createElement('div', { key: 's' }, React.createElement('button', {
          type: 'button',
          'data-testid': 'folder-modal-submit',
          onClick: () => {
            modal.submitError = null
            void (props.onSubmit as (d: unknown) => Promise<void>)(modal.draft)
              .catch((err: unknown) => { modal.submitError = err })
          },
        }, 'save')),
        React.createElement('div', { key: 'c' }, React.createElement('button', {
          type: 'button',
          'data-testid': 'folder-modal-close',
          onClick: props.onClose as () => void,
        }, 'cancel')),
      ])
    },
  }
})

const cfg = vi.hoisted(() => ({
  value: { tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: false } as Record<string, unknown>,
}))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => cfg.value,
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({
  chatFolders: vi.fn(),
  createChatFolder: vi.fn(),
  updateChatFolder: vi.fn(),
  deleteChatFolder: vi.fn(),
  createChatSlot: vi.fn(),
  chatSlotProject: vi.fn(),
  setSlotColor: vi.fn(),
  sessions: vi.fn(),
  sessionsSearch: vi.fn(),
  chatTags: vi.fn(),
  tagColumns: vi.fn(),
  kirocrewConfig: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as unknown as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
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
import { RECENT_UNIT_MS, DEFAULT_RECENT_WINDOW_MS } from '../pages/recentWindow'

interface TestSlot {
  key: string
  title?: string
  running: boolean
  messages?: number
  folder_id?: string
  last_ts?: string
  last_turn_ts?: string
}

const ALPHA: ChatFolder = { id: 'f1', name: 'Alpha', order: 0 }
const BETA: ChatFolder = { id: 'f2', name: 'Beta', order: 1 }
const FILED: TestSlot = { key: 'k1', title: 'filed chat', running: false, messages: 2, folder_id: 'f1' }
const LOOSE: TestSlot = { key: 'k2', title: 'loose chat', running: false, messages: 1 }

function renderSidebar(opts: { slots?: TestSlot[]; folders?: ChatFolder[] } = {}) {
  const slots = opts.slots ?? [FILED, LOOSE]
  const folders = opts.folders ?? [ALPHA]
  mocks.chatFolders.mockResolvedValue(folders)
  // Redux Toolkit REPLACES a slice's state with `preloadedState` rather than
  // merging it with initialState, so spread the genuine defaults first — a
  // hand-rolled partial drops keys the reducers legitimately assume.
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
      activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {},
      goalLoops: {}, workflowRuns: {}, subagentQueued: {}, slotHistory: [],
    } as unknown as RootState['chat'],
  })
  // staleTime + refetchOnMount keep the seeded folder list authoritative. An
  // on-mount refetch resolving mid-test re-renders the folder rows and unmounts
  // an open ⋯ menu underneath the assertions.
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Infinity, refetchOnMount: false },
      mutations: { retry: false },
    },
  })
  qc.setQueryData(['chat-folders'], folders)
  qc.setQueryData(['tag-columns'], [])
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots as never} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false}
              defaultAgent="" installedAgents={[{ name: 'builder', source: 'builtin' }]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...view, store, qc }
}

/**
 * Open a folder header's ⋯ menu. Keyboard activation is the path happy-dom
 * handles (the pointer path needs real PointerEvents), and the menu survives
 * only for the current tick: Radix tears it down on the first macrotask because
 * nothing in happy-dom holds the focus it grabs. That is why this helper is
 * SYNCHRONOUS and every caller must drive the item it wants in the same tick,
 * with no await in between — the item's onClick still runs, and what it sets in
 * motion (a mutation, the rename field, the folder modal) outlives the menu.
 */
function openFolderMenu(folderId: string) {
  fireEvent.keyDown(screen.getByTestId(`folder-menu-${folderId}`), { key: 'Enter' })
  // The menu is up in this tick; confirm it so a silent no-open cannot
  // masquerade as a passing negative assertion.
  expect(screen.getByTestId(`folder-settings-${folderId}`)).toBeTruthy()
}

/**
 * The folder-header rename field. Every keystroke re-renders the sidebar and
 * replaces this input's DOM node, so a captured reference goes stale after the
 * first event — re-read it before each interaction instead. It is the only
 * textbox in the sidebar with no placeholder (the session search box has one).
 */
function folderRenameField(): HTMLInputElement {
  const el = screen.getAllByRole('textbox').find(node => !node.getAttribute('placeholder'))
  if (!el) throw new Error('folder rename field is not mounted')
  return el as HTMLInputElement
}

/** Open the sort-and-filter menu and wait for its Filter heading. */
async function openFilterMenu() {
  fireEvent.keyDown(screen.getByLabelText('Sort and filter sessions'), { key: 'Enter' })
  await screen.findByText('Filter')
}

beforeEach(() => {
  // The api stubs live in a hoisted object shared across tests, so their call
  // logs have to be cleared here or a later negative assertion inherits an
  // earlier test's calls.
  vi.clearAllMocks()
  localStorage.clear()
  cfg.value = { tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: false }
  modal.props = null
  modal.submitError = null
  modal.draft = { name: 'Gamma', color: '', projectDir: '', defaultAgent: '', touched: [] }
  mocks.chatFolders.mockResolvedValue([ALPHA])
  mocks.createChatFolder.mockResolvedValue({ id: 'f9', name: 'Gamma', order: 2 })
  mocks.updateChatFolder.mockResolvedValue({ ok: true })
  mocks.deleteChatFolder.mockResolvedValue({ ok: true })
  mocks.createChatSlot.mockResolvedValue({ key: 'k-new', title: '', running: false, messages: 0 })
  mocks.chatSlotProject.mockResolvedValue(undefined)
  mocks.setSlotColor.mockResolvedValue({ ok: true })
  mocks.sessions.mockResolvedValue({ sessions: [], has_more: false })
  mocks.sessionsSearch.mockResolvedValue({ sessions: [] })
  mocks.chatTags.mockResolvedValue([])
  mocks.tagColumns.mockResolvedValue([])
  mocks.kirocrewConfig.mockResolvedValue({ dashboard: { recent_tint_count: 3 } })
})
afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('ChatSidebar — list-view folder header', () => {
  it('collapses the folder through the row toggle', async () => {
    renderSidebar()
    fireEvent.click(screen.getByLabelText('Collapse folder Alpha'))
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith('f1', { collapsed: true }))
  })

  it('renames from the ⋯ menu and commits the trimmed name on Enter', async () => {
    renderSidebar()
    openFolderMenu('f1')
    fireEvent.click(screen.getByTestId('folder-rename-f1'))
    fireEvent.change(folderRenameField(), { target: { value: '  Renamed  ' } })
    fireEvent.keyDown(folderRenameField(), { key: 'Enter' })
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith('f1', { name: 'Renamed' }))
    // Committing closes the field.
    await waitFor(() => expect(screen.getAllByRole('textbox').every(n => n.getAttribute('placeholder'))).toBe(true))
  })

  it('opens the same field on a double-click of the name, and Escape abandons it', async () => {
    renderSidebar()
    fireEvent.doubleClick(screen.getByTitle('Double-click to rename'))
    fireEvent.change(folderRenameField(), { target: { value: 'Nope' } })
    fireEvent.keyDown(folderRenameField(), { key: 'Escape' })
    await waitFor(() => expect(screen.getAllByRole('textbox').every(n => n.getAttribute('placeholder'))).toBe(true))
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
    expect(screen.getByTitle('Double-click to rename').textContent).toBe('Alpha')
  })

  it('does not PATCH a name that trims to nothing', async () => {
    renderSidebar()
    openFolderMenu('f1')
    fireEvent.click(screen.getByTestId('folder-rename-f1'))
    fireEvent.change(folderRenameField(), { target: { value: '   ' } })
    fireEvent.keyDown(folderRenameField(), { key: 'Enter' })
    await waitFor(() => expect(screen.getAllByRole('textbox').every(n => n.getAttribute('placeholder'))).toBe(true))
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
  })

  it('creates a subfolder seeded with this folder as the parent', async () => {
    renderSidebar()
    openFolderMenu('f1')
    fireEvent.click(screen.getByText('New subfolder'))
    const host = await screen.findByTestId('folder-modal')
    expect(host.getAttribute('data-mode')).toBe('create')
    expect(host.getAttribute('data-parent')).toBe('f1')
    modal.draft = { name: 'Gamma', color: '#abcdef', projectDir: '', defaultAgent: 'builder', touched: ['name'] }
    fireEvent.click(screen.getByTestId('folder-modal-submit'))
    await waitFor(() => expect(mocks.createChatFolder).toHaveBeenCalledWith(
      'Gamma', 'f1', { project_dir: undefined, default_agent: 'builder', color: '#abcdef' },
    ))
    // Only a successful create closes the modal.
    await waitFor(() => expect(screen.queryByTestId('folder-modal')).toBeNull())
  })

  it('PATCHes only the fields the settings modal reports as touched', async () => {
    renderSidebar()
    openFolderMenu('f1')
    fireEvent.click(screen.getByText('Folder settings'))
    const host = await screen.findByTestId('folder-modal')
    expect(host.getAttribute('data-mode')).toBe('edit')
    expect(host.getAttribute('data-folder')).toBe('f1')
    modal.draft = { name: 'Edited', color: '', projectDir: '/tmp/ignored', defaultAgent: 'x', touched: ['name', 'color'] }
    fireEvent.click(screen.getByTestId('folder-modal-submit'))
    // '' is a legitimate color instruction (clears back to gray), so it rides
    // along; projectDir/defaultAgent were untouched and must not.
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith('f1', { name: 'Edited', color: '' }))
    await waitFor(() => expect(screen.queryByTestId('folder-modal')).toBeNull())
  })

  it('closes an untouched settings save without issuing a PATCH', async () => {
    renderSidebar()
    openFolderMenu('f1')
    fireEvent.click(screen.getByText('Folder settings'))
    await screen.findByTestId('folder-modal')
    modal.draft = { name: 'Alpha', color: '', projectDir: '', defaultAgent: '', touched: [] }
    fireEvent.click(screen.getByTestId('folder-modal-submit'))
    await waitFor(() => expect(screen.queryByTestId('folder-modal')).toBeNull())
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
  })

  it('rethrows a rejected settings save so the modal can show the reason', async () => {
    mocks.updateChatFolder.mockRejectedValue(new Error('project_dir is not a directory'))
    renderSidebar()
    openFolderMenu('f1')
    fireEvent.click(screen.getByText('Folder settings'))
    await screen.findByTestId('folder-modal')
    modal.draft = { name: 'Edited', color: '', projectDir: '/nope', defaultAgent: '', touched: ['projectDir'] }
    fireEvent.click(screen.getByTestId('folder-modal-submit'))
    await waitFor(() => expect(modal.submitError).toBeInstanceOf(Error))
    // Still open — the sidebar never called onClose.
    expect(screen.getByTestId('folder-modal')).toBeTruthy()
    // The optimistic rename was rolled back, so the header shows the old name.
    await waitFor(() => expect(screen.getByTitle('Double-click to rename').textContent).toBe('Alpha'))
  })

  it('re-parents the folder from the Move folder to submenu', async () => {
    renderSidebar({ folders: [ALPHA, BETA] })
    openFolderMenu('f1')
    fireEvent.keyDown(screen.getByText('Move folder to'), { key: 'ArrowRight' })
    fireEvent.click(screen.getByTitle('Beta'))
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith('f1', { parent_id: 'f2' }))
  })

  it('ignores a Move folder to pick that is already the current parent', async () => {
    renderSidebar({ folders: [ALPHA, BETA] })
    openFolderMenu('f1')
    fireEvent.keyDown(screen.getByText('Move folder to'), { key: 'ArrowRight' })
    fireEvent.click(screen.getByTitle('No folder (root)'))
    await waitFor(() => expect(screen.queryByText('Move folder to')).toBeNull())
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
  })

  it('offers Hide when empty only for a folder with archived sessions and no live ones', async () => {
    const archived: ChatFolder = { ...ALPHA, history_count: 2 }
    renderSidebar({ slots: [LOOSE], folders: [archived] })
    openFolderMenu('f1')
    fireEvent.click(screen.getByTestId('folder-hide-f1'))
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith('f1', { hidden: true }))
  })

  it('withholds Hide when empty while the folder still holds a session', async () => {
    renderSidebar({ slots: [FILED], folders: [{ ...ALPHA, history_count: 2 }] })
    openFolderMenu('f1')
    expect(screen.queryByTestId('folder-hide-f1')).toBeNull()
  })

  it('deletes the folder only once the confirm is accepted', async () => {
    const confirmFn = vi.fn().mockReturnValue(false)
    vi.stubGlobal('confirm', confirmFn)
    renderSidebar()
    openFolderMenu('f1')
    fireEvent.click(screen.getByTestId('folder-delete-f1'))
    expect(confirmFn).toHaveBeenCalledWith('Delete “Alpha”? Sessions will be ungrouped.')
    expect(mocks.deleteChatFolder).not.toHaveBeenCalled()

    confirmFn.mockReturnValue(true)
    openFolderMenu('f1')
    fireEvent.click(screen.getByTestId('folder-delete-f1'))
    await waitFor(() => expect(mocks.deleteChatFolder).toHaveBeenCalledWith('f1'))
  })

  it('starts a chat inside the folder from the row button', async () => {
    renderSidebar()
    fireEvent.click(screen.getByLabelText('New chat in Alpha'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalled())
    // Folder membership rides the create payload (9th arg) so the slot is
    // published in its final location instead of jumping in from the root.
    expect(mocks.createChatSlot.mock.calls[0][8]).toBe('f1')
  })

  it('expands a collapsed folder before creating the chat inside it', async () => {
    renderSidebar({ folders: [{ ...ALPHA, collapsed: true }] })
    fireEvent.click(screen.getByLabelText('New chat in Alpha'))
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith('f1', { collapsed: false }))
  })

  it('logs a failed folder-scoped create instead of failing silently', async () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    mocks.createChatSlot.mockRejectedValue(new Error('no capacity'))
    renderSidebar()
    fireEvent.click(screen.getByLabelText('New chat in Alpha'))
    await waitFor(() => expect(err).toHaveBeenCalledWith('Failed to create chat in folder:', expect.anything()))
    // The failure also surfaces inline under the folder row (the generic path
    // renders the raw error message), announced to screen readers.
    const notice = await screen.findByTestId('folder-create-error-f1')
    expect(notice.getAttribute('role')).toBe('alert')
    expect(notice.textContent).toContain('no capacity')
  })

  it('names the stale project directory when the backend refuses the folder scope', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    // The create itself succeeds; the follow-up project-scope POST is refused
    // (HTTP 400 "Not a directory") — createSlot rolls the session back and
    // rethrows, and the sidebar maps it to the specific stale-directory
    // message naming the folder's resolved project path.
    mocks.chatSlotProject.mockRejectedValue(new ApiError(400, 'Not a directory'))
    renderSidebar({ folders: [{ ...ALPHA, project_dir: '/renamed-away/proj' }] })
    fireEvent.click(screen.getByLabelText('New chat in Alpha'))
    const notice = await screen.findByTestId('folder-create-error-f1')
    expect(notice.textContent).toContain('/renamed-away/proj')
    expect(notice.textContent).toContain('no longer exists')
  })

  it('clears the inline notice on the next successful create in the folder', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    mocks.createChatSlot.mockRejectedValueOnce(new Error('no capacity'))
    renderSidebar()
    fireEvent.click(screen.getByLabelText('New chat in Alpha'))
    await screen.findByTestId('folder-create-error-f1')
    // The next create in the same folder goes through (the beforeEach default
    // resolves), which supersedes the notice.
    fireEvent.click(screen.getByLabelText('New chat in Alpha'))
    await waitFor(() => expect(screen.queryByTestId('folder-create-error-f1')).toBeNull())
  })

  it('surfaces a failed folder create in flat view, where no folder header mounts', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    // Flat view renders no folder headers, so the per-folder notice mount
    // points do not exist — the lane-level fallback must carry the failure.
    // Drive the failure from the tree view's folder row (the reliable path in
    // happy-dom — the New menu folders live in a Radix submenu it cannot
    // open), then flip to flat view with the error state live.
    mocks.chatSlotProject.mockRejectedValue(new ApiError(400, 'Not a directory'))
    renderSidebar({ folders: [{ ...ALPHA, project_dir: '/renamed-away/proj' }] })
    fireEvent.click(screen.getByLabelText('New chat in Alpha'))
    await screen.findByTestId('folder-create-error-f1')
    fireEvent.click(screen.getByTestId('flat-view-toggle'))
    await screen.findByTestId('flat-view-lane')
    // The tree's per-folder mount is gone; the lane-level fallback carries it.
    const notice = await screen.findByTestId('folder-create-error-f1')
    expect(notice.textContent).toContain('/renamed-away/proj')
  })
})

describe('ChatSidebar — sort and filter menu', () => {
  it('turns a filter on from the menu and clears it from its chip', async () => {
    renderSidebar()
    await openFilterMenu()
    fireEvent.click(screen.getByText('Unread'))
    const chip = await screen.findByLabelText('Clear Unread filter')
    expect(localStorage.getItem('mc-session-unread-only')).toBe('1')
    fireEvent.click(chip)
    await waitFor(() => expect(screen.queryByLabelText('Clear Unread filter')).toBeNull())
    expect(localStorage.getItem('mc-session-unread-only')).toBe('0')
  })

  it('drains a persisted unread filter that loads with nothing unread', async () => {
    // Restoring "unread only" against an empty unread set would show an empty
    // list with no explanation, so the filter turns itself off on the first
    // post-load tick and clears its persistence key.
    localStorage.setItem('mc-session-unread-only', '1')
    renderSidebar()
    await waitFor(() => expect(localStorage.getItem('mc-session-unread-only')).toBe('0'))
    expect(screen.queryByLabelText('Clear Unread filter')).toBeNull()
    expect(screen.getByText('loose chat')).toBeTruthy()
  })

  it('persists the chosen sort order', async () => {
    renderSidebar()
    await openFilterMenu()
    fireEvent.click(screen.getByText('A → Z'))
    await waitFor(() => expect(localStorage.getItem('mc-session-sort')).toBe('name-asc'))
  })

  it('restores every hidden folder in one action', async () => {
    localStorage.setItem('mc-flat-hidden-folders', JSON.stringify(['f1', 'f2']))
    renderSidebar({ folders: [ALPHA, BETA] })
    await openFilterMenu()
    fireEvent.click(await screen.findByTestId('folder-filter-show-all'))
    await waitFor(() => expect(localStorage.getItem('mc-flat-hidden-folders')).toBe('[]'))
    // With nothing hidden the reset row retires itself.
    expect(screen.queryByTestId('folder-filter-show-all')).toBeNull()
  })

  it('keeps a still-running session in the Recent filter however old its settled instant is', async () => {
    // The ordering key stops advancing mid-turn by design, so a turn outliving
    // the recency window would age out of "Recent" while it is the busiest
    // session on screen. Running has to satisfy the filter on its own.
    const ancient = new Date(Date.now() - 48 * 60 * 60 * 1000).toISOString()
    localStorage.setItem('mc-session-recent-only', '1')
    renderSidebar({
      slots: [
        { key: 'k-run', title: 'still working', running: true, messages: 40, last_turn_ts: ancient },
        { key: 'k-idle', title: 'long finished', running: false, messages: 40, last_turn_ts: ancient },
      ],
    })
    expect(await screen.findByText('still working')).toBeTruthy()
    expect(screen.queryByText('long finished')).toBeNull()
  })

  it('toggles the Recent filter from its submenu row, by pointer and by keyboard', async () => {
    renderSidebar()
    await openFilterMenu()
    const row = await screen.findByRole('menuitem', { name: /Recent/ })
    fireEvent.click(row)
    await waitFor(() => expect(localStorage.getItem('mc-session-recent-only')).toBe('1'))
    // Enter is preventDefaulted so it toggles instead of opening the submenu.
    fireEvent.keyDown(row, { key: 'Enter' })
    await waitFor(() => expect(localStorage.getItem('mc-session-recent-only')).toBe('0'))
  })

  it('commits a preset window from the Recent picker', async () => {
    renderSidebar()
    await openFilterMenu()
    fireEvent.keyDown(await screen.findByRole('menuitem', { name: /Recent/ }), { key: 'ArrowRight' })
    fireEvent.click(await screen.findByText('1 week'))
    await waitFor(() => expect(localStorage.getItem('mc-session-recent-window-ms')).toBe(String(7 * RECENT_UNIT_MS.days)))
    // The preset re-seeds the custom drafts so the boxes track the choice.
    expect(await screen.findByLabelText('Custom recency amount')).toHaveValue(7)
  })

  it('clamps a custom amount and commits it on Enter', async () => {
    renderSidebar()
    await openFilterMenu()
    fireEvent.keyDown(await screen.findByRole('menuitem', { name: /Recent/ }), { key: 'ArrowRight' })
    const amount = await screen.findByLabelText('Custom recency amount')
    fireEvent.change(amount, { target: { value: '0' } })
    fireEvent.keyDown(amount, { key: 'Enter' })
    // 0 is below the minimum, so it clamps to 1 of the current unit (hours).
    await waitFor(() => expect(localStorage.getItem('mc-session-recent-window-ms')).toBe(String(RECENT_UNIT_MS.hours)))
    expect(amount).toHaveValue(1)
  })

  it('commits a custom amount on blur too', async () => {
    renderSidebar()
    await openFilterMenu()
    fireEvent.keyDown(await screen.findByRole('menuitem', { name: /Recent/ }), { key: 'ArrowRight' })
    const amount = await screen.findByLabelText('Custom recency amount')
    fireEvent.change(amount, { target: { value: '3' } })
    fireEvent.blur(amount)
    await waitFor(() => expect(localStorage.getItem('mc-session-recent-window-ms')).toBe(String(3 * RECENT_UNIT_MS.hours)))
  })

  it('falls back to the default window when localStorage throws', async () => {
    // Private mode / disabled storage: the read happens in a useState
    // initializer during render, so a throw must not take the sidebar down.
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation((key: string) => {
      if (key === 'mc-session-recent-window-ms') throw new Error('storage disabled')
      return null
    })
    renderSidebar()
    await openFilterMenu()
    const row = await screen.findByRole('menuitem', { name: /Recent/ })
    expect(row.textContent).toContain('1h')
    expect(DEFAULT_RECENT_WINDOW_MS).toBe(RECENT_UNIT_MS.hours)
  })
})
