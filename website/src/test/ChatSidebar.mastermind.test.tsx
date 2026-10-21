/**
 * The Mastermind session in the Sessions sidebar.
 *
 * It is the ORDINARY session row (the same SessionRow every session renders as),
 * and while the backend reports Mastermind enabled (GET /api/mastermind) the row
 * model holds it first and top-level in every layout -- tree, flat, conductor and
 * board (above the column strip) -- decorates it (Brain glyph, "N messages
 * remembered", a faint swept ring) and locks it: no close, rename, pin or
 * folder move. Disabled, it is a plain row with none of that.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, render, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// Render framer-motion elements as plain DOM (jsdom can't run projection). One
// component per tag, so a re-render does not remount every motion child.
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
        if (k === 'children' || FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const made = new Map<string, ReturnType<typeof make>>()
  const motionFor = (tag: string) => {
    if (!made.has(tag)) made.set(tag, make(tag))
    return made.get(tag)!
  }
  return {
    motion: new Proxy({}, { get: (_t, tag: string) => motionFor(tag) }),
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})
vi.mock('../components/ProjectPicker', () => ({ default: () => null }))

const mocks = vi.hoisted(() => ({
  board: false,
  info: { enabled: true, slot_exists: true, messages: 42, pending_summaries: 0, view_bytes: 0, view_lines: 0 },
  reduceMotion: false,
  deleteChatSlot: vi.fn(),
}))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: mocks.board, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const COLUMNS = [{ id: 'col-all', name: 'Everything', tag_ids: [] as string[], mode: 'any' as const, order: 0 }]
const FOLDERS = [{ id: 'f1', name: 'Alpha', order: 0 }]

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'mastermind') return vi.fn().mockImplementation(() => Promise.resolve(mocks.info))
      if (prop === 'deleteChatSlot') return mocks.deleteChatSlot
      if (prop === 'chatFolders') return vi.fn().mockResolvedValue(FOLDERS)
      if (prop === 'tagColumns') return vi.fn().mockImplementation(() => Promise.resolve(mocks.board ? COLUMNS : []))
      return vi.fn().mockResolvedValue([])
    },
  }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: q.includes('prefers-reduced-motion') ? mocks.reduceMotion : false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'
import { deleteSlot } from '../store/chatSlice'
import { setMastermindEnabledState } from '../store/dashboardSlice'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

// The Mastermind slot is the OLDEST row and filed in a folder on purpose: neither
// recency nor its folder may move it off the top.
const MASTERMIND = { key: 'mastermind', title: 'Mastermind', running: false, messages: 12, pinned: true, folder_id: 'f1', modified: 1000 }
const PINNED = { key: 'p1', title: 'Pinned one', running: false, messages: 3, pinned: true, modified: 5000 }
const FILED = { key: 'k-filed', title: 'Filed', running: false, messages: 1, folder_id: 'f1', modified: 4000 }
const PLAIN = { key: 'k1', title: 'Plain', running: false, messages: 1, modified: 3000 }
// Gives the conductor lane a lineage to draw (it is only offered with one).
const WORKER = { key: 'k-worker', title: 'Worker', running: false, messages: 1, modified: 2000, parent: { slot: 'k1', key: 'k1' } }
const SLOTS = [PINNED, FILED, PLAIN, WORKER, MASTERMIND] as unknown as ChatSlot[]

function renderSidebar(slots: ChatSlot[] = SLOTS) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], FOLDERS)
  qc.setQueryData(['tag-columns'], mocks.board ? COLUMNS : [])
  qc.setQueryData(['chat-tags'], [])
  qc.setQueryData(['mastermind'], mocks.info)
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

/** ThemeProvider settles its own state after mount; wait for a row first. */
const settle = () => screen.findByText('Plain')
const rowKeys = (c: HTMLElement) => Array.from(c.querySelectorAll('[data-session-row]')).map(el => el.getAttribute('data-session-row'))
const mastermindRow = (c: HTMLElement) => c.querySelector('[data-session-row="mastermind"]') as HTMLElement | null

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
  mocks.board = false
  mocks.reduceMotion = false
  mocks.info = { enabled: true, slot_exists: true, messages: 42, pending_summaries: 0, view_bytes: 0, view_lines: 0 }
  mocks.deleteChatSlot.mockRejectedValue(Object.assign(new Error('conflict'), {
    status: 409, body: JSON.stringify({ code: 'mastermind_protected' }),
  }))
})
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — the Mastermind row', () => {
  it('disabled: an ordinary row, undecorated, in its folder like any other', async () => {
    mocks.info = { ...mocks.info, enabled: false }
    const { container } = renderSidebar()
    await settle()
    const row = mastermindRow(container)
    expect(row).not.toBeNull()
    expect(row!.hasAttribute('data-mastermind')).toBe(false)
    expect(container.querySelector('[data-testid="mastermind-row-glow"]')).toBeNull()
    expect(container.querySelector('[data-testid="mastermind-row-meta"]')).toBeNull()
    // Still filed in its folder: drawn after that folder's header, not above it.
    expect(screen.getByText('Alpha').compareDocumentPosition(row!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // And its ordinary close control is there.
    expect(within(row!).getByRole('button', { name: 'Close session' })).toBeTruthy()
  })

  it('enabled, tree: the first row, above every folder and pinned row, decorated', async () => {
    const { container } = renderSidebar()
    await settle()
    expect(rowKeys(container)[0]).toBe('mastermind')
    const row = mastermindRow(container)!
    // Above the folder its slot is filed in, and drawn only once.
    const folderHeader = screen.getByText('Alpha')
    expect(row.compareDocumentPosition(folderHeader) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(container.querySelectorAll('[data-session-row="mastermind"]')).toHaveLength(1)
    expect(row.hasAttribute('data-mastermind')).toBe(true)
    expect(within(row).getByTestId('mastermind-row-icon')).toBeTruthy()
    expect(within(row).getByTestId('mastermind-row-glow')).toBeTruthy()
    expect(within(row).getByTestId('mastermind-row-meta')).toHaveTextContent('42 messages remembered')
    // It is still the ordinary row: the same title, status hooks and keyboard stop.
    expect(row).toHaveAttribute('role', 'button')
    expect(row).toHaveAttribute('tabindex', '0')
    expect(within(row).getByText('Mastermind')).toBeTruthy()
  })

  it('enabled, flat: the first row', async () => {
    localStorage.setItem('mc-sidebar-lane', 'flat')
    const { container, getByTestId } = renderSidebar()
    await settle()
    expect(getByTestId('flat-view-lane')).toBeTruthy()
    expect(rowKeys(container)[0]).toBe('mastermind')
    expect(mastermindRow(container)!.hasAttribute('data-mastermind')).toBe(true)
  })

  it('enabled, conductor: the first row', async () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { container, getByTestId } = renderSidebar()
    await settle()
    expect(getByTestId('conductor-view-lane')).toBeTruthy()
    expect(rowKeys(container)[0]).toBe('mastermind')
  })

  it('enabled, board: one full-width row above the column strip, not inside a column', async () => {
    mocks.board = true
    const { container, getByTestId } = renderSidebar()
    await settle()
    const strip = getByTestId('column-strip')
    const head = getByTestId('board-head-row')
    expect(within(head).getByText('Mastermind')).toBeTruthy()
    expect(head.compareDocumentPosition(strip) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(strip.querySelector('[data-session-row="mastermind"]')).toBeNull()
    expect(rowKeys(container)[0]).toBe('mastermind')
  })

  it('the shortcut order the store publishes starts with it', async () => {
    const { store } = renderSidebar()
    await settle()
    await waitFor(() => expect(store.getState().dashboard.sidebarOrder?.[0]).toBe('mastermind'))
  })

  it('locked: no close, rename, pin or folder move -- but the ordinary row keeps them', async () => {
    const { container } = renderSidebar()
    await settle()
    const row = mastermindRow(container)!
    expect(within(row).queryByRole('button', { name: 'Close session' })).toBeNull()
    expect(row.getAttribute('draggable')).not.toBe('true')
    const plainRow = container.querySelector('[data-session-row="k1"]') as HTMLElement
    expect(within(plainRow).getByRole('button', { name: 'Close session' })).toBeTruthy()

    fireEvent.contextMenu(row)
    const items = (await screen.findAllByRole('menuitem')).map(i => i.textContent ?? '')
    expect(items.some(t => /Mark as (un)?read/.test(t))).toBe(true)
    for (const banned of [/^\s*Rename/, /Close session/, /^\s*(Un)?pin\b/, /Move to folder/]) {
      expect(items.some(t => banned.test(t)), `${banned} offered`).toBe(false)
    }
  })

  it('locked: double-clicking the title does not open rename', async () => {
    const { container } = renderSidebar()
    await settle()
    const title = mastermindRow(container)!.querySelector('[data-session-title]') as HTMLElement
    fireEvent.doubleClick(title)
    expect(mastermindRow(container)!.querySelector('textarea')).toBeNull()
  })

  it('reduced motion: the ring is static', async () => {
    mocks.reduceMotion = true
    const { container } = renderSidebar()
    await settle()
    expect(within(mastermindRow(container)!).getByTestId('mastermind-row-glow')).toHaveAttribute('data-motion', 'static')
  })

  it('the stylesheet pins the ring under prefers-reduced-motion and adds no @keyframes', () => {
    const css = readFileSync(join(__dirname, '..', 'styles', 'sidebar-and-side-panel.css'), 'utf8')
    expect(css).toMatch(/@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{\s*\.mastermind-row-glow\s*\{[^}]*background-position/)
    expect(css).not.toMatch(/@keyframes\s+[\w-]*mastermind/)
    // Built from the theme accent only: no literal colour in the rule.
    const rule = /\.mastermind-row-glow\{[^}]*\}/.exec(css)?.[0] ?? ''
    expect(rule).toContain('var(--accent)')
    expect(rule).not.toMatch(/#[0-9a-f]{3,8}\b|rgba?\(/i)
  })

  it('enabled: a close is refused on the client, before any optimistic removal, with the notice', async () => {
    const { store } = renderSidebar()
    await settle()
    await waitFor(() => expect(store.getState().dashboard.mastermindEnabled).toBe(true))
    // Every slots state the close passes through must still hold the row: no flash.
    const seen: boolean[] = []
    const unsubscribe = store.subscribe(() => {
      seen.push(store.getState().dashboard.slots.some(sl => sl.key === 'mastermind'))
    })
    await act(async () => { await store.dispatch(deleteSlot('mastermind')).catch(() => {}) })
    unsubscribe()
    expect(seen.length).toBeGreaterThan(0)
    expect(seen.every(Boolean)).toBe(true)
    expect(mocks.deleteChatSlot).not.toHaveBeenCalled()
    expect(store.getState().chat.closeRefused?.reason).toBe('mastermind')
    expect(await screen.findByTestId('close-refused')).toHaveTextContent('Settings → Mastermind')
  })

  it('disabled: the same close goes to the server', async () => {
    mocks.info = { ...mocks.info, enabled: false }
    mocks.deleteChatSlot.mockResolvedValue({ ok: true })
    const { store } = renderSidebar()
    await settle()
    await waitFor(() => expect(store.getState().dashboard.mastermindEnabled).toBe(false))
    await act(async () => { await store.dispatch(deleteSlot('mastermind')).catch(() => {}) })
    expect(mocks.deleteChatSlot).toHaveBeenCalledWith('mastermind')
    expect(store.getState().chat.closeRefused ?? null).toBeNull()
  })

  it('a server 409 mastermind_protected (client not yet told) also points to Settings', async () => {
    const { store } = renderSidebar()
    await settle()
    await waitFor(() => expect(store.getState().dashboard.mastermindEnabled).toBe(true))
    // As if the status read had not landed: only the server knows.
    act(() => { store.dispatch(setMastermindEnabledState(false)) })
    await act(async () => { await store.dispatch(deleteSlot('mastermind')).catch(() => {}) })
    expect(mocks.deleteChatSlot).toHaveBeenCalledWith('mastermind')
    expect(await screen.findByTestId('close-refused')).toHaveTextContent('Settings → Mastermind')
    expect(store.getState().chat.closeRefused?.reason).toBe('mastermind')
  })
})
