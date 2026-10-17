/**
 * Folder search in the chat sidebar — the two halves of "search a folder, land on
 * it":
 *
 *  1. The SEARCH BOX understands folder names. Typing a container's name keeps the
 *     sessions filed inside it, and keeps the folder row itself even when it holds
 *     nothing that matched. Before this, the box only ever asked about a session's
 *     own fields, so naming the parent hid every child.
 *
 *  2. The store-held `revealRequest`, tagged `folder` (set by the launcher's Folders
 *     tab) lands ON the folder: un-hide it, expand it and its ancestors, scroll to
 *     it, flash it. The session twin of this is covered in ChatSidebarCoverage.
 *
 * `staleTime: Infinity` + `refetchOnMount: false` on the folders query are
 * load-bearing here for the same reason they are in the tag-filter suite: the api
 * mock resolves `chatFolders()` to `[]`, so an on-mount refetch would empty the
 * tree and let a "the folder is visible" assertion pass for the wrong reason.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { requestFolderReveal, requestSlotReveal } from '../store/chatSlice'
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
/** Board lane is opt-in, so the flag has to be settable per test. */
const chatConfig = { tagColumnsEnabled: false, confirmCloseSession: false }
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => chatConfig,
  saveChatConfig: vi.fn(),
}))

/** Records folder PATCHes so the expand-on-reveal assertions can read them. */
const updateChatFolder = vi.fn().mockResolvedValue({})

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'updateChatFolder') return updateChatFolder
      if (prop === 'chatTags') return vi.fn().mockResolvedValue([])
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
import type { ChatFolder, ChatSlot, TagColumn } from '../types'

/**
 * `Sydney Property` holds a session whose TITLE shares nothing with the folder's
 * name — that gap is the point, since it is the row a folder-name search has to
 * keep and a title-only filter drops. `Archive` is deliberately empty.
 */
const FOLDERS: ChatFolder[] = [
  { id: 'f-syd', name: 'Sydney Property', collapsed: false, order: 0 },
  { id: 'f-nested', name: 'Inspections', collapsed: false, order: 0, parent_id: 'f-syd' },
  { id: 'f-archive', name: 'Archive', collapsed: false, order: 1 },
  { id: 'f-other', name: 'Trading Desk', collapsed: false, order: 2 },
]

const SLOTS: ChatSlot[] = [
  { key: 'k-syd', title: 'mortgage numbers', running: false, messages: 2, folder_id: 'f-syd' },
  { key: 'k-nested', title: 'saturday walkthrough', running: false, messages: 2, folder_id: 'f-nested' },
  { key: 'k-other', title: 'options ladder', running: false, messages: 2, folder_id: 'f-other' },
  { key: 'k-loose', title: 'scratch notes', running: false, messages: 2 },
] as unknown as ChatSlot[]

function renderSidebar(opts: {
  slots?: ChatSlot[]
  folders?: ChatFolder[]
  hiddenFolders?: string[]
  /** Seed the board lane: a column list plus the opt-in flag it is gated on. */
  columns?: TagColumn[]
  /** A reveal request already in the store at mount — the remount case, where a
   *  request set while this sidebar was unmounted is replayed on the way back in. */
  reveal?: { kind: 'session' | 'folder'; target: string; nonce: number }
} = {}) {
  const slots = opts.slots ?? SLOTS
  const folders = opts.folders ?? FOLDERS
  chatConfig.tagColumnsEnabled = !!opts.columns
  if (opts.hiddenFolders) {
    localStorage.setItem('mc-flat-hidden-folders', JSON.stringify(opts.hiddenFolders))
  }
  // Spread the real slice defaults: RTK REPLACES a slice with preloadedState
  // rather than merging, so a partial drops keys the reducers assume exist.
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
      activeSlot: null, slotStatusDetail: {},
      revealRequest: opts.reveal ?? null,
      revealNonce: opts.reveal?.nonce ?? 0,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: {
    queries: { retry: false, staleTime: Infinity, refetchOnMount: false }, mutations: { retry: false },
  } })
  qc.setQueryData(['chat-folders'], folders)
  qc.setQueryData(['tag-columns'], opts.columns ?? [])
  const view = render(
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
  return { ...view, store }
}

function typeSearch(utils: ReturnType<typeof renderSidebar>, text: string) {
  fireEvent.change(utils.getByPlaceholderText('Search sessions…'), { target: { value: text } })
}

/** jsdom has no scrollIntoView; install a spy and restore it afterwards. */
function stubScrollIntoView() {
  const proto = HTMLElement.prototype as HTMLElement & { scrollIntoView?: (o?: unknown) => void }
  const had = proto.scrollIntoView
  const spy = vi.fn()
  proto.scrollIntoView = spy
  return {
    spy,
    restore: () => { if (had) proto.scrollIntoView = had; else delete proto.scrollIntoView },
  }
}

beforeEach(() => { localStorage.clear(); updateChatFolder.mockClear() })
afterEach(() => vi.clearAllMocks())

describe('chat sidebar search — folder names', () => {
  it('keeps the sessions inside a folder whose NAME matches, even when no title does', async () => {
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('mortgage numbers')).not.toBeNull())
    typeSearch(utils, 'sydney')
    // The container matched, so what it holds is what was asked for…
    await waitFor(() => expect(utils.queryByText('options ladder')).toBeNull())
    expect(utils.queryByText('mortgage numbers')).not.toBeNull()
    // …including a session in a SUBFOLDER of the match: naming a parent is how you
    // ask for the tree under it.
    expect(utils.queryByText('saturday walkthrough')).not.toBeNull()
    // A session outside the matched subtree is still filtered out.
    expect(utils.queryByText('scratch notes')).toBeNull()
  })

  it('keeps an EMPTY folder visible when its own name matches', async () => {
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('Archive')).not.toBeNull())
    typeSearch(utils, 'archive')
    // Nothing is filed in Archive, so a "hide folders with no matching children"
    // rule would elide the one row the query definitely named. `Trading Desk` is
    // the control: also childless, name does not match, so it goes.
    await waitFor(() => expect(utils.queryByText('Trading Desk')).toBeNull())
    expect(utils.queryByText('Archive')).not.toBeNull()
  })

  it('still filters on session titles, so folder matching is additive', async () => {
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('options ladder')).not.toBeNull())
    typeSearch(utils, 'ladder')
    await waitFor(() => expect(utils.queryByText('mortgage numbers')).toBeNull())
    expect(utils.queryByText('options ladder')).not.toBeNull()
  })

  it('matches no folder for a query that names none', async () => {
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('mortgage numbers')).not.toBeNull())
    typeSearch(utils, 'zzzz-nothing')
    await waitFor(() => expect(utils.queryByText('mortgage numbers')).toBeNull())
    // Asserted on the CHILDLESS folders, because those are the ones the narrow can
    // elide. A folder that holds a subfolder keeps a child node either way (the
    // nested block's draggable wrapper is pushed before the nested render decides
    // it has nothing to show), so `Sydney Property` survives any narrow — existing
    // behaviour, unrelated to folder matching, and not this change's to alter.
    expect(utils.queryByText('Archive')).toBeNull()
    expect(utils.queryByText('Trading Desk')).toBeNull()
  })
})

describe('chat sidebar — reveal a folder row', () => {
  it('marks every folder row with data-folder-row, the reveal target', async () => {
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('Sydney Property')).not.toBeNull())
    expect(utils.container.querySelector('[data-folder-row="f-syd"]')).not.toBeNull()
    expect(utils.container.querySelector('[data-folder-row="f-nested"]')).not.toBeNull()
  })

  it('scrolls the folder into view, flashes it, and consumes the request', async () => {
    const scroll = stubScrollIntoView()
    try {
      const utils = renderSidebar()
      await waitFor(() => expect(utils.queryByText('Trading Desk')).not.toBeNull())
      utils.store.dispatch(requestFolderReveal('f-other'))
      await waitFor(() => expect(scroll.spy).toHaveBeenCalled())
      const row = utils.container.querySelector('[data-folder-row="f-other"]')
      expect(row?.className).toContain('session-reveal-flash')
      // Consumed immediately, so it cannot fire again on a later remount.
      expect(utils.store.getState().chat.revealRequest).toBeNull()
    } finally {
      scroll.restore()
    }
  })

  it('flashes only the revealed folder, not a session row', async () => {
    const scroll = stubScrollIntoView()
    try {
      const utils = renderSidebar()
      await waitFor(() => expect(utils.queryByText('Trading Desk')).not.toBeNull())
      utils.store.dispatch(requestFolderReveal('f-other'))
      await waitFor(() => expect(scroll.spy).toHaveBeenCalled())
      // The flash is keyed by (kind, id): a folder reveal must never light up a
      // session row, whose keys live in a different namespace that can collide.
      const flashed = [...utils.container.querySelectorAll('.session-reveal-flash')]
      expect(flashed.length).toBe(1)
      expect(flashed[0].getAttribute('data-folder-row')).toBe('f-other')
    } finally {
      scroll.restore()
    }
  })

  it('flashes the folder and not the session when a slot key EQUALS the folder id', async () => {
    // The `kind` half of the flash key only earns its place under a real
    // collision, and the sibling test above cannot produce one: its fixture keeps
    // folders on `f-*` and slots on `k-*`, so dropping the `kind` check entirely
    // would still leave it green. Nothing enforces those prefixes — a folder id
    // and a slot key are minted by different subsystems and share one string
    // space. Here they are deliberately the same string, and `data-session-row`
    // carries it verbatim because `sessionRowIdentity` falls back to `slot.key`
    // when a payload has no server-resolved `row_identity`.
    const COLLIDE = 'shared-identity'
    const folders: ChatFolder[] = [
      ...FOLDERS,
      { id: COLLIDE, name: 'Collision Folder', collapsed: false, order: 3 },
    ]
    const slots = [
      ...SLOTS,
      { key: COLLIDE, title: 'colliding session', running: false, messages: 2 },
    ] as unknown as ChatSlot[]

    const scroll = stubScrollIntoView()
    try {
      const utils = renderSidebar({ folders, slots })
      await waitFor(() => expect(utils.queryByText('Collision Folder')).not.toBeNull())
      // Both rows exist and answer to the same string.
      expect(utils.container.querySelector(`[data-folder-row="${COLLIDE}"]`)).not.toBeNull()
      expect(utils.container.querySelector(`[data-session-row="${COLLIDE}"]`)).not.toBeNull()

      utils.store.dispatch(requestFolderReveal(COLLIDE))
      await waitFor(() => expect(scroll.spy).toHaveBeenCalled())

      const flashed = [...utils.container.querySelectorAll('.session-reveal-flash')]
      expect(flashed.length).toBe(1)
      expect(flashed[0].getAttribute('data-folder-row')).toBe(COLLIDE)
      // Stated positively as well: the session sharing the string stays dark.
      const sessionRow = utils.container.querySelector(`[data-session-row="${COLLIDE}"]`)
      expect(sessionRow?.className ?? '').not.toContain('session-reveal-flash')
    } finally {
      scroll.restore()
    }
  })

  it('clears an unrelated search query, so the reveal does not land on a hidden row', async () => {
    const scroll = stubScrollIntoView()
    try {
      const utils = renderSidebar()
      await waitFor(() => expect(utils.queryByText('Trading Desk')).not.toBeNull())
      typeSearch(utils, 'ladder')
      await waitFor(() => expect(utils.queryByText('Archive')).toBeNull())
      utils.store.dispatch(requestFolderReveal('f-archive'))
      // The filter that was hiding it is dropped, so the row exists to scroll to.
      await waitFor(() => expect(utils.queryByText('Archive')).not.toBeNull())
      expect((utils.getByPlaceholderText('Search sessions…') as HTMLInputElement).value).toBe('')
      // The row did not exist on the reveal's first attempt (clearing the filter
      // lands through a re-render), so the scroll comes from the bounded retry
      // loop — which is exactly the race that loop exists for.
      await waitFor(() => expect(scroll.spy).toHaveBeenCalled())
    } finally {
      scroll.restore()
    }
  })

  it('expands the target folder AND its collapsed ancestors', async () => {
    const scroll = stubScrollIntoView()
    try {
      const utils = renderSidebar({
        folders: [
          { id: 'f-syd', name: 'Sydney Property', collapsed: true, order: 0 },
          { id: 'f-nested', name: 'Inspections', collapsed: true, order: 0, parent_id: 'f-syd' },
        ],
      })
      await waitFor(() => expect(utils.queryByText('Sydney Property')).not.toBeNull())
      utils.store.dispatch(requestFolderReveal('f-nested'))
      // Landing on a folder means seeing what is in it, so the target's OWN
      // disclosure opens too — not just the ancestors above it.
      await waitFor(() => expect(updateChatFolder).toHaveBeenCalledWith('f-nested', { collapsed: false }))
      expect(updateChatFolder).toHaveBeenCalledWith('f-syd', { collapsed: false })
    } finally {
      scroll.restore()
    }
  })

  it('un-hides a folder the filter menu had hidden, and its hidden ancestor', async () => {
    const scroll = stubScrollIntoView()
    try {
      // Hiding a parent hides the subtree, so clearing only the target would leave
      // it hidden behind its ancestor.
      const utils = renderSidebar({ hiddenFolders: ['f-syd', 'f-nested'] })
      await waitFor(() => expect(utils.queryByText('Trading Desk')).not.toBeNull())
      expect(utils.queryByText('Inspections')).toBeNull()
      utils.store.dispatch(requestFolderReveal('f-nested'))
      await waitFor(() => expect(utils.queryByText('Inspections')).not.toBeNull())
      expect(utils.queryByText('Sydney Property')).not.toBeNull()
      // The un-hiding is persisted, like every other write to this filter.
      expect(JSON.parse(localStorage.getItem('mc-flat-hidden-folders') || '[]')).toEqual([])
    } finally {
      scroll.restore()
    }
  })

  it('leaves the flat lane WITHOUT rewriting the persisted lane preference', async () => {
    const scroll = stubScrollIntoView()
    try {
      localStorage.setItem('mc-sidebar-flat-view', '1')
      const utils = renderSidebar()
      utils.store.dispatch(requestFolderReveal('f-other'))
      // The lane really does switch -- a folder row only exists in the tree lane, so
      // the reveal has nothing to scroll to until it does.
      await waitFor(() => expect(utils.container.querySelector('[data-folder-row="f-other"]')).not.toBeNull())
      await waitFor(() => expect(scroll.spy).toHaveBeenCalled())
      // …and the stored preference is untouched. The lane is a persisted choice, and
      // a flat-lane user who jumps to one folder asked to see that folder, not to
      // change which lane the app opens in. Writing '0' here silently and
      // permanently converted them to the tree.
      expect(localStorage.getItem('mc-sidebar-flat-view')).toBe('1')
    } finally {
      scroll.restore()
    }
  })

  it('ignores a request naming a folder that no longer exists', async () => {
    const scroll = stubScrollIntoView()
    try {
      const utils = renderSidebar()
      await waitFor(() => expect(utils.queryByText('Trading Desk')).not.toBeNull())
      utils.store.dispatch(requestFolderReveal('f-deleted'))
      await waitFor(() => expect(utils.store.getState().chat.revealRequest).toBeNull())
      // Nothing to scroll to, and nothing flashes — but the request is consumed
      // rather than left to re-fire on the next mount.
      expect(scroll.spy).not.toHaveBeenCalled()
      expect(utils.container.querySelectorAll('.session-reveal-flash').length).toBe(0)
    } finally {
      scroll.restore()
    }
  })

  it('force-shows a hidden empty folder WITHOUT writing the server hidden flag', async () => {
    // "Hide when empty" is `folder.hidden` on the server, and it is a second,
    // independent reason a row is absent — separate from the localStorage set the
    // sibling test covers. An empty hidden folder is dropped from the lane with no
    // disclosure listing it, so unlike the client-side hide there is no row in the
    // DOM at all and the bounded retry loop can only expire.
    //
    // The override is transient. Clearing the flag on the server would answer a
    // question the user did not ask — they wanted to see this folder now, not to stop
    // hiding it — and could not be undone from the row it reveals.
    const folders: ChatFolder[] = [
      ...FOLDERS,
      { id: 'f-hidden', name: 'Retired Deals', collapsed: false, order: 4, hidden: true, history_count: 3 },
    ]
    const scroll = stubScrollIntoView()
    try {
      const utils = renderSidebar({ folders })
      await waitFor(() => expect(utils.queryByText('Trading Desk')).not.toBeNull())
      // Precondition: the flag really does keep the row out of the lane.
      expect(utils.container.querySelector('[data-folder-row="f-hidden"]')).toBeNull()

      utils.store.dispatch(requestFolderReveal('f-hidden'))
      // The row appears and is scrolled to…
      await waitFor(() => expect(utils.container.querySelector('[data-folder-row="f-hidden"]')).not.toBeNull())
      await waitFor(() => expect(scroll.spy).toHaveBeenCalled())
      // …and nothing was persisted to get it there.
      const hiddenWrites = updateChatFolder.mock.calls.filter(
        (c: unknown[]) => typeof c[1] === 'object' && c[1] !== null && 'hidden' in (c[1] as object),
      )
      expect(hiddenWrites).toEqual([])
    } finally {
      scroll.restore()
    }
  })

  it('force-shows the hidden ANCESTOR too, or the target has no lane to appear in', async () => {
    // Hiding a parent takes its subtree with it, so an override scoped to the target
    // alone would reveal a row whose container is still gone.
    const folders: ChatFolder[] = [
      { id: 'f-par', name: 'Retired', collapsed: false, order: 0, hidden: true, history_count: 2 },
      { id: 'f-kid', name: 'Old Inspections', collapsed: false, order: 0, parent_id: 'f-par' },
      { id: 'f-other', name: 'Trading Desk', collapsed: false, order: 1 },
    ]
    const scroll = stubScrollIntoView()
    try {
      const utils = renderSidebar({ folders, slots: [] as unknown as ChatSlot[] })
      await waitFor(() => expect(utils.queryByText('Trading Desk')).not.toBeNull())
      expect(utils.container.querySelector('[data-folder-row="f-kid"]')).toBeNull()

      utils.store.dispatch(requestFolderReveal('f-kid'))
      await waitFor(() => expect(utils.container.querySelector('[data-folder-row="f-kid"]')).not.toBeNull())
      expect(utils.container.querySelector('[data-folder-row="f-par"]')).not.toBeNull()
    } finally {
      scroll.restore()
    }
  })

  it('leaves an unrelated hidden folder hidden', async () => {
    // The override is scoped to the target's own ancestor chain. A blanket
    // force-visible would un-hide whatever the user hid elsewhere, on every reveal.
    const folders: ChatFolder[] = [
      ...FOLDERS,
      { id: 'f-hidden', name: 'Retired Deals', collapsed: false, order: 4, hidden: true, history_count: 3 },
    ]
    const scroll = stubScrollIntoView()
    try {
      const utils = renderSidebar({ folders })
      await waitFor(() => expect(utils.queryByText('Trading Desk')).not.toBeNull())
      utils.store.dispatch(requestFolderReveal('f-other'))
      await waitFor(() => expect(scroll.spy).toHaveBeenCalled())
      expect(utils.container.querySelector('[data-folder-row="f-hidden"]')).toBeNull()
    } finally {
      scroll.restore()
    }
  })

  it('holds ONE pending reveal, so a newer request replaces an older one', async () => {
    // The reveal request is a single kind-tagged field. Two pending requests used to
    // be representable, and because the sidebar's two effects run in declaration
    // order, an older one could execute last and cancel the newer one's retry loop.
    // With one field the newer request overwrites the older and the hazard is gone
    // from the state rather than being repaired by comparing nonces.
    const scroll = stubScrollIntoView()
    try {
      const utils = renderSidebar()
      await waitFor(() => expect(utils.queryByText('Trading Desk')).not.toBeNull())
      utils.store.dispatch(requestFolderReveal('f-other'))
      utils.store.dispatch(requestSlotReveal('k-loose'))
      // Only the second survived to be consumed.
      await waitFor(() => expect(utils.container.querySelectorAll('.session-reveal-flash').length).toBe(1))
      const flashed = utils.container.querySelector('.session-reveal-flash')
      expect(flashed?.getAttribute('data-session-row')).toBe('k-loose')
      expect(flashed?.hasAttribute('data-folder-row')).toBe(false)
      expect(utils.store.getState().chat.revealRequest).toBeNull()
    } finally {
      scroll.restore()
    }
  })

  it('replays a request set before mount, whichever kind it is', async () => {
    // The reason the request lives in the store at all: either surface can be opened
    // from a page where this sidebar is not mounted.
    const scroll = stubScrollIntoView()
    try {
      const utils = renderSidebar({ reveal: { kind: 'folder', target: 'f-other', nonce: 1 } })
      await waitFor(() => expect(utils.container.querySelectorAll('.session-reveal-flash').length).toBe(1))
      const flashed = utils.container.querySelector('.session-reveal-flash')
      expect(flashed?.getAttribute('data-folder-row')).toBe('f-other')
      expect(utils.store.getState().chat.revealRequest).toBeNull()
    } finally {
      scroll.restore()
    }
  })
})

describe('chat sidebar — what a folder-name match looks like', () => {
  it('marks the matched letters in the folder name that matched', async () => {
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('Archive')).not.toBeNull())
    typeSearch(utils, 'arch')
    await waitFor(() => expect(utils.container.querySelector('[data-folder-row="f-archive"] mark')).not.toBeNull())
    const mark = utils.container.querySelector('[data-folder-row="f-archive"] mark')
    // The marked run is the query's own letters, in the name's original case.
    expect(mark?.textContent).toBe('Arch')
    expect(mark?.className).toContain('search-match')
  })

  it('leaves an ancestor row unmarked, so the marked row is the one that explains the result', async () => {
    // Searching the SUBFOLDER's name keeps its parent on screen as the path to it.
    // The parent's own name never matched, so it carries no mark — and that
    // difference is the cue the reader was missing.
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('Inspections')).not.toBeNull())
    typeSearch(utils, 'inspect')
    await waitFor(() => expect(utils.container.querySelector('[data-folder-row="f-nested"] mark')).not.toBeNull())
    expect(utils.container.querySelector('[data-folder-row="f-syd"] mark')).toBeNull()
  })

  it('drops a folder whose whole subtree draws nothing, instead of listing it with a phantom count', async () => {
    // "archive" names only `Archive`. `Sydney Property` held `Inspections`, which
    // the narrow drops — so the parent stood on screen wearing the count `1` for a
    // child that never rendered, directly above the note saying no sessions match.
    // The wrapper for a nested block is pushed BEFORE that block's own render can
    // return `[]`, so `childNodes.length` could not see the subtree was empty.
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('Inspections')).not.toBeNull())
    typeSearch(utils, 'archive')
    await waitFor(() => expect(utils.queryByText('Trading Desk')).toBeNull())
    expect(utils.container.querySelector('[data-folder-row="f-archive"]')).not.toBeNull()
    expect(utils.container.querySelector('[data-folder-row="f-syd"]')).toBeNull()
    expect(utils.container.querySelector('[data-folder-row="f-nested"]')).toBeNull()
  })

  it('keeps a genuine ancestor, and counts only the child it actually draws', async () => {
    // The mirror case: "inspect" names the SUBFOLDER, so the parent is the path to
    // a real match and belongs on screen. Its count is 1 because `Inspections` is
    // drawn beneath it — the number and the body agree, which is the property the
    // "archive" case violated.
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('Inspections')).not.toBeNull())
    typeSearch(utils, 'inspect')
    await waitFor(() => expect(utils.queryByText('Trading Desk')).toBeNull())
    const parent = utils.container.querySelector('[data-folder-row="f-syd"]')
    expect(parent).not.toBeNull()
    expect(utils.container.querySelector('[data-folder-row="f-nested"]')).not.toBeNull()
    expect((parent?.textContent || '').replace(/\s+/g, '')).toBe('SydneyProperty1')
  })

  it('marks nothing while the search box is empty', async () => {
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('Archive')).not.toBeNull())
    expect(utils.container.querySelectorAll('mark').length).toBe(0)
  })

  it('does not tell the reader "no sessions match" while matched folders are on screen', async () => {
    // Two contradictions this copy has to avoid. The bare "No sessions match" read
    // as the opposite of the matched folder rows above it. And pointing at "the
    // folders above" was false in turn: the tree keeps a folder that merely HOLDS a
    // subfolder, so a row that never matched anything sat under a sentence claiming
    // it had. The copy now claims only what it knows -- a folder name matched -- and
    // the marks say which row.
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('Archive')).not.toBeNull())
    typeSearch(utils, 'archive')
    await waitFor(() => expect(utils.queryByText('Trading Desk')).toBeNull())
    expect(utils.queryByText('No sessions match')).toBeNull()
    expect(utils.queryByText('No sessions match — only a folder name did')).not.toBeNull()
  })

  it('survives a stored folder whose name is not a string', async () => {
    // `ChatFolder.name` is typed `string`, but the value comes off disk: a hand edit
    // or an older writer can leave a number, null, or an object there. Every consumer
    // that calls a string method on it throws, and a throw inside the search memo is
    // a throw in RENDER — the sidebar unmounts, so there is no row left to explain it
    // and no way to clear the box that triggered it. The malformed folder simply does
    // not match rather than being stringified, so nothing matches `[object Object]`.
    const folders = [
      ...FOLDERS,
      { id: 'f-num', name: 42, collapsed: false, order: 4 },
      { id: 'f-null', name: null, collapsed: false, order: 5 },
      { id: 'f-obj', name: { en: 'Deals' }, collapsed: false, order: 6 },
    ] as unknown as ChatFolder[]
    const utils = renderSidebar({ folders })
    await waitFor(() => expect(utils.queryByText('Trading Desk')).not.toBeNull())
    // Typing is what reaches the name reader — both the match and the highlight.
    typeSearch(utils, 'archive')
    await waitFor(() => expect(utils.queryByText('Trading Desk')).toBeNull())
    // Still rendering: the well-formed match survived the malformed siblings.
    expect(utils.queryByText('Archive')).not.toBeNull()
    // And a query that would only match a stringified name matches nothing.
    typeSearch(utils, 'object')
    await waitFor(() => expect(utils.queryByText('Archive')).toBeNull())
    expect(utils.queryByText('No sessions match')).not.toBeNull()
  })

  it('keeps the plain wording when no folder name matched', async () => {
    // A query that narrows to nothing and names no folder has no rows above the
    // line to contradict, so the original sentence is the true one.
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('mortgage numbers')).not.toBeNull())
    typeSearch(utils, 'zzzz-nothing')
    await waitFor(() => expect(utils.queryByText('No sessions match')).not.toBeNull())
    expect(utils.queryByText('No sessions match — only a folder name did')).toBeNull()
  })

  it('does not count a HIDDEN folder as a name match', async () => {
    // A "hide when empty" folder renders no row, so counting its name as a match
    // would put the folder-aware wording on screen with no folder above it — the
    // same contradiction the wording exists to remove, through a different door.
    const folders: ChatFolder[] = [
      ...FOLDERS,
      { id: 'f-hidden', name: 'Archived Deals', collapsed: false, order: 4, hidden: true, history_count: 3 },
    ]
    const utils = renderSidebar({ folders })
    await waitFor(() => expect(utils.queryByText('Trading Desk')).not.toBeNull())
    // `archived` names ONLY the hidden folder (`Archive` does not contain it).
    typeSearch(utils, 'archived')
    await waitFor(() => expect(utils.queryByText('Trading Desk')).toBeNull())
    expect(utils.container.querySelector('[data-folder-row="f-hidden"]')).toBeNull()
    expect(utils.queryByText('No sessions match — only a folder name did')).toBeNull()
    expect(utils.queryByText('No sessions match')).not.toBeNull()
  })
})

describe('chat sidebar — revealing a folder in the BOARD lane', () => {
  const COLUMNS: TagColumn[] = [
    { id: 'c-all', name: 'All', tag_ids: [], mode: 'any', order: 0, include_untagged: true },
  ]

  /**
   * The companion classes the reveal outline is declared against in `index.css`.
   *
   * jsdom does not load the stylesheet, so asserting only that
   * `session-reveal-flash` is on the element passes on a box NO rule can match —
   * which is exactly how the board lane first shipped: the class attached, the
   * outline never painted, and the reveal was silent whenever the column was
   * already on screen. Reading the rule back turns that into a real check.
   */
  function revealFlashCompanions(): string[] {
    const css = readFileSync(join(__dirname, '..', 'index.css'), 'utf8')
    const rule = css.match(/^([^\n{]*\.session-reveal-flash[^\n{]*)\{outline:2px solid var\(--accent\)/m)?.[1]
    expect(rule, 'index.css must declare the reveal outline').toBeDefined()
    return rule!.split(',')
      .map(sel => sel.trim().split('.session-reveal-flash')[0].replace(/^\./, ''))
      .filter(Boolean)
  }

  function expectCanPaint(el: Element | null | undefined) {
    expect(el).toBeTruthy()
    expect(el!.classList.contains('session-reveal-flash')).toBe(true)
    const companions = revealFlashCompanions()
    expect(
      companions.some(c => el!.classList.contains(c)),
      `flashed element carries none of the classes the outline is declared against (${companions.join(', ')}), so nothing paints`,
    ).toBe(true)
  }

  it('flashes the board column, which carries no folder header row', async () => {
    // The board lane renders a folder as a column body marked `data-folder-drop`,
    // with no `data-folder-row` anywhere — so the tree's header flash has nothing to
    // attach to and a board reveal used to scroll and then sit there unmarked. The
    // scroll alone is not the confirmation: the target is often already on screen,
    // so nothing moves and the click reads as dead.
    const scroll = stubScrollIntoView()
    try {
      const utils = renderSidebar({ columns: COLUMNS })
      await waitFor(() => expect(utils.container.querySelector('[data-folder-drop="f-other"]')).not.toBeNull())
      // Precondition: this lane really has no header row to fall back on.
      expect(utils.container.querySelector('[data-folder-row="f-other"]')).toBeNull()

      utils.store.dispatch(requestFolderReveal('f-other'))
      await waitFor(() => expect(scroll.spy).toHaveBeenCalled())
      const flashed = [...utils.container.querySelectorAll('.session-reveal-flash')]
        .find(el => el.getAttribute('data-folder-drop') === 'f-other')
      expectCanPaint(flashed)
    } finally {
      scroll.restore()
    }
  })

  it('flashes only the revealed folder column', async () => {
    const scroll = stubScrollIntoView()
    try {
      const utils = renderSidebar({ columns: COLUMNS })
      await waitFor(() => expect(utils.container.querySelector('[data-folder-drop="f-other"]')).not.toBeNull())
      utils.store.dispatch(requestFolderReveal('f-other'))
      await waitFor(() => expect(scroll.spy).toHaveBeenCalled())
      const marked = [...utils.container.querySelectorAll('.session-reveal-flash')]
        .map(el => el.getAttribute('data-folder-drop'))
        .filter((v): v is string => v !== null)
      expect(new Set(marked)).toEqual(new Set(['f-other']))
    } finally {
      scroll.restore()
    }
  })

  it('flashes a TREE folder row against the same declaration', async () => {
    // The tree lane's row is the case that already worked, asserted through the same
    // reader so the two lanes cannot drift: a future edit that renames the row class
    // or narrows the selector fails here as well as in the board case.
    const scroll = stubScrollIntoView()
    try {
      const utils = renderSidebar()
      await waitFor(() => expect(utils.queryByText('Trading Desk')).not.toBeNull())
      utils.store.dispatch(requestFolderReveal('f-other'))
      await waitFor(() => expect(scroll.spy).toHaveBeenCalled())
      expectCanPaint(utils.container.querySelector('[data-folder-row="f-other"]'))
    } finally {
      scroll.restore()
    }
  })
})
