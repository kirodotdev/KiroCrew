import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { markSlotUnread } from '../../store/dashboardSlice'

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

  it('toggles the drawer via the Details button', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await screen.findByText('oncall'))
    expect(await screen.findByTestId('member-drawer')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /details/i }))
    // AnimatePresence keeps the drawer mounted for the exit tween — wait for
    // the removal instead of asserting synchronously.
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
