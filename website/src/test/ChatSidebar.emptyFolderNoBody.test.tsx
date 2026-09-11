/**
 * A folder with nothing in it renders NO body, and its row carries its controls.
 *
 * It used to render a body whose only content was a "New chat in <name>"
 * placeholder, so every empty folder cost a second row: a tree of fourteen area
 * folders spent most of the sidebar's height on rows holding no session, and the
 * one session that existed sat below the fold. The placeholder existed because
 * the header's create control was invisible until hover. So the row shows that
 * control at rest and the placeholder is gone - one row per empty folder, with
 * its create action still reachable without hovering.
 *
 * The four load-bearing properties:
 *   (1) an empty folder renders no body at all - not a collapsed one, not an
 *       empty strip, and no second "New chat in <name>" row;
 *   (2) its row keeps the create + menu cluster visible, in flow, with its count
 *       still readable, so the row is neither inert nor mute about being empty;
 *   (3) a folder that holds a session is untouched: body renders, cluster stays
 *       hover-only, count unchanged;
 *   (4) the same two rules hold for a board column's copy of a folder.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatFolder, ChatSlot, ChatTag, TagColumn } from '../types'
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

const chatConfig = vi.hoisted(() => ({ tagColumnsEnabled: false, hideEmptyFolderBody: true }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ ...chatConfig, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({ updateChatFolder: vi.fn(), chatFolders: vi.fn() }))

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

import ChatSidebar from '../pages/ChatSidebar'

const EMPTY_FOLDER = 'folder-empty'
const FULL_FOLDER = 'folder-full'
const SLOT = 'chat-1'
const TAG = 'cccccccc-cccc-cccc-cccc-cccccccccccc'
const COL = 'col-working'

const folders: ChatFolder[] = [
  { id: EMPTY_FOLDER, name: 'Empty', order: 0 },
  { id: FULL_FOLDER, name: 'Full', order: 1 },
]
const slots: ChatSlot[] = [{ key: SLOT, title: 'Worker', messages: 3, running: false, folder_id: FULL_FOLDER, tags: [TAG] }]
const tags: ChatTag[] = [{ id: TAG, name: 'Working', color: '#1a1', order: 0, status: true }]
const columns: TagColumn[] = [{ id: COL, name: 'Working', tag_ids: [TAG], mode: 'any', order: 0 }]

function renderSidebar(folderData: ChatFolder[] = folders, slotData: ChatSlot[] = slots) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots: slotData, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-tags'], chatConfig.tagColumnsEnabled ? tags : [])
  qc.setQueryData(['tag-columns'], chatConfig.tagColumnsEnabled ? columns : [])
  qc.setQueryData(['chat-folders'], folderData)
  mocks.chatFolders.mockImplementation(() => Promise.resolve(folderData))
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slotData} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

/** The folder block, whose only direct `[aria-hidden]` child is FolderBody. */
function blockOf(container: HTMLElement, folderId: string): HTMLElement {
  const block = container.querySelector(`[data-folder-drop="${folderId}"]`)
  if (!block) throw new Error(`no folder block for ${folderId}`)
  return block as HTMLElement
}
function bodyOf(container: HTMLElement, folderId: string): Element | null {
  return blockOf(container, folderId).querySelector(':scope > [aria-hidden]')
}
/** The row's action cluster, located from the create button it contains. */
function clusterOf(container: HTMLElement, testId: string): HTMLElement {
  const button = container.querySelector(`[data-testid="${testId}"]`)
  if (!button?.parentElement) throw new Error(`no cluster for ${testId}`)
  return button.parentElement
}

beforeEach(() => {
  localStorage.clear()
  chatConfig.tagColumnsEnabled = false
  chatConfig.hideEmptyFolderBody = true
  mocks.chatFolders.mockImplementation(() => Promise.resolve(folders))
  mocks.updateChatFolder.mockResolvedValue({})
})
afterEach(() => vi.clearAllMocks())

describe('sidebar: a folder with nothing in it has no body', () => {
  it('is OFF by default: the folder keeps its labelled create row', () => {
    // The default must be byte-identical to the behaviour before this change, so
    // nobody's sidebar reshapes itself without them choosing it in Settings.
    chatConfig.hideEmptyFolderBody = false
    const { container } = renderSidebar()
    const body = bodyOf(container, EMPTY_FOLDER)
    expect(body).not.toBeNull()
    expect(body!.textContent).toContain('New chat in Empty')
    // And the row is a real toggle again, with the ordinary hover-only cluster.
    const shell = container.querySelector(`[data-testid="folder-collapse-${EMPTY_FOLDER}"]`)?.parentElement as HTMLElement
    expect(shell.tagName).toBe('BUTTON')
    expect(shell.getAttribute('aria-expanded')).toBe('true')
    expect(clusterOf(container, `folder-new-chat-${EMPTY_FOLDER}`).className).toContain('opacity-0')
  })

  it('renders no body and no placeholder row for an empty folder', () => {
    const { container } = renderSidebar()
    expect(bodyOf(container, EMPTY_FOLDER)).toBeNull()
    // The placeholder was the whole second row. It is gone, not hidden: a
    // collapsed body would still be in the DOM, aria-hidden.
    expect(container.querySelectorAll('[aria-label="New chat in Empty"]').length).toBe(1)
    // A folder that holds a session still renders its body.
    expect(bodyOf(container, FULL_FOLDER)).not.toBeNull()
  })

  it('keeps the empty row usable: controls in flow, count still readable', () => {
    const { container } = renderSidebar()
    const cluster = clusterOf(container, `folder-new-chat-${EMPTY_FOLDER}`)
    // Visible at rest, and in flow rather than floating over the count slot -
    // hover-only was what made the row read as an inert dead end once its body
    // was gone, and absolute positioning is what used to cover the count.
    expect(cluster.className).not.toContain('opacity-0')
    expect(cluster.className).not.toContain('absolute')
    expect(blockOf(container, EMPTY_FOLDER).textContent).toContain('0')

    // A populated folder keeps the hover-only overlay it always had.
    const full = clusterOf(container, `folder-new-chat-${FULL_FOLDER}`)
    expect(full.className).toContain('opacity-0')
    expect(full.className).toContain('absolute')
  })

  it('does not present a row with no body as a control', async () => {
    // With no body there is nothing to toggle, so the row is not a button: no
    // button element, no tab stop, no expanded state to announce, and clicking it
    // writes nothing. A focusable control that looks clickable and does nothing
    // is the failure this pins - it reads as broken rather than as empty.
    const { container } = renderSidebar()
    const shellOf = (id: string) =>
      container.querySelector(`[data-testid="folder-collapse-${id}"]`)?.parentElement as HTMLElement

    expect(shellOf(EMPTY_FOLDER).tagName).toBe('SPAN')
    expect(shellOf(EMPTY_FOLDER).hasAttribute('aria-expanded')).toBe(false)
    expect(shellOf(EMPTY_FOLDER).className).not.toContain('cursor-pointer')
    // It DOES keep a tab stop, because the row is still a drag handle - see the
    // keyboard case below. What it loses is the disclosure, not the reachability.
    fireEvent.click(shellOf(EMPTY_FOLDER))
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()

    // A folder with a body is a real button and still persists its collapse.
    expect(shellOf(FULL_FOLDER).tagName).toBe('BUTTON')
    expect(shellOf(FULL_FOLDER).getAttribute('aria-expanded')).toBe('true')
    expect(shellOf(FULL_FOLDER).getAttribute('aria-label')).toBe('Collapse folder Full')
    fireEvent.click(shellOf(FULL_FOLDER))
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledTimes(1))
  })

  it('dims an inert row\'s glyph and stops it brightening on hover', () => {
    // The glyph says "inactive" by weight, not by shape. Shape is the wrong
    // channel: the closed shape is this product's "click to expand" affordance,
    // so an inert row wearing it invites the very dead click it should prevent.
    // So an inert row keeps the same shape and goes dimmer, and - unlike every
    // pressable sibling - does not brighten when the row is hovered.
    const { container } = renderSidebar()
    const glyph = (id: string) =>
      container.querySelector(`[data-testid="folder-collapse-${id}"]`) as HTMLElement

    expect(glyph(EMPTY_FOLDER).className).toContain('text-muted/40')
    expect(glyph(EMPTY_FOLDER).className).not.toContain('group-hover:')
    // A pressable row keeps the resting tone and the hover step.
    expect(glyph(FULL_FOLDER).className).toContain('text-muted/70')
    expect(glyph(FULL_FOLDER).className).toContain('group-hover:text-muted')
  })

  it('treats a parent whose only child is hidden as holding nothing', () => {
    // The count and the body have to be built from the SAME predicate. A
    // hidden-when-empty child is skipped by the render loop, so no body renders -
    // and a count that still included it made the row present as a toggle with
    // nothing to toggle, which is the dead click by another door. A folder the
    // user hid is a folder they asked not to see, so it is not part of the count.
    const hiddenChild: ChatFolder[] = [
      { id: EMPTY_FOLDER, name: 'Empty', order: 0 },
      { id: 'folder-hidden', name: 'Hidden', parent_id: EMPTY_FOLDER, order: 0, hidden: true },
    ]
    const { container } = renderSidebar(hiddenChild, [])
    // No body, because the only child is not rendered.
    expect(bodyOf(container, EMPTY_FOLDER)).toBeNull()
    // And the row agrees: inert, showing 0 rather than 1.
    const shell = container.querySelector(`[data-testid="folder-collapse-${EMPTY_FOLDER}"]`)?.parentElement as HTMLElement
    expect(shell.tagName).toBe('SPAN')
    expect(shell.textContent).toContain('0')
    expect(shell.textContent).not.toContain('1')
  })

  it('keeps an empty list row reachable by keyboard while it is draggable', () => {
    // The sortable listeners sit on the row div, which has no tab stop of its own,
    // so keyboard activation only reaches them by bubbling from the shell inside.
    // Swapping the <button> for a <span> without a tab stop took keyboard
    // reordering away from empty folders - the row is a drag handle even when it
    // has nothing to disclose.
    const { container } = renderSidebar()
    const shell = container.querySelector(`[data-testid="folder-collapse-${EMPTY_FOLDER}"]`)?.parentElement as HTMLElement
    expect(shell.tagName).toBe('SPAN')
    expect(shell.getAttribute('tabindex')).toBe('0')
    expect(shell.getAttribute('role')).toBe('button')
    expect(shell.getAttribute('aria-roledescription')).toBe('Drag to reorder')
    // Still not a disclosure: no expanded state and no collapse label.
    expect(shell.hasAttribute('aria-expanded')).toBe(false)
  })

  it('draws an inert row open even when it carries a stored collapsed flag', () => {
    // A folder collapsed BEFORE it emptied still has `collapsed: true`. Binding the
    // glyph to that alone drew the CLOSED shape on an inert row, which is this
    // product's "click to expand" affordance on a row that cannot expand - the
    // exact invitation the dimmed-open glyph exists to avoid.
    const collapsedEmpty: ChatFolder[] = [{ id: EMPTY_FOLDER, name: 'Empty', order: 0, collapsed: true }]
    const { container } = renderSidebar(collapsedEmpty, [])
    const glyph = container.querySelector(`[data-testid="folder-collapse-${EMPTY_FOLDER}"]`) as HTMLElement
    expect(glyph.className).toContain('text-muted/40')
    // Same shape an expanded folder's glyph uses, so the row invites no click.
    const openFolder: ChatFolder[] = [{ id: EMPTY_FOLDER, name: 'Empty', order: 0 }]
    const second = renderSidebar(openFolder, [])
    const openGlyph = second.container.querySelector(`[data-testid="folder-collapse-${EMPTY_FOLDER}"]`) as HTMLElement
    expect(glyph.innerHTML).toBe(openGlyph.innerHTML)
  })

  it('keeps a stored collapsed flag through an empty spell', async () => {
    // The premise the first-principles lane asked a human to confirm, pinned
    // instead: a folder marked collapsed BEFORE it emptied still has that flag
    // when content returns. Nothing in this PR writes the flag while a folder is
    // empty, so the body that comes back must come back closed.
    const collapsedEmpty: ChatFolder[] = [{ id: EMPTY_FOLDER, name: 'Empty', order: 0, collapsed: true }]
    const { container } = renderSidebar(collapsedEmpty, [])
    // Empty: no body at all, and nothing has been written.
    expect(bodyOf(container, EMPTY_FOLDER)).toBeNull()
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()

    // Content arrives for the same folder, still flagged collapsed.
    const withSlot: ChatSlot[] = [{ key: SLOT, title: 'Worker', messages: 3, running: false, folder_id: EMPTY_FOLDER, tags: [TAG] }]
    const second = renderSidebar(collapsedEmpty, withSlot)
    const body = bodyOf(second.container, EMPTY_FOLDER)
    expect(body).not.toBeNull()
    // Honors the pre-existing flag: the body is back, and closed.
    expect(body!.getAttribute('aria-hidden')).toBe('true')
  })

  it('is OFF by default in a board column too, placeholder and all', () => {
    // The default has to be identical in BOTH views. This case exists because it
    // was not: the placeholder was deleted from the board copy and restored only
    // in the tree, so a default board user lost the labelled create row and got
    // an empty bordered strip instead - the body rendered with nothing in it.
    chatConfig.tagColumnsEnabled = true
    chatConfig.hideEmptyFolderBody = false
    const { container } = renderSidebar()
    const emptyRow = container.querySelector(`[data-testid="col-${COL}-folder-${EMPTY_FOLDER}"]`) as HTMLElement
    const body = emptyRow.querySelector(':scope > [aria-hidden]')
    expect(body).not.toBeNull()
    expect(body!.textContent).toContain('New chat in Empty')
    // And the row is still a real toggle with its hover-only cluster.
    expect(emptyRow.firstElementChild!.getAttribute('aria-expanded')).toBe('true')
    expect(clusterOf(container, `col-${COL}-folder-${EMPTY_FOLDER}-new-chat`).className).toContain('opacity-0')
  })

  it('applies both rules to a board column copy of the folder', () => {
    chatConfig.tagColumnsEnabled = true
    const { container } = renderSidebar()
    const emptyRow = container.querySelector(`[data-testid="col-${COL}-folder-${EMPTY_FOLDER}"]`) as HTMLElement
    expect(emptyRow).toBeTruthy()
    expect(emptyRow.querySelector(':scope > [aria-hidden]')).toBeNull()
    expect(clusterOf(container, `col-${COL}-folder-${EMPTY_FOLDER}-new-chat`).className).not.toContain('opacity-0')

    const fullRow = container.querySelector(`[data-testid="col-${COL}-folder-${FULL_FOLDER}"]`) as HTMLElement
    expect(fullRow.querySelector(':scope > [aria-hidden]')).toBeTruthy()
    expect(clusterOf(container, `col-${COL}-folder-${FULL_FOLDER}-new-chat`).className).toContain('opacity-0')

    // A board row is a drag handle as well as a toggle, and `useSortable` here
    // passes down only `listeners` - never its `attributes` - so this tab stop is
    // the ONLY thing that lets the keyboard sensor reach the row. An empty folder
    // keeps it (reorderable by keyboard, with nothing to disclose) while a
    // populated one also keeps its expanded state.
    const emptyHeader = emptyRow.firstElementChild as HTMLElement
    expect(emptyHeader.getAttribute('tabindex')).toBe('0')
    expect(emptyHeader.hasAttribute('aria-expanded')).toBe(false)
    expect(emptyHeader.getAttribute('aria-roledescription')).toBe('Drag to reorder')
    const fullHeader = fullRow.firstElementChild as HTMLElement
    expect(fullHeader.getAttribute('tabindex')).toBe('0')
    expect(fullHeader.getAttribute('aria-expanded')).toBe('true')
  })
})
