/**
 * The hide's on-screen trace, in the one lane that has no folder header to hang it from.
 *
 * A board column draws no folder header, so it carries no reveal row — and the hide is
 * persistent, written to localStorage, so a reader who returns a day later sees fewer
 * sessions than they left and nothing on screen that accounts for the difference. A tint
 * on the funnel and a count in its hover `title` do not account for it: the tint is the
 * same accent the view toggle beside it uses for "active", and a hover title is unreachable
 * on a touch device and unspoken by a screen reader.
 *
 * So this file asserts the trace is CONTENT, not decoration, and asserts it in both
 * directions: present with its number while a hide withholds rows, and absent when nothing
 * is hidden — a permanent notice would cost the notice its meaning exactly as a permanent
 * tint does.
 *
 * The count itself comes from one derived population, and that is load-bearing rather than
 * tidy: the raw checkbox set counts folders whose hidden ancestor already removed the whole
 * block, folders the hide-when-empty attribute removes regardless, and keeps counting while
 * a search suspends the hide entirely. Two numbers for one fact, on one screen, is the
 * defect this file also pins against.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { render, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import enManual from '../i18n/locales/en.manual.json'
import { ThemeProvider } from '../hooks/useTheme'

const { boardColumns } = vi.hoisted(() => ({
  boardColumns: [
    {
      id: 'col-idle', name: '', tag_ids: [] as string[], mode: 'any' as const,
      order: 0, source: 'state' as const, state_key: 'idle' as const,
    },
  ],
}))

// Render framer-motion elements as plain DOM (jsdom can't run projection).
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
    'onAnimationStart', 'style', 'custom', 'mode', 'presenceAffectsLayout',
  ])
  const strip = (props: Record<string, unknown>) => {
    const out: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(props)) {
      if (k === 'style') { out.style = v; continue }
      if (!FRAMER_PROPS.has(k)) out[k] = v
    }
    return out
  }
  const make = (tag: string) => React.forwardRef<unknown, Record<string, unknown>>(
    (props, ref) => React.createElement(tag, { ...strip(props), ref }),
  )
  return {
    motion: new Proxy({} as Record<string, unknown>, { get: (_t, tag: string) => make(tag) }),
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'tagColumns') return () => Promise.resolve(boardColumns)
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

const SIDEBAR_SRC = readFileSync(join(__dirname, '..', 'pages', 'ChatSidebar.tsx'), 'utf8')

const HIDDEN_FOLDER = 'folder-hidden'
const SHOWN_FOLDER = 'folder-shown'
const NESTED_FOLDER = 'folder-nested'
/** A folder carrying its OWN hide-when-empty attribute, with archived sessions that
 *  could later revive it. The tree and flat lanes drop it for that attribute alone; a
 *  board column draws its block regardless, which is why the announced count has to
 *  know which lane is on screen. */
const HIDE_WHEN_EMPTY_FOLDER = 'folder-hwe'

const FOLDERS: ChatFolder[] = [
  { id: HIDDEN_FOLDER, name: 'hidden folder', collapsed: false, order: 0 },
  { id: SHOWN_FOLDER, name: 'shown folder', collapsed: false, order: 1 },
  { id: NESTED_FOLDER, name: 'nested folder', collapsed: false, order: 2, parent_id: SHOWN_FOLDER },
  { id: HIDE_WHEN_EMPTY_FOLDER, name: 'quiet folder', collapsed: false, order: 3, hidden: true, history_count: 2 },
] as unknown as ChatFolder[]

/** A conductor inside the hidden folder with a child outside it — the shape that leaves a
 *  visible row whose creator is concealed, which is the citation glyph's own case. */
const SLOTS: ChatSlot[] = [
  { key: 'k-hidden-conductor', title: 'Hidden Conductor', running: false, messages: 2, modified: 4000, folder_id: HIDDEN_FOLDER },
  { key: 'k-shown-child', title: 'Shown Child', running: false, messages: 2, modified: 3000, folder_id: SHOWN_FOLDER, parent: { slot: 'k-hidden-conductor', key: 'k-hidden-conductor' } },
  { key: 'k-shown-plain', title: 'Shown Plain', running: false, messages: 2, modified: 2000, folder_id: SHOWN_FOLDER },
  { key: 'k-nested', title: 'Nested Session', running: false, messages: 2, modified: 1000, folder_id: NESTED_FOLDER },
] as unknown as ChatSlot[]

function renderSidebar(withColumns: boolean, slots: ChatSlot[] = SLOTS) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {}, workflowRuns: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], FOLDERS)
  qc.setQueryData(['tag-columns'], withColumns ? boardColumns : [])
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

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
})
afterEach(() => vi.clearAllMocks())

function hideFolders(...ids: string[]) {
  localStorage.setItem('mc-flat-hidden-folders', JSON.stringify(ids))
}

/** The board lane's notice, or null. */
const notice = (c: HTMLElement) => c.querySelector('[data-testid="board-hidden-folders"]')
/** A folder's block header inside the board's one column, or null when the column
 *  does not draw that folder at all. */
const header = (c: HTMLElement, id: string) =>
  c.querySelector(`[data-testid="col-col-idle-folder-${id}"]`)
/** The notice's COUNT text alone.
 *
 *  The row also carries the action word, and `textContent` concatenates siblings with
 *  no separator ("1 hidden folderShow"), so a `\b` after the noun would fail on a row
 *  that is perfectly correct. The count is read from its own element and the action
 *  from its own; the row-level assertion that both are on screen is a `toContain`.
 */
const noticeCount = (c: HTMLElement) =>
  c.querySelector('[data-testid="board-hidden-folders-count"]')?.textContent ?? ''
/** The notice's ACTION text alone, read the same way and for the same reason. */
const noticeAction = (c: HTMLElement) =>
  c.querySelector('[data-testid="board-hidden-folders-action"]')?.textContent ?? ''

describe('a board says on screen how many folders it is hiding', () => {
  it('names one hidden folder in the singular, as content rather than as a tooltip', () => {
    hideFolders(HIDDEN_FOLDER)
    const { container } = renderSidebar(true)
    const row = notice(container)
    expect(row, 'a board with a hide draws no notice at all').not.toBeNull()
    // Content, not an attribute: this is the assertion that a touch reader and a
    // screen reader both get the number, since neither reaches a hover `title`.
    expect(noticeCount(container)).toMatch(/^1 hidden folder$/)
    expect(row?.textContent).toContain('1 hidden folder')
    expect(noticeCount(container)).not.toMatch(/folders/)
    expect(row?.getAttribute('data-hidden-folder-count')).toBe('1')
  })

  it('names two hidden folders in the plural', () => {
    // Two DIFFERENT containers, which is also the case the count has to survive: the
    // tree lane answers this with one reveal row per container, and a board has to
    // collapse both into the single number it has room for.
    hideFolders(HIDDEN_FOLDER, NESTED_FOLDER)
    const { container } = renderSidebar(true)
    expect(noticeCount(container)).toMatch(/^2 hidden folders$/)
    expect(notice(container)?.getAttribute('data-hidden-folder-count')).toBe('2')
  })

  it('draws no notice when nothing is hidden', () => {
    // The reverse direction. A standing notice says a filter is active when none is,
    // which costs it exactly the meaning the tint lost by being permanent.
    const { container } = renderSidebar(true)
    expect(notice(container)).toBeNull()
  })

  it('leaves the notice out of a lane that already ends its containers with a reveal row', () => {
    // Not a board: the tree lane hangs the same announcement off the container it
    // belongs to, at the depth it happened. Two announcements of one hide would be the
    // same double-reporting the single derived count exists to prevent.
    hideFolders(HIDDEN_FOLDER)
    localStorage.setItem('mc-sidebar-lane', 'tree')
    const { container } = renderSidebar(false)
    expect(notice(container)).toBeNull()
    expect(container.querySelector('[data-testid="hidden-reveal-root"]')).not.toBeNull()
  })

  it('is a button whose name says which control brings the folders back', () => {
    hideFolders(HIDDEN_FOLDER)
    const { container } = renderSidebar(true)
    const row = notice(container)
    expect(row?.tagName).toBe('BUTTON')
    const name = row?.getAttribute('aria-label') ?? ''
    // The count AND the way back, because the notice is the one thing on screen that
    // points at the menu holding the undo.
    expect(name).toMatch(/1 hidden folder\b/)
    expect(name).toMatch(/sort & filter/i)
  })

  it('names the action in VISIBLE text, not only in the accessible name', () => {
    // The hover-only failure this row exists to end, applied to the row itself: a
    // sighted reader with no pointer gets the count and then has to GUESS the row is
    // tappable, because "open sort & filter to bring them back" lived in `title` and
    // `aria-label` alone. So the word for the action is on screen beside the count.
    hideFolders(HIDDEN_FOLDER)
    const { container } = renderSidebar(true)
    const row = notice(container)
    expect(noticeAction(container)).toMatch(/^Show$/)
    expect(row?.textContent).toContain('Show')
    // And it is content, not a decorative glyph carrying the meaning: every svg inside
    // the row is hidden from the reader, so the name it announces comes from text.
    for (const svg of Array.from(row?.querySelectorAll('svg') ?? [])) {
      expect(svg.getAttribute('aria-hidden')).toBe('true')
    }
  })

  it('takes the action word from the catalog, so all 13 locales carry it', () => {
    // `pages.chatSidebar.show` already exists in every catalog, which is why the visible
    // affordance costs no new translation. A literal English word here would read as
    // English in twelve locales.
    hideFolders(HIDDEN_FOLDER)
    const { container } = renderSidebar(true)
    expect(notice(container)?.textContent).toContain(enManual.pages.chatSidebar.show)
  })

  it('shows the folders it says it will, even when the folder list was rolled up', () => {
    // The word on the button is the promise. The menu's folder list is the way back and
    // sits behind the shelf, so opening the menu over a rolled-up shelf puts a dense
    // panel on screen with no folders in it -- the reader clicked Show and nothing was
    // shown. So the click unshelves as well as opening.
    localStorage.setItem('mc-filter-folders-shelved', '1')
    hideFolders(HIDDEN_FOLDER)
    const { container, queryByTestId } = renderSidebar(true)
    fireEvent.click(notice(container) as Element)
    expect(queryByTestId('folder-filter-show-all'), 'the menu opened but the folder list stayed rolled up').not.toBeNull()
    expect(queryByTestId(`folder-filter-${HIDDEN_FOLDER}`)).not.toBeNull()
  })

  it('opens the filter menu, where the per-folder checkbox and Show all folders live', () => {
    hideFolders(HIDDEN_FOLDER)
    const { container, queryByTestId } = renderSidebar(true)
    expect(queryByTestId('folder-filter-show-all'), 'the menu is open before anything was clicked').toBeNull()
    fireEvent.click(notice(container) as Element)
    expect(queryByTestId('folder-filter-show-all')).not.toBeNull()
    expect(queryByTestId(`folder-filter-${HIDDEN_FOLDER}`)).not.toBeNull()
  })

  it('carries its colour as theme tokens, so it holds in a light theme as well as a dark one', () => {
    // Every theme in `index.css` defines `--warn` and `--warn-subtle`; a literal colour
    // would be legible in whichever theme it was picked against and wrong in the other
    // fifteen. Warn rather than accent is the separate point: accent is what the view
    // toggle beside the funnel uses for "this lane is active".
    hideFolders(HIDDEN_FOLDER)
    const { container } = renderSidebar(true)
    const cls = notice(container)?.className ?? ''
    expect(cls).toMatch(/\btext-warn\b/)
    expect(cls).toMatch(/\bbg-warn-subtle\b/)
    expect(cls).not.toMatch(/#[0-9a-fA-F]{3,6}|rgb\(/)
  })
})

describe('one derived count, reported the same way everywhere', () => {
  it('pluralizes the funnel title through the catalog instead of gluing fragments', () => {
    hideFolders(HIDDEN_FOLDER)
    const { container } = renderSidebar(true)
    const funnel = container.querySelector('[data-folder-hide-active]')
    expect(funnel?.getAttribute('data-folder-hide-active')).toBe('1')
    // A noun, in the singular, agreeing with its number — none of which a
    // `${count} ${hidden}` concatenation can promise in any language.
    expect(funnel?.getAttribute('title')).toMatch(/1 hidden folder\b/)
    expect(funnel?.getAttribute('title')).not.toMatch(/folders/)
  })

  it('pluralizes the funnel title for more than one', () => {
    hideFolders(HIDDEN_FOLDER, NESTED_FOLDER)
    const { container } = renderSidebar(true)
    expect(container.querySelector('[data-folder-hide-active]')?.getAttribute('title')).toMatch(/2 hidden folders/)
  })

  it('keeps the funnel tinted warn, not the accent the view toggle uses for "active"', () => {
    hideFolders(HIDDEN_FOLDER)
    const { container } = renderSidebar(true)
    const cls = container.querySelector('[data-folder-hide-active]')?.className ?? ''
    expect(cls).toMatch(/\btext-warn\b/)
    expect(cls).not.toMatch(/\btext-accent\b/)
  })

  it('keeps the funnel name a control name, with the count in content instead', () => {
    // A button's accessible name names the button. Putting a changing number in it
    // renames the control under a reader navigating by name, and the number is already
    // on screen as content — which is where a screen reader meets it anyway.
    hideFolders(HIDDEN_FOLDER)
    const { container, getByLabelText } = renderSidebar(true)
    expect(getByLabelText('Sort and filter sessions')).not.toBeNull()
    expect(noticeCount(container)).toMatch(/^1 hidden folder$/)
  })

  it('counts a folder whose hidden ancestor already took the block away only once', () => {
    // The parent and its child both unchecked. The child's block is gone with the
    // parent's, so announcing two withheld folders would name one the reader cannot
    // act on separately — and it is the raw checkbox set that would say two.
    hideFolders(SHOWN_FOLDER, NESTED_FOLDER)
    const { container } = renderSidebar(true)
    expect(notice(container)?.getAttribute('data-hidden-folder-count')).toBe('1')
    expect(noticeCount(container)).toMatch(/^1 hidden folder$/)
  })

  it('announces a hide-when-empty folder the person also unchecked, because a board draws its block anyway', () => {
    // A board column's folder list filters on `isFolderFilteredOut` ALONE, so it draws
    // this folder's block whatever the folder's own hide-when-empty attribute says.
    // Unchecking it therefore does take the block away, and the announcement has to
    // follow: a count narrowed by `isFolderHidden` reads zero here, which leaves the
    // header gone with nothing on screen to account for it.
    hideFolders(HIDE_WHEN_EMPTY_FOLDER)
    const { container } = renderSidebar(true)
    expect(header(container, HIDE_WHEN_EMPTY_FOLDER), 'the uncheck did not remove the block').toBeNull()
    expect(notice(container), 'the board hid a block and said nothing').not.toBeNull()
    expect(noticeCount(container)).toMatch(/^1 hidden folder$/)
    expect(notice(container)?.getAttribute('data-hidden-folder-count')).toBe('1')
    const funnel = container.querySelector('[data-folder-hide-active]')
    expect(funnel?.getAttribute('data-folder-hide-active')).toBe('1')
    expect(funnel?.getAttribute('title')).toMatch(/1 hidden folder\b/)
  })

  it('CONTROL: the board draws that folder when it is NOT unchecked', () => {
    // Without this the pin above proves nothing: a folder the board never renders is
    // not being withheld by the uncheck, and announcing it would be the over-report.
    const { container } = renderSidebar(true)
    expect(header(container, HIDE_WHEN_EMPTY_FOLDER)).not.toBeNull()
    expect(notice(container)).toBeNull()
  })

  it('stays silent about it in a lane that drops it for its own attribute', () => {
    // The other direction, and why the count is lane-scoped rather than simply widened.
    // The tree lane narrows by `isFolderHidden` itself, so this folder is absent there
    // whether or not it is unchecked -- the uncheck takes nothing a reader would have
    // seen, and announcing it would claim a withholding that did not happen.
    hideFolders(HIDE_WHEN_EMPTY_FOLDER)
    localStorage.setItem('mc-sidebar-lane', 'tree')
    const { container } = renderSidebar(false)
    expect(container.querySelector('[data-folder-hide-active]')).toBeNull()
    expect(container.querySelector('[data-testid="hidden-reveal-root"]')).toBeNull()
  })

  it('says nothing is withheld while a search suspends the hide', () => {
    // Searching turns the folder hide off entirely, so every match stays reachable. A
    // count that kept reporting during a search would claim rows are withheld at the
    // moment none are.
    hideFolders(HIDDEN_FOLDER)
    const { container, getByPlaceholderText } = renderSidebar(true)
    expect(notice(container)).not.toBeNull()
    const box = container.querySelector('input[type="search"]')
      ?? getByPlaceholderText(/search/i)
    fireEvent.change(box as Element, { target: { value: 'Shown' } })
    expect(notice(container)).toBeNull()
    expect(container.querySelector('[data-folder-hide-active]')).toBeNull()
  })

  it('renders the reveal row through the same pluralized key', () => {
    // The flat lane collapses every hide into one row, so it is where a plural reveal
    // row is reachable — and it must read exactly as the board's notice does.
    hideFolders(HIDDEN_FOLDER, NESTED_FOLDER)
    localStorage.setItem('mc-sidebar-lane', 'flat')
    const { container } = renderSidebar(false)
    expect(container.querySelector('[data-testid="hidden-reveal-flat"]')?.textContent).toMatch(/2 hidden folders/)
  })

  it('renders a single-folder reveal row in the singular', () => {
    hideFolders(HIDDEN_FOLDER)
    localStorage.setItem('mc-sidebar-lane', 'flat')
    const { container } = renderSidebar(false)
    const text = container.querySelector('[data-testid="hidden-reveal-flat"]')?.textContent ?? ''
    expect(text).toMatch(/1 hidden folder\b/)
    expect(text).not.toMatch(/folders/)
  })

  it('selects the plural form in code nowhere on this surface', () => {
    // The guard for the whole pair above. A `count === 1 ? a : b` at the call site is
    // English's rule applied to every language — Russian selects between four forms and
    // Chinese between one, neither of which a ternary can express. So the two fragment
    // keys a ternary needs must not be referenced at all.
    //
    // Namespace-qualified, because `show_hidden_folder` legitimately ends in the shorter
    // name and an unqualified needle matches it. Built from parts so this assertion does
    // not contain its own needles and cannot report its own line. Compared as booleans to
    // keep a failure's output the needle rather than the whole file.
    const ns = 'pages.chatSidebar.'
    for (const leaf of ['hidden\'', 'hidden' + '_folder\'', 'hidden' + '_folders\'']) {
      expect(SIDEBAR_SRC.includes(ns + leaf), `${ns}${leaf} is still referenced`).toBe(false)
    }
    // Control: the replacement really is present, so the three absences above mean the
    // fragments are gone rather than that the file failed to load.
    expect(SIDEBAR_SRC.includes(ns + 'hidden_folder_count')).toBe(true)
  })
})

describe('the citation glyph says whose session opened this one', () => {
  it('names the open creator on the glyph itself', () => {
    // A concealed conductor with a visible child is what puts this glyph on screen: the
    // row is placed under nothing, yet its creator is open and running, so the lane owes
    // the reader the creator's name rather than a bare arrow.
    hideFolders(HIDDEN_FOLDER)
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { container } = renderSidebar(false)
    const glyph = container.querySelector('[data-cites-parent]')
    expect(glyph, 'no row carries an open-creator citation in this fixture').not.toBeNull()
    // `role="img"` is what makes the name reachable: an `aria-label` on a bare `<svg>`
    // sits on an element with no role that admits a name.
    expect(glyph?.getAttribute('role')).toBe('img')
    const name = glyph?.getAttribute('aria-label') ?? ''
    expect(name).toMatch(/Opened by/i)
    expect(name).toContain('k-hidden-conductor')
    // The drawing itself is decorative — named twice, a reader hears it twice.
    expect(glyph?.querySelector('svg')?.getAttribute('aria-hidden')).toBe('true')
    expect(glyph?.querySelector('svg')?.getAttribute('aria-label')).toBeNull()
  })

  it('names a closed creator on the same glyph the same way', () => {
    // The sibling branch. One glyph in two states, and a name in only one of them is a
    // glyph that is named or not depending on a fact the reader cannot see.
    const orphaned = SLOTS.map(s => (
      (s as unknown as { key: string }).key === 'k-shown-child'
        ? { ...s, parent: { slot: 'k-absent', key: 'k-absent' } }
        : s
    )) as unknown as ChatSlot[]
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { container } = renderSidebar(false, orphaned)
    const glyph = container.querySelector('[data-orphan-of]')
    expect(glyph).not.toBeNull()
    expect(glyph?.getAttribute('role')).toBe('img')
    expect(glyph?.getAttribute('aria-label')).toContain('k-absent')
    expect(glyph?.querySelector('svg')?.getAttribute('aria-hidden')).toBe('true')
  })
})
