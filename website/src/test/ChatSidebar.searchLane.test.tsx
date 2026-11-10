/**
 * The sidebar while a search is active: one list in the server's relevance order,
 * each row showing WHY it matched (the server's snippet, query highlighted) and the
 * folder it is filed in, under a count line that can clear the filters while keeping
 * the query. Clearing the query brings the folder groups back unchanged.
 *
 * The fixture spreads the server's best hits across folders in the reverse of the
 * folder order, so a list still grouped by folder would draw them out of rank.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { turnOnCrewModePreview } from './crewModePreview'
import { _laneScrollAnchor, _resetLaneScrollMemory } from '../hooks/useLaneScrollMemory'

const { searchReply, sessionsSearchMock } = vi.hoisted(() => {
  const searchReply: { current: unknown } = { current: null }
  return { searchReply, sessionsSearchMock: vi.fn(() => (searchReply.current instanceof Error ? Promise.reject(searchReply.current) : Promise.resolve(searchReply.current))) }
})

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...Object.fromEntries(
        ['sessions', 'chatSlots', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot', 'resumeChatSlot',
          'deleteSession', 'agentDetail', 'spawnList', 'fetchHistory', 'renameSlot', 'forkSession', 'updateChatFolder',
        ].map(k => [k, vi.fn().mockResolvedValue({})]),
      ),
      setPinnedOrder: vi.fn(async (keys: string[]) => ({ ok: true, order: keys })),
      chatFolders: vi.fn().mockResolvedValue([]),
      chatTags: vi.fn().mockResolvedValue([]),
      // A Crew Mode root with no workers: nothing needs attention, so its row draws its usual tail.
      crewWorkers: vi.fn(async (root: string) => ({ root, workers: [] })),
      sessionsSearch: sessionsSearchMock,
    },
  }
})

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch

import ChatSidebar from '../pages/ChatSidebar'
import type { ChatFolder, ChatSlot } from '../types'
import type { RootState } from '../store'

const FOLDERS: ChatFolder[] = [
  { id: 'f-dev', name: 'Development', collapsed: false, order: 0 },
  { id: 'f-write', name: 'Writing', collapsed: false, order: 1 },
  { id: 'f-prog', name: 'Programs', collapsed: true, order: 2 },
]

const slot = (key: string, title: string, folder: string | undefined, lastMessage: string, over: Partial<ChatSlot> = {}): ChatSlot => ({
  key, title, messages: 3, running: false, mode: '', created: '', last_ts: '2026-11-01T00:00:00Z',
  folder_id: folder, last_message: lastMessage, ...over,
} as ChatSlot)

const SLOTS = [
  slot('k-dev', 'Debugging Slow Session Performance', 'f-dev', 'The mechanism already exists'),
  slot('k-write', 'Team Crews PRFAQ', 'f-write', 'Your numbering is authoritative', { pinned: true }),
  slot('k-prog', 'Kiro Crew Roadmap Planning', 'f-prog', 'Pipelines need to stay green'),
  slot('k-title', 'Memory hygiene notes', 'f-dev', 'Nothing about it here'),
  slot('k-miss', 'Unrelated chat', 'f-write', 'nope'),
]

// Best first: Programs, then Writing, then Development -- the reverse of the folder order.
const RANKED = {
  sessions: [
    { key: 'dashboard_k-prog', title: 'Kiro Crew Roadmap Planning', snippet: '…Memory v2 needs a migration plan…' },
    { key: 'k-write', title: 'Team Crews PRFAQ', snippet: '…each crew keeps its own memory store…' },
    { key: 'k-dev', title: 'Debugging Slow Session Performance', snippet: '…the memory recall runs on every turn…' },
    // A title-only hit: no snippet, so the row keeps its preview.
    { key: 'k-title', title: 'Memory hygiene notes' },
  ],
}

/** A test's own slot list, reset before each test. */
const SLOTS_OVERRIDE: { current: ChatSlot[] | null } = { current: null }

function renderSidebar() {
  const slots = SLOTS_OVERRIDE.current ?? SLOTS
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null, slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { ...defaults.chat, activeSlot: null, slotStatusDetail: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false } } })
  qc.setQueryData(['chat-folders'], FOLDERS)
  qc.setQueryData(['tag-columns'], [])
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar slots={slots} activeSlot={null} unreadSlots={[]} history={[]} historyHasMore={false} defaultAgent="default" installedAgents={[]} />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return view
}

const search = (text: string) => fireEvent.change(screen.getByPlaceholderText(/search sessions/i), { target: { value: text } })
const row = (key: string) => document.querySelector<HTMLElement>(`[data-session-row="${key}"]`)
const drawnKeys = () => [...document.querySelectorAll<HTMLElement>('[data-session-row]')].map(el => el.getAttribute('data-session-row'))

beforeEach(() => {
  sessionsSearchMock.mockClear()
  searchReply.current = RANKED
  SLOTS_OVERRIDE.current = null
  localStorage.clear()
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
})

describe('the sidebar while a search is active', () => {
  it('keeps a row that owes an approval at its one status line, without the snippet', async () => {
    const owed = SLOTS.map(s => (s.key === 'k-dev' ? { ...s, pending_approval: true } : s))
    SLOTS_OVERRIDE.current = owed
    renderSidebar()
    search('memory')
    await waitFor(() => {
      expect(row('k-write')?.querySelector('[data-session-search-snippet]')).not.toBeNull()
      expect(row('k-dev')?.textContent ?? '').toContain('Needs approval')
    })
    expect(row('k-dev')?.querySelector('[data-session-search-snippet]')).toBeNull()
  })

  it('shows the snippet on an idle Crew Mode root, whose usual tail is its last message', async () => {
    turnOnCrewModePreview()
    SLOTS_OVERRIDE.current = SLOTS.map(s => (s.key === 'k-write' ? { ...s, crew_mode_root: true } : s))
    renderSidebar()
    search('memory')
    await waitFor(() => {
      expect(row('k-write')?.querySelector('[data-session-search-snippet]')?.textContent).toBe('…each crew keeps its own memory store…')
      expect(row('k-write')?.textContent ?? '').not.toContain('Your numbering is authoritative')
    })
  })

  it('shows the server snippet with the query highlighted in place of the last message', async () => {
    renderSidebar()
    search('memory')
    // Read inside waitFor: the lane swap remounts the rows while the reply lands.
    await waitFor(() => {
      const write = row('k-write')!
      expect(write.querySelector('[data-session-search-snippet]')?.textContent).toBe('…each crew keeps its own memory store…')
      expect(write.querySelector('[data-session-search-snippet] strong')?.textContent).toBe('memory')
      expect(write.textContent).not.toContain('Your numbering is authoritative')
      // The title-only hit has no snippet, so it keeps today's preview.
      expect(row('k-title')?.querySelector('[data-session-search-snippet]')).toBeNull()
      expect(row('k-title')?.textContent).toContain('Nothing about it here')
    })
  })

  it('draws one list in the server rank, each row labelled with its folder', async () => {
    renderSidebar()
    search('memory')
    await waitFor(() => expect(drawnKeys()).toEqual(['k-prog', 'k-write', 'k-dev', 'k-title']))
    // No folder blocks: the collapsed Programs folder cannot hide its best hit.
    expect(document.querySelector('[data-folder-row="f-dev"]')).toBeNull()
    expect(row('k-prog')?.querySelector('[data-testid="session-search-folder"]')?.textContent).toBe('Programs')
    expect(row('k-write')?.querySelector('[data-testid="session-search-folder"]')?.textContent).toBe('Writing')
    // The label sits OUTSIDE the time's `shrink-0` group, as its own truncating item in
    // front of it: inside, its nowrap text would set the group's width and a long
    // folder path would clip the time in a narrow sidebar. jsdom has no layout, so the
    // structure is what is pinned here (the clipping itself is measured in Chromium).
    const group = row('k-write')!.querySelector('[data-testid="session-row-trailing-meta"]')!
    const label = row('k-write')!.querySelector('[data-testid="session-search-folder"]')!
    expect(group.className).toMatch(/\bshrink-0\b/)
    expect(group.contains(label)).toBe(false)
    expect(label.nextElementSibling).toBe(group)
    expect(label.className).toMatch(/\btruncate\b/)
    expect(label.className).toMatch(/\bmin-w-0\b/)
  })

  it('counts the matches above the results', async () => {
    renderSidebar()
    search('memory')
    await waitFor(() => expect(screen.getByTestId('sidebar-search-count').textContent).toBe('4 matches, best first'))
    expect(screen.queryByTestId('sidebar-search-clear-filters')).toBeNull()
  })

  it('clears the filters hiding hits but keeps the query', async () => {
    // The Pinned chip on: only the pinned hit passes.
    localStorage.setItem('mc-session-pinned-only', '1')
    renderSidebar()
    search('memory')
    await waitFor(() => expect(drawnKeys()).toEqual(['k-write']))
    expect(screen.getByTestId('sidebar-search-count').textContent).toBe('1 match, best first')
    fireEvent.click(screen.getByTestId('sidebar-search-clear-filters'))
    await waitFor(() => expect(drawnKeys()).toEqual(['k-prog', 'k-write', 'k-dev', 'k-title']))
    expect((screen.getByPlaceholderText(/search sessions/i) as HTMLInputElement).value).toBe('memory')
    expect(screen.queryByTestId('sidebar-search-clear-filters')).toBeNull()
  })

  it('offers Clear filters when the filters hide every hit', async () => {
    // The In progress chip on: no session is running, so nothing passes. No row is
    // pinned here, since a pinned row passes the status chips whatever they say.
    localStorage.setItem('mc-session-running-only', '1')
    SLOTS_OVERRIDE.current = SLOTS.map(s => ({ ...s, pinned: false }))
    renderSidebar()
    search('memory')
    await waitFor(() => expect(screen.getByTestId('sidebar-search-count').textContent).toBe('0 matches, best first'))
    expect(drawnKeys()).toEqual([])
    fireEvent.click(screen.getByTestId('sidebar-search-clear-filters'))
    await waitFor(() => expect(drawnKeys()).toEqual(['k-prog', 'k-write', 'k-dev', 'k-title']))
  })

  it('says older sessions were not searched when the scan window cut the search short', async () => {
    searchReply.current = { ...RANKED, scan: { searched: 500, total: 900 } }
    renderSidebar()
    search('memory')
    await waitFor(() => expect(screen.getByTestId('sidebar-search-scan-note').textContent).toContain('500'))
  })

  it('says the search failed and falls back to title matches', async () => {
    searchReply.current = new Error('boom')
    renderSidebar()
    search('memory')
    await waitFor(() => expect(screen.getByTestId('sidebar-search-failed')).not.toBeNull())
    // Through the shared error surface, which announces itself.
    expect(screen.getByTestId('sidebar-search-failed')).toHaveAttribute('role', 'alert')
    // No "0 matches" above the failure: the rows are title matches, not a result count.
    expect(screen.queryByTestId('sidebar-search-count')).toBeNull()
    expect(drawnKeys()).toEqual(['k-title'])
    expect(row('k-title')?.querySelector('[data-session-search-snippet]')).toBeNull()
  })

  it('keeps the folder groups until the first reply lands, with no premature count', async () => {
    let answer!: (v: unknown) => void
    searchReply.current = null
    sessionsSearchMock.mockImplementationOnce(() => new Promise(r => { answer = r }))
    renderSidebar()
    await waitFor(() => expect(document.querySelector('[data-folder-row="f-dev"]')).not.toBeNull())
    search('memory')
    await waitFor(() => expect(sessionsSearchMock).toHaveBeenCalled())
    expect(document.querySelector('[data-folder-row="f-dev"]')).not.toBeNull()
    expect(screen.queryByTestId('sidebar-search-header')).toBeNull()
    // The folder rows on screen are not the results yet, and the line says so.
    expect(screen.getByTestId('sidebar-search-pending').textContent).toBe('Searching…')
    answer(RANKED)
    await waitFor(() => expect(drawnKeys()).toEqual(['k-prog', 'k-write', 'k-dev', 'k-title']))
    expect(screen.getByTestId('sidebar-search-count').textContent).toBe('4 matches, best first')
    expect(screen.queryByTestId('sidebar-search-pending')).toBeNull()
  })

  it('opens every new query at the top, and gives the folder lane back its own scroll', async () => {
    const lane = (id: string) => document.querySelector<HTMLElement>(`[data-testid="${id}"]`)
    _resetLaneScrollMemory()
    renderSidebar()
    await waitFor(() => expect(lane('tree-view-lane')).not.toBeNull())
    // Browse the folder lane part-way down, and wait until the memory has it.
    lane('tree-view-lane')!.scrollTop = 150
    fireEvent.scroll(lane('tree-view-lane')!)
    await waitFor(() => expect(_laneScrollAnchor('chat-sidebar-lane:tree')?.scrollTop).toBe(150))
    search('memory')
    await waitFor(() => expect(lane('flat-view-lane')).not.toBeNull())
    // Scrolled part-way down the first query's results (the search lane records nothing).
    lane('flat-view-lane')!.scrollTop = 200
    // A different query on the same mounted lane starts at its best match.
    search('crew')
    await waitFor(() => expect(lane('flat-view-lane')!.scrollTop).toBe(0))
    // Clearing restores the folder lane where the person left it, folders intact.
    search('')
    await waitFor(() => expect(lane('tree-view-lane')).not.toBeNull())
    await waitFor(() => expect(lane('tree-view-lane')!.scrollTop).toBe(150))
    expect(document.querySelector('[data-folder-row="f-dev"]')).not.toBeNull()
  })

  it('keeps the folder groups for a query below the minimum, and restores them when cleared', async () => {
    renderSidebar()
    await waitFor(() => expect(document.querySelector('[data-folder-row="f-dev"]')).not.toBeNull())
    const folderRows = () => [...document.querySelectorAll('[data-folder-row]')].map(el => el.getAttribute('data-folder-row'))
    const before = { rows: drawnKeys(), folders: folderRows() }
    search('m')
    expect(document.querySelector('[data-folder-row="f-dev"]')).not.toBeNull()
    expect(screen.queryByTestId('sidebar-search-header')).toBeNull()
    search('memory')
    await waitFor(() => expect(document.querySelector('[data-folder-row="f-dev"]')).toBeNull())
    search('')
    await waitFor(() => expect(document.querySelector('[data-folder-row="f-dev"]')).not.toBeNull())
    expect(screen.queryByTestId('sidebar-search-header')).toBeNull()
    expect(document.querySelector('[data-session-search-snippet]')).toBeNull()
    expect(document.querySelector('[data-testid="session-search-folder"]')).toBeNull()
    // The grouped view is back as it was: the same folders, the same rows in the same order.
    expect({ rows: drawnKeys(), folders: folderRows() }).toEqual(before)
    // With no folder label the trailing group is the `shrink-0` group rows always drew.
    expect(row('k-write')!.querySelector('[data-testid="session-row-trailing-meta"]')?.className).toMatch(/\bshrink-0\b/)
    expect(row('k-write')?.textContent).toContain('Your numbering is authoritative')
  })
})
