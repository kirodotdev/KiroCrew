import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { renderWithProviders } from '../../test/helpers'
import { markSlotUnread, sseConnected, sseSlots } from '../../store/dashboardSlice'

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
    // The drawer's wake block reads the default crew through the shared
    // ['default-agent'] query (defaultAgentQuery), not the whole registry.
    defaultAgent: vi.fn(() => Promise.resolve({ default_agent: '' })),
    // The auto-patrol block and roster badge read the whole loop registry;
    // the default is "feature on, nothing armed" so every other case renders
    // the page without a loop in the way.
    autonudgeList: vi.fn(() => Promise.resolve({ enabled: true, loops: [] })),
    // The side panel's + menu gates its Summary row on this read; "disabled"
    // keeps the chat-style Summary row out of the menu so the Crew summary tab
    // is the one summary these cases see.
    sessionSummary: vi.fn(() => Promise.resolve({ enabled: false })),
  },
}))

/* The page now hosts the chat page's SidePanel. Its strip and + menu are what
 * these cases drive; the heavy tab BODIES (editors, terminals, previews) are
 * not, so they are stubbed the way the panel's own suites stub them
 * (test/sidePanelPinnedAlwaysPresent.test.tsx). Terminal is reported ENABLED
 * so the + menu case below can assert the per-chat Terminal row is offered on
 * a member DM. */
vi.mock('../chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../chat/FilesHomePanel', () => ({ default: () => null }))
vi.mock('../chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../../components/ArtifactPanel', () => ({ default: () => null }))
// The Browser body's IDENTITY is what one case below pins (the slot key the
// native WebContentsView is keyed by must not flip during a thread re-POST),
// so this stub exposes it instead of rendering nothing.
vi.mock('../../components/WebPreviewPanel', () => ({
  default: ({ sessionKey }: { sessionKey: string }) => (
    <div data-testid="web-preview-stub" data-session-key={sessionKey} />
  ),
}))
vi.mock('../../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../../components/CliPanel', () => ({
  default: () => null,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => true,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../../hooks/useDevMode', () => ({ useDevMode: () => false }))

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

/** Records every navigate() call AND performs it against the MemoryRouter, so
 *  the history tests below drive real entries (push/replace/pop) instead of
 *  asserting on a spy alone. */
const navigateSpy = vi.fn()
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>()
  const { useCallback } = await import('react')
  return {
    ...actual,
    useNavigate: () => {
      const real = actual.useNavigate()
      // Stable identity, like the real hook's: consumers may list it in deps.
      return useCallback(
        ((...args: unknown[]) => {
          navigateSpy(...args)
          ;(real as (...a: unknown[]) => void)(...args)
        }) as typeof real,
        [real],
      )
    },
  }
})

import { api } from '../../api/client'
import MembersPage, { CREW_SUMMARY_TAB_ID, MEMBERS_UNCONFIRMED_WITHHELD_VIEWS, MEMBERS_UNFED_VIEWS, MEMBERS_WITHHELD_VIEWS, panelSitsBeside, resolveDefaultMember } from './MembersPage'
import { __resetPanelTabs, VIEW_DATA_SOURCE } from '../../hooks/usePanelTabs'

/** The page's own memory key (mirrors the constant in MembersPage.tsx). */
const LAST_MEMBER_KEY = 'mc-members-last-member'

/** A window wide enough to dock the side panel BESIDE the thread (see
 *  panelSitsBeside): roster 264 + gaps 24 + shell reserve 560 + panel min 320
 *  = 1168. happy-dom's default is narrower, which would put every case in
 *  overlay mode with the panel closed. Narrow-window cases set their own. */
const WIDE_WINDOW = 1440
const NARROW_WINDOW = 1000
function setWindowWidth(px: number) {
  Object.defineProperty(window, 'innerWidth', { value: px, configurable: true, writable: true })
}

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

/** Echoes the requested slug back as the thread's member — the happy path for
 *  any roster, so auto-open on mount resolves cleanly for whichever member is
 *  first. Cases that need a collision or a failure pass `thread`. */
function echoThread(slug: string) {
  return Promise.resolve({ slot_key: 'member-' + slug, slug, member: slug, created: true })
}

/** Renders the page at the URL and lets the roster load. `thread` replaces
 *  the thread-endpoint mock BEFORE mount: the page opens a member on its own
 *  as soon as the roster is in, so a mock installed after render would miss
 *  that first POST. */
/**
 * Ceiling for a wait on the chat pane. `renderPage` returns once the roster
 * fetch has been ISSUED; the pane sits behind a real chain after that -- members
 * resolve, the roster commits, the auto-open effect POSTs `memberThread`, that
 * resolves, and the pane mounts. Under load (a shared host, coverage
 * instrumentation) that ran past the 1000ms default in one of four full runs; a
 * named ceiling, not a longer guess -- website/docs/testing.md.
 */
const PANE_READY = { timeout: 5000 }

async function renderPage(
  members = [row()],
  defaultAgent = 'kirocrew',
  { route = '/members', thread }: { route?: string; thread?: Record<string, unknown> | Error } = {},
) {
  ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({
    members,
    default_agent: defaultAgent,
  })
  const threadMock = api.memberThread as ReturnType<typeof vi.fn>
  if (thread instanceof Error) threadMock.mockRejectedValue(thread)
  else if (thread) threadMock.mockResolvedValue(thread)
  else threadMock.mockImplementation(echoThread)
  const utils = renderWithProviders(
    <>
      <MembersPage />
      <LocationProbe />
    </>,
    { route },
  )
  await waitFor(() => expect(api.members).toHaveBeenCalled())
  return utils
}

/** Exposes the router's current search string, so tests can assert the URL
 *  the page writes without reaching into MemoryRouter. */
function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="location-probe">{loc.pathname + loc.search}</div>
}
const currentUrl = () => screen.getByTestId('location-probe').textContent

/* The open member's name also renders in the thread header (and the Crew summary tab),
 * so a bare screen query by name is ambiguous once anything is open — and
 * something is open from the first paint now. Scope name lookups to the
 * roster column. */
const roster = () => within(screen.getByTestId('member-roster'))
const rosterRow = async (name: string) =>
  within(await screen.findByTestId('member-roster')).findByText(name)

beforeEach(() => {
  vi.clearAllMocks()
  // clearAllMocks keeps implementations, so a case that made the drawer's
  // fetches reject would leak its error alerts into the next one. Reinstall
  // the quiet defaults.
  vi.mocked(api.memberActivity).mockImplementation(() =>
    Promise.resolve({ slug: '', member: '', capped: false, entries: [] }),
  )
  vi.mocked(api.crons).mockImplementation(() => Promise.resolve({ jobs: [] }))
  vi.mocked(api.webhooks).mockImplementation(() => Promise.resolve({ tokens: [] }))
  vi.mocked(api.defaultAgent).mockImplementation(() => Promise.resolve({ default_agent: '' }))
  // The patrol cases make this registry read REJECT (mockRejectedValue also
  // outlives clearAllMocks); a leaked rejection renders the roster's patrol
  // error alert into every later case.
  vi.mocked(api.autonudgeList).mockImplementation(() => Promise.resolve({ enabled: true, loops: [] }))
  // The remembered member must not leak between cases.
  localStorage.clear()
  // The side panel's tab strip is a module-level, persisted store; a tab
  // opened in one case would otherwise be on the strip in the next.
  __resetPanelTabs()
  setWindowWidth(WIDE_WINDOW)
})

describe('MembersPage roster', () => {
  it('renders one row per member from the API', async () => {
    await renderPage([row(), row({ name: 'research', slug: 'research' })])
    expect(await rosterRow('oncall')).toBeInTheDocument()
    expect(roster().getByText('research')).toBeInTheDocument()
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
    // No roster to count: the header says so with a dash, never "0 members"
    // above a failure it would contradict.
    expect(screen.getByTestId('member-count')).toHaveTextContent('\u2014')
    expect(screen.getByTestId('member-count')).not.toHaveTextContent(/members/i)
  })
})

/* The roster is a React Query read (issue #9418). These cases pin what that
 * buys the user: a return to the page renders the CACHED roster and thread at
 * once — never the empty column, never the skeleton — while the network
 * refreshes behind; and a crew written anywhere else reaches the list through
 * the registry-prefix invalidation, in place. The page is unmounted and
 * remounted INSIDE one provider tree (rerender keeps the QueryClient), which
 * is exactly a navigation away and back. */
describe('MembersPage roster cache (React Query)', () => {
  const page = (
    <>
      <MembersPage />
      <LocationProbe />
    </>
  )

  it('a second mount renders the cached roster immediately and, inside the stale window, issues no request at all', async () => {
    const utils = await renderPage([row(), row({ name: 'research', slug: 'research' })])
    await rosterRow('research')
    expect(api.members).toHaveBeenCalledTimes(1)
    // Navigate away…
    utils.rerender(<LocationProbe />)
    expect(screen.queryByTestId('member-roster')).toBeNull()
    // …and back. The rows are there on the very first frame: no request has
    // had a chance to answer yet, so this can only be the cache.
    utils.rerender(page)
    expect(roster().getByText('oncall')).toBeInTheDocument()
    expect(roster().getByText('research')).toBeInTheDocument()
    expect(screen.queryByText(/No crew members yet/i)).toBeNull()
    // The roster carries its own 30s staleTime (membersRosterQuery), which
    // wins over the test client's 0: a return inside that window is served
    // from cache with NO refetch — that is the request the user stopped
    // paying for. The refresh-behind path is pinned by the invalidation case
    // below, and by the fixed staleTime through refetchOnMount.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20))
    })
    expect(api.members).toHaveBeenCalledTimes(1)
    expect(roster().getByText('oncall')).toBeInTheDocument()
  })

  it('a second mount mounts the cached thread at once; the repair POST is re-issued but never waited on', async () => {
    const utils = await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
    expect(api.memberThread).toHaveBeenCalledTimes(1)
    utils.rerender(<LocationProbe />)
    // The re-open's POST hangs forever: if the thread column waited on the
    // network, "Opening the conversation…" would be all it shows.
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}))
    utils.rerender(page)
    expect(await screen.findByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
    expect(screen.queryByText(/Opening the conversation/i)).toBeNull()
    // Every open still goes through the endpoint — the cache decides what to
    // render while the POST is out, it never replaces the POST.
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledTimes(2))
  })

  it('a failed repair over a cached thread keeps the thread up and says the RECONNECT failed, not the open', async () => {
    const utils = await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
    utils.rerender(<LocationProbe />)
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    utils.rerender(page)
    const notice = await screen.findByTestId('member-thread-error')
    expect(notice).toHaveTextContent(/Couldn't reconnect this conversation/i)
    // "Could not open" would contradict the conversation still rendered below.
    expect(notice).not.toHaveTextContent(/Could not open/i)
    expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
  })

  it('invalidating the crew-registry prefix (what the crew editor and the websocket hook do) refreshes the roster in place', async () => {
    const { queryClient } = await renderPage([row()])
    await rosterRow('oncall')
    ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({
      members: [row(), row({ name: 'research', slug: 'research' })],
      default_agent: 'kirocrew',
    })
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ['kirocrew-agents'] })
    })
    expect(await rosterRow('research')).toBeInTheDocument()
    // In place: the row that was already there never left the screen.
    expect(roster().getByText('oncall')).toBeInTheDocument()
    expect(screen.queryByText(/No crew members yet/i)).toBeNull()
  })

  it('a refetch failure after a good read keeps the last roster instead of flipping to the error state', async () => {
    const { queryClient } = await renderPage([row()])
    await rosterRow('oncall')
    ;(api.members as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ['kirocrew-agents'] })
    })
    await waitFor(() => expect(api.members).toHaveBeenCalledTimes(2))
    expect(roster().getByText('oncall')).toBeInTheDocument()
    expect(screen.queryByText(/Could not load the member roster/i)).toBeNull()
  })
})

describe('MembersPage thread', () => {
  it('opens the pinned DM thread on click: creates the thread and mounts the chat stack on its slot', async () => {
    await renderPage()
    fireEvent.click(await rosterRow('oncall'))
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledWith('oncall'))
    // The stub echoes the slot key: proves ChatPane received THE member slot,
    // not a fresh ordinary slot. Mutating the mounted key breaks this line.
    const pane = await screen.findByTestId('chat-pane-stub', PANE_READY)
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
    fireEvent.click(await rosterRow('oncall'))
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledWith('oncall'))
    expect(await screen.findByTestId('chat-pane-stub', PANE_READY)).toHaveTextContent('member-oncall')
  })

  it('surfaces a visible error when thread creation fails', async () => {
    // Installed BEFORE mount: the page opens the first member on its own, so
    // the failing POST is the auto-open itself.
    await renderPage([row()], 'kirocrew', { thread: new Error('409') })
    expect(
      await screen.findByText(/Could not open this member's conversation/i),
    ).toBeInTheDocument()
    expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
  })

  it('surfaces a slug collision instead of silently mounting another member thread', async () => {
    // Two crews folding to one slug: the endpoint attributes the thread to the
    // first-bound crew. Opening the OTHER one must not mount that thread.
    await renderPage(
      [row({ name: 'Oncall', slug: 'oncall' }), row({ name: 'oncall', slug: 'oncall' })],
      'kirocrew',
      { thread: { slot_key: 'member-oncall', slug: 'oncall', member: 'Oncall', created: false } },
    )
    fireEvent.click(await rosterRow('oncall'))
    expect(await screen.findByText(/shares its short name with/i)).toBeInTheDocument()
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
    // Let the page's own first open (alpha, first row) settle before queuing
    // the one-shot responses, so the re-click below is the call that hangs.
    expect(await screen.findByTestId('chat-pane-stub', PANE_READY)).toHaveTextContent('member-alpha')
    ;(api.memberThread as ReturnType<typeof vi.fn>)
      .mockReturnValueOnce(pendingA)
      .mockResolvedValueOnce({
        slot_key: 'member-beta',
        slug: 'beta',
        member: 'beta',
        created: true,
      })
    fireEvent.click(await rosterRow('alpha'))
    fireEvent.click(await rosterRow('beta'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-beta'))
    rejectA(new Error('late'))
    // The stale rejection lands in alpha's bucket; beta's view stays clean.
    await waitFor(() =>
      expect(screen.queryByText(/Could not open this member's conversation/i)).toBeNull(),
    )
    expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-beta')
  })
})

describe('MembersPage side panel (Crew summary tab) and edit jump', () => {
  it('shows the read-only config summary and the shared-memory disclosure', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall', model: 'claude-opus-5' })])
    fireEvent.click(await rosterRow('oncall'))
    const summary = await screen.findByTestId('member-crew-summary')
    expect(summary).toHaveTextContent('kirocrew')
    expect(summary).toHaveTextContent('claude-opus-5')
    expect(summary).toHaveTextContent(/share one memory/i)
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
    const summary = await screen.findByTestId('member-crew-summary')
    expect(summary).toHaveTextContent(/from the oncall-own store/i)
    expect(summary).toHaveTextContent(/not separated yet/i)
    expect(summary).toHaveTextContent(/still known to every member/i)
    expect(summary).not.toHaveTextContent(/share one memory/i)
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
    const summary = await screen.findByTestId('member-crew-summary')
    expect(summary).toHaveTextContent(/from the triage store/i)
    expect(summary).not.toHaveTextContent(/share one memory/i)
  })

  it('docks the chat SidePanel beside the thread on a wide window: permanent, no Details toggle, no close control', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    expect(await screen.findByTestId('member-crew-summary')).toBeInTheDocument()
    // The panel is part of the page while a member is open, like the roster:
    // nothing in the header opens or closes it, and its strip renders no
    // close control (the chat page's panel shows one because ChatPage passes
    // onClose; this page does not).
    expect(screen.queryByTestId('member-panel-toggle')).toBeNull()
    expect(screen.queryByRole('button', { name: /close panel/i })).toBeNull()
    // The strip is the SidePanel's: its own resize splitter (the same shared
    // handle the chat page drags) pins that the page mounted the real
    // component rather than a lookalike.
    expect(screen.getByRole('separator', { name: /resize/i })).toBeInTheDocument()
  })

  it('the Crew summary is the FIRST tab, selected by default, and has no close control', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-crew-summary')
    const tabs = screen.getAllByRole('tab')
    // Ahead of the pinned Changes / Artifacts / Files block, not merely present.
    expect(tabs[0]).toBe(screen.getByTestId('side-panel-leading-tab'))
    expect(tabs[0]).toHaveAttribute('aria-selected', 'true')
    expect(tabs[0]).toHaveAccessibleName(/crew summary/i)
    // Structure, not label: no nested button means no close (or transfer) control.
    expect(tabs[0].querySelectorAll('button')).toHaveLength(0)
    // The chat page's own Summary (session summary) is a different tab and
    // must not be what the strip opened on — the ids are distinct by contract.
    expect(CREW_SUMMARY_TAB_ID).not.toBe('summary')
  })

  it('selecting another tab swaps the body; the Crew summary comes back on its chip', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-crew-summary')
    // The pinned Artifacts view is always on the strip (SidePanel contract).
    fireEvent.click(screen.getByRole('tab', { name: 'Artifacts' }))
    await waitFor(() => expect(screen.queryByTestId('member-crew-summary')).toBeNull())
    fireEvent.click(screen.getByTestId('side-panel-leading-tab'))
    expect(await screen.findByTestId('member-crew-summary')).toBeInTheDocument()
  })

  it('the + menu offers the chat panel\'s per-chat Terminal on a member DM (a member thread is a chat slot)', async () => {
    const { store } = await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-crew-summary')
    // The WS slots frame has delivered the member slot's record (its project
    // is the shell's cwd) — the condition Terminal waits for.
    act(() => {
      store.dispatch(sseSlots([{ key: 'member-oncall', mode: 'member', running: false, messages: 0, project: '/srv/oncall' }] as never))
    })
    // Radix opens the dropdown on pointerdown (mouse), not click.
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'Open side panel tab' }),
      { button: 0, ctrlKey: false, pointerType: 'mouse' },
    )
    const menu = await screen.findByRole('menu')
    expect(within(menu).getByRole('menuitem', { name: 'Terminal' })).toBeInTheDocument()
    expect(within(menu).getByRole('menuitem', { name: 'Side Chat' })).toBeInTheDocument()
    // And the leading tab is never offered there: it is permanent, not a view.
    expect(within(menu).queryByRole('menuitem', { name: /crew summary/i })).toBeNull()
  })

  it('withholds the views this page cannot feed: no Changes chip, no Pins / Issues / Links / Summary rows', async () => {
    // The set itself is the contract the design lanes asked for: every view
    // fed by ChatPage-owned transcript indexes, plus the chat page's session
    // Summary (an indistinguishable sibling of the Crew summary chip).
    expect([...MEMBERS_UNFED_VIEWS].sort()).toEqual(['changes', 'issues', 'links', 'pins', 'summary'])
    // Nothing else is withheld once the thread is confirmed: Side chat is
    // offered (its draft persists in the chat-core store, and the selection
    // toolbar's Ask lands in it — MembersPage.sideChat.test.tsx).
    expect([...MEMBERS_WITHHELD_VIEWS].sort()).toEqual([...MEMBERS_UNFED_VIEWS].sort())
    // While the thread is unconfirmed EVERY classified view is withheld, plus
    // Terminal and app tabs — derived from the classification, so a new
    // ViewKind lands in this set without anyone listing it.
    expect([...MEMBERS_UNCONFIRMED_WITHHELD_VIEWS].sort()).toEqual(
      [...(Object.keys(VIEW_DATA_SOURCE) as string[]), 'terminal', 'app'].sort(),
    )
    expect(MEMBERS_UNCONFIRMED_WITHHELD_VIEWS).toEqual(expect.arrayContaining([...MEMBERS_WITHHELD_VIEWS]))
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-crew-summary')
    // Pinned block: Crew summary, Artifacts, Files — and NOT Changes.
    expect(screen.getAllByRole('tab').map((t) => t.getAttribute('aria-label'))).toEqual([
      'Crew summary', 'Artifacts', 'Files',
    ])
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'Open side panel tab' }),
      { button: 0, ctrlKey: false, pointerType: 'mouse' },
    )
    const menu = await screen.findByRole('menu')
    for (const name of ['Pins', 'Issues', 'Links', 'Summary']) {
      expect(within(menu).queryByRole('menuitem', { name })).toBeNull()
    }
  })

  it('Terminal waits for the slot RECORD, not just the confirmed key: no shell before the WS slots frame carries its cwd', async () => {
    // The thread POST answers before the `slots` frame that carries the slot's
    // project. A shell opened in that window would spawn with no cwd (the
    // backend's HOME fallback) and never re-root, so Terminal is withheld until
    // the record is present; the other slot-bound views are already offered.
    const { store } = await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-crew-summary')
    expect(await screen.findByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'Open side panel tab' }),
      { button: 0, ctrlKey: false, pointerType: 'mouse' },
    )
    let menu = await screen.findByRole('menu')
    expect(within(menu).queryByRole('menuitem', { name: 'Terminal' })).toBeNull()
    expect(within(menu).getByRole('menuitem', { name: 'Side Chat' })).toBeInTheDocument()
    fireEvent.keyDown(menu, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    // The frame lands: Terminal is offered.
    act(() => {
      store.dispatch(sseSlots([{ key: 'member-oncall', mode: 'member', running: false, messages: 0 }] as never))
    })
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'Open side panel tab' }),
      { button: 0, ctrlKey: false, pointerType: 'mouse' },
    )
    menu = await screen.findByRole('menu')
    expect(within(menu).getByRole('menuitem', { name: 'Terminal' })).toBeInTheDocument()
  })

  it('a slot record left over from before a reconnect does not root the panel: Terminal waits for the fresh snapshot', async () => {
    // A reconnect drops `slotsLoaded` but keeps the pre-disconnect records
    // until the fresh frame lands. A record present in that window may name
    // the project the key had BEFORE a restart, so a shell spawned (or a file
    // saved) against it would land in the wrong workspace. The record must
    // come from the current snapshot: withheld while unloaded, offered once
    // the frame arrives — even when it is byte-identical.
    const { store } = await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-crew-summary')
    act(() => {
      store.dispatch(sseSlots([{ key: 'member-oncall', mode: 'member', running: false, messages: 0, project: '/srv/old' }] as never))
    })
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'Open side panel tab' }),
      { button: 0, ctrlKey: false, pointerType: 'mouse' },
    )
    let menu = await screen.findByRole('menu')
    expect(within(menu).getByRole('menuitem', { name: 'Terminal' })).toBeInTheDocument()
    fireEvent.keyDown(menu, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    // Reconnect: the stale record survives in the store, but is no longer a
    // current snapshot.
    act(() => { store.dispatch(sseConnected()) })
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'Open side panel tab' }),
      { button: 0, ctrlKey: false, pointerType: 'mouse' },
    )
    menu = await screen.findByRole('menu')
    expect(within(menu).queryByRole('menuitem', { name: 'Terminal' })).toBeNull()
    expect(within(menu).getByRole('menuitem', { name: 'Side Chat' })).toBeInTheDocument()
    fireEvent.keyDown(menu, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    // The fresh frame lands: bound again, to the record it carries.
    act(() => {
      store.dispatch(sseSlots([{ key: 'member-oncall', mode: 'member', running: false, messages: 0, project: '/srv/new' }] as never))
    })
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'Open side panel tab' }),
      { button: 0, ctrlKey: false, pointerType: 'mouse' },
    )
    menu = await screen.findByRole('menu')
    expect(within(menu).getByRole('menuitem', { name: 'Terminal' })).toBeInTheDocument()
  })

  it('a re-open with a cached key keeps the panel UNBOUND while its POST is in flight, then rebinds on confirmation', async () => {
    // First open confirms `member-oncall`. The repair re-click re-POSTs; until
    // that answer lands the cached key is only the thread column's render
    // hint — the panel offers no slot-bound view, so nothing can be dispatched
    // against a key the endpoint may be about to refuse.
    let resolveRepost: (v: unknown) => void = () => {}
    const pending = new Promise((resolve) => { resolveRepost = resolve })
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    expect(await screen.findByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
    await screen.findByRole('tab', { name: 'Artifacts' })
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockReturnValueOnce(pending)
    fireEvent.click(await rosterRow('oncall'))
    // In flight: thread still renders the cached key, panel is summary-only.
    await waitFor(() =>
      expect(screen.getAllByRole('tab').map((t) => t.getAttribute('aria-label'))).toEqual(['Crew summary']),
    )
    expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
    // Confirmed: the slot-bound views return.
    act(() => { resolveRepost({ slot_key: 'member-oncall', slug: 'oncall', member: 'oncall', created: false }) })
    await screen.findByRole('tab', { name: 'Artifacts' })
  })

  it('a thread re-POST withholds the Browser view but never re-keys its body: the native view stays mounted on the same slot', async () => {
    // A Browser tab's body is a native WebContentsView keyed by the slot the
    // panel hands it. The revalidation window (a routine WS reconnect
    // re-POSTs) must HIDE slot-bound views, not re-key them: a body keyed to
    // '' and back would close its WebContentsView and lose browsing history
    // and form state. So the panel's `slot` is the strip's bucket key (the
    // last confirmed key), steady through the window; only the strip and the
    // + menu (hiddenViews) react to the in-flight state.
    let resolveRepost: (v: unknown) => void = () => {}
    const pending = new Promise((resolve) => { resolveRepost = resolve })
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    expect(await screen.findByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
    await screen.findByRole('tab', { name: 'Artifacts' })
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'Open side panel tab' }),
      { button: 0, ctrlKey: false, pointerType: 'mouse' },
    )
    const menu = await screen.findByRole('menu')
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Browser' }))
    const body = await screen.findByTestId('web-preview-stub')
    expect(body).toHaveAttribute('data-session-key', 'member-oncall')
    // Re-open with the POST hanging: the Browser CHIP is withheld…
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockReturnValueOnce(pending)
    fireEvent.click(await rosterRow('oncall'))
    await waitFor(() =>
      expect(screen.getAllByRole('tab').map((t) => t.getAttribute('aria-label'))).toEqual(['Crew summary']),
    )
    // …while its BODY is the same mounted element, on the same key — not
    // unmounted, not re-keyed to the empty slot.
    expect(screen.getByTestId('web-preview-stub')).toBe(body)
    expect(body).toHaveAttribute('data-session-key', 'member-oncall')
    act(() => { resolveRepost({ slot_key: 'member-oncall', slug: 'oncall', member: 'oncall', created: false }) })
    await screen.findByRole('tab', { name: 'Browser' })
    expect(screen.getByTestId('web-preview-stub')).toBe(body)
    expect(body).toHaveAttribute('data-session-key', 'member-oncall')
  })

  it('a stored tab focus survives the re-open round-trip: Artifacts stays focused, not reset to Crew summary', async () => {
    // While the re-open POST is in flight every slot view is withheld and the
    // strip falls back to the Crew summary — but that fallback must not be
    // written into the bucket, or every switch would wipe the user's focus.
    let resolveRepost: (v: unknown) => void = () => {}
    const pending = new Promise((resolve) => { resolveRepost = resolve })
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-crew-summary')
    fireEvent.click(screen.getByRole('tab', { name: 'Artifacts' }))
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Artifacts' })).toHaveAttribute('aria-selected', 'true'))
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockReturnValueOnce(pending)
    fireEvent.click(await rosterRow('oncall'))
    // In flight: summary shown as the fallback (and its body loads).
    expect(await screen.findByTestId('member-crew-summary')).toBeInTheDocument()
    act(() => { resolveRepost({ slot_key: 'member-oncall', slug: 'oncall', member: 'oncall', created: false }) })
    // Confirmed: the stored focus is back on Artifacts, untouched by the fallback.
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Artifacts' })).toHaveAttribute('aria-selected', 'true'))
    expect(screen.queryByTestId('member-crew-summary')).toBeNull()
  })

  it('a STALE success cannot rebind a key the latest refusal unbound: only the newest POST writes', async () => {
    // Two re-clicks on a slow link: the first POST hangs, the second answers
    // 409 (the key is foreign now) and unbinds the panel. When the first
    // finally resolves with the old key it is dropped whole — the panel stays
    // unbound rather than silently pointing at the refused session. The thread
    // column keeps the cached conversation up under its reconnect notice (the
    // page's own failed-repair contract); it is the PANEL that must not bind.
    let resolveFirst: (v: unknown) => void = () => {}
    const first = new Promise((resolve) => { resolveFirst = resolve })
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    expect(await screen.findByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
    ;(api.memberThread as ReturnType<typeof vi.fn>)
      .mockReturnValueOnce(first)
      .mockRejectedValueOnce(new Error('409'))
    fireEvent.click(await rosterRow('oncall'))
    fireEvent.click(await rosterRow('oncall'))
    expect(await screen.findByTestId('member-thread-error')).toHaveTextContent(/Couldn't reconnect/i)
    await waitFor(() =>
      expect(screen.getAllByRole('tab').map((t) => t.getAttribute('aria-label'))).toEqual(['Crew summary']),
    )
    act(() => { resolveFirst({ slot_key: 'member-oncall', slug: 'oncall', member: 'oncall', created: false }) })
    // Still unbound after the stale answer: the refusal stands, no slot-bound views.
    await act(async () => { await new Promise((r) => setTimeout(r, 20)) })
    expect(screen.getByTestId('member-thread-error')).toHaveTextContent(/Couldn't reconnect/i)
    expect(screen.getAllByRole('tab').map((t) => t.getAttribute('aria-label'))).toEqual(['Crew summary'])
    expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
  })

  it('binds the panel to the POST-confirmed slot only: a rejected thread leaves every slot-bound view withheld', async () => {
    // The roster binding says `member-oncall`, but the thread endpoint refuses
    // (a stale binding whose canonical key an ordinary slot now occupies). The
    // panel must not aim Side chat / Artifacts / Files at that occupant: with no
    // confirmed slot, only the slot-free Crew summary is on the strip and the
    // + menu offers nothing slot-bound.
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })], 'kirocrew', { thread: new Error('409') })
    await screen.findByText(/Could not open this member's conversation/i)
    expect(await screen.findByTestId('member-crew-summary')).toBeInTheDocument()
    expect(screen.getAllByRole('tab').map((t) => t.getAttribute('aria-label'))).toEqual(['Crew summary'])
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'Open side panel tab' }),
      { button: 0, ctrlKey: false, pointerType: 'mouse' },
    )
    const menu = await screen.findByRole('menu')
    for (const name of ['Side Chat', 'Browser', 'Artifacts', 'Files', 'Subagents', 'Workflows', 'Git', 'Terminal']) {
      expect(within(menu).queryByRole('menuitem', { name })).toBeNull()
    }
    // Terminal too: while unconfirmed the strip lives in the shared no-slot
    // bucket, and a PTY opened there would be orphaned when the confirmation
    // re-keys the strip to the member's slot.
  })

  it('a refused re-open UNBINDS the panel while the cached thread stays up under the reconnect notice', async () => {
    // First open confirms `member-oncall`; the repair re-click then gets a 409
    // (the canonical key now belongs to a session that is not this member's).
    // Keeping the cached key for the panel would leave its views aimed at that
    // foreign session, so the refusal clears the PANEL binding. The thread
    // column is the other contract: it keeps the conversation the user is
    // looking at and says the reconnect failed, not the open.
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    expect(await screen.findByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
    expect(screen.getByRole('tab', { name: 'Artifacts' })).toBeInTheDocument()
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('409'))
    fireEvent.click(await rosterRow('oncall'))
    expect(await screen.findByTestId('member-thread-error')).toHaveTextContent(/Couldn't reconnect/i)
    await waitFor(() =>
      expect(screen.getAllByRole('tab').map((t) => t.getAttribute('aria-label'))).toEqual(['Crew summary']),
    )
    expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
  })

  it('an overlay opened on a narrow window does not lie in wait: docking resets it, so re-narrowing finds it closed', async () => {
    setWindowWidth(NARROW_WINDOW)
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('chat-pane-stub')
    fireEvent.click(screen.getByTestId('member-panel-toggle'))
    expect(await screen.findByTestId('member-crew-summary')).toBeInTheDocument()
    // Widen: the panel docks (no toggle, no close control)…
    setWindowWidth(WIDE_WINDOW)
    fireEvent(window, new Event('resize'))
    await waitFor(() => expect(screen.queryByTestId('member-panel-toggle')).toBeNull())
    expect(screen.queryByRole('button', { name: /close panel/i })).toBeNull()
    // …and narrowing again finds the overlay CLOSED, not popped back over the thread.
    setWindowWidth(NARROW_WINDOW)
    fireEvent(window, new Event('resize'))
    await waitFor(() => expect(screen.getByTestId('member-panel-toggle')).toBeInTheDocument())
    await waitFor(() => expect(screen.queryByTestId('member-crew-summary')).toBeNull())
  })

  it('narrow window: the panel becomes an overlay the Details button opens and its own close control dismisses', async () => {
    setWindowWidth(NARROW_WINDOW)
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('chat-pane-stub')
    // Closed by default: an always-open overlay would cover the thread.
    expect(screen.queryByTestId('member-crew-summary')).toBeNull()
    fireEvent.click(screen.getByTestId('member-panel-toggle'))
    expect(await screen.findByTestId('member-crew-summary')).toBeInTheDocument()
    // An overlay MUST be dismissable, so here the strip does render the close
    // control the docked column omits.
    fireEvent.click(screen.getByRole('button', { name: /close panel/i }))
    // AnimatePresence keeps the overlay mounted for the exit tween — wait for
    // the removal instead of asserting synchronously.
    await waitFor(() => expect(screen.queryByTestId('member-crew-summary')).toBeNull())
  })

  it('the overlay is a full-bleed scrim: clicking the dimmed chat closes it, clicking inside the panel does not', async () => {
    setWindowWidth(NARROW_WINDOW)
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('chat-pane-stub')
    fireEvent.click(screen.getByTestId('member-panel-toggle'))
    const overlay = await screen.findByTestId('member-side-panel')
    expect(overlay).toHaveAttribute('data-placement', 'overlay')
    // Inside the panel: no dismissal.
    fireEvent.click(screen.getByTestId('member-crew-summary'))
    expect(screen.getByTestId('member-crew-summary')).toBeInTheDocument()
    // On the scrim itself: dismissed.
    fireEvent.click(overlay)
    await waitFor(() => expect(screen.queryByTestId('member-crew-summary')).toBeNull())
  })

  it('panelSitsBeside: the docking boundary is the shell reserve + roster + gaps + panel minimum', () => {
    // 560 (rail + chat minimum) + 320 (panel min) + 264 (roster) + 24 (gaps) = 1168.
    expect(panelSitsBeside({ winW: 1168, rosterW: 264, isMobile: false })).toBe(true)
    expect(panelSitsBeside({ winW: 1167, rosterW: 264, isMobile: false })).toBe(false)
    // A wider roster needs a wider window; mobile never docks.
    expect(panelSitsBeside({ winW: 1168, rosterW: 300, isMobile: false })).toBe(false)
    expect(panelSitsBeside({ winW: 2000, rosterW: 264, isMobile: true })).toBe(false)
  })

  it('the edit affordance lives in the Crew summary tab only and navigates to this member\'s editor in the crew manager', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-crew-summary')
    // Edit is a rare secondary action: it must NOT be a header-level peer.
    expect(screen.queryByTestId('member-edit-jump')).toBeNull()
    // Mutation check: the assertion is on the DESTINATION (explicit ?tab=crews
    // beats CapabilitiesPage's remembered last tab, and ?crew=<name> opens
    // THIS member's editor rather than the roster), so retargeting the jump
    // anywhere else fails here.
    for (const btn of screen.getAllByRole('button', { name: /edit in crew manager/i })) {
      fireEvent.click(btn)
      expect(navigateSpy).toHaveBeenCalledWith('/capabilities?tab=crews&crew=oncall')
      navigateSpy.mockClear()
    }
  })

  it('the roster header has an add-member entry that lands on the crew manager\'s create form', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    await rosterRow('oncall')
    // Adding a member IS creating a crew; the crew manager stays the only
    // write path, so the entry is a navigation (destination pinned with the
    // explicit ?tab=crews, same as the edit affordance). It opens the create
    // form directly — `new=1` — not the crew list a second click would be
    // needed on (#9513), and names its origin so the create can return here.
    fireEvent.click(screen.getByTestId('member-add'))
    expect(navigateSpy).toHaveBeenCalledWith('/capabilities?tab=crews&new=1&from=members')
  })

  it('the empty roster\'s call to action lands on the same create form as the header "+"', async () => {
    await renderPage([])
    const cta = await screen.findByTestId('member-empty-cta')
    expect(cta).toHaveTextContent('Add member')
    fireEvent.click(cta)
    expect(navigateSpy).toHaveBeenCalledWith('/capabilities?tab=crews&new=1&from=members')
  })

  it('the Crew summary renders the recorded activity timeline and honest counters derived from it', async () => {
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
    fireEvent.click(await rosterRow('oncall'))
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

  it('the Crew summary lists wake sources filtered to the member, via the shared predicates', async () => {
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
    fireEvent.click(await rosterRow('oncall'))
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
    fireEvent.click(await rosterRow('oncall'))
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
    fireEvent.click(await rosterRow('oncall'))
    const stats = await screen.findByTestId('member-stats')
    await waitFor(() => expect(stats).toHaveTextContent('2+'))
  })

  it('a failed activity fetch renders the error state, never the affirmative empty state', async () => {
    vi.mocked(api.memberActivity).mockRejectedValue(new Error('boom'))
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-activity-error')
    expect(screen.queryByText(/no recorded activity/i)).toBeNull()
  })

  it('roster rows show the last message preview, not an Idle/Working label', async () => {
    await renderPage([
      row({ last_message: 'Six new issues triaged.' }),
      row({ name: 'quiet', slug: 'quiet' }),
    ])
    await rosterRow('oncall')
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
    await rosterRow('busy')
    // Exactly one dot: the running member's. An idle member renders nothing
    // where the dot would be, not a gray placeholder.
    expect(screen.getAllByTestId('member-presence-dot')).toHaveLength(1)
  })

  it('the search box filters the roster by name', async () => {
    await renderPage([
      row({ name: 'radar', slug: 'radar' }),
      row({ name: 'scribe', slug: 'scribe' }),
    ])
    await rosterRow('radar')
    // SearchInput spreads props onto its inner <input>, so the testid IS the input.
    const box = screen.getByTestId('member-search') as HTMLInputElement
    fireEvent.change(box, { target: { value: 'scr' } })
    expect(roster().queryByText('radar')).toBeNull()
    expect(roster().getByText('scribe')).toBeTruthy()
    fireEvent.change(box, { target: { value: '' } })
    expect(roster().getByText('radar')).toBeTruthy()
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
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('chat-pane-stub', PANE_READY)
    await waitFor(() =>
      expect(store.getState().dashboard.unreadSlots).not.toContain('member-oncall'),
    )
  })

  it('a live message re-flagging the MOUNTED thread is drained again, not left as a stuck badge', async () => {
    const { store } = await renderPage()
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('chat-pane-stub', PANE_READY)
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
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('chat-pane-stub', PANE_READY)
    expect(store.getState().dashboard.unreadSlots).toEqual(
      expect.arrayContaining(['member-research', 'chat-123']),
    )
  })

  it('a flagged member shows the unread dot on its roster row; unflagged members do not', async () => {
    // Land on scout, so oncall's flag is a genuine unread on a CLOSED thread
    // (the open thread drains its own flag on arrival).
    localStorage.setItem(LAST_MEMBER_KEY, 'scout')
    const { store } = await renderPage([
      row({ bound: true, slot_key: 'member-oncall' }),
      row({ name: 'scout', slug: 'scout' }),
    ])
    expect(await screen.findByTestId('chat-pane-stub', PANE_READY)).toHaveTextContent('member-scout')
    expect(screen.queryByTestId('member-unread-dot')).toBeNull()
    act(() => {
      store.dispatch(markSlotUnread('member-oncall'))
    })
    // Exactly one dot: the flagged member's, not every row's.
    expect(screen.getAllByTestId('member-unread-dot')).toHaveLength(1)
  })

  it('opening the thread clears the roster dot along with the badge', async () => {
    localStorage.setItem(LAST_MEMBER_KEY, 'scout')
    const { store } = await renderPage([
      row({ bound: true, slot_key: 'member-oncall' }),
      row({ name: 'scout', slug: 'scout' }),
    ])
    expect(await screen.findByTestId('chat-pane-stub', PANE_READY)).toHaveTextContent('member-scout')
    act(() => {
      store.dispatch(markSlotUnread('member-oncall'))
    })
    expect(await screen.findByTestId('member-unread-dot')).toBeInTheDocument()
    fireEvent.click(await rosterRow('oncall'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall'))
    await waitFor(() => expect(screen.queryByTestId('member-unread-dot')).toBeNull())
  })
})

describe('MembersPage Crew summary — driving sessions', () => {
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
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-crew-summary')
    return utils
  }

  it('before the first slots frame it shows a skeleton, never the affirmative "not driving"', async () => {
    // No sseSlots dispatch: `slotsLoaded` is false, so an empty list is
    // ambiguous (cold open / WS reconnect) and must not read as a verdict.
    const { store } = await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-crew-summary')
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

  it('a slots frame never reorders the list; a change to the driven set re-sorts it', async () => {
    // Each row navigates, so a row that moves between aim and click sends the
    // reader into a DIFFERENT session — and `lastActivityEpoch` advances on
    // every frame from a worker that is merely working.
    const { store } = await openDrawer([
      worker('chat-1-a', { last_turn_ts: '2026-09-04T12:00:00Z' }),
      worker('chat-1-b', { last_turn_ts: '2026-09-04T11:00:00Z' }),
    ])
    // `textContent` concatenates the title with the status words, so read the
    // key off the row's title attribute ("Worker <key>" + separator + label).
    const keys = () =>
      screen
        .getAllByTestId('member-driving-row')
        .map((r) => r.getAttribute('title')?.split(' ')[1])
    expect(keys()).toEqual(['chat-1-a', 'chat-1-b'])
    // b becomes the most recently active AND starts running: the status dot must
    // update in place while the row stays where the reader last saw it.
    act(() => {
      store.dispatch(
        sseSlots([
          worker('chat-1-a', { last_turn_ts: '2026-09-04T12:00:00Z' }),
          worker('chat-1-b', { running: true, last_turn_ts: '2026-09-04T13:30:00Z' }),
        ] as never),
      )
    })
    await waitFor(() =>
      expect(screen.getAllByTestId('member-driving-row')[1]).toHaveAttribute('data-status', 'running'),
    )
    expect(keys()).toEqual(['chat-1-a', 'chat-1-b'])
    // A new worker opens — the driven set changed, so the whole list re-sorts by
    // recency at once rather than appending out of order.
    act(() => {
      store.dispatch(
        sseSlots([
          worker('chat-1-a', { last_turn_ts: '2026-09-04T12:00:00Z' }),
          worker('chat-1-b', { running: true, last_turn_ts: '2026-09-04T13:30:00Z' }),
          worker('chat-1-c', { last_turn_ts: '2026-09-04T13:00:00Z' }),
        ] as never),
      )
    })
    await waitFor(() => expect(screen.getAllByTestId('member-driving-row')).toHaveLength(3))
    expect(keys()).toEqual(['chat-1-b', 'chat-1-c', 'chat-1-a'])
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

  it('the fold is per member: expanding one member does not leak into the next summary opened', async () => {
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
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-crew-summary')
    fireEvent.click(screen.getByTestId('member-driving-toggle'))
    expect(screen.getAllByTestId('member-driving-row')).toHaveLength(6)
    fireEvent.click(await rosterRow('research'))
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
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-crew-summary')
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
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-crew-summary')
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

  it('a failed registry read is announced on the roster even with no summary open', async () => {
    ;(api.autonudgeList as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    await rosterRow('oncall')
    expect(await screen.findByTestId('member-roster-patrol-error')).toHaveTextContent(/patrol status/i)
    expect(screen.queryByTestId('member-patrol-dot')).toBeNull()
  })

  it('the roster badge renders only for an ACTIVE loop — a stopped loop shows no badge, beside — not instead of — the presence dot', async () => {
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
    await rosterRow('radar')
    // ONE badge: radar's (active, accent, carrying the wake readout for AT).
    // scout's loop has stopped and scribe never armed one — both show
    // nothing at the roster: "not patrolling" is a member's resting state,
    // and a standing mark on it read as an error. The stopped loop's reason
    // lives in the drawer block (tested above), not on the avatar.
    const badges = await screen.findAllByTestId('member-patrol-dot')
    expect(badges).toHaveLength(1)
    expect(badges[0]).toHaveAttribute('data-state', 'active')
    expect(badges[0]).toHaveAttribute('aria-label', expect.stringMatching(/3 of 24/))
    expect(badges[0].closest('li')).toHaveTextContent('radar')
    expect(screen.queryByTitle(/patrol stopped/i)).toBeNull()
    // Both signals on one avatar: patrol badge (top-right) AND presence dot
    // (bottom-right) — neither replaces the other.
    expect(screen.getAllByTestId('member-presence-dot')).toHaveLength(1)
  })

  it('a loop that stops in place (registry re-read flips active to false) drops the badge instead of recolouring it', async () => {
    ;(api.autonudgeList as ReturnType<typeof vi.fn>).mockResolvedValue({ enabled: true, loops: [loop()] })
    const { queryClient } = await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    await rosterRow('oncall')
    expect(await screen.findByTestId('member-patrol-dot')).toHaveAttribute('data-state', 'active')
    ;(api.autonudgeList as ReturnType<typeof vi.fn>).mockResolvedValue({
      enabled: true,
      loops: [loop({ active: false, stopped_reason: 'manual' })],
    })
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ['autonudge-loops'] })
    })
    // AnimatePresence keeps the badge for its exit tween — wait for removal.
    // No 'stopped' badge ever appears in between.
    await waitFor(() => expect(screen.queryByTestId('member-patrol-dot')).toBeNull())
    expect(screen.queryByTitle(/patrol stopped/i)).toBeNull()
  })

  it('the registry is a live React Query read: invalidating it (what the websocket hook does on every frame and reconnect) arms and disarms the badge', async () => {
    const { queryClient } = await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    await rosterRow('oncall')
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

describe('MembersPage member edit entry (issue #9425)', () => {
  const EDIT_LINK = '/capabilities?tab=crews&crew=oncall'

  beforeEach(() => { localStorage.clear() })

  it('the DM header carries a pencil right of the name, named "Edit member", that opens this member\'s editor', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    const btn = await screen.findByTestId('member-edit-name-button')
    expect(btn.tagName).toBe('BUTTON')
    // The label names what the click does — the whole editor, not the builder.
    expect(btn).toHaveAccessibleName('Edit member')
    expect(btn).toHaveAttribute('title', 'Edit member')
    expect(btn.querySelector('svg')).not.toBeNull()
    // It sits INSIDE the title row, right AFTER the name — never a
    // header-level peer (docked wide, the header carries no panel control at
    // all; the panel is a permanent column).
    const titleRow = screen.getByTestId('member-title-row')
    expect(titleRow).toContainElement(btn)
    expect(titleRow.textContent).toContain('oncall')
    const nameEl = within(titleRow).getByText('oncall')
    expect(nameEl.compareDocumentPosition(btn) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.queryByTestId('member-panel-toggle')).toBeNull()
    fireEvent.click(btn)
    // Mutation check on the DESTINATION: this page never writes — the crew
    // manager opens THIS crew's editor. No `&avatar=1`: the builder is one
    // row inside that editor, not where an "edit this member" click lands.
    expect(navigateSpy).toHaveBeenCalledWith(EDIT_LINK)
    expect(navigateSpy).not.toHaveBeenCalledWith(expect.stringContaining('avatar=1'))
  })

  it('the pencil is invisible at rest, revealed by hovering the title row or by focus, and low-contrast-persistent on touch', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    const btn = await screen.findByTestId('member-edit-name-button')
    const cls = btn.className
    expect(cls).toContain('opacity-0')
    expect(cls).toContain('group-hover/title:opacity-100')
    expect(cls).toContain('focus-visible:opacity-100')
    // Reveal is scoped to the TITLE row, so hovering the panel toggle (overlay
    // mode) to the right does not summon it.
    expect(screen.getByTestId('member-title-row').className).toContain('group/title')
    // Transition present, deferring to prefers-reduced-motion.
    expect(cls).toContain('transition-opacity')
    expect(cls).toContain('motion-reduce:transition-none')
    // No hover on touch: the pencil stays, dimmed, instead of never appearing.
    expect(cls).toContain('[@media(hover:none)]:opacity-60')
  })

  it('the chat surface\'s avatar is just an avatar: no scrim, no badge, no chip, no text "Edit avatar" button', async () => {
    // The #9116 shapes the user rejected: the face wrapped as an "Edit avatar"
    // button, a full-width "Edit avatar" text button in the summary and an
    // "Edit this avatar" chip beside the header face. The default-face
    // fixture (`{}`) is exactly the one that used to summon the chip.
    await renderPage([row({ bound: true, slot_key: 'member-oncall', avatar: {} })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-crew-summary')
    expect(screen.queryByTestId('member-avatar-button')).toBeNull()
    expect(screen.queryByTestId('avatar-edit-scrim')).toBeNull()
    expect(screen.queryByTestId('avatar-edit-badge')).toBeNull()
    expect(screen.queryByTestId('avatar-edit-hint')).toBeNull()
    expect(screen.queryByTestId('member-edit-avatar')).toBeNull()
    expect(screen.queryByRole('button', { name: /edit avatar/i })).toBeNull()
    expect(screen.queryByText('Edit this avatar')).toBeNull()
  })

  it('the DM header has no rule under it — it meets the transcript on spacing alone, like ChatPage\'s session header', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    const header = await screen.findByTestId('member-thread-header')
    expect(header.tagName).toBe('HEADER')
    expect(header.className).not.toMatch(/\bborder-b\b/)
    expect(header.className).not.toMatch(/\bborder-border\b/)
    // Still set off from the transcript by its own padding.
    expect(header.className).toMatch(/\bpy-2\b/)
  })

  it('the Crew summary\'s one text route agrees with the pencil on the destination', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-crew-summary')
    fireEvent.click(screen.getByTestId('member-edit-in-manager'))
    expect(navigateSpy).toHaveBeenCalledWith(EDIT_LINK)
  })

  it('encodes the crew name in the deep link', async () => {
    await renderPage([row({ name: 'on call/2', slug: 'on-call-2', bound: true, slot_key: 'member-on-call-2' })])
    fireEvent.click(await rosterRow('on call/2'))
    fireEvent.click(await screen.findByTestId('member-edit-name-button'))
    expect(navigateSpy).toHaveBeenCalledWith('/capabilities?tab=crews&crew=on%20call%2F2')
  })
})

describe('resolveDefaultMember', () => {
  const ordered = [row({ name: 'alpha', slug: 'alpha' }), row({ name: 'beta', slug: 'beta' })]

  it('default: nothing remembered -> the first row in display order', () => {
    expect(resolveDefaultMember(null, ordered)?.name).toBe('alpha')
    expect(resolveDefaultMember('', ordered)?.name).toBe('alpha')
  })

  it('restore: the remembered member when it is still on the roster', () => {
    expect(resolveDefaultMember('beta', ordered)?.name).toBe('beta')
  })

  it('stale: a remembered member that is gone falls back to the first row', () => {
    expect(resolveDefaultMember('ghost', ordered)?.name).toBe('alpha')
  })

  it('an empty roster resolves to nothing, never throws', () => {
    expect(resolveDefaultMember('beta', [])).toBeUndefined()
  })
})

describe('MembersPage default member, memory and URL', () => {
  const alphaBeta = () => [row({ name: 'alpha', slug: 'alpha' }), row({ name: 'beta', slug: 'beta' })]

  it('a fresh visit opens the first member in display order — never the empty column', async () => {
    await renderPage([
      row({ name: 'zeta-quiet', slug: 'zeta-quiet' }),
      row({ name: 'fresh-talker', slug: 'fresh-talker', last_active_ts: 200 }),
      row({ name: 'old-talker', slug: 'old-talker', last_active_ts: 100 }),
    ])
    // No click: the most-recently-active member (the roster's first row) is
    // opened on arrival, its thread mounted, and the URL says so.
    expect(await screen.findByTestId('chat-pane-stub', PANE_READY)).toHaveTextContent('member-fresh-talker')
    expect(api.memberThread).toHaveBeenCalledWith('fresh-talker')
    expect(screen.queryByText(/Pick a member/i)).toBeNull()
    expect(currentUrl()).toBe('/members?member=fresh-talker')
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('fresh-talker')
  })

  it('a refresh-frame refetch never reorders the roster; a membership change re-sorts it', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    const utils = await renderPage([
      row({ name: 'alpha', slug: 'alpha', last_active_ts: 100 }),
      row({ name: 'beta', slug: 'beta', last_active_ts: 50 }),
    ])
    const names = () =>
      roster()
        .getAllByRole('listitem')
        .map((li) => within(li).queryByText(/^(alpha|beta|gamma)$/)?.textContent)
        .filter(Boolean)
    await waitFor(() => expect(names()).toEqual(['alpha', 'beta']))
    // beta's activity advances server-side and a refresh-frame refetch lands
    // it. The ORDER must hold: re-sorting here moves rows under the cursor
    // mid-click, so the click opens a different member's durable thread.
    membersMock.mockResolvedValue({
      members: [
        row({ name: 'alpha', slug: 'alpha', last_active_ts: 100 }),
        row({ name: 'beta', slug: 'beta', last_active_ts: 999, last_message: 'fresh row content' }),
      ],
      default_agent: 'kirocrew',
    })
    act(() => {
      void utils.queryClient.invalidateQueries({ queryKey: ['kirocrew-agents'] })
    })
    // Content updated in place…
    await roster().findByText('fresh row content')
    // …but the order did not move.
    expect(names()).toEqual(['alpha', 'beta'])
    // A membership change (a new crew appears) re-sorts from scratch by recency.
    membersMock.mockResolvedValue({
      members: [
        row({ name: 'alpha', slug: 'alpha', last_active_ts: 100 }),
        row({ name: 'beta', slug: 'beta', last_active_ts: 999 }),
        row({ name: 'gamma', slug: 'gamma', last_active_ts: 500 }),
      ],
      default_agent: 'kirocrew',
    })
    act(() => {
      void utils.queryClient.invalidateQueries({ queryKey: ['kirocrew-agents'] })
    })
    await rosterRow('gamma')
    expect(names()).toEqual(['beta', 'gamma', 'alpha'])
  })

  it('restores the remembered member on return (and after a reload)', async () => {
    localStorage.setItem(LAST_MEMBER_KEY, 'beta')
    await renderPage(alphaBeta())
    expect(await screen.findByTestId('chat-pane-stub', PANE_READY)).toHaveTextContent('member-beta')
    expect(api.memberThread).toHaveBeenCalledTimes(1)
    expect(api.memberThread).toHaveBeenCalledWith('beta')
    expect(currentUrl()).toBe('/members?member=beta')
  })

  it('a remembered member that was deleted or renamed falls back to the first row, without an error', async () => {
    localStorage.setItem(LAST_MEMBER_KEY, 'ghost')
    await renderPage(alphaBeta())
    expect(await screen.findByTestId('chat-pane-stub', PANE_READY)).toHaveTextContent('member-alpha')
    expect(screen.queryByRole('alert')).toBeNull()
    // Nobody was named, so nothing is announced: the memory just moves on.
    expect(screen.queryByTestId('member-gone-notice')).toBeNull()
    // The stale memory is replaced by what is actually open.
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('alpha')
    expect(currentUrl()).toBe('/members?member=alpha')
  })

  it('a URL naming a member wins over the remembered one (shallow link)', async () => {
    localStorage.setItem(LAST_MEMBER_KEY, 'alpha')
    await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=beta' })
    expect(await screen.findByTestId('chat-pane-stub', PANE_READY)).toHaveTextContent('member-beta')
    expect(api.memberThread).toHaveBeenCalledTimes(1)
    // Opening via the link also becomes the memory for the next visit.
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('beta')
  })

  it('a URL naming a member that is gone falls back to the first row and SAYS so', async () => {
    await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=ghost' })
    expect(await screen.findByTestId('chat-pane-stub', PANE_READY)).toHaveTextContent('member-alpha')
    // The user asked for a specific member: the swap is announced above the
    // thread (a status, not an error — the fallback did open something).
    const notice = screen.getByTestId('member-gone-notice')
    // Leads with the swap, names the gone member, and wears the warn tone —
    // this line is what stops a message going to the wrong member.
    expect(notice).toHaveTextContent(/^Showing alpha/)
    expect(notice).toHaveTextContent('“ghost” is no longer on the roster')
    expect(notice.className).toContain('text-warn')
    expect(screen.queryByRole('alert')).toBeNull()
    expect(currentUrl()).toBe('/members?member=alpha')
    // The stand-in was the page's choice, not the user's, so it is NOT
    // remembered: a dead link leaves the memory exactly as it found it.
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBeNull()
    // Opening another member retires the notice — and, being a choice, is remembered.
    fireEvent.click(await rosterRow('beta'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-beta'))
    expect(screen.queryByTestId('member-gone-notice')).toBeNull()
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('beta')
  })

  it('a gone link falls back to the REMEMBERED member first, and leaves the memory alone', async () => {
    localStorage.setItem(LAST_MEMBER_KEY, 'beta')
    await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=ghost' })
    // The remembered member, not the first row, is the stand-in.
    expect(await screen.findByTestId('chat-pane-stub', PANE_READY)).toHaveTextContent('member-beta')
    expect(screen.getByTestId('member-gone-notice')).toHaveTextContent(/^Showing beta/)
    expect(currentUrl()).toBe('/members?member=beta')
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('beta')
    // Re-clicking the stand-in acknowledges the swap: the notice retires and
    // the (unchanged) memory is now an explicit choice.
    fireEvent.click(await rosterRow('beta'))
    await waitFor(() => expect(screen.queryByTestId('member-gone-notice')).toBeNull())
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('beta')
    // Choosing another member IS a choice, and is remembered.
    fireEvent.click(await rosterRow('alpha'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-alpha'))
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('alpha')
  })

  it('clicking a member writes the URL and the memory', async () => {
    await renderPage(alphaBeta())
    expect(await screen.findByTestId('chat-pane-stub', PANE_READY)).toHaveTextContent('member-alpha')
    fireEvent.click(await rosterRow('beta'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-beta'))
    expect(currentUrl()).toBe('/members?member=beta')
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('beta')
    // The row reflects the selection the URL drove.
    expect(roster().getByText('beta').closest('button')).toHaveAttribute('aria-current', 'true')
  })

  it('the open row scrolls itself into view, so a member opened by URL is never below the fold', async () => {
    // happy-dom has no scrollIntoView; install one to observe the call.
    const scroll = vi.fn()
    const proto = HTMLElement.prototype as HTMLElement & { scrollIntoView?: (o?: unknown) => void }
    const had = proto.scrollIntoView
    proto.scrollIntoView = scroll
    try {
      await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=beta' })
      await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-beta'))
      const row = roster().getByText('beta').closest('button')!
      expect(row).toHaveAttribute('aria-current', 'true')
      expect(scroll).toHaveBeenCalledWith({ block: 'nearest' })
      // Only the open row asks — the rest of the roster stays where it is.
      expect(scroll.mock.instances.every((el) => el === row)).toBe(true)
    } finally {
      if (had) proto.scrollIntoView = had
      else delete proto.scrollIntoView
    }
  })

  it('a link that outruns the cached roster waits for the refetch instead of calling the member gone', async () => {
    // The crew manager's create (#9513) invalidates the roster and lands here
    // with the NEW member's name while the cache still holds the pre-create
    // list. That is not a gone member — it is a fetch in flight.
    ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: alphaBeta(), default_agent: 'kirocrew' })
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementation(echoThread)
    function Elsewhere() {
      const nav = useNavigate()
      return (
        <button data-testid="return-with-new-member" onClick={() => nav('/members?member=staging')}>
          go
        </button>
      )
    }
    function Leave() {
      const nav = useNavigate()
      return (
        <button data-testid="go-elsewhere" onClick={() => nav('/elsewhere')}>
          leave
        </button>
      )
    }
    const { queryClient } = renderWithProviders(
      <>
        <Routes>
          <Route path="/elsewhere" element={<Elsewhere />} />
          <Route path="/members" element={<MembersPage />} />
        </Routes>
        <Leave />
        <LocationProbe />
      </>,
      { route: '/members' },
    )
    expect(await screen.findByTestId('chat-pane-stub')).toHaveTextContent('member-alpha')
    fireEvent.click(screen.getByTestId('go-elsewhere'))
    await screen.findByTestId('return-with-new-member')

    // The create happened elsewhere: the registry prefix is invalidated and
    // the next roster read (slow, so the race is observable) has the member.
    let release: () => void = () => {}
    ;(api.members as ReturnType<typeof vi.fn>).mockImplementation(
      () =>
        new Promise((resolve) => {
          release = () =>
            resolve({ members: [...alphaBeta(), row({ name: 'staging', slug: 'staging' })], default_agent: 'kirocrew' })
        }),
    )
    void queryClient.invalidateQueries({ queryKey: ['kirocrew-agents'] })
    fireEvent.click(screen.getByTestId('return-with-new-member'))
    await waitFor(() => expect(api.members).toHaveBeenCalledTimes(2))

    // Mid-fetch: the cached roster (no staging) is on screen, but the URL is
    // NOT rewritten and no one is declared gone.
    expect(currentUrl()).toBe('/members?member=staging')
    expect(screen.queryByTestId('member-gone-notice')).toBeNull()
    expect(screen.queryByTestId('member-gone-roster-notice')).toBeNull()

    await act(async () => {
      release()
    })
    // The fresh roster has the member: their thread opens, still no notice.
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-staging'))
    expect(currentUrl()).toBe('/members?member=staging')
    expect(screen.queryByTestId('member-gone-notice')).toBeNull()
  })

  it('switching members holds ONE history entry: after walking two members, Back leaves the page in one press', async () => {
    // Driven history, not a spy: a page before /members, a real push into
    // it, real replaces while switching, and a real pop out of it.
    ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: alphaBeta(), default_agent: 'kirocrew' })
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementation(echoThread)
    function Elsewhere() {
      const nav = useNavigate()
      return (
        <button data-testid="go-members" onClick={() => nav('/members')}>
          go
        </button>
      )
    }
    function BackProbe() {
      const nav = useNavigate()
      return (
        <button data-testid="history-back" onClick={() => nav(-1)}>
          back
        </button>
      )
    }
    renderWithProviders(
      <>
        <Routes>
          <Route path="/elsewhere" element={<Elsewhere />} />
          <Route path="/members" element={<MembersPage />} />
        </Routes>
        <BackProbe />
        <LocationProbe />
      </>,
      { route: '/elsewhere' },
    )
    fireEvent.click(screen.getByTestId('go-members'))
    // Arrival: the auto-open REPLACES the bare /members entry.
    expect(await screen.findByTestId('chat-pane-stub', PANE_READY)).toHaveTextContent('member-alpha')
    expect(currentUrl()).toBe('/members?member=alpha')
    // Walk two members.
    fireEvent.click(await rosterRow('beta'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-beta'))
    fireEvent.click(await rosterRow('alpha'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-alpha'))
    expect(currentUrl()).toBe('/members?member=alpha')
    // One Back: off the page — the switches replaced, they did not stack.
    fireEvent.click(screen.getByTestId('history-back'))
    await waitFor(() => expect(currentUrl()).toBe('/elsewhere'))
    expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
    // The memory still holds the last member the user chose.
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('alpha')
  })

  describe('below md', () => {
    // happy-dom ships matchMedia on the prototype; the setup polyfill (if it
    // ran) puts one on the instance. Save whatever own descriptor exists and
    // put it back, so the override never outlives its case: useIsMobile caches
    // on the function's identity.
    const ownDescriptor = Object.getOwnPropertyDescriptor(window, 'matchMedia')
    beforeEach(() => {
      // Narrow viewport: useIsMobile's max-width query matches, so the side
      // panel is an overlay (panelSitsBeside is false on mobile).
      window.matchMedia = vi.fn().mockImplementation((q: string) => ({
        matches: /max-width/.test(q),
        media: q,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      }))
    })
    afterEach(() => {
      if (ownDescriptor) Object.defineProperty(window, 'matchMedia', ownDescriptor)
      else delete (window as unknown as { matchMedia?: typeof window.matchMedia }).matchMedia
    })

    it('does not auto-open: no ?member= IS the roster, like a two-level list', async () => {
      localStorage.setItem(LAST_MEMBER_KEY, 'beta')
      await renderPage(alphaBeta())
      await rosterRow('alpha')
      expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
      expect(api.memberThread).not.toHaveBeenCalled()
      expect(currentUrl()).toBe('/members')
    })

    it('a stale ?member= returns to the roster and says where the member went', async () => {
      await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=ghost' })
      await rosterRow('alpha')
      await waitFor(() => expect(currentUrl()).toBe('/members'))
      expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
      expect(api.memberThread).not.toHaveBeenCalled()
      // The roster is the answer surface here, so the notice sits above it.
      const notice = screen.getByTestId('member-gone-roster-notice')
      expect(notice).toHaveTextContent('“ghost” is no longer on the roster')
      expect(notice).toHaveAttribute('role', 'status')
      // Tapping a member retires it.
      fireEvent.click(await rosterRow('beta'))
      await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-beta'))
      expect(screen.queryByTestId('member-gone-roster-notice')).toBeNull()
    })

    it('tapping a member opens it; the header back POPS the entry the roster pushed', async () => {
      await renderPage(alphaBeta())
      fireEvent.click(await rosterRow('beta'))
      expect(await screen.findByTestId('chat-pane-stub', PANE_READY)).toHaveTextContent('member-beta')
      expect(currentUrl()).toBe('/members?member=beta')
      navigateSpy.mockClear()
      fireEvent.click(screen.getByTestId('member-back'))
      // The entry was pushed from this page's roster, so back is a history
      // pop — the browser's own Back afterwards does not land on a second,
      // identical roster entry.
      expect(navigateSpy).toHaveBeenCalledWith(-1)
      // The memory survives the back gesture: the next desktop visit resumes here.
      expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('beta')
    })

    it('from a deep link the header back drops the param in place — there is no roster entry behind it', async () => {
      await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=beta' })
      expect(await screen.findByTestId('chat-pane-stub', PANE_READY)).toHaveTextContent('member-beta')
      navigateSpy.mockClear()
      fireEvent.click(screen.getByTestId('member-back'))
      await waitFor(() => expect(screen.queryByTestId('chat-pane-stub')).toBeNull())
      expect(currentUrl()).toBe('/members')
      expect(navigateSpy).not.toHaveBeenCalledWith(-1)
    })
  })
})
