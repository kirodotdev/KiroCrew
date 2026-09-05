import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { Routes, Route, useSearchParams } from 'react-router-dom'
import SessionsPage, { recencyGroup } from '../pages/SessionsPage'
import { renderWithProviders, createTestStore } from './helpers'
import type { ChatSlot, ChatTag } from '../types'

// The real ChatPage activates the session named by ?sid= (the path slug is
// decorative), so the stub surfaces that param for assertions.
function ChatStub() {
  const [sp] = useSearchParams()
  return <span data-testid="chat-sid">{sp.get('sid') ?? ''}</span>
}

// The page reads the tag vocabulary through the api client; stub it so tests
// control which tags exist and which carry the status flag.
const chatTags = vi.fn<[], Promise<ChatTag[]>>()
vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, chatTags: (...a: unknown[]) => chatTags(...(a as [])) } }
})

const NOW = new Date(2026, 7, 28, 15, 0, 0).getTime() // local Aug 28 2026 15:00

function slot(key: string, title: string, tsMs: number, extra: Partial<ChatSlot> = {}): ChatSlot {
  // No `surface`: the default (empty) surface is the ordinary chat surface per
  // isChatPageSurface — '', 'orchestrator' and 'crew' are the chat-page set.
  return {
    key, title, messages: 1, running: false,
    last_ts: new Date(tsMs).toISOString(), ...extra,
  } as ChatSlot
}

function storeWith(slots: ChatSlot[], unread: string[] = []) {
  const store = createTestStore()
  const state = store.getState()
  return createTestStore({
    ...state,
    // Seeded slots represent a store that has already received its first SSE
    // frame; slotsLoaded:true is what distinguishes "loaded, and empty" from
    // "still loading" (the skeleton branch is tested separately).
    dashboard: { ...state.dashboard, slots, unreadSlots: unread, slotsLoaded: true },
  })
}

function renderPage(slots: ChatSlot[], unread: string[] = []) {
  const store = storeWith(slots, unread)
  return renderWithProviders(
    <Routes>
      <Route path="/sessions" element={<SessionsPage />} />
      <Route
        path="/chat/:slug?"
        element={
          <div data-testid="chat-target">
            <ChatStub />
          </div>
        }
      />
    </Routes>,
    { store, route: '/sessions' },
  )
}

beforeEach(() => {
  vi.useFakeTimers({ now: NOW, toFake: ['Date'] })
  chatTags.mockResolvedValue([])
})

describe('recencyGroup', () => {
  it('buckets by local calendar day, not rolling 24h windows', () => {
    const now = new Date(2026, 7, 28, 0, 30).getTime() // 00:30 local
    // 1h ago is 23:30 YESTERDAY by calendar even though within 24h.
    expect(recencyGroup(now - 3600_000, now)).toBe('yesterday')
    expect(recencyGroup(now - 60_000, now)).toBe('today')
    expect(recencyGroup(now - 3 * 86400_000, now)).toBe('earlier')
  })

  it('is DST-safe: yesterday start comes from Date day arithmetic', () => {
    // Nov 1 2026, the day US DST falls back (25h day). 2026-11-02 12:00 local:
    const now = new Date(2026, 10, 2, 12, 0).getTime()
    const yesterdayNoon = new Date(2026, 10, 1, 12, 0).getTime()
    expect(recencyGroup(yesterdayNoon, now)).toBe('yesterday')
  })
})

describe('pre-load skeleton', () => {
  it('shows skeleton rows (not the first-run empty state) before the first slots frame lands', async () => {
    // slotsLoaded:false with zero slots is the ambiguous cold-open frame: a
    // bookmark opened on a phone before SSE delivers. Must NOT flash the
    // "No sessions" empty state + create CTA at a user who has sessions.
    const base = createTestStore().getState()
    const store = createTestStore({
      ...base,
      dashboard: { ...base.dashboard, slots: [], unreadSlots: [], slotsLoaded: false },
    })
    renderWithProviders(
      <Routes>
        <Route path="/sessions" element={<SessionsPage />} />
      </Routes>,
      { store, route: '/sessions' },
    )
    expect(await screen.findByTestId('sessions-loading')).toBeInTheDocument()
    expect(screen.queryByTestId('empty-state')).toBeNull()
  })
})

describe('SessionsPage', () => {
  it('renders sessions under recency group headings, newest first', async () => {
    renderPage([
      slot('s-old', 'Old thing', NOW - 5 * 86400_000),
      slot('s-today', 'Fresh thing', NOW - 3600_000),
      slot('s-yday', 'Middle thing', new Date(2026, 7, 27, 12).getTime()),
    ])
    expect(await screen.findByText('Today')).toBeInTheDocument()
    expect(screen.getByText('Yesterday')).toBeInTheDocument()
    expect(screen.getByText('Earlier')).toBeInTheDocument()
    const rows = screen.getAllByTestId(/sessions-row-/)
    expect(rows.map(r => r.getAttribute('data-testid'))).toEqual([
      'sessions-row-s-today', 'sessions-row-s-yday', 'sessions-row-s-old',
    ])
  })

  it('omits empty groups', async () => {
    renderPage([slot('s1', 'Only today', NOW - 60_000)])
    expect(await screen.findByText('Today')).toBeInTheDocument()
    expect(screen.queryByText('Yesterday')).toBeNull()
    expect(screen.queryByText('Earlier')).toBeNull()
  })

  it('navigates to the full chat UX with ?sid=<key> on row click', async () => {
    renderPage([slot('s1', 'A session', NOW - 60_000)])
    fireEvent.click(await screen.findByTestId('sessions-row-s1'))
    expect(screen.getByTestId('chat-target')).toBeInTheDocument()
    // ChatPage activates the session from ?sid= — a key in the path slug is
    // ignored and would silently reopen the last-used session instead.
    expect(screen.getByTestId('chat-sid')).toHaveTextContent('s1')
  })

  it('never auto-selects or auto-creates: empty store renders empty state, no slot created', async () => {
    const { store } = renderPage([])
    expect(await screen.findByTestId('empty-state')).toBeInTheDocument()
    expect(store.getState().chat.activeSlot).toBeNull()
    expect(store.getState().dashboard.slots).toHaveLength(0)
  })

  it('filters by search query', async () => {
    renderPage([
      slot('s1', 'Alpha work', NOW - 60_000),
      slot('s2', 'Beta work', NOW - 120_000),
    ])
    fireEvent.change(await screen.findByTestId('sessions-search'), { target: { value: 'alpha' } })
    expect(screen.getByTestId('sessions-row-s1')).toBeInTheDocument()
    expect(screen.queryByTestId('sessions-row-s2')).toBeNull()
  })

  it('shows FilteredEmpty (not the first-run empty state) when a filter hides all sessions, and Clear restores them', async () => {
    renderPage([slot('s1', 'Alpha work', NOW - 60_000)])
    fireEvent.change(await screen.findByTestId('sessions-search'), { target: { value: 'zzz-no-match' } })
    expect(screen.getByTestId('filtered-empty')).toBeInTheDocument()
    expect(screen.queryByTestId('empty-state')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /clear/i }))
    expect(screen.getByTestId('sessions-row-s1')).toBeInTheDocument()
  })

  it('renders the page header title and marks the active chip with aria-pressed', async () => {
    renderPage([slot('s1', 'A session', NOW - 60_000)], ['s1'])
    expect(await screen.findByTestId('page-title')).toHaveTextContent('All sessions')
    const unreadChip = screen.getByTestId('sessions-chip-unread')
    expect(unreadChip).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(unreadChip)
    expect(unreadChip).toHaveAttribute('aria-pressed', 'true')
  })

  it('Unread chip filters to unread sessions and toggles back to All', async () => {
    renderPage(
      [slot('s1', 'Read', NOW - 60_000), slot('s2', 'Unread', NOW - 120_000)],
      ['s2'],
    )
    expect(await screen.findByTestId('sessions-unread-s2')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('sessions-chip-unread'))
    expect(screen.queryByTestId('sessions-row-s1')).toBeNull()
    expect(screen.getByTestId('sessions-row-s2')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('sessions-chip-unread'))
    expect(screen.getByTestId('sessions-row-s1')).toBeInTheDocument()
  })

  it('counts only visible unread sessions in the chip badge, ignoring orphaned unread keys', async () => {
    // 'ghost' is in unreadSlots but has no visible slot (deleted / non-chat
    // surface); the badge must match the rows the Unread chip reveals.
    renderPage(
      [slot('s1', 'Read', NOW - 60_000), slot('s2', 'Unread', NOW - 120_000)],
      ['s2', 'ghost'],
    )
    const unreadChip = await screen.findByTestId('sessions-chip-unread')
    expect(unreadChip).toHaveTextContent(/^Unread\s*1$/)
  })

  it('echoes the active chip label (not empty quotes) when a chip-only filter hides all sessions', async () => {
    // Unread chip active with zero unread visible cannot happen via clicks
    // (the chip shows no rows only when every unread key is orphaned), so
    // reach it through search+chip: type a query, clear it via the chip path.
    renderPage([slot('s1', 'Read', NOW - 60_000)], ['ghost'])
    fireEvent.click(await screen.findByTestId('sessions-chip-unread'))
    const empty = screen.getByTestId('filtered-empty')
    expect(empty).toHaveTextContent('Unread')
    expect(empty).not.toHaveTextContent('""')
    expect(empty).not.toHaveTextContent('“”')
  })

  it('offers a New session action inside the first-run empty state', async () => {
    renderPage([])
    expect(await screen.findByTestId('sessions-empty-new')).toBeInTheDocument()
  })

  it('offers chips only for status tags in use and filters by them', async () => {
    chatTags.mockResolvedValue([
      { id: 'review', name: 'Review', color: '#a78bfa', order: 1, status: true },
      { id: 'done', name: 'Done', color: '#00d492', order: 2, status: true },  // not in use
      { id: 'proj', name: 'proj', color: '#888888', order: 3 },                // not a status tag
    ])
    renderPage([
      // slot.tags carries tag IDs, mirroring the backend registry.
      slot('s1', 'In review', NOW - 60_000, { tags: ['review', 'proj'] }),
      slot('s2', 'Plain', NOW - 120_000),
    ])
    expect(await screen.findByTestId('sessions-chip-tag-review')).toBeInTheDocument()
    expect(screen.queryByTestId('sessions-chip-tag-done')).toBeNull()
    expect(screen.queryByTestId('sessions-chip-tag-proj')).toBeNull()
    // The chip shows the display NAME, resolved from the id.
    expect(screen.getByTestId('sessions-chip-tag-review')).toHaveTextContent('Review')
    fireEvent.click(screen.getByTestId('sessions-chip-tag-review'))
    expect(screen.getByTestId('sessions-row-s1')).toBeInTheDocument()
    expect(screen.queryByTestId('sessions-row-s2')).toBeNull()
  })

  it('excludes non-chat surfaces (app worker slots)', async () => {
    renderPage([
      slot('s1', 'Chat one', NOW - 60_000),
      slot('s-app', 'App worker', NOW - 60_000, { surface: 'dashboard' }),
    ])
    expect(await screen.findByTestId('sessions-row-s1')).toBeInTheDocument()
    expect(screen.queryByTestId('sessions-row-s-app')).toBeNull()
  })

  it('creates a slot and navigates to it from the New session button', async () => {
    vi.useRealTimers()
    const fetchMock = vi.spyOn(global, 'fetch').mockImplementation(async () =>
      new Response(JSON.stringify({ key: 's-new', messages: 0, running: false }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }),
    )
    try {
      renderPage([])
      fireEvent.click(await screen.findByTestId('sessions-new'))
      await waitFor(() => expect(screen.getByTestId('chat-target')).toBeInTheDocument())
    } finally {
      fetchMock.mockRestore()
    }
  })
})
