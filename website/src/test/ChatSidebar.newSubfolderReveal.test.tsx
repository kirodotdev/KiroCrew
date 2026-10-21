/**
 * Creating a subfolder inside a COLLAPSED folder has to leave the new folder on
 * screen. The parent's "New subfolder" action used to file the child correctly and
 * then leave the parent shut, so the folder the person had just named was hidden
 * and the create looked like it had done nothing.
 *
 * The suite drives the real create modal through the parent's ⋯ menu and its
 * right-click twin, against the real sidebar. The api mock is the only stand-in:
 * `createChatFolder` answers with the server's record and `chatFolders` serves the
 * list the refetch would return.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor, screen } from '@testing-library/react'
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
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
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
  createChatFolder: vi.fn(),
  updateChatFolder: vi.fn(),
  chatFolders: vi.fn(),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop in mocks) return mocks[prop as keyof typeof mocks]
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

/** `Clients` is collapsed and already holds one subfolder, `Acme`, so the new
 *  child has a sibling to sort after. `Notes` is open, for the expanded case. */
const FOLDERS: ChatFolder[] = [
  { id: 'f-clients', name: 'Clients', collapsed: true, order: 0 },
  { id: 'f-acme', name: 'Acme', collapsed: false, order: 0, parent_id: 'f-clients' },
  { id: 'f-notes', name: 'Notes', collapsed: false, order: 1 },
]

const SLOTS = [
  { key: 'k-acme', title: 'acme kickoff', running: false, messages: 2, folder_id: 'f-acme' },
] as unknown as ChatSlot[]

/** The server's record for the created folder, as `POST /api/chat/folders` answers. */
const created = (parentId: string, name = 'Globex'): ChatFolder =>
  ({ id: 'f-new', name, collapsed: false, order: 1, parent_id: parentId, created: 1 }) as ChatFolder

let server: ChatFolder[]
/** The query client of the latest render, for the one assertion that reads the cache. */
let lastClient: QueryClient

function renderSidebar(folders: ChatFolder[] | null = FOLDERS) {
  server = (folders ?? []).map(f => ({ ...f }))
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: {}, connected: true, slots: SLOTS, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { ...defaults.chat, activeSlot: null, slotStatusDetail: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: {
    queries: { retry: false, staleTime: Infinity, refetchOnMount: false }, mutations: { retry: false },
  } })
  lastClient = qc
  // `null` leaves the folder list unloaded, the state a failed first load leaves.
  if (folders) qc.setQueryData(['chat-folders'], folders)
  qc.setQueryData(['tag-columns'], [])
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return store
}

/** jsdom has no scrollIntoView; record which element each call scrolled. */
function stubScrollIntoView() {
  const proto = HTMLElement.prototype as HTMLElement & { scrollIntoView?: (o?: unknown) => void }
  const had = proto.scrollIntoView
  const calls: { el: HTMLElement; opts: unknown }[] = []
  proto.scrollIntoView = function (this: HTMLElement, opts?: unknown) { calls.push({ el: this, opts }) }
  return {
    calls,
    restore: () => { if (had) proto.scrollIntoView = had; else delete proto.scrollIntoView },
  }
}

/** Open the parent's "New subfolder" modal, through the ⋯ menu or the right-click menu. */
async function openNewSubfolder(folderId: string, name: string, via: 'menu' | 'context') {
  const row = document.querySelector<HTMLElement>(`[data-folder-row="${folderId}"]`)!
  // Synchronous on purpose: happy-dom keeps a Radix menu up for the current tick
  // only, so the item has to be driven before anything awaits.
  if (via === 'menu') {
    fireEvent.keyDown(screen.getByTestId(`folder-menu-${folderId}`), { key: 'Enter' })
    fireEvent.click(screen.getByTestId(`folder-new-subfolder-${folderId}`))
  } else {
    fireEvent.contextMenu(row, { clientX: 40, clientY: 20 })
    fireEvent.click(screen.getByTestId(`folder-new-subfolder-${folderId}-ctx`))
  }
  // The modal seeds its draft in an effect after it opens, so type once that has
  // settled and the field holds what was typed.
  await screen.findByTestId('folder-config-name')
  await waitFor(() => {
    const field = screen.getByTestId('folder-config-name') as HTMLInputElement
    if (field.value !== name) fireEvent.change(field, { target: { value: name } })
    expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false)
  })
}

const newRow = () => document.querySelector<HTMLElement>('[data-folder-row="f-new"]')
/** Visible means rendered and not inside a collapsed folder body, which stays
 *  mounted but `inert` while shut. */
const isShown = (el: HTMLElement | null) => !!el && !el.closest('[inert]')
/** Matched by folder id, not element identity: the refetch that replaces the
 *  seeded record can remount the row after it was scrolled. */
const scrolledNew = (c: { el: HTMLElement }) => c.el.dataset.folderRow === 'f-new'

let scroll: ReturnType<typeof stubScrollIntoView>

beforeEach(() => {
  localStorage.clear()
  scroll = stubScrollIntoView()
  mocks.updateChatFolder.mockImplementation(async (id: string, body: Partial<ChatFolder>) => {
    server = server.map(f => (f.id === id ? { ...f, ...body } : f))
    return {}
  })
  mocks.chatFolders.mockImplementation(async () => server.map(f => ({ ...f })))
  mocks.createChatFolder.mockImplementation(async (name: string, parentId?: string) => {
    const row = created(parentId ?? '', name)
    server = [...server, row]
    return row
  })
})
afterEach(() => { scroll.restore(); vi.clearAllMocks() })

describe('New subfolder inside a collapsed folder', () => {
  it.each(['menu', 'context'] as const)('expands the parent and shows the new folder (via %s)', async via => {
    renderSidebar()
    expect(isShown(document.querySelector('[data-folder-row="f-acme"]'))).toBe(false)
    await openNewSubfolder('f-clients', 'Globex', via)
    fireEvent.click(screen.getByTestId('folder-config-submit'))

    await waitFor(() => expect(mocks.createChatFolder).toHaveBeenCalledWith('Globex', 'f-clients', expect.anything()))
    await waitFor(() => expect(screen.queryByTestId('folder-config-name')).toBeNull())
    // The parent opened through the same persisted toggle a click on its chevron uses.
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith('f-clients', { collapsed: false }))
    await waitFor(() => expect(isShown(newRow())).toBe(true))
    // Filed once, under the parent, after its existing child.
    expect(document.querySelectorAll('[data-folder-row="f-new"]')).toHaveLength(1)
    const order = [...document.querySelectorAll<HTMLElement>('[data-folder-row]')].map(e => e.dataset.folderRow)
    expect(order.indexOf('f-acme')).toBeLessThan(order.indexOf('f-new'))
    // Scrolled into view, then flashed as confirmation.
    await waitFor(() => expect(scroll.calls.some(c => scrolledNew(c))).toBe(true))
    await waitFor(() => expect(newRow()!.className).toContain('session-reveal-flash'))
  })

  it('opens a collapsed nested parent and leaves its open ancestor as it was', async () => {
    const nested: ChatFolder[] = [
      { id: 'f-top', name: 'Top', collapsed: false, order: 0 },
      { id: 'f-mid', name: 'Mid', collapsed: true, order: 0, parent_id: 'f-top' },
    ]
    renderSidebar(nested)
    expect(isShown(document.querySelector('[data-folder-row="f-mid"]'))).toBe(true)
    await openNewSubfolder('f-mid', 'Deep', 'context')
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    await waitFor(() => expect(mocks.createChatFolder).toHaveBeenCalledWith('Deep', 'f-mid', expect.anything()))
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith('f-mid', { collapsed: false }))
    await waitFor(() => expect(isShown(newRow())).toBe(true))
    expect(mocks.updateChatFolder).not.toHaveBeenCalledWith('f-top', expect.anything())
  })

  it('leaves an already-open parent alone and still points at the new folder', async () => {
    renderSidebar()
    await openNewSubfolder('f-notes', 'Drafts', 'menu')
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    await waitFor(() => expect(isShown(newRow())).toBe(true))
    await waitFor(() => expect(scroll.calls.some(c => scrolledNew(c))).toBe(true))
    expect(mocks.updateChatFolder).not.toHaveBeenCalledWith('f-notes', expect.anything())
  })

  it('does nothing to the parent when the create is cancelled', async () => {
    renderSidebar()
    await openNewSubfolder('f-clients', 'Globex', 'menu')
    // A typed name guards Escape and the backdrop, so cancel through the button.
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByTestId('folder-config-name')).toBeNull())
    expect(mocks.createChatFolder).not.toHaveBeenCalled()
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
    expect(scroll.calls).toHaveLength(0)
  })

  it('does not turn a failed folder load into a one-folder list when a folder is created', async () => {
    // The first load fails and the refetch the create triggers stays in flight, so
    // whatever the create itself writes into the cache is all the sidebar has.
    mocks.chatFolders.mockRejectedValueOnce(new Error('folders offline'))
    mocks.chatFolders.mockImplementation(() => new Promise(() => {}))
    renderSidebar(null)
    await screen.findByTestId('chat-folders-error')
    fireEvent.keyDown(screen.getByLabelText('More create options'), { key: 'Enter' })
    fireEvent.click(screen.getByTestId('new-folder'))
    await screen.findByTestId('folder-config-name')
    await waitFor(() => {
      const field = screen.getByTestId('folder-config-name') as HTMLInputElement
      if (field.value !== 'Loose') fireEvent.change(field, { target: { value: 'Loose' } })
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false)
    })
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    await waitFor(() => expect(screen.queryByTestId('folder-config-name')).toBeNull())
    await waitFor(() => expect(mocks.chatFolders.mock.calls.length).toBeGreaterThan(1))
    // The folder list is still unknown. A one-folder list in the cache would read
    // as a successful load: `foldersFailed` clears and the tree shows that folder
    // alone.
    expect(lastClient.getQueryData(['chat-folders'])).toBeUndefined()
  })

  it('keeps the parent shut and the modal open when the create fails', async () => {
    mocks.createChatFolder.mockRejectedValue(new Error('folder limit reached'))
    renderSidebar()
    await openNewSubfolder('f-clients', 'Globex', 'menu')
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    await waitFor(() => expect(mocks.createChatFolder).toHaveBeenCalled())
    // The modal and the sidebar's folder error line both name the reason.
    expect((await screen.findAllByText(/folder limit reached/)).length).toBeGreaterThan(0)
    expect(screen.queryByTestId('folder-config-name')).not.toBeNull()
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
    expect(newRow()).toBeNull()
    expect(scroll.calls).toHaveLength(0)
  })
})
