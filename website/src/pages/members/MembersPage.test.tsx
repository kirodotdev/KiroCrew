import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { renderWithProviders } from '../../test/helpers'
import { NavigationLeaveGuardProvider } from '../../components/NavigationLeaveGuard'
import { ApiError } from '../../api/apiError'
import { memberProjectionStore } from '../../state/memberProjectionStore'
import { markSlotUnread, sseConnected, sseSlots } from '../../store/dashboardSlice'
import { memberBriefingQueryKey, memberThreadQueryKey } from '../../api/membersQuery'
import { getViewedThreadSlot, _resetViewedThreadForTests } from '../../lib/viewedThread'
import { bindSlotReadSender, emitSlotRead, _resetSlotReadRelayForTest } from '../../lib/slotReadRelay'
import {
  __resetErrorJournalForTests,
  __resetNavSeamForTests,
  consumeChatHandoff,
  installSoftNavigate,
  recordError,
} from '../../utils/errorReport'

/* ── api client mock ─────────────────────────────────────────────────────
 * The page reads exactly two endpoints; mocking them keeps every case
 * network-free. MemberRosterRow is a type-only import so the mock does not
 * need to provide it. */
vi.mock('../../api/client', () => ({
  api: {
    members: vi.fn(),
    memberThread: vi.fn(),
    memberActivity: vi.fn(() => Promise.resolve({ slug: '', member: '', capped: false, entries: [] })),
    // The Notes tab's read. "No notes yet" is the state every case not about
    // Notes wants: an empty state, not an alert.
    memberBriefing: vi.fn(() => Promise.resolve({ slug: '', member: '', supported: true, text: '', updated_ts: null, redacted: false, truncated: false })),
    // The Work log's session record (CrewLogTab) reads the thread's crew-log
    // folds; an empty, resolved read renders its own quiet empty state.
    sessionCrewLogProjections: vi.fn(() => Promise.resolve({ folds: {}, resolved: true, writesDrained: true })),
    // The auto-patrol block and roster badge read the whole loop registry;
    // the default is "feature on, nothing armed" so every other case renders
    // the page without a loop in the way.
    autonudgeList: vi.fn(() => Promise.resolve({ enabled: true, loops: [] })),
    // The side panel's + menu gates its Summary row on this read; "disabled"
    // keeps the chat-style Summary row out of the menu, next to the Work log
    // chip it would be a second, unrelated summary of the same thread.
    sessionSummary: vi.fn(() => Promise.resolve({ enabled: false })),
    // The Dashboard tab's webview. Stubbed as "nothing published", which is the
    // state every case here is about: without it the reader rejects and the
    // tab raises a red alert, so a silent fallback (a remembered crew that
    // was renamed away) would read as an error on a page that is behaving.
    memberPanel: vi.fn(() => Promise.resolve({ panel: null, html: null })),
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
  default: ({ slotKey, agentLocked, followContentWidth, busyMode }: { slotKey: string; agentLocked?: boolean; followContentWidth?: boolean; busyMode?: string }) => (
    <div data-testid="chat-pane-stub" data-agent-locked={agentLocked ? '1' : '0'} data-follow-content-width={followContentWidth ? '1' : '0'} data-busy-mode={busyMode ?? 'split'}>
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
import MembersPage, { CREW_DASHBOARD_TAB_ID, CREW_NOTES_TAB_ID, CREW_PANEL_TAB_IDS, CREW_WORK_LOG_TAB_ID, MEMBERS_UNCONFIRMED_WITHHELD_VIEWS, MEMBERS_UNFED_VIEWS, MEMBERS_WITHHELD_VIEWS, panelSitsBeside, resolveDefaultMember } from './MembersPage'
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
  const base = {
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
  // Every roster row now carries a baseline projections block (the backend
  // contract). The `roster` face mirrors the row's own config fields so the
  // page reads identical values whether from the row or the seeded store; a
  // case that wants a divergence overrides `projections` explicitly.
  const projections = {
    asOfSeq: 1,
    values: {
      roster: {
        name: base.name,
        slug: base.slug,
        kiro_agent: base.kiro_agent,
        workspace: base.workspace,
        memory_store: base.memory_store,
        model: base.model,
        slot_key: base.slot_key,
        last_active_ts: (base as { last_active_ts?: number }).last_active_ts,
        last_message: (base as { last_message?: string }).last_message,
        starred: (base as { starred?: boolean }).starred,
      },
      // `activity` is deliberately omitted from the default fixture: the
      // activity-focused cases feed entries through api.memberActivity, and a
      // member with no activity projection must fall back to that query. Cases
      // that want a pushed activity projection seed it via memberProjectionStore.
      wake: { patrol: 'none' as const },
      driving: { open: [] },
    },
  }
  return { ...base, projections, ...overrides }
}

/** Echoes the requested slug back as the thread's member — the happy path for
 *  any roster, so an open (a click, or the restore of a remembered member)
 *  resolves cleanly for whichever member it names. Cases that need a collision
 *  or a failure pass `thread`. */
function echoThread(slug: string) {
  return Promise.resolve({ slot_key: 'member-' + slug, slug, member: slug, created: true })
}

/** Renders the page at the URL and lets the roster load. `thread` replaces
 *  the thread-endpoint mock BEFORE mount: a remembered member (or a
 *  ?member= URL) opens a thread as soon as the roster is in, so a mock
 *  installed after render would miss that first POST. A fresh visit with
 *  nothing remembered opens no one (#11763). */
/**
 * Ceiling for a wait on the chat pane. `renderPage` returns once the roster
 * fetch has been ISSUED; the pane sits behind a real chain after that -- members
 * resolve, the roster commits, the open (a remembered restore, a ?member= URL,
 * or a click) POSTs `memberThread`, that
 * resolves, and the pane mounts. Under load (a shared host, coverage
 * instrumentation) that ran past the 1000ms default in one of four full runs; a
 * named ceiling, not a longer guess -- website/docs/testing.md.
 */
const PANE_READY = { timeout: 5000 }

/* The panel opens on Notes; the blocks these cases assert on live in the
 * Work log tab. Click its chip and return the body. */
async function openWorkLog() {
  fireEvent.click(await screen.findByTestId('side-panel-leading-tab-crew-work-log', PANE_READY))
  return screen.findByTestId('member-work-log', PANE_READY)
}

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
    <NavigationLeaveGuardProvider>
      <MembersPage />
      <LocationProbe />
    </NavigationLeaveGuardProvider>,
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

/* The open member's name also renders in the thread header (and the panel's identity row),
 * so a bare screen query by name is ambiguous once a member is open (a click,
 * a remembered restore, or a ?member= URL). Scope name lookups to the roster
 * column. */
const roster = () => within(screen.getByTestId('member-roster'))
const rosterRow = async (name: string) =>
  within(await screen.findByTestId('member-roster')).findByText(name)

beforeEach(() => {
  vi.clearAllMocks()
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
  // clearAllMocks keeps implementations, so a case that made the drawer's
  // fetches reject would leak its error alerts into the next one. Reinstall
  // the quiet defaults.
  vi.mocked(api.memberActivity).mockImplementation(() =>
    Promise.resolve({ slug: '', member: '', capped: false, entries: [] }),
  )
  vi.mocked(api.memberBriefing).mockImplementation(() =>
    Promise.resolve({ slug: '', member: '', supported: true, text: '', updated_ts: null, redacted: false, truncated: false }),
  )
  // The patrol cases make this registry read REJECT (mockRejectedValue also
  // outlives clearAllMocks); a leaked rejection renders the roster's patrol
  // error alert into every later case.
  vi.mocked(api.autonudgeList).mockImplementation(() => Promise.resolve({ enabled: true, loops: [] }))
  // The remembered member must not leak between cases.
  localStorage.clear()
  // The projection store is a module-level singleton fed by the roster seed;
  // clear it so one case's seeded values do not survive into the next.
  memberProjectionStore.clear()
  // The side panel's tab strip is a module-level, persisted store; a tab
  // opened in one case would otherwise be on the strip in the next.
  __resetPanelTabs()
  setWindowWidth(WIDE_WINDOW)
  // Module-level "thread on screen" registration; a case that unmounted
  // mid-effect would otherwise leave its slot registered for the next one.
  _resetViewedThreadForTests()
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
    // A remembered member restores the thread on arrival (a fresh visit no
    // longer auto-opens anyone, #11763); this test is about the cache on a
    // second mount, not the arrival rule.
    localStorage.setItem(LAST_MEMBER_KEY, 'oncall')
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
    // A remembered member restores the thread on arrival (#11763); this test
    // is about a failed REPAIR over a cached thread, not the arrival rule.
    localStorage.setItem(LAST_MEMBER_KEY, 'oncall')
    const utils = await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
    utils.rerender(<LocationProbe />)
    const rawReason = 'The recorded private memory is unavailable. token=repair-secret-value'
    const report = recordError({ source: 'api', message: rawReason, status: 409, code: 'memory_unavailable' })
    vi.mocked(api.memberThread).mockRejectedValue(new Error(rawReason))
    utils.rerender(page)
    const notice = await screen.findByTestId('member-thread-error')
    expect(notice).toHaveTextContent(/Couldn't reconnect this conversation/i)
    expect(notice).not.toHaveTextContent('The recorded private memory is unavailable.')
    // "Could not open" would contradict the conversation still rendered below.
    expect(notice).not.toHaveTextContent(/Could not open/i)
    expect(within(notice).queryByRole('button', { name: /ask the agent/i })).toBeNull()
    const pane = screen.getByTestId('chat-pane-stub')
    expect(pane).toHaveTextContent('member-oncall')
    expect(utils.queryClient.getQueryData(memberThreadQueryKey('oncall'))).toEqual({
      slot_key: 'member-oncall', failed: true, errorReport: report,
    })
    const details = screen.getByTestId('member-thread-error-details') as HTMLDetailsElement
    const reason = within(details).getByText(report.message)
    expect(within(details).getByText('Details').tagName).toBe('SUMMARY')
    expect(details.open).toBe(false)
    expect(reason).not.toBeVisible()
    // Exercise the native disclosure state without relying on happy-dom to
    // emulate the browser's default summary-click action.
    details.open = true
    expect(reason).toBeVisible()
    expect(reason).toHaveTextContent('token=[redacted]')
    expect(details).not.toHaveTextContent('repair-secret-value')
    expect(within(details).queryByRole('button', { name: /ask the agent/i })).toBeNull()

    let completeRepair!: (value: Awaited<ReturnType<typeof api.memberThread>>) => void
    vi.mocked(api.memberThread).mockReturnValueOnce(new Promise((resolve) => { completeRepair = resolve }))
    fireEvent.click(await rosterRow('oncall'))
    await waitFor(() => {
      expect(api.memberThread).toHaveBeenCalledTimes(3)
      expect(utils.queryClient.getQueryData(memberThreadQueryKey('oncall'))).toEqual({ slot_key: 'member-oncall' })
      expect(screen.queryByTestId('member-thread-error')).toBeNull()
    })
    expect(screen.queryByTestId('member-thread-error-details')).toBeNull()
    expect(screen.getByTestId('chat-pane-stub')).toBe(pane)
    await act(async () => {
      completeRepair({ slot_key: 'member-oncall-confirmed', slug: 'oncall', member: 'oncall', created: false })
    })
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall-confirmed'))
    expect(utils.queryClient.getQueryData(memberThreadQueryKey('oncall'))).toEqual({
      slot_key: 'member-oncall-confirmed',
    })
    expect(screen.queryByTestId('member-thread-error-details')).toBeNull()
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
  it('opens a memory-page deep link by exact member name rather than a lossy slug', async () => {
    vi.mocked(api.members).mockResolvedValue({ members: [row({ name: 'Review & QA', slug: 'review-qa' }), row({ name: 'Review QA', slug: 'review-qa-other' })], default_agent: 'default' })
    vi.mocked(api.memberThread).mockResolvedValue({ slot_key: 'member-review-qa', slug: 'review-qa', member: 'Review & QA', created: false })
    renderWithProviders(<MembersPage />, { route: '/members?member=Review%20%26%20QA' })
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-review-qa')
    expect(api.memberThread).toHaveBeenCalledExactlyOnceWith('review-qa')
  })

  it('shows the concrete private memory refusal when opening a member conversation fails', async () => {
    // A remembered member drives the restore that fails here (a fresh visit
    // no longer auto-opens anyone, #11763); the failing POST is that restore.
    localStorage.setItem(LAST_MEMBER_KEY, 'oncall')
    const rawReason = 'Private memory database is unreadable; restore the oncall backup. token=private-secret-value'
    const report = recordError({
      source: 'api', message: rawReason, status: 409, code: 'memory_unavailable',
      endpoint: '/api/members/oncall/thread', detail: rawReason,
    })
    const { queryClient } = await renderPage([row()], 'kirocrew', { thread: new Error(rawReason) })
    const notice = await screen.findByTestId('member-thread-error')
    expect(notice).toHaveTextContent(/Could not open this member's conversation/i)
    expect(notice).not.toHaveTextContent('Private memory database is unreadable')
    expect(queryClient.getQueryData(memberThreadQueryKey('oncall'))).toEqual({
      slot_key: '', failed: true, errorReport: report,
    })
    const details = screen.getByTestId('member-thread-error-details') as HTMLDetailsElement
    const reason = within(details).getByText(report.message)
    expect(reason).not.toBeVisible()
    details.open = true
    expect(reason).toBeVisible()
    expect(reason).toHaveTextContent('restore the oncall backup')
    expect(details).not.toHaveTextContent('private-secret-value')
    expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
    // The localized banner cannot recover this report by matching its own
    // text. The endpoint/code in the hand-off prove the explicit prop survives.
    const handoffNavigate = vi.fn()
    installSoftNavigate(handoffNavigate)
    fireEvent.click(within(notice).getByRole('button', { name: /ask the agent/i }))
    const handoff = consumeChatHandoff()
    expect(handoff).toContain('/api/members/oncall/thread')
    expect(handoff).toContain('memory_unavailable')
    expect(handoff).toContain('restore the oncall backup')
    expect(handoff).not.toContain('private-secret-value')
    expect(handoffNavigate).toHaveBeenCalled()
    installSoftNavigate(null)
  })

  it('opens the pinned DM thread on click: creates the thread and mounts the chat stack on its slot', async () => {
    await renderPage()
    fireEvent.click(await rosterRow('oncall'))
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledWith('oncall'))
    // The stub echoes the slot key: proves ChatPane received THE member slot,
    // not a fresh ordinary slot. Mutating the mounted key breaks this line.
    const pane = await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
    expect(pane).toHaveTextContent('member-oncall')
    // The host declares the pin: ChatPane must not offer the agent picker
    // (every selection would 409 against the server-side pin).
    expect(pane).toHaveAttribute('data-agent-locked', '1')
    // The DM column is the page's widest region, so the pane is told to
    // follow the user's Content width setting (ChatPane resolves both the
    // transcript and composer halves itself; its default stays off for
    // split-view panes, which are already narrow).
    expect(pane).toHaveAttribute('data-follow-content-width', '1')
    // A DM has no queue concept: a send while the member is working steers
    // into its running turn. The pane's own steer-only behaviour (plain send
    // button, no split, no QueueStack) is pinned in ChatPane.steerOnly.test;
    // this line pins that the Members page is the host that asks for it.
    expect(pane).toHaveAttribute('data-busy-mode', 'steer-only')
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
    const names = Array.from(list.querySelectorAll('li button .font-semibold')).map(
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
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
  })

  it('surfaces a visible error when thread creation fails', async () => {
    // Installed BEFORE mount: a remembered member restores on arrival (a
    // fresh visit no longer auto-opens anyone, #11763), so the failing POST
    // is that restore.
    localStorage.setItem(LAST_MEMBER_KEY, 'oncall')
    await renderPage([row()], 'kirocrew', { thread: new Error('Create private memory in the member editor.') })
    expect(
      await screen.findByText(/Could not open this member's conversation/i),
    ).toBeInTheDocument()
    // Non-API exceptions have no journal report; do not invent a diagnostic
    // object or leak an unredacted thrown message into the localized banner.
    expect(screen.getByTestId('member-thread-error')).not.toHaveTextContent('Create private memory in the member editor.')
    expect(screen.queryByTestId('member-thread-error-details')).toBeNull()
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
    // A remembered member restores alpha on arrival (a fresh visit no longer
    // auto-opens anyone, #11763); this test needs a member open first.
    localStorage.setItem(LAST_MEMBER_KEY, 'alpha')
    let rejectA: (e: Error) => void = () => {}
    const pendingA = new Promise((_, reject) => {
      rejectA = reject
    })
    const { queryClient } = await renderPage([
      row({ name: 'alpha', slug: 'alpha' }),
      row({ name: 'beta', slug: 'beta' }),
    ])
    // Let the page's restore of alpha settle before queuing the one-shot
    // responses, so the re-click below is the call that hangs.
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-alpha')
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
    const report = recordError({ source: 'api', message: 'alpha-private-memory-unavailable', code: 'memory_unavailable' })
    await act(async () => { rejectA(new Error(report.message)) })
    // The stale rejection lands in alpha's bucket; beta's view stays clean.
    await waitFor(() => expect(queryClient.getQueryData(memberThreadQueryKey('alpha'))).toEqual({
      slot_key: 'member-alpha', failed: true, errorReport: report,
    }))
    expect(queryClient.getQueryData(memberThreadQueryKey('beta'))).toEqual({ slot_key: 'member-beta' })
    expect(screen.queryByTestId('member-thread-error')).toBeNull()
    expect(screen.queryByTestId('member-thread-error-details')).toBeNull()
    expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-beta')
    expect(screen.queryByText('alpha-private-memory-unavailable', { exact: false })).toBeNull()
  })
})

describe('MembersPage side panel (Notes / Work log / Dashboard) and edit jump', () => {
  it('a starred:true frame flips the Starred filter count and membership at page level without a roster refetch', async () => {
    // The page-level Starred count and filter read the MERGED list (rows +
    // pushed roster projection), so a `member_projection` frame that stars a
    // member must move the menu count and the filtered membership WITHOUT a
    // second GET /api/members — otherwise the row shows starred while the
    // count reads 0 (the bug this fixes).
    await renderPage([
      row({ name: 'oncall', slug: 'oncall', bound: true, slot_key: 'member-oncall' }),
      row({ name: 'research', slug: 'research' }),
    ])
    // The Starred filter lives inside the filter menu; open it to read the
    // count — Enter on the trigger, as the sidebar's own filter tests do.
    fireEvent.keyDown(await screen.findByTestId('member-filter-menu'), { key: 'Enter' })
    const starItem = await screen.findByTestId('member-filter-starred')
    // No stars yet: the count reads 0.
    expect(starItem).toHaveTextContent('0')
    const callsBefore = (api.members as ReturnType<typeof vi.fn>).mock.calls.length

    act(() => {
      // seq > the baseline seed's asOfSeq (1) so higher-seq-wins applies it.
      memberProjectionStore.apply(
        'research',
        'roster',
        { name: 'research', slug: 'research', starred: true },
        5,
      )
    })

    await waitFor(() => expect(screen.getByTestId('member-filter-starred')).toHaveTextContent('1'))
    // No roster refetch drove the change.
    expect((api.members as ReturnType<typeof vi.fn>).mock.calls.length).toBe(callsBefore)

    // Enabling the filter shows exactly that member.
    fireEvent.click(screen.getByTestId('member-filter-starred'))
    await waitFor(() => {
      const names = Array.from(document.querySelectorAll('[data-testid^="member-star-"]')).map((el) =>
        el.getAttribute('data-testid')!.replace('member-star-', ''),
      )
      expect(names).toEqual(['research'])
    })
    // The member roster was fetched exactly once across the whole case.
    expect(api.members).toHaveBeenCalledTimes(1)
  })

  it('docks the chat SidePanel beside the thread on a wide window: permanent, no Details toggle, no close control', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    expect(await screen.findByTestId('member-notes')).toBeInTheDocument()
    // The panel is part of the page while a member is open, like the roster:
    // nothing in the header opens or closes it, and its strip renders no
    // close control (the chat page's panel shows one because ChatPage passes
    // onClose; this page does not).
    expect(screen.queryByTestId('member-panel-toggle')).toBeNull()
    expect(screen.queryByRole('button', { name: /close panel/i })).toBeNull()
    // The strip is the SidePanel's: its own resize splitter (the same shared
    // handle the chat page drags) pins that the page mounted the real
    // component rather than a lookalike. Named precisely: the roster's own
    // grip ("Resize member list") is a second resize separator on the page.
    expect(screen.getByRole('separator', { name: /resize panel/i })).toBeInTheDocument()
  })

  it('Notes is the FIRST tab, selected by default, and has no close control', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-notes')
    const tabs = screen.getAllByRole('tab')
    // Ahead of the pinned Changes / Artifacts / Files block, not merely present.
    expect(tabs[0]).toBe(screen.getByTestId('side-panel-leading-tab-crew-notes'))
    expect(tabs[0]).toHaveAttribute('aria-selected', 'true')
    expect(tabs[0]).toHaveAccessibleName(/notes/i)
    // Structure, not label: no nested button means no close (or transfer) control.
    expect(tabs[0].querySelectorAll('button')).toHaveLength(0)
    // The chat page's own Summary (session summary) is a different tab and
    // must not be what the strip opened on — the ids are distinct by contract.
    expect(CREW_PANEL_TAB_IDS).not.toContain('summary')
    expect(CREW_PANEL_TAB_IDS[0]).toBe(CREW_NOTES_TAB_ID)
  })

  it('the three crewmate tabs come first, in order, all LABELLED: Notes, Work log, Dashboard', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-notes')
    const tabs = screen.getAllByRole('tab')
    expect(tabs[0]).toBe(screen.getByTestId(`side-panel-leading-tab-${CREW_NOTES_TAB_ID}`))
    expect(tabs[1]).toBe(screen.getByTestId(`side-panel-leading-tab-${CREW_WORK_LOG_TAB_ID}`))
    expect(tabs[2]).toBe(screen.getByTestId(`side-panel-leading-tab-${CREW_DASHBOARD_TAB_ID}`))
    // Labelled even while inactive — three icon-only chips would be unlabelled
    // navigation. The pinned views behind them stay icon-only when inactive.
    expect(tabs[1]).toHaveTextContent('Work log')
    expect(tabs[2]).toHaveTextContent('Dashboard')
    expect(tabs[0]).toHaveTextContent('Notes')
  })

  it('selecting another tab swaps the body; Notes comes back on its chip', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-notes')
    // The pinned Artifacts view is always on the strip (SidePanel contract).
    fireEvent.click(screen.getByRole('tab', { name: 'Artifacts' }))
    await waitFor(() => expect(screen.queryByTestId('member-notes')).toBeNull())
    fireEvent.click(screen.getByTestId('side-panel-leading-tab-crew-notes'))
    expect(await screen.findByTestId('member-notes')).toBeInTheDocument()
  })

  it('the Work log tab hosts the counters, driving sessions, patrol and activity; the Dashboard tab hosts the published webview', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-notes')
    // Not on Notes: the work-log blocks are not merely hidden, they are absent.
    expect(screen.queryByTestId('member-stats')).toBeNull()
    const workLog = await openWorkLog()
    expect(within(workLog).getByTestId('member-stats')).toBeInTheDocument()
    expect(within(workLog).getByTestId('member-summary-status')).toBeInTheDocument()
    expect(within(workLog).getByText('Sessions it\'s driving')).toBeInTheDocument()
    expect(within(workLog).getByText('Auto patrol')).toBeInTheDocument()
    expect(within(workLog).getByText('Recent activity')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('side-panel-leading-tab-crew-dashboard'))
    const dashboard = await screen.findByTestId('member-dashboard')
    expect(await within(dashboard).findByTestId('crew-webview-empty')).toHaveTextContent(
      'This crewmate has not published a dashboard yet.',
    )
    expect(screen.queryByTestId('member-work-log')).toBeNull()
  })

  it('the panel carries no settings: no template / model / workspace / memory lines, and no memory-binding diagnostic, even for a mismatched member', async () => {
    await renderPage([
      row({ bound: true, slot_key: 'member-oncall', memory_store: 'beta-own', memory_version: 2, memory_owner: 'beta' }),
    ])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-notes')
    for (const id of CREW_PANEL_TAB_IDS) {
      fireEvent.click(screen.getByTestId(`side-panel-leading-tab-${id}`))
      const body = await screen.findByTestId('side-panel-leading-body')
      expect(body).not.toHaveTextContent(/memory store/i)
      expect(body).not.toHaveTextContent(/private memory/i)
      expect(body).not.toHaveTextContent(/configured memory store is unavailable/i)
      expect(body).not.toHaveTextContent(/Wake sources/i)
      expect(body).not.toHaveTextContent(/Agent template/i)
      expect(body).not.toHaveTextContent(/Edit in crew manager/i)
    }
  })
  it('the + menu offers the chat panel\'s per-chat Terminal on a member DM (a member thread is a chat slot)', async () => {
    const { store } = await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-notes')
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
    // Summary (a second, unrelated summary next to the Work log chip).
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
    await screen.findByTestId('member-notes')
    // Leading block: Notes, Work log, Dashboard; pinned: Artifacts, Files — and NOT Changes.
    expect(screen.getAllByRole('tab').map((t) => t.getAttribute('aria-label'))).toEqual([
      'Notes', 'Work log', 'Dashboard', 'Artifacts', 'Files',
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
    await screen.findByTestId('member-notes')
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
    await screen.findByTestId('member-notes')
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
      expect(screen.getAllByRole('tab').map((t) => t.getAttribute('aria-label'))).toEqual(['Notes', 'Work log', 'Dashboard']),
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
      expect(screen.getAllByRole('tab').map((t) => t.getAttribute('aria-label'))).toEqual(['Notes', 'Work log', 'Dashboard']),
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

  it('a stored tab focus survives the re-open round-trip: Artifacts stays focused, not reset to Notes', async () => {
    // While the re-open POST is in flight every slot view is withheld and the
    // strip falls back to Notes — but that fallback must not be
    // written into the bucket, or every switch would wipe the user's focus.
    let resolveRepost: (v: unknown) => void = () => {}
    const pending = new Promise((resolve) => { resolveRepost = resolve })
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-notes')
    fireEvent.click(screen.getByRole('tab', { name: 'Artifacts' }))
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Artifacts' })).toHaveAttribute('aria-selected', 'true'))
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockReturnValueOnce(pending)
    fireEvent.click(await rosterRow('oncall'))
    // In flight: summary shown as the fallback (and its body loads).
    expect(await screen.findByTestId('member-notes')).toBeInTheDocument()
    act(() => { resolveRepost({ slot_key: 'member-oncall', slug: 'oncall', member: 'oncall', created: false }) })
    // Confirmed: the stored focus is back on Artifacts, untouched by the fallback.
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Artifacts' })).toHaveAttribute('aria-selected', 'true'))
    expect(screen.queryByTestId('member-notes')).toBeNull()
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
      expect(screen.getAllByRole('tab').map((t) => t.getAttribute('aria-label'))).toEqual(['Notes', 'Work log', 'Dashboard']),
    )
    act(() => { resolveFirst({ slot_key: 'member-oncall', slug: 'oncall', member: 'oncall', created: false }) })
    // Still unbound after the stale answer: the refusal stands, no slot-bound views.
    await act(async () => { await new Promise((r) => setTimeout(r, 20)) })
    expect(screen.getByTestId('member-thread-error')).toHaveTextContent(/Couldn't reconnect/i)
    expect(screen.getAllByRole('tab').map((t) => t.getAttribute('aria-label'))).toEqual(['Notes', 'Work log', 'Dashboard'])
    expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
  })

  it('binds the panel to the POST-confirmed slot only: a rejected thread leaves every slot-bound view withheld', async () => {
    // The roster binding says `member-oncall`, but the thread endpoint refuses
    // (a stale binding whose canonical key an ordinary slot now occupies). The
    // panel must not aim Side chat / Artifacts / Files at that occupant: with no
    // confirmed slot, only the slot-free Notes / Work log / Dashboard chips are on the strip and the
    // + menu offers nothing slot-bound. A remembered member restores on
    // arrival (a fresh visit no longer auto-opens anyone, #11763), so the
    // refused open is that restore.
    localStorage.setItem(LAST_MEMBER_KEY, 'oncall')
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })], 'kirocrew', { thread: new Error('409') })
    await screen.findByText(/Could not open this member's conversation/i)
    expect(await screen.findByTestId('member-notes')).toBeInTheDocument()
    expect(screen.getAllByRole('tab').map((t) => t.getAttribute('aria-label'))).toEqual(['Notes', 'Work log', 'Dashboard'])
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
      expect(screen.getAllByRole('tab').map((t) => t.getAttribute('aria-label'))).toEqual(['Notes', 'Work log', 'Dashboard']),
    )
    expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
  })

  it('an overlay opened on a narrow window does not lie in wait: docking resets it, so re-narrowing finds it closed', async () => {
    setWindowWidth(NARROW_WINDOW)
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('chat-pane-stub')
    fireEvent.click(screen.getByTestId('member-panel-toggle'))
    expect(await screen.findByTestId('member-notes')).toBeInTheDocument()
    // Widen: the panel docks (no toggle, no close control)…
    setWindowWidth(WIDE_WINDOW)
    fireEvent(window, new Event('resize'))
    await waitFor(() => expect(screen.queryByTestId('member-panel-toggle')).toBeNull())
    expect(screen.queryByRole('button', { name: /close panel/i })).toBeNull()
    // …and narrowing again finds the overlay CLOSED, not popped back over the thread.
    setWindowWidth(NARROW_WINDOW)
    fireEvent(window, new Event('resize'))
    await waitFor(() => expect(screen.getByTestId('member-panel-toggle')).toBeInTheDocument())
    await waitFor(() => expect(screen.queryByTestId('member-notes')).toBeNull())
  })

  it('narrow window: the panel becomes an overlay the Details button opens and its own close control dismisses', async () => {
    setWindowWidth(NARROW_WINDOW)
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('chat-pane-stub')
    // Closed by default: an always-open overlay would cover the thread.
    expect(screen.queryByTestId('member-notes')).toBeNull()
    fireEvent.click(screen.getByTestId('member-panel-toggle'))
    expect(await screen.findByTestId('member-notes')).toBeInTheDocument()
    // An overlay MUST be dismissable, so here the strip does render the close
    // control the docked column omits.
    fireEvent.click(screen.getByRole('button', { name: /close panel/i }))
    // AnimatePresence keeps the overlay mounted for the exit tween — wait for
    // the removal instead of asserting synchronously.
    await waitFor(() => expect(screen.queryByTestId('member-notes')).toBeNull())
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
    fireEvent.click(screen.getByTestId('member-notes'))
    expect(screen.getByTestId('member-notes')).toBeInTheDocument()
    // On the scrim itself: dismissed.
    fireEvent.click(overlay)
    await waitFor(() => expect(screen.queryByTestId('member-notes')).toBeNull())
  })

  it('panelSitsBeside: the docking boundary is the shell reserve + roster + gaps + panel minimum', () => {
    // 560 (rail + chat minimum) + 320 (panel min) + 264 (roster) + 24 (gaps) = 1168.
    expect(panelSitsBeside({ winW: 1168, rosterW: 264, isMobile: false })).toBe(true)
    expect(panelSitsBeside({ winW: 1167, rosterW: 264, isMobile: false })).toBe(false)
    // A wider roster needs a wider window; mobile never docks.
    expect(panelSitsBeside({ winW: 1168, rosterW: 300, isMobile: false })).toBe(false)
    expect(panelSitsBeside({ winW: 2000, rosterW: 264, isMobile: true })).toBe(false)
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

  it('the page header draws the same two-ghost brand mark as the nav rail, and both add entries a bare plus', async () => {
    await renderPage([])
    const roster = await screen.findByTestId('member-roster')
    // The nav rail names this page with `CrewMemberMark` (surfaces/builtins),
    // so the header that opens under that row must not switch to Lucide's
    // person-pair `Users` — one glyph for one thing. The mark is a CSS mask
    // over currentColor, so it is found by its test id, not an svg class.
    expect(within(roster).getByTestId('crew-member-mark')).toBeInTheDocument()
    // Both "add member" entries carry a bare `Plus`, not `UserPlus`: the page
    // icon already says "members", and `UserPlus` would put the one Lucide
    // person figure on a page whose members are ghosts. Asserting on the
    // rendered svg class pins the glyph, not just that some icon rendered.
    for (const id of ['member-add', 'member-empty-cta']) {
      const icon = screen.getByTestId(id).querySelector('svg')
      expect(icon).toHaveClass('lucide-plus')
      expect(icon).not.toHaveClass('lucide-user-plus')
    }
  })

  // The fold is by LOCAL calendar day (`groupActivityDays` floors each entry
  // with `setHours(0,0,0,0)`), so every fixture timestamp is built the same
  // way: a wall-clock time on a calendar day, not `now - k*86400`. The live
  // clock alone cannot prove that: the fixture is only wrong for the two
  // minutes after midnight, which a CI shard hit once (00:00:23 UTC, "Show 2
  // more days" — today's entries had landed on yesterday). The second row pins
  // the clock to 23 s after local midnight and runs the identical assertions,
  // so the case fails on a midnight-straddling fixture on every run.
  it.each([
    ['the live clock', false],
    ['a clock 23 seconds after local midnight', true],
  ])('the Work log folds the recorded activity by day, with the time strip behind each row, and honest counters (%s)', async (_label, pinMidnight) => {
    const midnight = new Date()
    midnight.setHours(0, 0, 0, 0)
    if (pinMidnight) {
      // Only Date is faked: waitFor/findBy keep real timers, so nothing here
      // needs advancing, and the finally below restores it even on a throw.
      vi.useFakeTimers({ toFake: ['Date'] })
      vi.setSystemTime(new Date(midnight.getTime() + 23_000))
    }
    try {
      const now = Date.now() / 1000
      const todayStart = midnight.getTime() / 1000
      // A wall-clock time on the calendar day `daysAgo` before today. `setDate`
      // walks the calendar (month ends, and a DST change on the way keeps the
      // wall clock rather than a 24h count); noon exists on every day in every
      // zone, unlike the first hour of a spring-forward day.
      const onDay = (daysAgo: number) => {
        const d = new Date(midnight)
        d.setDate(d.getDate() - daysAgo)
        d.setHours(12, 0, 0, 0)
        return d.getTime() / 1000
      }
      // Today's entries sit just inside today's local day, so they are today
      // however close to midnight the test runs — `now - 60` is yesterday for
      // the first minute after midnight.
      const today = (secondsIn: number) => todayStart + secondsIn
      vi.mocked(api.memberActivity).mockResolvedValue({
        slug: 'oncall',
        member: 'oncall',
        capped: false,
        entries: [
          { ts: today(2), via: 'chat', project: '' },
          { ts: today(1), via: 'select_crew', project: '/srv/kirocrew' },
          { ts: onDay(1), via: 'chat', project: '' },
          { ts: onDay(2), via: 'chat', project: '' },
          { ts: onDay(3), via: 'chat', project: '' },
          { ts: onDay(4), via: 'chat', project: '' },
          // Older than 7 days: a day row of its own, but in neither counter.
          { ts: onDay(9), via: 'chat', project: '' },
        ],
      })
      await renderPage([row({ bound: true, slot_key: 'member-oncall', last_active_ts: now - 60 })])
      fireEvent.click(await rosterRow('oncall'))
      await openWorkLog()
      await screen.findByTestId('member-activity-days')
      // Six distinct days, three shown before the fold; the button names the rest.
      expect(screen.getAllByTestId('member-activity-day')).toHaveLength(3)
      const more = screen.getByTestId('member-activity-more')
      expect(more).toHaveTextContent('Show 3 more days')
      // Today's row: the two entries collapse into counts by how the member was
      // reached, and the project rides along as its last path segment.
      const todayRow = screen.getAllByTestId('member-activity-day')[0]
      expect(todayRow).toHaveTextContent('1 chat')
      expect(todayRow).toHaveTextContent('1 auto-picked')
      expect(todayRow).toHaveTextContent('kirocrew')
      expect(todayRow).not.toHaveTextContent('/srv/')
      // Nothing is listed until a day is opened; opening it shows one time chip
      // per entry, the routing decision still told apart from the conversation.
      expect(screen.queryByTestId('member-activity-times')).toBeNull()
      fireEvent.click(todayRow)
      const times = screen.getByTestId('member-activity-times')
      expect(times.children).toHaveLength(2)
      expect(within(times).getByTitle(/auto-picked by the orchestrator/i)).toBeTruthy()
      fireEvent.click(more)
      expect(screen.getAllByTestId('member-activity-day')).toHaveLength(6)
      // Counters are derived from the same entries — 6 within 7 days; the
      // 9-day-old one is excluded. Today's count is 2 on either clock now that
      // both of today's entries are inside the local day, but only the week
      // card is pinned exactly, as before.
      const stats = screen.getByTestId('member-stats')
      expect(stats).toHaveTextContent('6')
    } finally {
      vi.useRealTimers()
    }
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
    await openWorkLog()
    const stats = await screen.findByTestId('member-stats')
    await waitFor(() => expect(stats).toHaveTextContent('2+'))
  })

  it('a saturated projection ring renders counters as floors (N+), even with no query capped flag', async () => {
    // The pushed activity projection is a newest-first ring bounded at
    // ACTIVITY_RING (50). When it is full, older in-window events fell off, so
    // the tile must read "50+" not an exact "50" — even though the query path's
    // `capped` flag is false (the count comes from the projection, not the
    // query). Regression for the UX finding on the projection-served path.
    const now = Date.now() / 1000
    vi.mocked(api.memberActivity).mockResolvedValue({
      slug: 'oncall',
      member: 'oncall',
      capped: false,
      entries: [],
    })
    // 50 records, all within today — a full ring.
    const recent = Array.from({ length: 50 }, (_, i) => ({
      ts: now - i,
      via: 'chat',
      project: '',
    }))
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await openWorkLog()
    await screen.findByTestId('member-stats')
    act(() => {
      memberProjectionStore.apply('oncall', 'activity', { recent, today: 50, week: 50 }, 5)
    })
    await waitFor(() =>
      expect(screen.getByTestId('member-stats')).toHaveTextContent('50+'),
    )
  })

  it('a failed activity fetch renders the error state, never the affirmative empty state', async () => {
    vi.mocked(api.memberActivity).mockRejectedValue(new Error('boom'))
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await openWorkLog()
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

  it('a projection left behind by a refused append does not outrank the transcript preview', async () => {
    // Every other field the projected view merges is CONFIG-derived, so the event
    // log is where it is written and the projection is the record. A message
    // preview is not: the row carries it from the conversation transcript, which
    // is the store the message was persisted through, and the member/message
    // event is a second copy appended afterwards on a best-effort hook. When that
    // append is refused the projection keeps the PREVIOUS message, so giving it
    // precedence renders a stale preview over the fresh value sitting beside it in
    // the same payload -- with nothing on the card saying which one it is.
    const stale = {
      asOfSeq: 1,
      values: {
        roster: {
          name: 'oncall',
          slug: 'oncall',
          last_message: 'a message from before the refused append',
        },
        wake: { patrol: 'none' as const },
        driving: { open: [] },
      },
    }
    // The second member is the CONTROL: its row carries no transcript preview at
    // all, and its projection does. Without it, dropping the projection entirely
    // would satisfy the assertions below while a pushed frame stopped rendering.
    const fillIn = {
      asOfSeq: 1,
      values: {
        roster: {
          name: 'quiet',
          slug: 'quiet',
          last_message: 'only the projection has this one',
        },
        wake: { patrol: 'none' as const },
        driving: { open: [] },
      },
    }
    await renderPage([
      row({ last_message: 'the message the transcript actually holds', projections: stale }),
      row({ name: 'quiet', slug: 'quiet', last_message: '', projections: fillIn }),
    ])
    await rosterRow('oncall')

    expect(screen.getByText('the message the transcript actually holds')).toBeTruthy()
    expect(screen.queryByText('a message from before the refused append')).toBeNull()
    expect(screen.getByText('only the projection has this one')).toBeTruthy()
  })

  it('a just-stopped thread shows a Stopped chip; a later real message takes it down', async () => {
    // #9708: after PR #9689 the roster preview is the last CONVERSATIONAL line
    // (the stop card's JSON is skipped), so a thread the user just stopped
    // reads as ongoing work ("Running the analysis now."). The server flags the
    // thread whose newest event is a stop with `last_message_stopped`, and the
    // page renders a LOCALIZED chip beside the preview — the word is never sent
    // from the server. The other row (no flag) is the cleared state: the moment
    // a newer real message lands the server drops the flag and the chip is gone.
    await renderPage([
      row({ last_message: 'Running the analysis now.', last_message_stopped: true }),
      row({ name: 'talker', slug: 'talker', last_message: 'On it — pushing the fix.' }),
    ])
    await rosterRow('oncall')
    // The stopped member's row carries the chip, with the localized label…
    const chip = roster().getByTestId('member-stopped-indicator')
    expect(chip).toBeTruthy()
    expect(chip).toHaveTextContent('Stopped')
    // …and the conversational preview still shows beside it.
    expect(roster().getByText('Running the analysis now.')).toBeTruthy()
    // The un-flagged member (a newer real message replaced the stop) shows no
    // chip — exactly one chip on the whole roster.
    expect(roster().getAllByTestId('member-stopped-indicator')).toHaveLength(1)
    expect(roster().getByText('On it — pushing the fix.')).toBeTruthy()
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

  it('keeps a member present while its delegated workers run, then clears it', async () => {
    const { store } = await renderPage([
      row({ bound: true, slot_key: 'member-oncall', running: false }),
    ])
    fireEvent.click(await rosterRow('oncall'))
    await openWorkLog()
    act(() => {
      store.dispatch(sseSlots([{
        key: 'member-oncall', mode: 'member', running: false,
        subagents_running: true, messages: 0,
      }] as never))
    })
    expect(screen.getAllByTestId('member-presence-dot')).toHaveLength(1)
    expect(screen.getByTestId('member-summary-status')).toHaveTextContent('Delegated work running')
    expect(screen.getByTestId('member-driving-empty')).toHaveTextContent(/not driving any sessions/i)
    act(() => {
      store.dispatch(sseSlots([{
        key: 'member-oncall', mode: 'member', running: true,
        subagents_running: true, messages: 0,
      }] as never))
    })
    expect(screen.getByTestId('member-summary-status')).toHaveTextContent(/^Working$/)
    act(() => {
      store.dispatch(sseSlots([{
        key: 'member-oncall', mode: 'member', running: false,
        subagents_running: false, messages: 0,
      }] as never))
    })
    expect(screen.queryByTestId('member-presence-dot')).toBeNull()
    expect(screen.getByTestId('member-summary-status')).not.toHaveTextContent('Delegated work running')
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

describe('MembersPage Notes tab (the crewmate\'s own notes)', () => {
  const briefing = (over: Record<string, unknown> = {}) => ({
    slug: 'oncall',
    member: 'oncall',
    supported: true,
    text: '## What I look after\n\n- The issue queue.\n',
    updated_ts: Date.now() / 1000 - 600,
    redacted: false,
    truncated: false,
    ...over,
  })

  async function openNotes() {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    return screen.findByTestId('member-notes', PANE_READY)
  }

  it('renders the briefing as markdown, dated, read-only: no editor and no file open anywhere in the tab', async () => {
    vi.mocked(api.memberBriefing).mockResolvedValue(briefing())
    const notes = await openNotes()
    const body = await within(notes).findByTestId('member-notes-body')
    expect(within(body).getByRole('heading', { name: 'What I look after' })).toBeInTheDocument()
    expect(within(body).getByText('The issue queue.')).toBeInTheDocument()
    // Keyed by the exact name as well as the slug — slugs are lossy.
    expect(api.memberBriefing).toHaveBeenCalledWith('oncall', 'oncall')
    expect(within(notes).getByTestId('member-notes-footer')).toHaveTextContent(/^Updated /)
    // The file is agent-written; the dashboard offers no editor for it.
    expect(within(notes).queryByRole('button', { name: /edit/i })).toBeNull()
    expect(within(notes).queryByTestId('member-notes-hidden')).toBeNull()
  })

  it('no notes yet is an EMPTY state naming the crewmate, undated — never an error', async () => {
    vi.mocked(api.memberBriefing).mockResolvedValue(briefing({ text: '', updated_ts: null }))
    const notes = await openNotes()
    const empty = await within(notes).findByTestId('member-notes-empty')
    expect(empty).toHaveTextContent("oncall hasn't written any notes yet.")
    expect(within(notes).queryByRole('alert')).toBeNull()
    expect(within(notes).queryByTestId('member-notes-footer')).toBeNull()
  })

  it('a platform that cannot read the file safely says so in one sentence, not as an alert', async () => {
    vi.mocked(api.memberBriefing).mockResolvedValue(briefing({ supported: false, text: '', updated_ts: null }))
    const notes = await openNotes()
    expect(await within(notes).findByTestId('member-notes-unsupported')).toHaveTextContent(
      "Notes can't be read on this computer.",
    )
    expect(within(notes).queryByRole('alert')).toBeNull()
  })

  it('a failed read renders the shared ErrorNotice, never the affirmative empty state', async () => {
    vi.mocked(api.memberBriefing).mockRejectedValue(new Error('boom'))
    const notes = await openNotes()
    const notice = await within(notes).findByTestId('member-notes-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent("Couldn't load this crewmate's notes.")
    expect(within(notes).queryByTestId('member-notes-empty')).toBeNull()
    expect(within(notes).queryByTestId('member-notes-footer')).toBeNull()
  })

  it('two crewmates sharing the slug: the read is refused and the panel says so in plain words', async () => {
    vi.mocked(api.memberBriefing).mockRejectedValue(new ApiError(409, 'briefing_slug_ambiguous'))
    const notes = await openNotes()
    const line = await within(notes).findByTestId('member-notes-collision')
    expect(line).toHaveTextContent(/Two crewmates share this short name/)
    expect(within(notes).queryByRole('alert')).toBeNull()
    expect(within(notes).queryByTestId('member-notes-footer')).toBeNull()
  })

  it('a briefing that was redacted on the way out says so in a visible line above the text, not a tooltip', async () => {
    vi.mocked(api.memberBriefing).mockResolvedValue(briefing({ text: 'Token: [REDACTED: credential]', redacted: true }))
    const notes = await openNotes()
    await within(notes).findByTestId('member-notes-body')
    const hidden = within(notes).getByTestId('member-notes-hidden')
    expect(hidden).toHaveAttribute('role', 'status')
    expect(hidden).toHaveTextContent(/looks like a secret was hidden here/)
    // Above the body, so the placeholder is explained before it is read.
    expect(hidden.compareDocumentPosition(within(notes).getByTestId('member-notes-body')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('a briefing cut at the cap says the rest is only in the file, above the text', async () => {
    vi.mocked(api.memberBriefing).mockResolvedValue(
      briefing({ text: 'y'.repeat(200) + '\n[... briefing truncated at cap — prune it]', truncated: true }),
    )
    const notes = await openNotes()
    await within(notes).findByTestId('member-notes-body')
    expect(within(notes).getByTestId('member-notes-hidden')).toHaveTextContent(/longer than the panel shows/)
  })

  it('keys the briefing under the registry prefix, so a roster refresh revalidates cached notes', () => {
    expect(memberBriefingQueryKey('oncall', 'oncall')).toEqual([
      'kirocrew-agents',
      'member-briefing',
      'oncall',
      'oncall',
    ])
  })

  it('a refetch that fails over cached notes keeps them on screen under a "could not refresh" notice, never silently', async () => {
    vi.mocked(api.memberBriefing)
      .mockResolvedValueOnce(briefing())
      .mockRejectedValueOnce(new Error('boom'))
    const notes = await openNotes()
    await within(notes).findByTestId('member-notes-body')
    // Leaving and returning re-issues the read (`enabled` flips); the second
    // answer is a failure, but the first answer is still what the user saw.
    await openWorkLog()
    fireEvent.click(screen.getByTestId('side-panel-leading-tab-crew-notes'))
    const again = await screen.findByTestId('member-notes', PANE_READY)
    const notice = await within(again).findByTestId('member-notes-refresh-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent("Couldn't refresh these notes.")
    expect(within(again).getByTestId('member-notes-body')).toBeInTheDocument()
    expect(within(again).queryByTestId('member-notes-error')).toBeNull()
  })

  it('the read is gated on the tab being on screen: nothing is fetched while the Work log is showing', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-notes', PANE_READY)
    await waitFor(() => expect(api.memberBriefing).toHaveBeenCalledTimes(1))
    await openWorkLog()
    // The Work log's own reads happen; the notes read does not repeat.
    await waitFor(() => expect(api.memberActivity).toHaveBeenCalled())
    expect(api.memberBriefing).toHaveBeenCalledTimes(1)
  })
})

describe('MembersPage Work log — session record', () => {
  it('embeds the thread\'s Crew Log once the slot is confirmed, under its own heading', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    const workLog = await openWorkLog()
    const record = await within(workLog).findByTestId('member-session-record')
    expect(record).toHaveTextContent('This conversation')
    expect(within(record).getByTestId('crew-log-tab')).toBeInTheDocument()
    await waitFor(() => expect(api.sessionCrewLogProjections).toHaveBeenCalledWith('member-oncall'))
  })

  it('renders no session record while the thread open is refused (no confirmed slot)', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })], 'kirocrew', {
      thread: new Error('409'),
    })
    fireEvent.click(await rosterRow('oncall'))
    const workLog = await openWorkLog()
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledWith('oncall'))
    expect(within(workLog).queryByTestId('member-session-record')).toBeNull()
    expect(api.sessionCrewLogProjections).not.toHaveBeenCalled()
  })
})

describe('MembersPage viewed-thread registration', () => {
  // The websocket unread-marker gates on `chat.activeSlot` OR the slot
  // registered in `viewedThread`; this page never moves `chat.activeSlot`, so
  // the registration is what stops every message in the OPEN thread from
  // being flagged (and drained a render later -- a badge that lit and
  // vanished on the parent dashboard's crew tab for each message).

  it('registers the mounted thread while the window is visible and focused', async () => {
    await renderPage()
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
    await waitFor(() => expect(getViewedThreadSlot()).toBe('member-oncall'))
  })

  it('re-registers on switch: the new thread replaces the old one', async () => {
    await renderPage([row(), row({ name: 'scout', slug: 'scout' })])
    fireEvent.click(await rosterRow('oncall'))
    await waitFor(() => expect(getViewedThreadSlot()).toBe('member-oncall'))
    fireEvent.click(await rosterRow('scout'))
    await waitFor(() => expect(getViewedThreadSlot()).toBe('member-scout'))
  })

  it('retires the registration while the window is hidden, and restores it on reveal', async () => {
    await renderPage()
    fireEvent.click(await rosterRow('oncall'))
    await waitFor(() => expect(getViewedThreadSlot()).toBe('member-oncall'))
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    await waitFor(() => expect(getViewedThreadSlot()).toBeNull())
    hidden.mockReturnValue(false)
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    await waitFor(() => expect(getViewedThreadSlot()).toBe('member-oncall'))
    hidden.mockRestore()
  })

  it('flushes the pending trailing read synchronously when the registration retires', async () => {
    const { unmount } = await renderPage()
    fireEvent.click(await rosterRow('oncall'))
    await waitFor(() => expect(getViewedThreadSlot()).toBe('member-oncall'))
    // Start after the opening read, with only this burst in the relay buffer.
    _resetSlotReadRelayForTest()
    vi.useFakeTimers()
    const sent: [string, string | undefined][] = []
    bindSlotReadSender((slot, ts) => sent.push([slot, ts]))
    try {
      emitSlotRead('member-oncall', '2026-09-10T00:00:01Z')
      emitSlotRead('member-oncall', '2026-09-10T00:00:02Z')
      expect(sent).toEqual([['member-oncall', '2026-09-10T00:00:01Z']])

      unmount()

      // No timer advancement: the component cleanup must send the trailing read.
      expect(sent).toEqual([
        ['member-oncall', '2026-09-10T00:00:01Z'],
        ['member-oncall', '2026-09-10T00:00:02Z'],
      ])
      expect(getViewedThreadSlot()).toBeNull()
    } finally {
      unmount()
      _resetSlotReadRelayForTest()
      vi.useRealTimers()
    }
  })

  it('retires the registration on unmount', async () => {
    const { unmount } = await renderPage()
    fireEvent.click(await rosterRow('oncall'))
    await waitFor(() => expect(getViewedThreadSlot()).toBe('member-oncall'))
    unmount()
    expect(getViewedThreadSlot()).toBeNull()
  })
})

describe('MembersPage unread drain', () => {
  // A flag set while the thread was NOT on screen (closed, or this window
  // hidden) is drained when it opens or is revealed -- the page itself must
  // do it, or the Crew Members rail badge is permanent (nothing else clears
  // a live member slot's unread).

  it('opening a flagged member thread drains its unread flag', async () => {
    const { store } = await renderPage()
    act(() => {
      store.dispatch(markSlotUnread('member-oncall'))
    })
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
    await waitFor(() =>
      expect(store.getState().dashboard.unreadSlots).not.toContain('member-oncall'),
    )
  })

  it('a live message re-flagging the MOUNTED thread is drained again, not left as a stuck badge', async () => {
    const { store } = await renderPage()
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
    // A flag landing on the open thread from elsewhere (a manual mark-as-unread,
    // a restored badge) is still drained -- the marker itself no longer flags
    // the registered thread, so this is the belt behind that suspender.
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
    await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
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
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-scout')
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
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-scout')
    act(() => {
      store.dispatch(markSlotUnread('member-oncall'))
    })
    expect(await screen.findByTestId('member-unread-dot')).toBeInTheDocument()
    fireEvent.click(await rosterRow('oncall'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall'))
    await waitFor(() => expect(screen.queryByTestId('member-unread-dot')).toBeNull())
  })
})

describe('MembersPage Work log — driving sessions', () => {
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
    await openWorkLog()
    return utils
  }

  it('before the first slots frame it shows a skeleton, never the affirmative "not driving"', async () => {
    // No sseSlots dispatch: `slotsLoaded` is false, so an empty list is
    // ambiguous (cold open / WS reconnect) and must not read as a verdict.
    const { store } = await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await openWorkLog()
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
    await openWorkLog()
    fireEvent.click(screen.getByTestId('member-driving-toggle'))
    expect(screen.getAllByTestId('member-driving-row')).toHaveLength(6)
    fireEvent.click(await rosterRow('research'))
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledWith('research'))
    await openWorkLog()
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
    await openWorkLog()
    await waitFor(() => expect(screen.queryByTestId('member-patrol-loading')).toBeNull())
    return utils
  }

  // `mockResolvedValue` / `mockRejectedValue` outlive `vi.clearAllMocks()`
  // (that clears calls, not implementations), so each case starts from the
  // module default rather than inheriting the previous case's registry.
  beforeEach(() => {
    ;(api.autonudgeList as ReturnType<typeof vi.fn>).mockResolvedValue({ enabled: true, loops: [] })
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

  it('a stop the loop registry has already forgotten still renders from the wake projection', async () => {
    // The restart case: the gateway died mid-patrol, the registry came back
    // empty, and the loader synthesised the stop into the member log. The
    // registry alone would read "nothing scheduled"; the pushed `wake` value
    // is the only record that the patrol existed and how it ended.
    await openDrawerWith({ loops: [] })
    act(() => {
      memberProjectionStore.apply(
        'oncall',
        'wake',
        { patrol: 'stopped', slot_key: 'member-oncall', stopped_reason: 'interrupted', since: Date.now() },
        99,
      )
    })
    await waitFor(() => expect(screen.getByTestId('member-patrol')).toHaveAttribute('data-state', 'stopped'))
    expect(screen.getByTestId('member-patrol-status')).toHaveTextContent(/patrol stopped/i)
    expect(screen.getByTestId('member-patrol-reason')).toHaveTextContent(/interrupted/i)
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

  it('a failed registry read renders the error state, never the affirmative empty state', async () => {
    ;(api.autonudgeList as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await openWorkLog()
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
    await screen.findByTestId('member-notes')
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

  it('encodes the crew name in the deep link', async () => {
    await renderPage([row({ name: 'on call/2', slug: 'on-call-2', bound: true, slot_key: 'member-on-call-2' })])
    fireEvent.click(await rosterRow('on call/2'))
    fireEvent.click(await screen.findByTestId('member-edit-name-button'))
    expect(navigateSpy).toHaveBeenCalledWith('/capabilities?tab=crews&crew=on%20call%2F2')
  })
})

describe('resolveDefaultMember', () => {
  const ordered = [row({ name: 'alpha', slug: 'alpha' }), row({ name: 'beta', slug: 'beta' })]

  it('nothing remembered -> undefined: a fresh visit opens no one', () => {
    // No first-row fallback anymore (#11763): with no memory there is no
    // member the user chose, so the page lands on the empty column.
    expect(resolveDefaultMember(null, ordered)).toBeUndefined()
    expect(resolveDefaultMember('', ordered)).toBeUndefined()
  })

  it('restore: the remembered member when it is still on the roster', () => {
    expect(resolveDefaultMember('beta', ordered)?.name).toBe('beta')
  })

  it('stale: a remembered member that is gone resolves to undefined, not the first row', () => {
    expect(resolveDefaultMember('ghost', ordered)).toBeUndefined()
  })

  it('an empty roster resolves to nothing, never throws', () => {
    expect(resolveDefaultMember('beta', [])).toBeUndefined()
  })
})

describe('MembersPage default member, memory and URL', () => {
  const alphaBeta = () => [row({ name: 'alpha', slug: 'alpha' }), row({ name: 'beta', slug: 'beta' })]

  it('a fresh visit with nothing remembered opens no one — it lands on the roster, not the first row', async () => {
    await renderPage([
      row({ name: 'zeta-quiet', slug: 'zeta-quiet' }),
      row({ name: 'fresh-talker', slug: 'fresh-talker', last_active_ts: 200 }),
      row({ name: 'old-talker', slug: 'old-talker', last_active_ts: 100 }),
    ])
    // No memory, no ?member=: the page must NOT prime the user on whichever
    // row the 'recent' sort floated to the top (#11763). The 'Pick a member'
    // empty pane shows, no thread is mounted for a default, the URL stays
    // bare, and the memory is untouched — until the user chooses.
    await screen.findByText(/Pick a member/i)
    expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
    expect(api.memberThread).not.toHaveBeenCalled()
    expect(currentUrl()).toBe('/members')
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBeNull()
    // A click opens the chosen member: the roster is fully interactive.
    fireEvent.click(await rosterRow('old-talker'))
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-old-talker')
    expect(api.memberThread).toHaveBeenCalledWith('old-talker')
    expect(currentUrl()).toBe('/members?member=old-talker')
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('old-talker')
  })

  it('a fresh visit WITH a remembered member still auto-opens it', async () => {
    localStorage.setItem(LAST_MEMBER_KEY, 'beta')
    await renderPage(alphaBeta())
    // Returning users are unaffected: the remembered member is restored on
    // arrival with no click, its thread mounted, and the URL rewritten.
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-beta')
    expect(api.memberThread).toHaveBeenCalledWith('beta')
    expect(screen.queryByText(/Pick a member/i)).toBeNull()
    expect(currentUrl()).toBe('/members?member=beta')
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
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-beta')
    expect(api.memberThread).toHaveBeenCalledTimes(1)
    expect(api.memberThread).toHaveBeenCalledWith('beta')
    expect(currentUrl()).toBe('/members?member=beta')
  })

  it('a remembered member that was deleted or renamed lands on the empty pane, without an error', async () => {
    localStorage.setItem(LAST_MEMBER_KEY, 'ghost')
    await renderPage(alphaBeta())
    // The remembered member is gone and the URL named no one, so there is
    // nothing to restore and no first-row fallback (#11763): the page lands
    // on the 'Pick a member' empty pane, the URL stays bare, and nothing is
    // announced (nobody was named). The stale memory is left as-is until the
    // user makes a new choice.
    await screen.findByText(/Pick a member/i)
    expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
    expect(api.memberThread).not.toHaveBeenCalled()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByTestId('member-gone-notice')).toBeNull()
    expect(currentUrl()).toBe('/members')
  })

  it('a URL naming a member wins over the remembered one (shallow link)', async () => {
    localStorage.setItem(LAST_MEMBER_KEY, 'alpha')
    await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=beta' })
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-beta')
    expect(api.memberThread).toHaveBeenCalledTimes(1)
    // Opening via the link also becomes the memory for the next visit.
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('beta')
  })

  it('a URL naming a gone member with NOTHING remembered returns to the roster and SAYS so', async () => {
    await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=ghost' })
    // The user asked for a specific member, but there is nothing to stand in
    // for them (no memory) — so the page returns to the roster with the notice
    // rather than silently opening the first row (#11763). The empty pane
    // shows and no thread is mounted.
    const notice = await screen.findByTestId('member-gone-roster-notice')
    expect(notice).toHaveTextContent('“ghost” is no longer on the roster')
    expect(notice).toHaveAttribute('role', 'status')
    expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
    expect(api.memberThread).not.toHaveBeenCalled()
    await screen.findByText(/Pick a member/i)
    expect(screen.queryByRole('alert')).toBeNull()
    // The return to `/members` is a navigate() issued from an effect once the
    // roster has loaded; on a slow runner the notice can render a tick before
    // that effect has run, so wait for the URL like the other redirect tests do.
    await waitFor(() => expect(currentUrl()).toBe('/members'))
    // Nothing was opened, so nothing is remembered.
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBeNull()
    // Opening a member retires the notice — and, being a choice, is remembered.
    fireEvent.click(await rosterRow('beta'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-beta'))
    expect(screen.queryByTestId('member-gone-roster-notice')).toBeNull()
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('beta')
  })

  it('a gone link falls back to the REMEMBERED member first, and leaves the memory alone', async () => {
    localStorage.setItem(LAST_MEMBER_KEY, 'beta')
    await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=ghost' })
    // The remembered member, not the first row, is the stand-in.
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-beta')
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
    // A fresh visit with nothing remembered opens no one (#11763): the empty
    // pane shows until the user picks a member.
    await screen.findByText(/Pick a member/i)
    expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
    fireEvent.click(await rosterRow('beta'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-beta'))
    expect(currentUrl()).toBe('/members?member=beta')
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('beta')
    // The row reflects the selection the URL drove.
    expect(roster().getByText('beta').closest('button')).toHaveAttribute('aria-current', 'true')
  })

  it('returning to a bare /members with nothing remembered takes the thread DOWN', async () => {
    // A member CAN be open with nothing remembered: `safeSetItem` returns
    // false when storage is denied (a locked-down embedding context, blocked
    // cookies), so the click never persists and the later read is null. Denied
    // for this one key so every other raw read in the shared providers still
    // works — the page's own two storage calls are both on it.
    const realGet = Storage.prototype.getItem
    const realSet = Storage.prototype.setItem
    const denyRead = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(function (
      this: Storage,
      key: string,
    ) {
      if (key === LAST_MEMBER_KEY) throw new DOMException('denied', 'SecurityError')
      return realGet.call(this, key)
    })
    const denyWrite = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (
      this: Storage,
      key: string,
      value: string,
    ) {
      if (key === LAST_MEMBER_KEY) throw new DOMException('denied', 'SecurityError')
      return realSet.call(this, key, value)
    })
    try {
      ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: alphaBeta(), default_agent: 'kirocrew' })
      ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementation(echoThread)
      // The app's own return-to-the-list route: the crew editor exits to a
      // BARE /members (KiroCrewAgentsPage), as does the rail's Crew Members row.
      function ReturnToList() {
        const nav = useNavigate()
        return (
          <button data-testid="return-to-list" onClick={() => nav('/members')}>
            list
          </button>
        )
      }
      renderWithProviders(
        <>
          <MembersPage />
          <ReturnToList />
          <LocationProbe />
        </>,
        { route: '/members' },
      )
      fireEvent.click(await rosterRow('beta'))
      expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-beta')
      fireEvent.click(screen.getByTestId('return-to-list'))
      await waitFor(() => expect(currentUrl()).toBe('/members'))
      // The URL names no one and there is nothing to restore, so the roster is
      // the answer: a thread left standing here is one the user did not ask
      // for, and the next message would go to it.
      await waitFor(() => expect(screen.queryByTestId('chat-pane-stub')).toBeNull())
      await screen.findByText(/Pick a member/i)
      expect(roster().getByText('beta').closest('button')).not.toHaveAttribute('aria-current')
    } finally {
      denyRead.mockRestore()
      denyWrite.mockRestore()
    }
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
    // list. That is not a gone member — it is a fetch in flight. A remembered
    // member seeds the initial arrival (a fresh visit no longer auto-opens
    // anyone, #11763); this test is about the in-flight link, not the arrival.
    localStorage.setItem(LAST_MEMBER_KEY, 'alpha')
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

  it('the FIRST click from a bare desktop URL replaces too: Back still leaves the page in one press', async () => {
    // A fresh visit with nothing remembered leaves the URL bare (#11763), so
    // this is the one open that happens with no `?member=` yet. Above md it is
    // not a navigation step — the roster and the thread sit side by side — so
    // it must REPLACE, or Back would land on the bare roster instead of
    // leaving the page. Below md that same click is the two-level step and
    // does push (its own case in the below-md block).
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
    await screen.findByText(/Pick a member/i)
    expect(currentUrl()).toBe('/members')
    fireEvent.click(await rosterRow('beta'))
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-beta')
    expect(currentUrl()).toBe('/members?member=beta')
    // One Back: off the page. A pushed open would have left the bare roster
    // entry behind it, costing a second press.
    fireEvent.click(screen.getByTestId('history-back'))
    await waitFor(() => expect(currentUrl()).toBe('/elsewhere'))
    expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
  })

  it('switching members holds ONE history entry: after walking two members, Back leaves the page in one press', async () => {
    // Driven history, not a spy: a page before /members, a real push into
    // it, real replaces while switching, and a real pop out of it. A
    // remembered member seeds the arrival auto-open — a fresh visit with
    // nothing remembered no longer opens anyone (#11763), and this test is
    // about the history shape of SWITCHING, so the restore stands in for the
    // arrival that the auto-open used to provide.
    localStorage.setItem(LAST_MEMBER_KEY, 'alpha')
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
    // Arrival: the remembered-member restore REPLACES the bare /members entry.
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-alpha')
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
      expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-beta')
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
      expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-beta')
      navigateSpy.mockClear()
      fireEvent.click(screen.getByTestId('member-back'))
      await waitFor(() => expect(screen.queryByTestId('chat-pane-stub')).toBeNull())
      expect(currentUrl()).toBe('/members')
      expect(navigateSpy).not.toHaveBeenCalledWith(-1)
    })

    it('the overlay fills the phone: the panel is handed the window width, not left to a 100% it cannot resolve (#9979)', async () => {
      // 390 is the audit's phone frame. Below SIDE_PANEL_MIN_W the panel would
      // clamp up to its minimum instead; 390 is above it, so the width the
      // panel carries must be the window's own.
      setWindowWidth(390)
      await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=beta' })
      expect(await screen.findByTestId('chat-pane-stub', PANE_READY)).toHaveTextContent('member-beta')
      fireEvent.click(screen.getByTestId('member-panel-toggle'))
      const summary = await screen.findByTestId('member-notes')
      const overlay = screen.getByTestId('member-side-panel')
      expect(overlay).toHaveAttribute('data-placement', 'overlay')
      // The SidePanel root is the first element inside the overlay's inner
      // wrapper that carries an inline width; with fillWidth it is an explicit
      // px value equal to the window, never the '100%' fallback.
      const panelRoot = Array.from(overlay.querySelectorAll<HTMLElement>('div'))
        .find((el) => el.style.width !== '' && el.contains(summary))
      expect(panelRoot).toBeDefined()
      expect(panelRoot!.style.width).toBe('390px')
      // A filled panel has no left-edge splitter: there is nothing to drag
      // against when the panel already spans the window (the chat page's rule).
      expect(overlay.querySelector('[role="separator"][aria-orientation="vertical"]')).toBeNull()
    })
  })
})


describe('MembersPage colliding slugs (live projection)', () => {
  it('withholds a live projection from every row sharing its slug', async () => {
    // A `member_projection` frame is keyed by slug ALONE, so when two configured
    // names fold to one slug nothing in the frame says which member it describes.
    // Applying it to both rows renders one member's roster state on the other's
    // row. The backend's roster read already withholds a projection for a
    // colliding row; the live path reaches the store directly, so it needs the
    // same rule or the two surfaces disagree about the same pair.
    //
    // Observed at page level through the Starred filter count, which reads the
    // MERGED list: a starred:true frame on the shared slug must move nothing.
    await renderPage([
      row({ name: 'Code_Reviewer', slug: 'code-reviewer' }),
      row({ name: 'code-reviewer', slug: 'code-reviewer' }),
    ])
    fireEvent.keyDown(await screen.findByTestId('member-filter-menu'), { key: 'Enter' })
    const starItem = await screen.findByTestId('member-filter-starred')
    expect(starItem).toHaveTextContent('0')

    act(() => {
      memberProjectionStore.apply(
        'code-reviewer',
        'roster',
        { name: 'code-reviewer', slug: 'code-reviewer', starred: true },
        5,
      )
    })

    // Still 0: neither row took the frame. Without the suppression BOTH rows
    // take it, so the count reads 2 -- one member's state on two identities.
    await waitFor(() => expect(starItem).toHaveTextContent('0'))
  })

  it('still applies a live projection when the slug is unique', async () => {
    // The complement, so the guard is a condition rather than a blanket refusal:
    // an ordinary roster keeps taking its frames.
    await renderPage([
      row({ name: 'oncall', slug: 'oncall' }),
      row({ name: 'research', slug: 'research' }),
    ])
    fireEvent.keyDown(await screen.findByTestId('member-filter-menu'), { key: 'Enter' })
    const starItem = await screen.findByTestId('member-filter-starred')
    expect(starItem).toHaveTextContent('0')

    act(() => {
      memberProjectionStore.apply(
        'research',
        'roster',
        { name: 'research', slug: 'research', starred: true },
        5,
      )
    })

    await waitFor(() => expect(starItem).toHaveTextContent('1'))
  })
})
