/**
 * Arrow-key roving over the sidebar session list.
 *
 * Covers the two halves separately: the scope-bounded/clamped index maths as a
 * plain-DOM unit test, and the wiring (bare arrows only, focus-only rove, Enter
 * still activates) against a rendered ChatSidebar.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { sessionRowsInScope, siblingSessionRow, focusSiblingSessionRow } from '../pages/chat/sessionRowNav'

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
// Board view is per-describe: the list-view suites need it off, the board suite
// needs it on, and loadChatConfig is called at render time so a mutable flag is
// enough (a vi.mock factory only runs once per module).
const chatConfig = { tagColumnsEnabled: false, confirmCloseSession: false }
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => chatConfig,
  saveChatConfig: vi.fn(),
}))

// Board fixtures are per-describe too. They have to come back from the api mock,
// not just be seeded into the query cache: the seeded entry is stale on mount, so
// react-query refetches and a blanket `[]` mock would erase it.
const fixtures: { chatTags: unknown[]; tagColumns: unknown[]; chatFolders: unknown[] } = {
  chatTags: [], tagColumns: [], chatFolders: [],
}

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop in fixtures) return vi.fn().mockResolvedValue(fixtures[prop as keyof typeof fixtures])
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

function renderSidebar(slots: any[]) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as any,
    chat: { activeSlot: null, slotStatusDetail: {} } as any,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  const utils = render(
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
  return { ...utils, store }
}

const THREE = [
  { key: 'k1', title: 'first', running: false, messages: 1 },
  { key: 'k2', title: 'second', running: false, messages: 1 },
  { key: 'k3', title: 'third', running: false, messages: 1 },
]

/** The focusable row element for a slot key, in list scope. */
function row(key: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(`[data-session-row="${key}"][data-session-scope="list"]`)
  if (!el) throw new Error(`no list-scope row for ${key}`)
  return el
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

describe('sessionRowNav', () => {
  /**
   * Built with createElement rather than an HTML-string template: the blocking
   * `frontend-security` AUTOSDE rule forbids assigning to the innerHTML property
   * anywhere under `src/**`, with no test exemption.
   */
  function mkRow(key: string, scope: string): HTMLElement {
    const el = document.createElement('div')
    el.dataset.sessionRow = key
    el.dataset.sessionScope = scope
    el.tabIndex = 0
    return el
  }

  function scopedDom(): HTMLElement {
    const host = document.createElement('div')
    host.append(mkRow('a', 'list'), mkRow('b', 'board-1'), mkRow('c', 'list'))
    document.body.appendChild(host)
    return host
  }

  afterEach(() => { document.body.replaceChildren() })

  it('collects only the rows sharing the scope, in DOM order', () => {
    const host = scopedDom()
    const first = host.querySelector<HTMLElement>('[data-session-row="a"]')!
    expect(sessionRowsInScope(first).map(el => el.dataset.sessionRow)).toEqual(['a', 'c'])
  })

  it('steps past a foreign-scope row rather than into it', () => {
    const host = scopedDom()
    const first = host.querySelector<HTMLElement>('[data-session-row="a"]')!
    expect(siblingSessionRow(first, 1)?.dataset.sessionRow).toBe('c')
  })

  it('clamps at both ends instead of wrapping', () => {
    const host = scopedDom()
    const first = host.querySelector<HTMLElement>('[data-session-row="a"]')!
    const last = host.querySelector<HTMLElement>('[data-session-row="c"]')!
    expect(siblingSessionRow(first, -1)).toBeNull()
    expect(siblingSessionRow(last, 1)).toBeNull()
    expect(focusSiblingSessionRow(last, 1)).toBe(false)
  })

  it('skips rows hidden inside an inert subtree (a collapsed folder)', () => {
    // A collapsed folder keeps its rows mounted under an [inert] wrapper, so
    // without the filter ArrowDown would stall on an unfocusable row.
    const host = document.createElement('div')
    const folder = document.createElement('div')
    folder.setAttribute('inert', '')
    folder.append(mkRow('hidden', 'list'))
    host.append(mkRow('a', 'list'), folder, mkRow('c', 'list'))
    document.body.appendChild(host)
    const first = host.querySelector<HTMLElement>('[data-session-row="a"]')!
    expect(sessionRowsInScope(first).map(el => el.dataset.sessionRow)).toEqual(['a', 'c'])
    expect(siblingSessionRow(first, 1)?.dataset.sessionRow).toBe('c')
  })
})

describe('chat sidebar — session list arrow navigation', () => {
  it('ArrowDown moves focus to the next row', async () => {
    const { findByText } = renderSidebar(THREE)
    await findByText('third')
    row('k1').focus()
    fireEvent.keyDown(row('k1'), { key: 'ArrowDown' })
    expect(document.activeElement).toBe(row('k2'))
  })

  it('ArrowUp moves focus to the previous row', async () => {
    const { findByText } = renderSidebar(THREE)
    await findByText('third')
    row('k2').focus()
    fireEvent.keyDown(row('k2'), { key: 'ArrowUp' })
    expect(document.activeElement).toBe(row('k1'))
  })

  it('roving does not switch the session — only Enter does', async () => {
    const { findByText, store } = renderSidebar(THREE)
    await findByText('third')
    row('k1').focus()
    fireEvent.keyDown(row('k1'), { key: 'ArrowDown' })
    fireEvent.keyDown(row('k2'), { key: 'ArrowDown' })
    expect(document.activeElement).toBe(row('k3'))
    expect(store.getState().chat.activeSlot).toBeNull()
    // Enter is still claimed by the row (the activation path), unchanged.
    expect(fireEvent.keyDown(row('k3'), { key: 'Enter' })).toBe(false)
  })

  it('leaves the arrow alone at the last row so the list can still scroll', async () => {
    const { findByText } = renderSidebar(THREE)
    await findByText('third')
    row('k3').focus()
    // fireEvent returns false when the handler called preventDefault.
    expect(fireEvent.keyDown(row('k3'), { key: 'ArrowDown' })).toBe(true)
    expect(document.activeElement).toBe(row('k3'))
  })

  it('ignores a modified arrow so Alt+arrow session cycling still reaches its handler', async () => {
    const { findByText } = renderSidebar(THREE)
    await findByText('third')
    row('k1').focus()
    fireEvent.keyDown(row('k1'), { key: 'ArrowDown', altKey: true })
    expect(document.activeElement).toBe(row('k1'))
  })
})

/**
 * A board column's foldered and ungrouped rows are ONE visible list, so the rove
 * has to cross the folder boundary. The row's `scope` stays per-folder (Framer
 * layoutId + rename target uniqueness), so the nav scope is threaded separately.
 */
describe('chat sidebar — board column arrow navigation', () => {
  const TAG = '11111111-1111-1111-1111-111111111111'
  const COL = 'col-aaaa'
  const FOLDER = 'folder-zzzz'
  const boardTags = [{ id: TAG, name: 'Blocked', color: '#e11', order: 0, status: true }]
  const boardColumns = [{ id: COL, name: 'Blocked', tag_ids: [TAG], mode: 'any', order: 0 }]
  const boardFolders = [{ id: FOLDER, name: 'CDF', order: 0, collapsed: false }]
  const boardSlots = [
    { key: 'b1', title: 'in folder', running: false, messages: 1, tags: [TAG], folder_id: FOLDER },
    { key: 'b2', title: 'at column root', running: false, messages: 1, tags: [TAG] },
  ]

  function renderBoard() {
    const store = createTestStore({
      dashboard: {
        status: {}, connected: true, slots: boardSlots, approvalMode: 'normal',
        channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
        slotsLoaded: true,
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
        sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      } as any,
      chat: { activeSlot: null, slotStatusDetail: {} } as any,
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    qc.setQueryData(['chat-tags'], boardTags)
    qc.setQueryData(['tag-columns'], boardColumns)
    qc.setQueryData(['chat-folders'], boardFolders)
    return render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatSidebar
                slots={boardSlots} activeSlot={null} unreadSlots={[]}
                history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
              />
            </MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  }

  beforeEach(() => {
    chatConfig.tagColumnsEnabled = true
    fixtures.chatTags = boardTags
    fixtures.tagColumns = boardColumns
    fixtures.chatFolders = boardFolders
  })
  afterEach(() => {
    chatConfig.tagColumnsEnabled = false
    fixtures.chatTags = []
    fixtures.tagColumns = []
    fixtures.chatFolders = []
  })

  it('scopes a foldered board row to its column, not its folder', async () => {
    const { findByText } = renderBoard()
    await findByText('in folder')
    const foldered = document.querySelector<HTMLElement>('[data-session-row="b1"]')!
    const rooted = document.querySelector<HTMLElement>('[data-session-row="b2"]')!
    expect(foldered.dataset.sessionScope).toBe(COL)
    expect(rooted.dataset.sessionScope).toBe(COL)
    // …and the rove therefore reaches across the boundary in one step.
    expect(siblingSessionRow(foldered, 1)).toBe(rooted)
  })
})
