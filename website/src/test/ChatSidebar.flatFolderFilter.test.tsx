/**
 * Chat sidebar flat view respects the filter menu's folder checkboxes.
 * The sort-and-filter menu lists every folder with a checkbox; unchecking one
 * drops its sessions (and its whole subtree's) from the flat lane. The choice
 * is a local view preference persisted under `mc-flat-hidden-folders`, so it
 * survives a reload and never touches folder membership or the tree's collapse
 * state. Folder visibility is surfaced and undone entirely in the filter
 * menu. Tree view is unaffected; searching bypasses the hiding.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
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
  // staleTime keeps the seeded folder list authoritative: the blanket api mock
  // resolves every call to [], so an on-mount refetch would wipe the folders
  // out from under the rows we are asserting on.
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

// Flat view on from the first render.
beforeEach(() => { localStorage.clear(); localStorage.setItem('mc-sidebar-flat-view', '1') })
afterEach(() => vi.clearAllMocks())

/** Seed the persisted hidden-folder set the filter checkboxes write to. */
function hideFolders(...ids: string[]) {
  localStorage.setItem('mc-flat-hidden-folders', JSON.stringify(ids))
}

describe('chat sidebar — flat view respects the folder filter', () => {
  const cronInFolder = { key: 'cron-abc123', title: 'nightly report', running: false, messages: 2, folder_id: 'cronsF' }
  const looseChat = { key: 'chat-1-100', title: 'loose chat', running: false, messages: 2 }

  it('hides an unchecked folder\'s session in flat view', () => {
    hideFolders('cronsF')
    const folders = [{ id: 'cronsF', name: 'crons', collapsed: false, order: 0 }]
    const { getByText, queryByText } = renderSidebar([cronInFolder, looseChat], folders)
    expect(queryByText('nightly report')).toBeNull() // folder unchecked → hidden
    expect(getByText('loose chat')).toBeTruthy()       // un-foldered → shown
  })

  it('shows the session when every folder is checked', () => {
    const folders = [{ id: 'cronsF', name: 'crons', collapsed: false, order: 0 }]
    const { getByText } = renderSidebar([cronInFolder, looseChat], folders)
    expect(getByText('nightly report')).toBeTruthy() // nothing hidden → shown
    expect(getByText('loose chat')).toBeTruthy()
  })

  it('ignores the folder tree\'s collapse state', () => {
    // Collapsing a folder in tree view is a tree affordance only — it must no
    // longer remove anything from the flat lane (that is now the checkbox's job).
    const folders = [{ id: 'cronsF', name: 'crons', collapsed: true, order: 0 }]
    const { getByText } = renderSidebar([cronInFolder, looseChat], folders)
    expect(getByText('nightly report')).toBeTruthy()
  })

  it('hiding a parent hides the whole subtree', () => {
    // Parent 'p' unchecked, child 'c' checked, session filed in the child: the
    // session is hidden because an ancestor is unchecked, even though the
    // folder it actually sits in is still checked.
    hideFolders('p')
    const folders = [
      { id: 'p', name: 'parent-fold', collapsed: false, order: 0 },
      { id: 'c', name: 'child-fold', collapsed: false, order: 1, parent_id: 'p' },
    ]
    const nested = { key: 'chat-9-900', title: 'nested chat', running: false, messages: 2, folder_id: 'c' }
    const { queryByText, getByText } = renderSidebar([nested, looseChat], folders)
    expect(queryByText('nested chat')).toBeNull()   // ancestor unchecked → hidden
    expect(getByText('loose chat')).toBeTruthy()    // un-foldered still shown
  })

  it('keeps hidden-folder sessions reachable while searching', () => {
    // A hidden folder must never become a search dead-end: an active query
    // bypasses the hiding so every match stays clickable.
    hideFolders('cronsF')
    localStorage.setItem('mc-sidebar-flat-view', '1')
    const folders = [{ id: 'cronsF', name: 'crons', collapsed: false, order: 0 }]
    const { getByPlaceholderText, getByText } = renderSidebar([cronInFolder, looseChat], folders)
    fireEvent.change(getByPlaceholderText(/search/i), { target: { value: 'nightly' } })
    expect(getByText('nightly report')).toBeTruthy()
  })

  it('survives a corrupt persisted value', () => {
    // Hand-edited / foreign localStorage must fall back to "nothing hidden"
    // rather than throwing inside the useState initializer.
    localStorage.setItem('mc-flat-hidden-folders', '{not json')
    const folders = [{ id: 'cronsF', name: 'crons', collapsed: false, order: 0 }]
    const { getByText } = renderSidebar([cronInFolder, looseChat], folders)
    expect(getByText('nightly report')).toBeTruthy()
  })

  it('unchecking a folder in the filter menu hides it and persists the choice', async () => {
    // The real user path: open the sort-and-filter menu → Folders → activate the
    // folder row. The session leaves the lane and the choice is persisted.
    // Radix opens on keyboard activation (Enter), the path jsdom handles —
    // its mouse path needs PointerEvent, which jsdom lacks.
    const folders = [{ id: 'cronsF', name: 'crons', collapsed: false, order: 0 }]
    const { getByLabelText, getByText, findByTestId, queryByText } = renderSidebar([cronInFolder, looseChat], folders)
    expect(getByText('nightly report')).toBeTruthy()
    fireEvent.keyDown(getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    fireEvent.click(await findByTestId('folder-filter-cronsF'))
    await waitFor(() => expect(queryByText('nightly report')).toBeNull())
    expect(JSON.parse(localStorage.getItem('mc-flat-hidden-folders') || '[]')).toEqual(['cronsF'])
  })

  it('renders folders last, scrolling with the menu rather than in a nested region', async () => {
    // Folders grow with the user's folder count, so the section sits at the very
    // bottom and simply overflows into the menu's own scroll. Anything placed
    // below it would drift out of easy reach. jsdom cannot measure layout, so
    // assert the two contracts that produce the behavior: the menu itself is the
    // single scroll container, and every folder row follows the Sort by heading.
    const folders = Array.from({ length: 30 }, (_, i) => ({ id: `f${i}`, name: `folder-${i}`, collapsed: false, order: i }))
    const { getByLabelText, findByTestId } = renderSidebar([looseChat], folders)
    fireEvent.keyDown(getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    await findByTestId('folder-filter-f0')
    const menu = document.querySelector('[role="menu"]')!
    expect(menu.className).toContain('overflow-y-auto')
    expect(menu.className).toMatch(/max-h-/)
    // No inner scroll container inside the menu.
    expect(menu.querySelectorAll('.overflow-y-auto').length).toBe(0)
    const rows = [...menu.querySelectorAll('[data-testid^="folder-filter-f"]')]
    expect(rows.length).toBe(30)
    const sortLabel = [...menu.children].find(el => el.textContent?.trim() === 'Sort by')!
    expect(sortLabel).toBeTruthy()
    // DOCUMENT_POSITION_FOLLOWING (4): the first folder row comes after Sort by.
    expect(sortLabel.compareDocumentPosition(rows[0]) & 4).toBeTruthy()
  })

  it('hides the folder block in list view too, not just the flat lane', () => {
    // Same hidden set, flat view OFF: the folder tree drops the whole block —
    // header and sessions together — while un-foldered sessions stay.
    localStorage.removeItem('mc-sidebar-flat-view')
    hideFolders('cronsF')
    const folders = [{ id: 'cronsF', name: 'crons', collapsed: false, order: 0 }]
    const { getByText, queryByText, queryByTestId } = renderSidebar([cronInFolder, looseChat], folders)
    expect(queryByTestId('folder-menu-cronsF')).toBeNull() // folder header gone
    expect(queryByText('nightly report')).toBeNull()       // and its session with it
    expect(getByText('loose chat')).toBeTruthy()     // un-foldered survives
  })

  it('shows the folder block in list view when nothing is hidden', () => {
    localStorage.removeItem('mc-sidebar-flat-view')
    const folders = [{ id: 'cronsF', name: 'crons', collapsed: false, order: 0 }]
    const { getByText } = renderSidebar([cronInFolder, looseChat], folders)
    expect(getByText('crons')).toBeTruthy()
  })

  it('announces a top-level hide with a reveal row at the bottom of the root list', () => {
    // Case 1: the container is the root list, so the row is un-indented and the
    // folder's own block stays absent until the row is peeked open.
    localStorage.removeItem('mc-sidebar-flat-view')
    hideFolders('cronsF')
    const folders = [{ id: 'cronsF', name: 'crons', collapsed: false, order: 0 }]
    const { getByTestId, queryByTestId } = renderSidebar([cronInFolder, looseChat], folders)
    const row = getByTestId('hidden-reveal-root')
    expect(row.textContent).toContain('1 hidden folder')
    expect(queryByTestId('hidden-reveal-cronsF')).toBeNull() // not at any other level
    expect(queryByTestId('folder-menu-cronsF')).toBeNull()   // still hidden
  })

  it('peeking the row open renders the hidden folder\'s real block, and closes again', () => {
    // The revealed block is the genuine folder block — that is what keeps ⋯ →
    // Show folder (the durable undo) reachable from the peek.
    localStorage.removeItem('mc-sidebar-flat-view')
    hideFolders('cronsF')
    const folders = [{ id: 'cronsF', name: 'crons', collapsed: false, order: 0 }]
    const { getByTestId, queryByTestId } = renderSidebar([cronInFolder, looseChat], folders)
    // Re-query after every click: React replaces the button node on re-render,
    // so a captured reference goes stale and would read the old attribute.
    const toggle = () => getByTestId('hidden-reveal-root').querySelector('button')!
    expect(toggle().getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(toggle())
    expect(toggle().getAttribute('aria-expanded')).toBe('true')
    expect(queryByTestId('folder-menu-cronsF')).toBeTruthy() // real block, menu and all
    fireEvent.click(toggle())
    expect(queryByTestId('folder-menu-cronsF')).toBeNull()   // collapses again
  })

  it('anchors a nested hide to its parent, not to the root', () => {
    // Case 2: the whole point of bottom-of-container. `child` is hidden, so the
    // row belongs to `parent`'s children — root must stay silent.
    localStorage.removeItem('mc-sidebar-flat-view')
    hideFolders('childF')
    const folders = [
      { id: 'parentF', name: 'parent', collapsed: false, order: 0 },
      { id: 'childF', name: 'child', collapsed: false, order: 1, parent_id: 'parentF' },
    ]
    const { getByTestId, queryByTestId } = renderSidebar(
      [{ ...cronInFolder, folder_id: 'childF' }, looseChat], folders)
    expect(getByTestId('hidden-reveal-parentF').textContent).toContain('1 hidden folder')
    expect(queryByTestId('hidden-reveal-root')).toBeNull()
  })

  it('reports one row per container, and stays silent under an already-hidden ancestor', () => {
    // Case 3 + the covered-by-ancestor rule: `parent` and its child `child` are
    // BOTH unchecked. The parent's block is gone, so there is no parent
    // container on screen to host a row — the child must not be announced
    // anywhere, or the same hide would be reported twice.
    localStorage.removeItem('mc-sidebar-flat-view')
    hideFolders('parentF', 'childF')
    const folders = [
      { id: 'parentF', name: 'parent', collapsed: false, order: 0 },
      { id: 'childF', name: 'child', collapsed: false, order: 1, parent_id: 'parentF' },
      { id: 'keepF', name: 'keep', collapsed: false, order: 2 },
    ]
    const { getByTestId, queryByTestId } = renderSidebar(
      [{ ...cronInFolder, folder_id: 'childF' }, looseChat], folders)
    expect(getByTestId('hidden-reveal-root').textContent).toContain('1 hidden folder') // parent only
    expect(queryByTestId('hidden-reveal-parentF')).toBeNull()
  })

  it('collapses every hide into one row in flat view, which has no containers', () => {
    // Flat view explodes chats out of their folders, so depth is meaningless and
    // a top-level hide and a nested one share the lane's single row.
    localStorage.setItem('mc-sidebar-flat-view', '1')
    hideFolders('cronsF', 'childF')
    const folders = [
      { id: 'cronsF', name: 'crons', collapsed: false, order: 0 },
      { id: 'parentF', name: 'parent', collapsed: false, order: 1 },
      { id: 'childF', name: 'child', collapsed: false, order: 2, parent_id: 'parentF' },
    ]
    const { getByTestId, queryByTestId } = renderSidebar([cronInFolder, looseChat], folders)
    expect(getByTestId('hidden-reveal-flat').textContent).toContain('2 hidden folders')
    expect(queryByTestId('hidden-reveal-root')).toBeNull()
    expect(queryByTestId('hidden-reveal-parentF')).toBeNull()
  })

  it('drops the reveal row entirely once nothing is hidden', () => {
    localStorage.removeItem('mc-sidebar-flat-view')
    const folders = [{ id: 'cronsF', name: 'crons', collapsed: false, order: 0 }]
    const { queryByTestId } = renderSidebar([cronInFolder, looseChat], folders)
    expect(queryByTestId('hidden-reveal-root')).toBeNull()
    expect(queryByTestId('hidden-reveal-flat')).toBeNull()
  })

  it('offers a show/hide toggle in the folder ⋯ menu, labelled by current state', async () => {
    // The item drives the same state as the filter checkbox. Radix's ⋯ menu
    // cannot be *activated* in jsdom (it needs PointerEvent — see the note in
    // ChatSidebar.boardFolderRename.test.tsx), so this pins what is observable:
    // the item is present and its label reflects the folder's current state.
    // The toggle behaviour itself is covered by the filter-menu test above.
    localStorage.removeItem('mc-sidebar-flat-view')
    const folders = [{ id: 'workF', name: 'work', collapsed: false, order: 0 }]
    const shown = renderSidebar([{ ...cronInFolder, folder_id: 'workF' }, looseChat], folders)
    fireEvent.keyDown(shown.getByTestId('folder-menu-workF'), { key: 'Enter' })
    expect((await shown.findByTestId('folder-visibility-workF')).textContent).toContain('Hide folder')
    shown.unmount()

    // A hidden folder has no row of its own, so its menu is reachable only via
    // its parent: assert the flipped label on a hidden CHILD, parent still shown.
    hideFolders('childF')
    const nested = renderSidebar([{ ...cronInFolder, folder_id: 'childF' }], [
      { id: 'parentF', name: 'parent', collapsed: false, order: 0 },
      { id: 'childF', name: 'child', collapsed: false, order: 1, parent_id: 'parentF' },
    ])
    fireEvent.keyDown(nested.getByTestId('folder-menu-parentF'), { key: 'Enter' })
    expect((await nested.findByTestId('folder-visibility-parentF')).textContent).toContain('Hide folder')
    // The hidden child has no row, so its menu is gone — that IS the effect.
    // (The "Show folder" label is reachable while searching, where the filter
    // goes inert and hidden rows return; not asserted here because the suite's
    // blanket api mock resolves the debounced search to an empty result set.)
    expect(nested.queryByTestId('folder-menu-childF')).toBeNull()
  })

  it('shelving the Folders section rolls up the rows but keeps the hidden count', async () => {
    // Shelving is cosmetic: rows go away, the hidden state does not.
    hideFolders('cronsF')
    const folders = [
      { id: 'cronsF', name: 'crons', collapsed: false, order: 0 },
      { id: 'workF', name: 'work', collapsed: false, order: 1 },
    ]
    const { getByLabelText, getByTestId, findByTestId, queryByTestId } = renderSidebar([cronInFolder, looseChat], folders)
    fireEvent.keyDown(getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    expect(await findByTestId('folder-filter-workF')).toBeTruthy()
    fireEvent.click(getByTestId('folder-filter-shelve'))
    await waitFor(() => expect(queryByTestId('folder-filter-workF')).toBeNull())
    // Heading still reports the state, and shelving did not unhide anything.
    // Scoped to the heading: the in-tree reveal row also says "1 hidden folder".
    expect(getByTestId('folder-filter-shelve').textContent).toMatch(/1 hidden/)
    expect(JSON.parse(localStorage.getItem('mc-flat-hidden-folders') || '[]')).toEqual(['cronsF'])
    expect(localStorage.getItem('mc-filter-folders-shelved')).toBe('1')
  })

  it('does not hang on cyclic folder ancestry (visited-set guard)', () => {
    // A hand-edited folders.json can contain a parent_id cycle. The ancestry
    // walks must terminate (visited-set guard) rather than freeze the tab.
    hideFolders('a')
    const folders = [
      { id: 'a', name: 'Aye', collapsed: false, order: 0, parent_id: 'b' },
      { id: 'b', name: 'Bee', collapsed: false, order: 1, parent_id: 'a' },
    ]
    const inCycle = { key: 'chat-7-700', title: 'cycle chat', running: false, messages: 2, folder_id: 'a' }
    const { queryByText, getByText } = renderSidebar([inCycle, looseChat], folders)
    // Render completed (no infinite loop) and the unchecked cycle member still
    // hides its session.
    expect(queryByText('cycle chat')).toBeNull()
    expect(getByText('loose chat')).toBeTruthy()
  })

  it('does not blow the stack when peeking a cyclic folder open', () => {
    // Collapsed, the cycle member is never rendered as a folder block: it has a
    // parent_id, so it is not a root. Peeking the reveal row renders it
    // directly, which reaches the unread walk -- that walk must terminate.
    hideFolders('a')
    const folders = [
      { id: 'a', name: 'Aye', collapsed: false, order: 0, parent_id: 'b' },
      { id: 'b', name: 'Bee', collapsed: false, order: 1, parent_id: 'a' },
    ]
    const inCycle = { key: 'chat-7-700', title: 'cycle chat', running: false, messages: 2, folder_id: 'a' }
    const { getByTestId } = renderSidebar([inCycle, looseChat], folders)
    // Re-query: React replaces the button node on re-render.
    const toggle = () => getByTestId('hidden-reveal-flat').querySelector('button')!
    expect(toggle().getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(toggle())
    expect(toggle().getAttribute('aria-expanded')).toBe('true')
    // Reaching this line at all is the assertion: an unguarded walk overflows
    // the stack during the peek render instead of returning.
    expect(getByTestId('hidden-reveal-flat')).toBeTruthy()
  })
})
