/**
 * A folder's conductor: the menu item that is always on, and the pinned card
 * that ships behind a Feature Previews flag, default off.
 *
 * The menu item is released. A folder whose sessions were all opened by one of
 * them offers that chat at the top of the folder's actions menu, and choosing it
 * opens that conversation. It is a menu item rather than a button beside the
 * row's other two because a row of actions is capped at two controls, and it
 * NAMES the chat: a label reading only "conductor" left a first-time reader
 * unable to say what the click opens, with two other chat affordances on the
 * same row.
 *
 * The pinned card is held. With the flag off, the lane is the lane on `main` --
 * the hover group holds exactly its two controls, a conducted folder draws the
 * folder glyph, counts what it counted, draws a card for every session filed in
 * it, and that card sits where recency puts it. Tens of thousands of people read
 * this list, so that is the contract asserted hardest here.
 *
 * With the flag on, the conductor's card is lifted to the top of its folder and
 * marked, INSIDE the folder body. Drawing it outside the body outdented its text
 * off the column that
 * docs/decisions/2026-10-09-folder-glyph-sits-on-its-connector-line.md fixes,
 * and the alignment case below is the pin on that.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatFolder, ChatSlot } from '../types'
import type { RootState } from '../store'

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
  loadChatConfig: () => ({ tagColumnsEnabled: false, hideEmptyFolderBody: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

// `useIsMobile` resolves at module load, so the hook itself is mocked rather
// than the viewport: this file asserts the DESKTOP folder row, whose menu is a
// flyout. At phone width the same items are inlined under a caption instead.
vi.mock('../hooks/useIsMobile', () => ({
  MOBILE_BREAKPOINT: 768,
  useIsMobile: () => false,
}))

const mocks = vi.hoisted(() => ({ chatFolders: vi.fn() }))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
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

import ChatSidebar, { FOLDER_BODY_CLS } from '../pages/ChatSidebar'

const LANE = 'folder-lane'
const PLAIN = 'folder-plain'
const SUB = 'folder-sub'
const LEAD = 'k-lead'
const W1 = 'k-worker-1'
const W2 = 'k-worker-2'
const SUB_LEAD = 'k-sub-lead'
const SUB_W1 = 'k-sub-worker-1'
const SUB_W2 = 'k-sub-worker-2'

const folders: ChatFolder[] = [
  { id: LANE, name: 'remote-crew', order: 0 },
  { id: PLAIN, name: 'docs', order: 1 },
]

/**
 * One conducted folder and one ordinary folder.
 *
 * No timestamps anywhere, deliberately: a row with no activity instant is never
 * stale, so every row stays in the live list and no assertion depends on where
 * `now` happens to sit. A single old `created` was enough to sweep a card into
 * the collapsed stale section, which reads exactly like the feature having
 * removed it.
 */
const slots: ChatSlot[] = [
  { key: LEAD, title: 'Remote crew rollout', messages: 9, running: false, folder_id: LANE },
  { key: W1, title: 'Fargate task definition', messages: 3, running: false, folder_id: LANE, parent: { slot: LEAD, key: LEAD } },
  { key: W2, title: 'Rotate remote crew token', messages: 2, running: false, folder_id: LANE, parent: { slot: LEAD, key: LEAD } },
  { key: 'k-plain-a', title: 'Blog draft', messages: 4, running: false, folder_id: PLAIN },
  { key: 'k-plain-b', title: 'Release notes', messages: 1, running: false, folder_id: PLAIN },
]

/**
 * Mount the sidebar. SYNCHRONOUS, and that is load-bearing.
 *
 * A Radix menu in jsdom opens only while the render is still in its first tick:
 * nothing here holds the focus it grabs, so the first macrotask both tears an
 * open menu down and leaves a later open gesture with nothing to open. So a test
 * that drives the menu must mount, open and assert with no await between them,
 * and call `settle` afterwards to let the lane finish painting.
 */
function renderSidebar(slotData: ChatSlot[] = slots, onSelectSlot?: (key: string) => void, folderData: ChatFolder[] = folders, over: { connected?: boolean } = {}) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: over.connected ?? true, slots: slotData, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, automations: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-tags'], [])
  qc.setQueryData(['tag-columns'], [])
  qc.setQueryData(['chat-folders'], folderData)
  qc.setQueryData(['kirocrewConfig'], { dashboard: {} })
  mocks.chatFolders.mockImplementation(() => Promise.resolve(folderData))
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slotData} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
              onSelectSlot={onSelectSlot}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return utils
}

/**
 * Wait for the lane to finish painting.
 *
 * `ThemeProvider` settles its own state after mount, so a test that returns
 * before the rows are on screen leaves an act() warning behind, and one that
 * asserts before them reads a half-rendered lane.
 */
async function settle(utils: { findByText: (text: string) => Promise<HTMLElement> }) {
  await utils.findByText('Blog draft')
}

/** The folder row's own box, the only element carrying `data-folder-row`. */
function rowOf(container: HTMLElement, folderId: string): HTMLElement {
  const row = container.querySelector(`[data-folder-row="${folderId}"]`)
  if (!row) throw new Error(`no folder row for ${folderId}`)
  return row as HTMLElement
}

/**
 * A session CARD, as the ordinary lanes draw it.
 *
 * `data-slot-key`, which sits on the outermost row element the card renders —
 * NOT `data-session-row`, which carries the row IDENTITY (origin-qualified for a
 * federated row) and so does not match a bare slot key.
 */
function cardOf(container: HTMLElement, key: string): Element | null {
  return container.querySelector(`[data-slot-key="${key}"]`)
}

/**
 * Open the folder's ⋯ menu, SYNCHRONOUSLY.
 *
 * Radix tears the menu down on the first macrotask because nothing in jsdom
 * holds the focus it grabs, so every caller must drive the item it wants in the
 * same tick with no await in between. Rename is the anchor item: it is in the
 * menu on main, so a silent no-open cannot pass a later query.
 */
function openFolderMenu(folderId: string) {
  // Keyboard, because a Radix trigger needs a real PointerEvent jsdom has not
  // got. Rename is the anchor: it is in this menu on main, so a silent no-open
  // cannot pass a later query.
  fireEvent.keyDown(document.querySelector(`[data-testid="folder-menu-${folderId}"]`)!, { key: 'Enter' })
  expect(document.querySelector(`[data-testid="folder-rename-${folderId}"]`)).not.toBeNull()
}

const menuItem = (folderId: string, suffix = ''): HTMLElement | null =>
  document.querySelector(`[data-testid="folder-conductor-chat-${folderId}${suffix}"]`)

/**
 * Type into the lane's search box, the one filter a test can drive without a
 * menu. `slotFilter` feeds `filters.search.text` with no debounce, so the lane
 * is narrowed on the next commit and the whole case stays synchronous.
 */
function typeSearch(container: HTMLElement, text: string) {
  const input = container.querySelector('[data-testid="search-field-glass"] input')
  if (!input) throw new Error('no search field')
  fireEvent.change(input, { target: { value: text } })
}

/** The folder row's own chat button -- the third control, not the menu item. */
const rowChatButton = (container: HTMLElement, folderId: string): HTMLElement | null =>
  container.querySelector(`[data-testid="folder-conductor-chat-row-${folderId}"]`)

/**
 * The folder row's action cluster, as the testids of its buttons in DOM order.
 *
 * The collapse toggle is excluded: it is the row's own disclosure control and
 * sits outside the cluster. What is left is what a reader sees on hover, in the
 * order they see it.
 */
function clusterOf(container: HTMLElement, folderId: string): string[] {
  return Array.from(rowOf(container, folderId).querySelectorAll('button'))
    .map(b => b.getAttribute('data-testid') ?? '')
    .filter(id => id.startsWith('folder-menu-')
      || id.startsWith('folder-new-chat-')
      || id.startsWith('folder-conductor-chat-row-'))
}

/** The pinned conductor card's own wrapper: the element carrying the accent bar. */
function pinnedOf(container: HTMLElement, folderId: string): HTMLElement | null {
  return container.querySelector(`[data-folder-conductor="${folderId}"]`)
}

/** Every session card inside one folder's block, in the order the DOM holds them. */
function cardOrderIn(container: HTMLElement, folderId: string): (string | null)[] {
  const block = container.querySelector(`[data-folder-drop="${folderId}"]`)
  if (!block) throw new Error(`no folder block for ${folderId}`)
  return Array.from(block.querySelectorAll('[data-slot-key]')).map(e => e.getAttribute('data-slot-key'))
}

beforeEach(() => {
  localStorage.clear()
  mocks.chatFolders.mockImplementation(() => Promise.resolve(folders))
  // Freeze the stale sweep's clock to the instant the time fixture is anchored
  // to (`NOW`, in seconds -> ms here). The sweep reads `Date.now()` at render
  // time; pinning it to the fixture instant keeps fixture-time and render-time
  // identical, so no row drifts past the stale cutoff between the two reads.
  // Real timers stay in place, so `settle`'s `findByText` retry still runs.
  vi.spyOn(Date, 'now').mockReturnValue(NOW * 1000)
})
afterEach(() => vi.restoreAllMocks())

describe('the folder menu item', () => {
  it('names the FOLDER, and says what the chat it opens is to it', async () => {
    // The label names the folder the menu was opened on -- the thing the reader
    // has in front of them -- not the conductor's own session title, which they
    // have not necessarily seen. And not "Chat with conductor" either: that word
    // on its own told a first-time reader nothing, which is what the second line
    // is for.
    const utils = renderSidebar()
    openFolderMenu(LANE)
    const item = menuItem(LANE)
    expect(item).not.toBeNull()
    expect(item!.textContent).toContain('remote-crew')
    expect(item!.textContent).not.toContain('Remote crew rollout')
    expect(item!.textContent).toContain('started this folder')
    await settle(utils)
  })

  it('is the first item in that menu', async () => {
    // First, because it is the one thing a reader opens the menu of a running
    // folder for; the housekeeping items below it are unchanged and unmoved.
    const utils = renderSidebar()
    openFolderMenu(LANE)
    const items = Array.from(document.querySelectorAll('[role="menuitem"]'))
    expect(items[0]?.getAttribute('data-testid')).toBe(`folder-conductor-chat-${LANE}`)
    await settle(utils)
  })

  it('is absent for a folder with no conductor', async () => {
    // `docs` holds two hand-made sessions: nobody opened anybody, so there is
    // nothing to open and the menu is the menu on main.
    const utils = renderSidebar()
    openFolderMenu(PLAIN)
    expect(menuItem(PLAIN)).toBeNull()
    await settle(utils)
  })

  it('opens the conductor when chosen', async () => {
    const onSelectSlot = vi.fn()
    const utils = renderSidebar(slots, onSelectSlot)
    openFolderMenu(LANE)
    fireEvent.click(menuItem(LANE)!)
    expect(onSelectSlot).toHaveBeenCalledWith(LEAD)
    await settle(utils)
  })

  it('is offered by the row\'s right-click menu too', async () => {
    // Both menus render from one list, so the action cannot be in one and not
    // the other. The context copy's testids carry a `-ctx` suffix.
    const utils = renderSidebar()
    fireEvent.contextMenu(rowOf(utils.container, LANE), { clientX: 40, clientY: 20 })
    expect(menuItem(LANE, '-ctx')).not.toBeNull()
    await settle(utils)
  })
})

describe('what a conducted folder must still render', () => {
  it('keeps the two controls it has on main, unmoved, and adds the third after them', async () => {
    // The two-controls-per-row cap is bypassed for this one action by an owner
    // ruling, so the assertion is no longer the COUNT -- it is that the existing
    // pair is untouched and the new control comes last. A reordering that put
    // the chat button first would move the ⋯ trigger and new-chat under a
    // reader's muscle memory, which the ruling did not ask for.
    const utils = renderSidebar()
    await settle(utils)
    expect(clusterOf(utils.container, LANE)).toEqual([
      `folder-menu-${LANE}`,
      `folder-new-chat-${LANE}`,
      `folder-conductor-chat-row-${LANE}`,
    ])
    // The MENU item is still only in the menu: the row button is an addition,
    // not a second copy of it rendered inline.
    expect(menuItem(LANE)).toBeNull()
  })

  it('draws a card for every session in it, the conductor included', async () => {
    const { container, findByText } = renderSidebar()
    await settle({ findByText })
    expect(cardOf(container, LEAD)).not.toBeNull()
    expect(cardOf(container, W1)).not.toBeNull()
    expect(cardOf(container, W2)).not.toBeNull()
  })

  it('keeps the folder glyph and the direct-child count', async () => {
    const { container, findByText } = renderSidebar()
    await settle({ findByText })
    // The lucide shape, not a mark standing in for it.
    const shape = container.querySelector(`[data-testid="folder-collapse-${LANE}-shape"]`)
    expect(shape?.tagName.toLowerCase()).toBe('svg')
    expect(rowOf(container, LANE).textContent).toContain('3')
  })
})

describe('a folder with no conductor', () => {
  it('renders the row and its cards exactly as it did before', async () => {
    const { container, findByText } = renderSidebar()
    await settle({ findByText })
    const shape = container.querySelector(`[data-testid="folder-collapse-${PLAIN}-shape"]`)
    expect(shape?.tagName.toLowerCase()).toBe('svg')
    expect(rowOf(container, PLAIN).textContent).toContain('2')
    expect(cardOf(container, 'k-plain-a')).not.toBeNull()
    expect(cardOf(container, 'k-plain-b')).not.toBeNull()
  })

  it('keeps the two-control cluster -- the third button is not emitted at all', async () => {
    // `docs` holds two hand-made sessions: nobody opened anybody. The cap is
    // bypassed only where there is something to reach, so this row is the row
    // on main, and the button is ABSENT rather than present-and-disabled.
    const { container, findByText } = renderSidebar()
    await settle({ findByText })
    expect(clusterOf(container, PLAIN)).toEqual([
      `folder-menu-${PLAIN}`,
      `folder-new-chat-${PLAIN}`,
    ])
    expect(rowChatButton(container, PLAIN)).toBeNull()
  })
})

/**
 * The folder ROW's chat button: the third control in the hover cluster.
 *
 * Zezhen's ruling put the action on the row itself and bypassed the
 * two-controls-per-row cap for it, so these cases pin the three things that
 * ruling is: the button is there for a conducted folder, it opens the conductor
 * the same selector resolves, and it is always on -- the preview flag holds the
 * pinned LAYOUT, never the way to reach the conversation.
 */
describe('the folder row\'s chat button', () => {
  it('renders for a conducted folder and carries the folder-named label', async () => {
    const { container, findByText } = renderSidebar()
    await settle({ findByText })
    const button = rowChatButton(container, LANE)
    expect(button).not.toBeNull()
    // Reachable without hover: the accessible name is the same string the menu
    // item shows, so the action is named identically wherever it is reached.
    expect(button!.getAttribute('aria-label')).toContain('remote-crew')
    expect(button!.getAttribute('title')).toBe(button!.getAttribute('aria-label'))
    // A glyph, not a mark standing in for it.
    expect(button!.querySelector('svg')).not.toBeNull()
  })

  it('carries its name IN THE PAGE, not only in the OS tooltip', async () => {
    // The UX finding this answers: an icon-only control keeps its name in
    // `title`, which is OS chrome a reader may never see and no screenshot can
    // show, and a blind read of the hovered row came back "nothing tells me
    // what it does". So the name is rendered in the page, revealed on hover and
    // on keyboard focus -- and it is the SAME string as `title`, `aria-label`
    // and the menu item, so one action never acquires a second name.
    const { container, findByText } = renderSidebar()
    await settle({ findByText })
    const button = rowChatButton(container, LANE)!
    const tip = button.querySelector('span[aria-hidden="true"]')
    expect(tip).not.toBeNull()
    expect(tip!.textContent!.trim()).toBe(button.getAttribute('aria-label'))
    // Hidden from assistive tech: the button's own `aria-label` already carries
    // the string, and a screen reader must not say it twice.
    expect(tip!.getAttribute('aria-hidden')).toBe('true')
    // Out of flow, so it costs the row no width at any sidebar width -- the
    // inline label this replaced made the hover overlay 136px of a 242px row
    // and covered the folder's own name.
    expect(tip!.className).toContain('absolute')
    expect(tip!.className).toContain('pointer-events-none')
    // Revealed by hover AND by keyboard focus, never only by hover.
    expect(tip!.className).toContain('group-hover/conductor-chat:block')
    expect(tip!.className).toContain('group-focus-visible/conductor-chat:block')
    expect(button.className).toContain('group/conductor-chat')
    // And the touch state is spelled out on the element rather than implied.
    // `hoverVariantPolicy` fails the build without it; this pins it here too,
    // so a reader who takes it for a redundant class has a second place saying
    // it is load-bearing.
    expect(tip!.className).toContain('[@media(hover:none)]:hidden')
  })

  it('switches to the conductor when clicked', async () => {
    const onSelectSlot = vi.fn()
    const { container, findByText } = renderSidebar(slots, onSelectSlot)
    await settle({ findByText })
    fireEvent.click(rowChatButton(container, LANE)!)
    expect(onSelectSlot).toHaveBeenCalledWith(LEAD)
  })

  it('is a no-op while the backend is offline', async () => {
    // The gate every session click has. Switching to a session nothing can load
    // would leave an empty pane, and the row the click came from is the only way
    // back to it.
    const onSelectSlot = vi.fn()
    const { container, findByText } = renderSidebar(slots, onSelectSlot, folders, { connected: false })
    await settle({ findByText })
    expect(rowChatButton(container, LANE)).not.toBeNull()
    fireEvent.click(rowChatButton(container, LANE)!)
    expect(onSelectSlot).not.toHaveBeenCalled()
  })

  it('is there with the preview flag OFF, which gates only the pinned layout', async () => {
    // The explicit ruling: always-on. The flag-off lane pins nothing, and the
    // row still has its way to the conversation.
    const { container, findByText } = renderSidebar()
    await settle({ findByText })
    expect(container.querySelectorAll('[data-folder-conductor]')).toHaveLength(0)
    expect(rowChatButton(container, LANE)).not.toBeNull()
  })
})
/**
 * The layout, which ships behind a Feature Previews flag, default OFF.
 *
 * With the flag off the lane is the lane on main: these cases assert that first,
 * because it is the contract that covers everyone who has not opted in.
 *
 * With it on, the chat that started a folder's other chats is lifted to the top
 * of that folder -- FIRST, before the sub-folders and before the sorted rows --
 * and marked with an accent bar and a one-word tag. It stays INSIDE the folder
 * body: an earlier revision drew it outside, which outdented its text 15px left
 * of the folder name and broke the shared edge recorded in
 * docs/decisions/2026-10-09-folder-glyph-sits-on-its-connector-line.md. The
 * alignment case below is the pin on that, by VALUE: the conductor's agent label
 * and every other card's sit at one x.
 */

/** Turn the pin on for this render, the way the Settings toggle does. */
const PIN_KEY = 'mc-preview-folder-conductor'
const pinOn = () => localStorage.setItem(PIN_KEY, '1')

/**
 * A payload whose RECENCY contradicts the pinning.
 *
 * The conductor is the oldest row in its folder and would therefore sort LAST
 * under the lane's `date-desc` order. Every instant is within the hour, so
 * nothing here is old enough for the stale sweep to collapse: the only thing
 * being tested is the pin.
 *
 * `NOW` is a FIXED instant, not `Date.now()` read at import time, and the stale
 * sweep's clock is frozen to the same instant for every render (see the
 * `Date.now` freeze in `beforeEach`). The fixture used to anchor on the live
 * clock while the render read `Date.now()` again when it ran: a clock that
 * stepped forward past the stale cutoff between the two reads would collapse
 * `Blog draft` out of the lane and make `settle` reject though no layout had
 * changed. One frozen instant for both reads keeps the sweep off these rows
 * deterministically. The value is an arbitrary fixed point in 2026, in seconds
 * (the slot `modified` unit); the freeze below hands the sweep the millisecond
 * form.
 */
const NOW = 1_767_225_600 // 2026-01-01T00:00:00Z, in seconds
const timeSlots: ChatSlot[] = [
  { key: LEAD, title: 'Remote crew rollout', messages: 9, running: false, folder_id: LANE, modified: NOW - 600 },
  { key: W1, title: 'Fargate task definition', messages: 3, running: false, folder_id: LANE, modified: NOW - 300, parent: { slot: LEAD, key: LEAD } },
  { key: W2, title: 'Rotate remote crew token', messages: 2, running: false, folder_id: LANE, modified: NOW - 30, parent: { slot: LEAD, key: LEAD } },
  { key: 'k-plain-a', title: 'Blog draft', messages: 4, running: false, folder_id: PLAIN, modified: NOW - 600 },
  { key: 'k-plain-b', title: 'Release notes', messages: 1, running: false, folder_id: PLAIN, modified: NOW - 30 },
] as unknown as ChatSlot[]

/** The agent-name line of one card: the column the recorded decision fixes. */
function agentLabelOf(container: HTMLElement, key: string): Element | null {
  return cardOf(container, key)?.querySelector('.session-agent-label') ?? null
}

describe('with the preview flag off', () => {
  it('pins nothing, bars nothing and tags nothing, in any folder', async () => {
    // The contract for everyone who has not opted in. Default off: the test
    // sets no key at all, so this is what a fresh device renders.
    const { container, findByText } = renderSidebar(timeSlots)
    await settle({ findByText })
    expect(container.querySelectorAll('[data-folder-conductor]')).toHaveLength(0)
    expect(container.querySelectorAll('[data-testid^="folder-conductor-tag-"]')).toHaveLength(0)
    // And the conductor sits where recency puts it, which here is LAST.
    expect(cardOrderIn(container, LANE)).toEqual([W2, W1, LEAD])
    expect(cardOrderIn(container, PLAIN)).toEqual(['k-plain-b', 'k-plain-a'])
  })

  it('still offers the folder menu item, which the flag does not gate', async () => {
    // The item is an ingress in a menu opened on purpose and moves nothing on
    // the resting list, so it is released while the layout is held.
    const utils = renderSidebar()
    openFolderMenu(LANE)
    expect(menuItem(LANE)).not.toBeNull()
    await settle(utils)
  })
})

describe('with the preview flag on', () => {
  beforeEach(pinOn)

  it('puts the conductor first in its folder even though it sorts last by time', async () => {
    const { container, findByText } = renderSidebar(timeSlots)
    await settle({ findByText })
    // The control in the same render: the plain folder DOES obey recency, so the
    // lane really is sorting newest-first and the conductor is first in spite of
    // it rather than because the payload happened to arrive that way.
    expect(cardOrderIn(container, PLAIN)).toEqual(['k-plain-b', 'k-plain-a'])
    expect(cardOrderIn(container, LANE)).toEqual([LEAD, W2, W1])
  })

  it('keeps the conductor on the folder body\'s own left edge', async () => {
    // The 2026-10-09 recorded decision fixes ONE column for a folder name and
    // the agent label of every session filed inside it. An earlier revision drew
    // the pinned card OUTSIDE the body, which outdented its text 15px off that
    // column; this pins the three things that put it back on it.
    const { container, findByText } = renderSidebar()
    await settle({ findByText })
    const pinned = pinnedOf(container, LANE)
    expect(pinned).not.toBeNull()
    // 1. It is INSIDE the body div, not a sibling of it, so it inherits the
    //    body's indent and connector line rather than skipping them.
    const body = container.querySelector(`[data-folder-drop="${LANE}"] .${FOLDER_BODY_CLS.split(' ').join('.')}`)
    expect(body).not.toBeNull()
    expect(body!.contains(pinned!)).toBe(true)
    // FIRST in that body. The rail is the body's own first child and draws no
    // row, so the pin is the element right after it.
    expect(body!.firstElementChild?.getAttribute('data-testid')).toBe(`folder-rail-${LANE}`)
    expect(body!.children[1]).toBe(pinned)
    // 2. Its wrapper adds NO box of its own -- no padding, margin or border that
    //    could move the card's text off the column.
    expect(pinned!.className).toBe('relative')
    // 3. Its card carries the same row box as a worker's, which is where the
    //    14px content pad lives (`ROW_BOX_CLS`).
    expect(cardOf(container, LEAD)!.className).toBe(cardOf(container, W1)!.className)
    expect(agentLabelOf(container, LEAD)!.className).toBe(agentLabelOf(container, W1)!.className)
  })

  it('wears the accent bar and the one-word tag, and nothing else does', async () => {
    const { container, findByText } = renderSidebar()
    await settle({ findByText })
    const pinned = pinnedOf(container, LANE)!
    const bar = pinned.querySelector('span[aria-hidden="true"][class*="bg-accent"]')
    expect(bar).not.toBeNull()
    const tags = container.querySelectorAll('[data-testid^="folder-conductor-tag-"]')
    expect(tags).toHaveLength(1)
    expect(tags[0].textContent).toBe('conductor')
    // Beside the agent name, on the card's own first line.
    expect(tags[0].closest('.session-agent-label')).not.toBeNull()
    expect(pinned.contains(tags[0])).toBe(true)
    // The tag spells no type scale of its own: it inherits ROW_META_CLS from
    // the agent-name line, which `listShellParity.test.ts` is the ratchet for.
    expect(tags[0].className).not.toContain('text-[10px]')
  })

  it('leaves the worker cards as full cards', async () => {
    // Not compacted into one-line rows: a worker still draws the agent-name line
    // every card in this lane draws, exactly as a plain folder's card does.
    const { container, findByText } = renderSidebar()
    await settle({ findByText })
    for (const key of [W1, W2, 'k-plain-a']) {
      expect(cardOf(container, key)!.querySelector('.session-agent-label')).not.toBeNull()
    }
  })

  it('pins nothing in a folder with no conductor', async () => {
    const { container, findByText } = renderSidebar()
    await settle({ findByText })
    expect(pinnedOf(container, PLAIN)).toBeNull()
    expect(cardOf(container, 'k-plain-a')!.querySelector('[data-testid^="folder-conductor-tag-"]')).toBeNull()
    const body = container.querySelector(`[data-folder-drop="${PLAIN}"] .${FOLDER_BODY_CLS.split(' ').join('.')}`)
    expect(body?.contains(cardOf(container, 'k-plain-a')!)).toBe(true)
  })
})

/**
 * Nesting, which is why the layout is the folder BODY's shape rather than a
 * one-off strip under the root folder row.
 *
 * `remote-crew` holds its own conductor and two workers, plus a sub-folder that
 * holds a conductor and two workers of its own. Each folder resolves its
 * conductor from its OWN rows, so the rule applies once per folder at whatever
 * depth the folder sits.
 */
const nestedFolders: ChatFolder[] = [
  { id: LANE, name: 'remote-crew', order: 0 },
  { id: SUB, name: 'microvm-lane', order: 0, parent_id: LANE },
  { id: PLAIN, name: 'docs', order: 1 },
]
const nestedSlots: ChatSlot[] = [
  { key: LEAD, title: 'Remote crew rollout', messages: 9, running: false, folder_id: LANE },
  { key: W1, title: 'Fargate task definition', messages: 3, running: false, folder_id: LANE, parent: { slot: LEAD, key: LEAD } },
  { key: W2, title: 'Rotate remote crew token', messages: 2, running: false, folder_id: LANE, parent: { slot: LEAD, key: LEAD } },
  { key: SUB_LEAD, title: 'MicroVM lane', messages: 7, running: false, folder_id: SUB, parent: { slot: LEAD, key: LEAD } },
  { key: SUB_W1, title: 'Boot image pin', messages: 2, running: false, folder_id: SUB, parent: { slot: SUB_LEAD, key: SUB_LEAD } },
  { key: SUB_W2, title: 'Snapshot restore', messages: 1, running: false, folder_id: SUB, parent: { slot: SUB_LEAD, key: SUB_LEAD } },
  { key: 'k-plain-a', title: 'Blog draft', messages: 4, running: false, folder_id: PLAIN },
  { key: 'k-plain-b', title: 'Release notes', messages: 1, running: false, folder_id: PLAIN },
]

describe('a conducted folder inside a conducted folder', () => {
  beforeEach(pinOn)

  it('pins and tags each folder\'s own conductor, at every depth', async () => {
    const { container, findByText } = renderSidebar(nestedSlots, undefined, nestedFolders)
    await settle({ findByText })
    for (const [folderId, key] of [[LANE, LEAD], [SUB, SUB_LEAD]] as const) {
      const pinned = pinnedOf(container, folderId)
      expect(pinned).not.toBeNull()
      expect(pinned!.contains(cardOf(container, key)!)).toBe(true)
      expect(pinned!.querySelector('span[aria-hidden="true"][class*="bg-accent"]')).not.toBeNull()
      expect(pinned!.querySelectorAll('[data-testid^="folder-conductor-tag-"]')).toHaveLength(1)
      // Each folder's own body holds its own pin, first, at that body's edge.
      const body = container.querySelector(`[data-folder-drop="${folderId}"] .${FOLDER_BODY_CLS.split(' ').join('.')}`)
      expect(body!.children[1]).toBe(pinned)
    }
    // Nothing is pinned in the plain folder, at any depth.
    expect(pinnedOf(container, PLAIN)).toBeNull()
    // The sub-folder's conductor is NOT pinned in its parent: `MicroVM lane` is
    // filed in `microvm-lane`, so the parent resolves its conductor from rows
    // that do not include it, and a session appears in one folder's block only.
    expect(cardOrderIn(container, SUB)).toEqual([SUB_LEAD, SUB_W1, SUB_W2])
    expect(cardOrderIn(container, LANE)[0]).toBe(LEAD)
  })
})

/**
 * A filter that conceals the folder's ROOT.
 *
 * Detection answers "which session in this folder opened the others", and that
 * is a fact about the folder, not about what the lane is showing. Read off the
 * FILTERED rows it was not: a query that dropped the root left the row below it
 * with no visible creator, so that row became a root of what was left, and the
 * menu offered it under a label reading "started this folder's other chats" --
 * false, and with the pin on the wrong card was lifted and tagged as well.
 *
 * The fixture is a three-level chain for exactly that reason. Hide a root above
 * two rows and the two are left rootless, which resolves to nothing; hide a root
 * above a MIDDLE row and the middle row inherits a subtree, which is the shape
 * that produced the false claim.
 *
 * So the folder's whole membership is detected over, and the answer is then
 * required to be a row the folder DRAWS. A filter that hides the root therefore
 * hides the feature -- no item, no pin -- rather than moving it to another
 * session.
 */
const MID = 'k-mid-supervisor'
/** Every title carrying `fargate` EXCEPT the root's; keys carry it nowhere. With
 *  no backend ranking the query matches title + key + agent, so both are checked. */
const chainSlots: ChatSlot[] = [
  { key: LEAD, title: 'Remote crew rollout', messages: 9, running: false, folder_id: LANE },
  { key: MID, title: 'Fargate lane supervisor', messages: 7, running: false, folder_id: LANE, parent: { slot: LEAD, key: LEAD } },
  { key: W1, title: 'Fargate task definition', messages: 3, running: false, folder_id: LANE, parent: { slot: MID, key: MID } },
  { key: W2, title: 'Fargate token rotation', messages: 2, running: false, folder_id: LANE, parent: { slot: MID, key: MID } },
  { key: 'k-plain-a', title: 'Blog draft', messages: 4, running: false, folder_id: PLAIN },
  { key: 'k-plain-b', title: 'Release notes', messages: 1, running: false, folder_id: PLAIN },
]

describe('a filter that hides the folder\'s root', () => {
  it('opens the root with no filter, and offers NOTHING once the query hides it', async () => {
    // One case, both states, so the pin cannot pass by the fixture simply
    // having no conductor. What it OPENS is asserted rather than what it reads,
    // because the label names the folder now and so says nothing about which
    // row was detected: with no query the item opens `k-lead`, the real root,
    // and not `k-mid-supervisor`, the row the filtered lane would leave on top.
    const onSelectSlot = vi.fn()
    const utils = renderSidebar(chainSlots, onSelectSlot)
    openFolderMenu(LANE)
    expect(menuItem(LANE)!.textContent).toContain('remote-crew')
    fireEvent.click(menuItem(LANE)!)
    expect(onSelectSlot).toHaveBeenCalledWith(LEAD)
    typeSearch(utils.container, 'fargate')
    openFolderMenu(LANE)
    // The three matching rows are on screen, the root is not, and no item is
    // offered on any of them.
    expect(cardOf(utils.container, MID)).not.toBeNull()
    expect(cardOf(utils.container, LEAD)).toBeNull()
    expect(menuItem(LANE)).toBeNull()
    await utils.findByText('Fargate task definition')
  })

  it('pins and tags nothing once the query hides it, with the preview flag on', async () => {
    pinOn()
    const { container, findByText } = renderSidebar(chainSlots)
    await findByText('Remote crew rollout')
    // The control in the same render: unfiltered, the real root IS pinned.
    expect(pinnedOf(container, LANE)!.contains(cardOf(container, LEAD)!)).toBe(true)
    typeSearch(container, 'fargate')
    expect(pinnedOf(container, LANE)).toBeNull()
    expect(container.querySelectorAll('[data-testid^="folder-conductor-tag-"]')).toHaveLength(0)
    // And the row the filtered lane left on top is an ordinary card, sorted
    // among its own workers rather than lifted above them.
    expect(cardOf(container, MID)!.querySelector('[data-testid^="folder-conductor-tag-"]')).toBeNull()
    await findByText('Fargate task definition')
  })
})

describe('with the backend offline', () => {
  it('switches no session when the item is chosen', async () => {
    // The same gate every session-row click has. Selecting a slot nothing can
    // load closes the menu onto an empty pane, with the row the click came from
    // no longer on screen to go back to.
    const onSelectSlot = vi.fn()
    const utils = renderSidebar(slots, onSelectSlot, folders, { connected: false })
    openFolderMenu(LANE)
    fireEvent.click(menuItem(LANE)!)
    expect(onSelectSlot).not.toHaveBeenCalled()
    await settle(utils)
  })
})
