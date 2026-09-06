import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { markSlotUnread, sseSlots } from '../../store/dashboardSlice'

/* ── api client mock ─────────────────────────────────────────────────────
 * The page reads exactly two endpoints; mocking them keeps every case
 * network-free. MemberRosterRow is a type-only import so the mock does not
 * need to provide it. */
vi.mock('../../api/client', () => ({
  api: {
    members: vi.fn(),
    memberThread: vi.fn(),
    memberActivity: vi.fn(() => Promise.resolve({ slug: '', member: '', capped: false, entries: [] })),
    crons: vi.fn(() => Promise.resolve({ jobs: [] })),
    webhooks: vi.fn(() => Promise.resolve({ tokens: [] })),
    kirocrewAgents: vi.fn(() => Promise.resolve({ agents: [], default_agent: '' })),
    // The auto-patrol block and roster badge read the whole loop registry;
    // the default is "feature on, nothing armed" so every other case renders
    // the page without a loop in the way.
    autonudgeList: vi.fn(() => Promise.resolve({ enabled: true, loops: [] })),
  },
}))

/* ChatPane is the full chat stack (WS, Redux slot machinery). The page's own
 * contract is only "mount it with the thread's slot key", so a stub that
 * ECHOES the slot key is the strongest cheap assertion available. */
vi.mock('../../components/ChatPane', () => ({
  default: ({ slotKey, agentLocked, followContentWidth }: { slotKey: string; agentLocked?: boolean; followContentWidth?: boolean }) => (
    <div data-testid="chat-pane-stub" data-agent-locked={agentLocked ? '1' : '0'} data-follow-content-width={followContentWidth ? '1' : '0'}>
      {slotKey}
    </div>
  ),
}))

const navigateSpy = vi.fn()
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>()
  return { ...actual, useNavigate: () => navigateSpy }
})

import { api } from '../../api/client'
import MembersPage from './MembersPage'

function row(overrides: Record<string, unknown> = {}) {
  return {
    name: 'oncall',
    slug: 'oncall',
    bound: false,
    slot_key: '',
    running: false,
    kiro_agent: 'kirocrew',
    workspace: 'default',
    memory_store: 'default',
    model: '',
    ...overrides,
  }
}

async function renderPage(members = [row()], defaultAgent = 'kirocrew') {
  ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({
    members,
    default_agent: defaultAgent,
  })
  ;(api.memberThread as ReturnType<typeof vi.fn>).mockResolvedValue({
    slot_key: 'member-oncall',
    slug: 'oncall',
    member: 'oncall',
    created: true,
  })
  const utils = renderWithProviders(<MembersPage />)
  await waitFor(() => expect(api.members).toHaveBeenCalled())
  return utils
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('MembersPage roster', () => {
  it('renders one row per member from the API', async () => {
    await renderPage([row(), row({ name: 'research', slug: 'research' })])
    expect(await screen.findByText('oncall')).toBeInTheDocument()
    expect(screen.getByText('research')).toBeInTheDocument()
  })

  it('shows the empty state when no crews exist', async () => {
    await renderPage([])
    expect(
      await screen.findByText(/No crew members yet/i),
    ).toBeInTheDocument()
  })

  it('shows the load-failure state when the roster call rejects', async () => {
    ;(api.members as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    renderWithProviders(<MembersPage />)
    expect(
      await screen.findByText(/Could not load the member roster/i),
    ).toBeInTheDocument()
  })
})

describe('MembersPage thread', () => {
  it('opens the pinned DM thread on click: creates the thread and mounts the chat stack on its slot', async () => {
    await renderPage()
    fireEvent.click(await screen.findByText('oncall'))
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledWith('oncall'))
    // The stub echoes the slot key: proves ChatPane received THE member slot,
    // not a fresh ordinary slot. Mutating the mounted key breaks this line.
    const pane = await screen.findByTestId('chat-pane-stub')
    expect(pane).toHaveTextContent('member-oncall')
    // The host declares the pin: ChatPane must not offer the agent picker
    // (every selection would 409 against the server-side pin).
    expect(pane).toHaveAttribute('data-agent-locked', '1')
    // The DM column is the page's widest region, so the pane is told to
    // follow the user's Content width setting (ChatPane resolves both the
    // transcript and composer halves itself; its default stays off for
    // split-view panes, which are already narrow).
    expect(pane).toHaveAttribute('data-follow-content-width', '1')
    // The pin is an invariant of every member thread, so the header does NOT
    // announce it — no chip, no term for a state that cannot be otherwise.
    expect(screen.queryByTestId('member-pin-chip')).toBeNull()
  })

  it('orders the roster by most recent activity, never-talked members last alphabetically', async () => {
    await renderPage([
      row({ name: 'zeta-quiet', slug: 'zeta-quiet' }),
      row({ name: 'alpha-quiet', slug: 'alpha-quiet' }),
      row({ name: 'old-talker', slug: 'old-talker', last_active_ts: 100 }),
      row({ name: 'fresh-talker', slug: 'fresh-talker', last_active_ts: 200 }),
    ])
    const list = await screen.findByRole('list')
    const names = Array.from(list.querySelectorAll('li button .font-medium')).map(
      (el) => el.textContent,
    )
    // Recent first; ts=0 rows trail in name order — mirroring an IM member list.
    expect(names.slice(0, 4)).toEqual(['fresh-talker', 'old-talker', 'alpha-quiet', 'zeta-quiet'])
  })

  it('opens a bound member through the thread endpoint too — the roster binding is never mounted unverified', async () => {
    // dm.json outlives the live slot (restart drops an unmessaged slot while
    // the binding survives), so mounting the roster's slot_key directly would
    // let the first message auto-create an ordinary UNPINNED slot on the
    // member key. The idempotent POST is the only creator/repairer.
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await screen.findByText('oncall'))
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledWith('oncall'))
    expect(await screen.findByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
  })

  it('surfaces a visible error when thread creation fails', async () => {
    await renderPage()
    // Override AFTER renderPage, which installs the default resolved mock.
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('409'))
    fireEvent.click(await screen.findByText('oncall'))
    expect(
      await screen.findByText(/Could not open this member's conversation/i),
    ).toBeInTheDocument()
    expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
  })

  it('surfaces a slug collision instead of silently mounting another member thread', async () => {
    // Two crews folding to one slug: the endpoint attributes the thread to the
    // first-bound crew. Clicking the OTHER one must not mount that thread.
    await renderPage([
      row({ name: 'Oncall', slug: 'oncall' }),
      row({ name: 'oncall', slug: 'oncall' }),
    ])
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockResolvedValue({
      slot_key: 'member-oncall',
      slug: 'oncall',
      member: 'Oncall',
      created: false,
    })
    fireEvent.click(await screen.findByText('oncall'))
    expect(await screen.findByText(/shares its identifier with/i)).toBeInTheDocument()
    // The misrouted thread is NOT mounted — that is the entire point.
    expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
  })

  it('keeps a late failure of a previously selected member out of the active view', async () => {
    let rejectA: (e: Error) => void = () => {}
    const pendingA = new Promise((_, reject) => {
      rejectA = reject
    })
    await renderPage([
      row({ name: 'alpha', slug: 'alpha' }),
      row({ name: 'beta', slug: 'beta' }),
    ])
    ;(api.memberThread as ReturnType<typeof vi.fn>)
      .mockReturnValueOnce(pendingA)
      .mockResolvedValueOnce({
        slot_key: 'member-beta',
        slug: 'beta',
        member: 'beta',
        created: true,
      })
    fireEvent.click(await screen.findByText('alpha'))
    fireEvent.click(screen.getByText('beta'))
    expect(await screen.findByTestId('chat-pane-stub')).toHaveTextContent('member-beta')
    rejectA(new Error('late'))
    // The stale rejection lands in alpha's bucket; beta's view stays clean.
    await waitFor(() =>
      expect(screen.queryByText(/Could not open this member's conversation/i)).toBeNull(),
    )
    expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-beta')
  })
})

describe('MembersPage drawer and edit jump', () => {
  it('shows the read-only config summary and the shared-memory disclosure', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall', model: 'claude-opus-5' })])
    fireEvent.click(await screen.findByText('oncall'))
    const drawer = await screen.findByTestId('member-drawer')
    expect(drawer).toHaveTextContent('kirocrew')
    expect(drawer).toHaveTextContent('claude-opus-5')
    expect(drawer).toHaveTextContent(/share one memory/i)
  })

  it('words the disclosure for a member on a dedicated store: markdown separate, conversation shared', async () => {
    // A named memory_store scopes only the markdown layer (preferences,
    // project notes). Conversation memory and lessons live in the one global
    // vector store every member reads, so the drawer must say BOTH halves
    // rather than go silent — silence would imply an isolation that does
    // not exist.
    await renderPage([
      row({ bound: true, slot_key: 'member-oncall', memory_store: 'oncall-own' }),
      row({ name: 'beta', slug: 'beta', memory_store: 'default' }),
    ])
    fireEvent.click(await screen.findByText('oncall'))
    const drawer = await screen.findByTestId('member-drawer')
    expect(drawer).toHaveTextContent(/from the oncall-own store/i)
    expect(drawer).toHaveTextContent(/not separated yet/i)
    expect(drawer).toHaveTextContent(/still known to every member/i)
    expect(drawer).not.toHaveTextContent(/share one memory/i)
  })

  it('keys the wording on the store itself, not on roster membership', async () => {
    // Two members on the same named store still get the store-scoped note:
    // whether the store is shared is a backend fact about which layers it
    // scopes, not something to infer client-side from who else uses it.
    await renderPage([
      row({ bound: true, slot_key: 'member-oncall', memory_store: 'triage' }),
      row({ name: 'beta', slug: 'beta', memory_store: 'triage' }),
    ])
    fireEvent.click(await screen.findByText('oncall'))
    const drawer = await screen.findByTestId('member-drawer')
    expect(drawer).toHaveTextContent(/from the triage store/i)
    expect(drawer).not.toHaveTextContent(/share one memory/i)
  })

  it('toggles the drawer via the Details button', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await screen.findByText('oncall'))
    expect(await screen.findByTestId('member-drawer')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /details/i }))
    // AnimatePresence keeps the drawer mounted for the exit tween — wait for
    // the removal instead of asserting synchronously.
    await waitFor(() => expect(screen.queryByTestId('member-drawer')).toBeNull())
  })

  it('the drawer is hosted in the shared DetailPanel: drag-resize handle present, header close works', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await screen.findByText('oncall'))
    await screen.findByTestId('member-drawer')
    // DetailPanel's resize splitter — the affordance the hand-rolled aside
    // never had. Its presence pins that the drawer went through the shared
    // component rather than a lookalike.
    expect(screen.getByRole('separator', { name: /resize/i })).toBeInTheDocument()
    // DetailPanel's own header close button (replaces the old mobile-only X).
    fireEvent.click(screen.getByRole('button', { name: /close panel/i }))
    await waitFor(() => expect(screen.queryByTestId('member-drawer')).toBeNull())
  })

  it('the edit affordance lives in the drawer only and navigates to the crew manager crews tab', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await screen.findByText('oncall'))
    // Edit is a rare secondary action: it must NOT be a header-level peer of
    // Details. The header carries exactly one action (the drawer toggle).
    expect(screen.queryByTestId('member-edit-jump')).toBeNull()
    // Mutation check: the assertion is on the DESTINATION (explicit ?tab=crews
    // beats CapabilitiesPage's remembered last tab), so retargeting the jump
    // anywhere else fails here.
    for (const btn of screen.getAllByRole('button', { name: /edit in crew manager/i })) {
      fireEvent.click(btn)
      expect(navigateSpy).toHaveBeenCalledWith('/capabilities?tab=crews')
      navigateSpy.mockClear()
    }
  })

  it('the roster header has an add-member entry that navigates to the crew manager crews tab', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    await screen.findByText('oncall')
    // Adding a member IS creating a crew; the crew manager stays the only
    // write path, so the entry is a navigation (destination pinned with the
    // explicit ?tab=crews, same as the edit affordance).
    fireEvent.click(screen.getByTestId('member-add'))
    expect(navigateSpy).toHaveBeenCalledWith('/capabilities?tab=crews')
  })

  it('the drawer renders the recorded activity timeline and honest counters derived from it', async () => {
    const now = Date.now() / 1000
    vi.mocked(api.memberActivity).mockResolvedValue({
      slug: 'oncall',
      member: 'oncall',
      capped: false,
      entries: [
        { ts: now - 120, via: 'chat', project: '' },
        { ts: now - 3600, via: 'select_crew', project: 'kirocrew' },
        // Older than 7 days: appears in the timeline but not in either counter.
        { ts: now - 9 * 86400, via: 'chat', project: '' },
      ],
    })
    await renderPage([row({ bound: true, slot_key: 'member-oncall', last_active_ts: now - 120 })])
    fireEvent.click(await screen.findByText('oncall'))
    const list = await screen.findByTestId('member-activity')
    expect(list.children).toHaveLength(3)
    // Routing decisions are labeled as intent, distinct from conversations,
    // and the project rides along when recorded.
    expect(list).toHaveTextContent(/routed to this member/i)
    expect(list).toHaveTextContent('kirocrew')
    // Counters are derived from the same entries — 2 within 7 days; the
    // 9-day-old one is excluded (today's count depends on wall clock, so only
    // the week card is pinned exactly).
    const stats = screen.getByTestId('member-stats')
    expect(stats).toHaveTextContent('2')
  })

  it('the drawer lists wake sources filtered to the member, via the shared predicates', async () => {
    vi.mocked(api.crons).mockResolvedValue({
      jobs: [
        { id: 'j1', name: 'nightly-triage', message: '', enabled: true, schedule: '0 2 * * *', last_status: '', agent: 'oncall' },
        { id: 'j2', name: 'other-crew-job', message: '', enabled: true, schedule: '@hourly', last_status: '', agent: 'research' },
        // Script jobs open no session — they wake NO crew (shared wakesCrew rule).
        { id: 'j3', name: 'script-job', message: '', enabled: true, schedule: '@daily', last_status: '', agent: 'oncall', script: 'x.py:f' },
      ],
    })
    vi.mocked(api.webhooks).mockResolvedValue({
      tokens: [
        { id: 'w1', label: 'ci-callback', agent: 'oncall', enabled: true },
        { id: 'w2', label: 'unbound-hook', agent: '', enabled: true },
      ],
    })
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await screen.findByText('oncall'))
    const list = await screen.findByTestId('member-wake-sources')
    expect(list).toHaveTextContent('nightly-triage')
    expect(list).toHaveTextContent('0 2 * * *')
    expect(list).toHaveTextContent('ci-callback')
    expect(list).not.toHaveTextContent('other-crew-job')
    expect(list).not.toHaveTextContent('script-job')
    expect(list).not.toHaveTextContent('unbound-hook')
  })

  it('a failed wake-sources fetch renders the error state, never the affirmative empty state', async () => {
    vi.mocked(api.crons).mockRejectedValue(new Error('boom'))
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await screen.findByText('oncall'))
    await screen.findByTestId('member-wake-error')
    // "Nothing wakes this member" would be a false statement about the member
    // when the request simply failed.
    expect(screen.queryByText(/nothing wakes this member/i)).toBeNull()
  })

  it('a saturated activity window renders counters as floors (N+), never exact claims', async () => {
    const now = Date.now() / 1000
    // Server capped the window and the OLDEST returned entry is still within
    // both counting windows — more in-window events exist beyond the cap.
    vi.mocked(api.memberActivity).mockResolvedValue({
      slug: 'oncall',
      member: 'oncall',
      capped: true,
      entries: [
        { ts: now - 60, via: 'chat', project: '' },
        { ts: now - 120, via: 'chat', project: '' },
      ],
    })
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await screen.findByText('oncall'))
    const stats = await screen.findByTestId('member-stats')
    await waitFor(() => expect(stats).toHaveTextContent('2+'))
  })

  it('a failed activity fetch renders the error state, never the affirmative empty state', async () => {
    vi.mocked(api.memberActivity).mockRejectedValue(new Error('boom'))
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await screen.findByText('oncall'))
    await screen.findByTestId('member-activity-error')
    expect(screen.queryByText(/no recorded activity/i)).toBeNull()
  })

  it('roster rows show the last message preview, not an Idle/Working label', async () => {
    await renderPage([
      row({ last_message: 'Six new issues triaged.' }),
      row({ name: 'quiet', slug: 'quiet' }),
    ])
    await screen.findByText('oncall')
    // The preview is the row's sub-line, like a session row. Presence rides
    // the avatar dot, so a textual status label must not come back.
    expect(screen.getByText('Six new issues triaged.')).toBeTruthy()
    expect(screen.queryByText(/^(idle|working)$/i)).toBeNull()
  })

  it('the presence dot renders only on running members — idle rows show no dot', async () => {
    await renderPage([
      row({ name: 'busy', slug: 'busy', running: true, bound: true, slot_key: 'member-busy' }),
      row({ name: 'idle-one', slug: 'idle-one' }),
    ])
    await screen.findByText('busy')
    // Exactly one dot: the running member's. An idle member renders nothing
    // where the dot would be, not a gray placeholder.
    expect(screen.getAllByTestId('member-presence-dot')).toHaveLength(1)
  })

  it('the search box filters the roster by name', async () => {
    await renderPage([
      row({ name: 'radar', slug: 'radar' }),
      row({ name: 'scribe', slug: 'scribe' }),
    ])
    await screen.findByText('radar')
    // SearchInput spreads props onto its inner <input>, so the testid IS the input.
    const box = screen.getByTestId('member-search') as HTMLInputElement
    fireEvent.change(box, { target: { value: 'scr' } })
    expect(screen.queryByText('radar')).toBeNull()
    expect(screen.getByText('scribe')).toBeTruthy()
    fireEvent.change(box, { target: { value: '' } })
    expect(screen.getByText('radar')).toBeTruthy()
  })
})

describe('MembersPage unread drain', () => {
  // The websocket unread-marker flags any slot that is not `chat.activeSlot`,
  // and this page never moves `chat.activeSlot` — so the page itself must
  // drain the mounted thread's unread flag, or the Crew Members rail badge is
  // permanent (nothing else clears a live member slot's unread).

  it('opening a flagged member thread drains its unread flag', async () => {
    const { store } = await renderPage()
    act(() => {
      store.dispatch(markSlotUnread('member-oncall'))
    })
    fireEvent.click(await screen.findByText('oncall'))
    await screen.findByTestId('chat-pane-stub')
    await waitFor(() =>
      expect(store.getState().dashboard.unreadSlots).not.toContain('member-oncall'),
    )
  })

  it('a live message re-flagging the MOUNTED thread is drained again, not left as a stuck badge', async () => {
    const { store } = await renderPage()
    fireEvent.click(await screen.findByText('oncall'))
    await screen.findByTestId('chat-pane-stub')
    // Simulate the websocket marker firing while the user is looking at the
    // thread (its check is against chat.activeSlot, which this page never sets).
    act(() => {
      store.dispatch(markSlotUnread('member-oncall'))
    })
    await waitFor(() =>
      expect(store.getState().dashboard.unreadSlots).not.toContain('member-oncall'),
    )
  })

  it('drains ONLY the mounted thread — other slots keep their unread flags', async () => {
    const { store } = await renderPage()
    act(() => {
      store.dispatch(markSlotUnread('member-research'))
      store.dispatch(markSlotUnread('chat-123'))
    })
    fireEvent.click(await screen.findByText('oncall'))
    await screen.findByTestId('chat-pane-stub')
    expect(store.getState().dashboard.unreadSlots).toEqual(
      expect.arrayContaining(['member-research', 'chat-123']),
    )
  })

  it('a flagged member shows the unread dot on its roster row; unflagged members do not', async () => {
    const { store } = await renderPage([
      row({ bound: true, slot_key: 'member-oncall' }),
      row({ name: 'scout', slug: 'scout' }),
    ])
    await screen.findByText('scout')
    expect(screen.queryByTestId('member-unread-dot')).toBeNull()
    act(() => {
      store.dispatch(markSlotUnread('member-oncall'))
    })
    // Exactly one dot: the flagged member's, not every row's.
    expect(screen.getAllByTestId('member-unread-dot')).toHaveLength(1)
  })

  it('opening the thread clears the roster dot along with the badge', async () => {
    const { store } = await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    act(() => {
      store.dispatch(markSlotUnread('member-oncall'))
    })
    expect(await screen.findByTestId('member-unread-dot')).toBeInTheDocument()
    fireEvent.click(await screen.findByText('oncall'))
    await screen.findByTestId('chat-pane-stub')
    await waitFor(() => expect(screen.queryByTestId('member-unread-dot')).toBeNull())
  })
})

describe('MembersPage drawer — driving sessions', () => {
  // The member operating model: the DM thread dispatches work into worker
  // sessions it opens (session_create) and steers (session_send). The backend
  // fences a member caller to the slots it created, so `created_by` on the
  // live slots frame IS the driven set — the drawer filters on it, no
  // endpoint, no transcript scraping.
  const worker = (key: string, overrides: Record<string, unknown> = {}) => ({
    key,
    title: `Worker ${key}`,
    messages: 3,
    running: false,
    created_by: 'member-oncall',
    created: '2026-09-04T10:00:00Z',
    last_turn_ts: '2026-09-04T12:00:00Z',
    ...overrides,
  })

  async function openDrawer(liveSlots: ReturnType<typeof worker>[]) {
    const utils = await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    act(() => {
      utils.store.dispatch(sseSlots(liveSlots as never))
    })
    fireEvent.click(await screen.findByText('oncall'))
    await screen.findByTestId('member-drawer')
    return utils
  }

  it('before the first slots frame it shows a skeleton, never the affirmative "not driving"', async () => {
    // No sseSlots dispatch: `slotsLoaded` is false, so an empty list is
    // ambiguous (cold open / WS reconnect) and must not read as a verdict.
    const { store } = await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await screen.findByText('oncall'))
    await screen.findByTestId('member-drawer')
    expect(screen.getByTestId('member-driving-loading')).toBeInTheDocument()
    expect(screen.queryByTestId('member-driving-empty')).toBeNull()
    // The first real snapshot (no worker of ours in it) settles the verdict.
    act(() => {
      store.dispatch(sseSlots([worker('chat-1-other', { created_by: 'member-research' })] as never))
    })
    await waitFor(() => expect(screen.getByTestId('member-driving-empty')).toBeInTheDocument())
    expect(screen.queryByTestId('member-driving-loading')).toBeNull()
  })

  it('renders the empty state when no live slot was created by the member', async () => {
    await openDrawer([
      // Someone else's worker and a person's own tab: neither belongs here.
      worker('chat-1-other', { created_by: 'member-research' }),
      worker('chat-1-own', { created_by: '' }),
    ])
    expect(screen.getByTestId('member-driving-empty')).toHaveTextContent(/not driving any sessions/i)
    expect(screen.queryByTestId('member-driving-row')).toBeNull()
  })

  it('lists only the sessions this member created, newest activity first, with the sidebar status vocabulary', async () => {
    await openDrawer([
      worker('chat-1-idle', { last_turn_ts: '2026-09-04T09:00:00Z' }),
      worker('chat-1-running', { running: true, last_turn_ts: '2026-09-04T11:00:00Z' }),
      worker('chat-1-approval', { running: true, pending_approval: true, last_turn_ts: '2026-09-04T12:00:00Z' }),
      worker('chat-1-input', { needs_input: true, last_turn_ts: '2026-09-04T10:00:00Z' }),
      worker('chat-1-foreign', { created_by: 'member-research', last_turn_ts: '2026-09-04T13:00:00Z' }),
    ])
    const rows = screen.getAllByTestId('member-driving-row')
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining('Worker chat-1-approval'),
      expect.stringContaining('Worker chat-1-running'),
      expect.stringContaining('Worker chat-1-input'),
      expect.stringContaining('Worker chat-1-idle'),
    ])
    // Approval outranks running (the sidebar's precedence): a running turn
    // parked on a tool gate is "needs approval", not "working".
    expect(rows.map((r) => r.getAttribute('data-status'))).toEqual(['permission', 'running', 'question', 'idle'])
    expect(rows[0]).toHaveTextContent(/needs approval/i)
    expect(rows[2]).toHaveTextContent(/needs your answer/i)
    expect(screen.queryByTestId('member-driving-empty')).toBeNull()
    expect(screen.queryByTestId('member-driving-toggle')).toBeNull()
  })

  it('a row is a jump into that session', async () => {
    await openDrawer([worker('chat-1-w')])
    fireEvent.click(screen.getByTestId('member-driving-row'))
    expect(navigateSpy).toHaveBeenCalledWith('/chat?sid=chat-1-w')
  })

  it('folds past five rows behind a Show-all toggle that expands and collapses', async () => {
    await openDrawer(Array.from({ length: 7 }, (_, i) => worker(`chat-1-w${i}`)))
    expect(screen.getAllByTestId('member-driving-row')).toHaveLength(5)
    const toggle = screen.getByTestId('member-driving-toggle')
    expect(toggle).toHaveTextContent('Show all (7)')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(toggle)
    expect(screen.getAllByTestId('member-driving-row')).toHaveLength(7)
    expect(toggle).toHaveTextContent(/show less/i)
    fireEvent.click(toggle)
    expect(screen.getAllByTestId('member-driving-row')).toHaveLength(5)
  })

  it('a worker closing (leaving the live slots) drops out of the list live', async () => {
    const { store } = await openDrawer([worker('chat-1-a'), worker('chat-1-b')])
    expect(screen.getAllByTestId('member-driving-row')).toHaveLength(2)
    act(() => {
      store.dispatch(sseSlots([worker('chat-1-a')] as never))
    })
    await waitFor(() => expect(screen.getAllByTestId('member-driving-row')).toHaveLength(1))
  })

  it('the two parked states are spoken as visible text and every row carries a hover title', async () => {
    await openDrawer([
      worker('chat-1-approval', { running: true, pending_approval: true }),
      worker('chat-1-running', { running: true, last_turn_ts: '2026-09-04T11:00:00Z' }),
    ])
    const [approval, running] = screen.getAllByTestId('member-driving-row')
    // Colour alone must not carry the owed decision: the label is visible text
    // (not sr-only) on the approval row, and hover restores the truncated title.
    expect(approval.querySelector('.sr-only')).toBeNull()
    expect(approval).toHaveTextContent(/needs approval/i)
    expect(approval).toHaveAttribute('title', expect.stringContaining('Worker chat-1-approval'))
    expect(approval).toHaveAttribute('title', expect.stringMatching(/needs approval/i))
    // Running stays dot-only in the row; its word lives in the title + for AT.
    expect(running.querySelector('.sr-only')).toHaveTextContent(/working/i)
    expect(running).toHaveAttribute('title', expect.stringMatching(/working/i))
  })

  it('the fold is per member: expanding one member does not leak into the next drawer opened', async () => {
    const utils = await renderPage([
      row({ bound: true, slot_key: 'member-oncall' }),
      row({ name: 'research', slug: 'research', bound: true, slot_key: 'member-research' }),
    ])
    // renderPage pins the thread endpoint to oncall's key; each member must
    // get its OWN key here or both drawers would read the same list.
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementation((slug: string) =>
      Promise.resolve({ slot_key: `member-${slug}`, slug, member: slug, created: false }),
    )
    act(() => {
      utils.store.dispatch(
        sseSlots([
          ...Array.from({ length: 6 }, (_, i) => worker(`chat-1-o${i}`)),
          ...Array.from({ length: 6 }, (_, i) => worker(`chat-1-r${i}`, { created_by: 'member-research' })),
        ] as never),
      )
    })
    fireEvent.click(await screen.findByText('oncall'))
    await screen.findByTestId('member-drawer')
    fireEvent.click(screen.getByTestId('member-driving-toggle'))
    expect(screen.getAllByTestId('member-driving-row')).toHaveLength(6)
    fireEvent.click(screen.getByText('research'))
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledWith('research'))
    await waitFor(() => expect(screen.getAllByTestId('member-driving-row')).toHaveLength(5))
    expect(screen.getByTestId('member-driving-toggle')).toHaveAttribute('aria-expanded', 'false')
  })
})

describe('MembersPage auto patrol (monitor loop status)', () => {
  // The auto-nudge loop bound to a member's own DM slot is what wakes a
  // standing member without anyone asking. The block reads the whole
  // registry (`GET /api/autonudge`) and filters on the member's slot key —
  // `member-<slug>` — so a loop on somebody else's slot must never show up
  // under this member.
  const loop = (overrides: Record<string, unknown> = {}) => ({
    id: 'loop-1',
    slot_key: 'member-oncall',
    message: 'Patrol the queue.\nSecond line the row must not show.',
    idle_secs: 1200,
    max_cycles: 24,
    cycle_count: 3,
    active: true,
    last_fire_ts: Date.now() / 1000 - 180,
    next_due_ts: Date.now() / 1000 + 900,
    banner: '',
    stopped_reason: '',
    ...overrides,
  })

  async function openDrawerWith(registry: { loops: unknown[] }) {
    ;(api.autonudgeList as ReturnType<typeof vi.fn>).mockResolvedValue({ enabled: true, ...registry })
    const utils = await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await screen.findByText('oncall'))
    await screen.findByTestId('member-drawer')
    await waitFor(() => expect(screen.queryByTestId('member-patrol-loading')).toBeNull())
    return utils
  }

  // `mockResolvedValue` / `mockRejectedValue` outlive `vi.clearAllMocks()`
  // (that clears calls, not implementations), so each case starts from the
  // module default rather than inheriting the previous case's registry.
  beforeEach(() => {
    ;(api.autonudgeList as ReturnType<typeof vi.fn>).mockResolvedValue({ enabled: true, loops: [] })
    // The wake-sources cases above leave `crons` REJECTING for the rest of the
    // file; the Wake sources assertions below need a good read.
    vi.mocked(api.crons).mockResolvedValue({ jobs: [] })
    vi.mocked(api.webhooks).mockResolvedValue({ tokens: [] })
  })

  it('an active loop renders as patrolling, with interval, cycles, last and next wake, and the banner-or-instruction line', async () => {
    await openDrawerWith({ loops: [loop()] })
    const block = screen.getByTestId('member-patrol')
    expect(block).toHaveAttribute('data-state', 'active')
    expect(screen.getByTestId('member-patrol-status')).toHaveTextContent(/patrolling/i)
    // Finite cap: self-describing in the drawer ("3 of 24"); the compact
    // "3/24" stays on the roster badge, where it has the tooltip's sentence.
    expect(screen.getByTestId('member-patrol-cycles')).toHaveTextContent('3 of 24')
    // Interval via the shared narrow-unit duration formatter (`20m`).
    expect(screen.getByTestId('member-patrol-interval')).toHaveTextContent('20m')
    // The value is the bare remainder ("Due in 14m 59s"): the row label already
    // says "Next wake", so the popover's full sentence would read doubled.
    expect(block).toHaveTextContent(/next wake/i)
    expect(screen.getByTestId('member-patrol-next')).toHaveTextContent(/^Due in \d/)
    expect(screen.getByTestId('member-patrol-next')).not.toHaveTextContent(/next cycle/i)
    // No banner: the instruction's FIRST line stands in, the rest is title-only.
    const instruction = screen.getByTestId('member-patrol-instruction')
    expect(instruction).toHaveTextContent('Patrol the queue.')
    expect(instruction).not.toHaveTextContent('Second line')
  })

  it('an unlimited cap says so instead of rendering a denominator of zero', async () => {
    await openDrawerWith({ loops: [loop({ max_cycles: 0, cycle_count: 61 })] })
    const cycles = screen.getByTestId('member-patrol-cycles')
    expect(cycles).toHaveTextContent('61')
    expect(cycles).toHaveTextContent(/no limit/i)
    expect(cycles).not.toHaveTextContent('61/0')
  })

  it('a banner, when set, is what the instruction row shows', async () => {
    await openDrawerWith({ loops: [loop({ banner: 'watching PR #123' })] })
    expect(screen.getByTestId('member-patrol-instruction')).toHaveTextContent('watching PR #123')
  })

  it('no loop on the member slot renders "no patrol scheduled" — and a loop on ANOTHER slot does not leak in', async () => {
    await openDrawerWith({ loops: [loop({ slot_key: 'member-research' }), loop({ slot_key: 'chat-1-abc' })] })
    expect(screen.getByTestId('member-patrol')).toHaveAttribute('data-state', 'none')
    expect(screen.getByTestId('member-patrol-status')).toHaveTextContent(/no patrol scheduled/i)
  })

  it('a stopped loop keeps its reason visible instead of collapsing into "no patrol scheduled"', async () => {
    // This is the failure the block exists for: a loop that hit its cycle
    // cap stops silently, and a page that reads that as "nothing scheduled"
    // hides the one fact that would have told someone the member is dead.
    await openDrawerWith({ loops: [loop({ active: false, stopped_reason: 'cycle_cap' })] })
    expect(screen.getByTestId('member-patrol')).toHaveAttribute('data-state', 'stopped')
    expect(screen.getByTestId('member-patrol-status')).toHaveTextContent(/patrol stopped/i)
    expect(screen.getByTestId('member-patrol-reason')).toHaveTextContent(/wake limit/i)
    expect(screen.queryByText(/no patrol scheduled/i)).toBeNull()
  })

  it('an active patrol is listed under Wake sources, so the card cannot say "nothing wakes this member" above a live one', async () => {
    await openDrawerWith({ loops: [loop()] })
    await waitFor(() => expect(screen.queryByTestId('member-wake-loading')).toBeNull())
    expect(screen.getByTestId('member-wake-patrol')).toHaveTextContent(/auto patrol/i)
    expect(screen.getByTestId('member-wake-patrol')).toHaveTextContent(/every 20m/i)
    expect(screen.queryByText(/nothing wakes this member/i)).toBeNull()
  })

  it('without a live patrol the Wake sources empty line still renders', async () => {
    await openDrawerWith({ loops: [loop({ active: false, stopped_reason: 'manual' })] })
    await waitFor(() => expect(screen.queryByTestId('member-wake-loading')).toBeNull())
    expect(screen.queryByTestId('member-wake-patrol')).toBeNull()
    expect(screen.getByText(/nothing wakes this member/i)).toBeInTheDocument()
  })

  it('a failed registry read renders the error state, never the affirmative empty state', async () => {
    ;(api.autonudgeList as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await screen.findByText('oncall'))
    await screen.findByTestId('member-drawer')
    // The shared ErrorNotice (structured context + agent hand-off), not a
    // hand-rolled alert box.
    const notice = await screen.findByTestId('member-patrol-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent(/patrol status/i)
    expect(screen.queryByTestId('member-patrol')).toBeNull()
    // The roster says so too: every badge is blank for an unknown reason,
    // which must not read as "no member has a patrol".
    expect(screen.getByTestId('member-roster-patrol-error')).toHaveAttribute('role', 'alert')
  })

  it('a failed registry read is announced on the roster even with no drawer open', async () => {
    ;(api.autonudgeList as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    await screen.findByText('oncall')
    expect(await screen.findByTestId('member-roster-patrol-error')).toHaveTextContent(/patrol status/i)
    expect(screen.queryByTestId('member-patrol-dot')).toBeNull()
  })

  it('the roster badge is accent on an active loop and warn on a stopped one, beside — not instead of — the presence dot', async () => {
    ;(api.autonudgeList as ReturnType<typeof vi.fn>).mockResolvedValue({
      enabled: true,
      loops: [
        loop({ slot_key: 'member-radar' }),
        loop({ id: 'loop-2', slot_key: 'member-scout', active: false, stopped_reason: 'cycle_cap' }),
      ],
    })
    await renderPage([
      row({ name: 'radar', slug: 'radar', bound: true, slot_key: 'member-radar', running: true }),
      row({ name: 'scout', slug: 'scout', bound: true, slot_key: 'member-scout' }),
      row({ name: 'scribe', slug: 'scribe', bound: true, slot_key: 'member-scribe' }),
    ])
    await screen.findByText('radar')
    // Two badges: radar's (active, accent) and scout's (stopped, warn — the
    // dead patrol must show at the roster, not only in the drawer). scribe
    // never armed one and shows nothing. The active badge carries the wake
    // readout for AT; the stopped one names the state.
    const badges = await screen.findAllByTestId('member-patrol-dot')
    expect(badges).toHaveLength(2)
    const byState = Object.fromEntries(badges.map((b) => [b.getAttribute('data-state'), b]))
    expect(byState.active).toHaveAttribute('aria-label', expect.stringMatching(/3 of 24/))
    expect(byState.stopped).toHaveAttribute('aria-label', expect.stringMatching(/patrol stopped/i))
    // Both signals on one avatar: patrol badge (top-right) AND presence dot
    // (bottom-right) — neither replaces the other.
    expect(screen.getAllByTestId('member-presence-dot')).toHaveLength(1)
  })

  it('the registry is a live React Query read: invalidating it (what the websocket hook does on every frame and reconnect) arms and disarms the badge', async () => {
    const { queryClient } = await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    await screen.findByText('oncall')
    await waitFor(() => expect(api.autonudgeList).toHaveBeenCalled())
    expect(screen.queryByTestId('member-patrol-dot')).toBeNull()
    ;(api.autonudgeList as ReturnType<typeof vi.fn>).mockResolvedValue({ enabled: true, loops: [loop()] })
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ['autonudge-loops'] })
    })
    expect(await screen.findByTestId('member-patrol-dot')).toBeInTheDocument()
    ;(api.autonudgeList as ReturnType<typeof vi.fn>).mockResolvedValue({ enabled: true, loops: [] })
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ['autonudge-loops'] })
    })
    // AnimatePresence keeps the badge for its exit tween — wait for removal.
    await waitFor(() => expect(screen.queryByTestId('member-patrol-dot')).toBeNull())
  })

  it('a refetch failure after a good read keeps the last verdict instead of flipping to the error state', async () => {
    const { queryClient } = await openDrawerWith({ loops: [loop()] })
    expect(screen.getByTestId('member-patrol')).toHaveAttribute('data-state', 'active')
    ;(api.autonudgeList as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ['autonudge-loops'] })
    })
    expect(screen.getByTestId('member-patrol')).toHaveAttribute('data-state', 'active')
    expect(screen.queryByTestId('member-patrol-error')).toBeNull()
  })
})
