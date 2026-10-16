/* MembersPage: the roster, its cached read, and opening a thread. The page's
 * other cases are in the MembersPage.<topic>.test.tsx files beside this one; the
 * ones that import src/test/MembersPageTestUtils.tsx (their shared fixtures) carry
 * the same api mock and module stubs as this file, word for word: change them
 * together. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { memberProjectionStore } from '../../state/memberProjectionStore'
import { memberThreadQueryKey } from '../../api/membersQuery'
import { __resetPaneDraftsForTests } from '../../utils/chatPaneDrafts'
import { _resetViewedThreadForTests } from '../../lib/viewedThread'
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
    // The roster's team grouping reads the team list; "no teams" keeps the
    // list flat, which is the shape every case here was written against.
    teams: { list: vi.fn(() => Promise.resolve({ teams: [] })), create: vi.fn(), update: vi.fn(), remove: vi.fn() },
    // The warm greeting's read of the crewmate's work ledger. No ledger is the
    // state every case not about the greeting wants: the chat opens bare.
    crewBoard: vi.fn(() => Promise.reject(Object.assign(new Error('no_ledger'), { status: 404 }))),
    memberRecap: vi.fn(() => Promise.reject(new Error('no recap in this test'))),
    memberThread: vi.fn(),
    // Mate's first greeting: the server decides, so the page only asks.
    memberGreet: vi.fn(() => Promise.resolve({ outcome: 'started' })),
    memberActivity: vi.fn(() => Promise.resolve({ slug: '', member: '', capped: false, entries: [] })),
    // The open member's folded views. The roster list carries the `roster` view
    // alone, so the drawer's activity timeline and patrol state read their
    // baseline here. Resolved-and-empty is the state every case not about those
    // blocks wants, and the patrol tile waits for this read before it forms a
    // verdict — an unstubbed reject would leave every drawer case racing it.
    memberProjections: vi.fn(() => Promise.resolve({ asOfSeq: 0, values: {} })),
    // The Notes tab's read. "No notes yet" is the state every case not about
    // Notes wants: an empty state, not an alert.
    memberBriefing: vi.fn(() => Promise.resolve({ slug: '', member: '', supported: true, text: '', updated_ts: null, redacted: false, truncated: false })),
    // The Work log's session record (CrewLogTab) reads the thread's crew-log
    // folds; an empty, resolved read renders its own quiet empty state.
    sessionCrewLogProjections: vi.fn(() => Promise.resolve({ folds: {}, resolved: true, writesDrained: true })),
    // The team view's "Needs you" reads each bound crewmate's chat tail.
    chatSlotDetail: vi.fn(() => Promise.resolve({ messages: [] })),
    // The auto-patrol block and roster badge read the whole loop registry;
    // the default is "feature on, nothing armed" so every other case renders
    // the page without a loop in the way.
    autonudgeList: vi.fn(() => Promise.resolve({ enabled: true, loops: [] })),
    autonudgeForSlot: vi.fn(() => Promise.resolve({ enabled: true, loop: null })),
    monitorForSlot: vi.fn(() => Promise.resolve({ enabled: true, monitor: null })),
    // The side panel's + menu gates its Summary row on this read; "disabled"
    // keeps the chat-style Summary row out of the menu, next to the Work log
    // chip it would be a second, unrelated summary of the same thread.
    sessionSummary: vi.fn(() => Promise.resolve({ enabled: false })),
    // The crew webview drawer. The Dashboard tab no longer reads it (the tab is
    // the crewmate's dynamic dashboard, mocked out below), but other surfaces on
    // this page still open it, and an unstubbed read rejects into a red alert.
    // The goal record behind the Profile card's About. "No goal yet" is the
    // state every case here wants: a failed read says so in an alert.
    memberGoals: {
      get: vi.fn(() => Promise.resolve({
        slug: '', member: '', goals: [],
        key_results: [], reports: [], removed_key_result: null, removed_goal: null,
        schedules: { time: '09:00', timezone: '', daily: false, weekly: false, weekday: 1, monthly: false, month_day: 1, heartbeat: false, interval: 60 },
        limits: { goal_text_max_chars: 2000, key_result_text_max_chars: 200, key_result_value_max_chars: 40, key_result_reading_max_chars: 120 },
      })),
    },
    // The panel's session-status frame reads the crewmate's automatic dashboard
    // card. "Waiting, nothing published" is the state every case here is about;
    // an unstubbed read rejects and the frame raises the same red alert, which
    // on the landing tab would read as an error on a page that is behaving.
    dashboardCard: vi.fn(() => Promise.resolve({ card: null, status: 'waiting', published_at: null, content_event_at: null, stale: false })),
    pendingQuestions: vi.fn(() => Promise.resolve([])),
    approvals: vi.fn(() => Promise.resolve([])),
    workflowRuns: vi.fn(() => Promise.resolve({ runs: [] })),
    sessionWorkProjection: vi.fn(() => Promise.resolve({ value: { items: [] } })),
    artifacts: vi.fn(() => Promise.resolve({ artifacts: [] })),
    // `dashboard.crewmate_threads` (reply threads) is read from the shared config
    // query; an empty config is the default -- the flag is OFF.
    kirocrewConfig: vi.fn(() => Promise.resolve({})),
    // The Schedules chip's count and its tab body read the whole cron list and
    // filter it per crewmate (`wakesCrew`). Resolved-and-empty is the state every
    // case not about schedules wants: the chip reads 0/0, which is an ANSWER of
    // none — an unstubbed reject would instead make it unknown and drop the badge,
    // and the strip assertions here would then pass for the wrong reason.
    crons: vi.fn(() => Promise.resolve({ jobs: [] })),
    // `wakesCrew`'s default-crew fallback needs to know which crew is the default,
    // or every unbound job in the install would be attributed to whichever
    // crewmate happens to be open.
    defaultAgent: vi.fn(() => Promise.resolve({ default_agent: 'kirocrew' })),
    // The schedule row's own controls (pause / run now), reached through
    // `useCronActions` once the Schedules tab is open.
    toggleCron: vi.fn(() => Promise.resolve({})),
    runCron: vi.fn(() => Promise.resolve({})),
    cancelCron: vi.fn(() => Promise.resolve({})),
    cronToChat: vi.fn(() => Promise.resolve({})),
    // New crewmate dialog's option reads (installed agents, workspaces) and its
    // create write. Quiet defaults — one custom agent list item and one
    // workspace — so the dialog renders without its own options-failed notice
    // in every case that does not open it.
    agentCatalog: vi.fn(() => Promise.resolve({ agents: [], default_agent: 'kirocrew' })),
    // The crew editor's roster read (KiroCrewAgent list), fired when the
    // thread-header pencil opens the in-place editor (CREW-18688).
    kirocrewAgents: vi.fn(() => Promise.resolve({ agents: [], default_agent: 'kirocrew' })),
    workspaces: vi.fn(() => Promise.resolve({ workspaces: [{ name: 'default' }] })),
    createWorkspace: vi.fn(() => Promise.resolve({ name: 'staging' })),
    createKirocrewAgent: vi.fn(() => Promise.resolve({ ok: true })),
    // The guided (embedded Meet CrewMates) flow's reads and its schedule write.
    agentsInstalled: vi.fn(() => Promise.resolve([])),
    getSlackConfig: vi.fn(() => Promise.resolve({ connected: false })),
    createCron: vi.fn(() => Promise.resolve({ ok: true })),
    crons: vi.fn(() => Promise.resolve({ jobs: [] })),
    // sendTurn's transport call, for any turn sent into a freshly
    // created crewmate's chat.
    sendChat: vi.fn(() =>
      Promise.resolve(new Response(JSON.stringify({ ok: true, delivered: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })),
    ),
  },
}))

// The reply-thread footer read. Spied so the flag cases below can pin that it
// is never issued while `dashboard.crewmate_threads` is off.
const threadsSummary = vi.fn(() => Promise.reject(new Error('threads unavailable')))
vi.mock('../../api/threads', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/threads')>()
  return {
    ...actual,
    threadsApi: { ...actual.threadsApi, summary: (...args: unknown[]) => threadsSummary(...args) },
  }
})

/* The page now hosts the chat page's SidePanel. Its strip and + menu are what
 * these cases drive; the heavy tab BODIES (editors, terminals, previews) are
 * not, so they are stubbed the way the panel's own suites stub them
 * (test/sidePanelPinnedAlwaysPresent.test.tsx). Terminal is reported ENABLED
 * so the + menu case below can assert the per-chat Terminal row is offered on
 * a member DM. */
vi.mock('../chat/ActivityViewer', () => ({ default: () => null }))
// The crew editor is exercised by its own suite. Here we only need to prove the
// thread-header pencil OPENS it in place (CREW-18688) — a route change no more —
// so a probe renders the crew it was pointed at.
vi.mock('../../components/crew/CrewEditorModal', () => ({
  default: ({ ctl }: { ctl: { editing: string } }) =>
    ctl.editing ? <div data-testid="crew-editor-dialog-open">{ctl.editing}</div> : null,
}))
// The editor hook runs inside MembersPage even when closed; its option/roster
// reads are gated on an open editor, but stub the module so the hook needs no
// live query wiring in this suite. Returns a minimal controller the probe reads.
vi.mock('../../components/crew/useCrewEditor', () => ({
  useCrewEditor: ({ editingName }: { editingName: string }) => ({ open: !!editingName, editing: editingName, dirtyPanes: new Set() }),
  INHERIT_MODEL: 'auto',
}))
// The Files body exposes the directory it was rooted in: one case below pins
// that it is the slot's RESOLVED project, never the crew's workspace NAME.
vi.mock('../chat/FilesHomePanel', () => ({
  default: ({ projectDir }: { projectDir?: string }) => (
    <div data-testid="files-home-stub" data-project-dir={projectDir ?? ''} />
  ),
}))
vi.mock('../chat/FolderPanel', () => ({ default: () => null }))
// The Dashboard tab's frame. Stubbed because its page pipeline is not this
// file's subject and its tail is asynchronous: the frame reads the dashboard,
// then mints a sandbox document for it, and a mint that settles after its own
// case has ended lands a state update on whichever case is running next --
// consuming a one-shot mock that case installed. It carries its own tests
// (CrewDynamicDashboard.test.tsx); here the stub reports the identity the tab
// passes it, which is all a page case can claim.
vi.mock('./CrewDynamicDashboard', () => ({
  default: ({ target, displayName, preview, onPreviewGone }: {
    target: { slug?: string; member?: string }
    displayName: string
    preview?: boolean
    onPreviewGone?: (gone: boolean) => void
  }) => (
    <div
      data-testid="crew-dashboard-stub"
      data-slug={target.slug}
      data-member={target.member}
      data-display-name={displayName}
      data-preview={preview ? 'true' : 'false'}
    >
      {onPreviewGone && (
        <button type="button" data-testid="crew-dashboard-stub-gone" onClick={() => onPreviewGone(true)}>gone</button>
      )}
    </div>
  ),
}))
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

// The profile settings write the live slot; the stub shows which slot the page
// handed it, so a refused thread is proven to get none.
vi.mock('./CrewProfileSettings', () => ({
  default: ({ slotKey, waiting }: { slotKey?: string | null; waiting?: boolean }) => (
    <div data-testid="profile-settings-slot" data-waiting={waiting ? '1' : '0'}>{slotKey ?? ''}</div>
  ),
}))
/** The page's own composer, read lazily so the ChatPane stub can compare identity. */
const CrewComposerRef = vi.hoisted(() => ({ current: null as unknown }))
/* ChatPane is the full chat stack (WS, Redux slot machinery). The page's own
 * contract is only "mount it with the thread's slot key", so a stub that
 * ECHOES the slot key is the strongest cheap assertion available. */
const chatPaneMounts = vi.hoisted(() => ({ count: 0 }))
vi.mock('../../components/ChatPane', async () => {
  const { useEffect } = await import('react')
  function ChatPaneStub({ slotKey, agentLocked, followContentWidth, busyMode, topInset, composerInput, crewmateGreeting, ...rest }: { slotKey: string; agentLocked?: boolean; followContentWidth?: boolean; busyMode?: string; topInset?: number; composerInput?: unknown; crewmateGreeting?: import('react').ReactNode } & Record<string, unknown>) {
    // Mount count: the New crewmate card must HIDE the pane, never unmount it.
    useEffect(() => { chatPaneMounts.count += 1 }, [])
    return (
      <div data-testid="chat-pane-stub" data-agent-locked={agentLocked ? '1' : '0'} data-follow-content-width={followContentWidth ? '1' : '0'} data-busy-mode={busyMode ?? 'split'} data-top-inset={topInset ?? ''} data-crew-composer={composerInput === CrewComposerRef.current ? '1' : '0'}>
        {slotKey}
        {crewmateGreeting}
        {/* A static opening card is gone: no host may pass one. */}
        {'assistantWelcome' in rest && <div data-testid="assistant-welcome-stub" />}
        {/* Stands in for rendered transcript links (markdown anchors). */}
        <a href="/members?create=1&name=scout&goal=Watch%20the%20release%20train" data-testid="chat-link-proposal">proposal</a>
        <a href="/members?create=1&goal=Second%20idea" data-testid="chat-link-proposal-2">second</a>
        <a href="http://[::1" data-testid="chat-link-malformed">bad</a>
        <a href="https://elsewhere.example/members?create=1&name=evil" data-testid="chat-link-foreign">foreign</a>
        {/* What the transcript's renderer hands the host for a plain click on either
            staged-dashboard link form (see dashboardPreview.link.test.tsx). */}
        {typeof rest.onArtifactOpen === 'function' && (
          <>
            <button data-testid="chat-open-preview-member" onClick={() => (rest.onArtifactOpen as (ref: string) => void)('dashboard-preview:member:oncall')}>preview by name</button>
            <button data-testid="chat-open-preview-slug" onClick={() => (rest.onArtifactOpen as (ref: string) => void)('dashboard-preview:oncall')}>preview by slug</button>
          </>
        )}
      </div>
    )
  }
  return { default: ChatPaneStub }
})

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
import { resetDashboardOnLoad } from './dashboardOnLoad'
import MembersPage from './MembersPage'
import { resetFirstGreetingRequests } from './useFirstGreeting'
import { __resetPanelTabs } from '../../hooks/usePanelTabs'
import {
  LAST_MEMBER_KEY,
  WIDE_WINDOW,
  setWindowWidth,
  row,
  assistantRow,
  defaultRow,
  PANE_READY,
  realCreateClock,
  renderPage,
  LocationProbe,
  currentUrl,
  roster,
  rosterRow,
  probeAddCrewmate,
  resetApiMocks,
} from '../../test/MembersPageTestUtils'

beforeEach(() => {
  vi.clearAllMocks()
  resetApiMocks()
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
  __resetPaneDraftsForTests()
  resetFirstGreetingRequests()
  // The remembered member must not leak between cases.
  localStorage.clear()
  // The page is reached only with the Crewmates preview on; the cases that
  // pin what happens with it off turn it off themselves.
  localStorage.setItem('mc-preview-crew', '1')
  // Nor the crewmate-created receipts (browser-session record).
  sessionStorage.clear()
  chatPaneMounts.count = 0
  // The projection store is a module-level singleton fed by the roster seed;
  // clear it so one case's seeded values do not survive into the next.
  memberProjectionStore.clear()
  // The side panel's tab strip is a module-level, persisted store; a tab
  // opened in one case would otherwise be on the strip in the next.
  __resetPanelTabs()
  // A fresh page load: no crewmate has had its strip put on the Dashboard yet.
  resetDashboardOnLoad()
  setWindowWidth(WIDE_WINDOW)
  // Module-level "thread on screen" registration; a case that unmounted
  // mid-effect would otherwise leave its slot registered for the next one.
  _resetViewedThreadForTests()
})

// Real timers back after every case: the status-strip and bounded-create cases
// fake the clock with no finally of their own, and a case that times out never
// reaches its finally, so the next case would otherwise inherit fake timers (or
// React Query's microtask scheduler).
afterEach(realCreateClock)

describe('MembersPage roster', () => {
  it('reply threads off (the default): no footer read is issued and no thread notice is drawn', async () => {
    await renderPage([row()], 'kirocrew', { route: '/members?member=oncall' })
    await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
    // The config read has resolved (to an empty config) by the time the pane is up.
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    expect(threadsSummary).not.toHaveBeenCalled()
    expect(screen.queryByTestId('member-threads-error-row')).toBeNull()
    expect(screen.queryByTestId('thread-panel')).toBeNull()
  })

  it('reply threads on: the footer read is issued for the confirmed slot and its failure is shown', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ dashboard: { crewmate_threads: true } })
    await renderPage([row()], 'kirocrew', { route: '/members?member=oncall' })
    await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
    await waitFor(() => expect(threadsSummary).toHaveBeenCalledWith('member-oncall'))
    await screen.findByTestId('member-threads-error-row', undefined, PANE_READY)
  })

  it('a failed config read is said with a Retry, not rendered as threads off', async () => {
    vi.mocked(api.kirocrewConfig).mockRejectedValue(new Error('boom'))
    await renderPage([row()], 'kirocrew', { route: '/members?member=oncall' })
    await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
    // The failure is a notice on the standard path, with the read offered again.
    await screen.findByTestId('member-threads-flag-error-row', undefined, PANE_READY)
    expect(screen.getByTestId('member-threads-flag-error')).toHaveTextContent(/Couldn't check whether reply threads are on/)
    // Not known to be on: no footer read, no panel -- and no silent "off" either.
    expect(threadsSummary).not.toHaveBeenCalled()
    expect(screen.queryByTestId('thread-panel')).toBeNull()
    // Retry re-reads; a config that now says on turns the feature on in place.
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ dashboard: { crewmate_threads: true } })
    fireEvent.click(screen.getByTestId('member-threads-flag-retry'))
    await waitFor(() => expect(threadsSummary).toHaveBeenCalledWith('member-oncall'))
    await waitFor(() => expect(screen.queryByTestId('member-threads-flag-error-row')).toBeNull())
  })

  it('repeats roster-only read failures above a DM while the desktop roster is folded', async () => {
    vi.mocked(api.autonudgeList).mockRejectedValue(new Error('patrol unavailable'))
    vi.mocked(api.teams.list).mockRejectedValue(new Error('teams unavailable'))

    await renderPage([row()], 'kirocrew', { route: '/members?member=oncall' })
    await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)

    const aside = screen.getByTestId('member-roster')
    expect(aside.className).toMatch(/\bhidden\b/)
    expect(aside.className).not.toMatch(/\bmd:flex\b/)
    const notices = within(await screen.findByTestId('member-main-roster-errors', undefined, PANE_READY))
    expect(notices.getByTestId('member-main-patrol-error')).toBeInTheDocument()
    // This one retries once before it says so (`retryThroughRestart`).
    expect(await notices.findByTestId('member-main-teams-error', undefined, PANE_READY)).toBeInTheDocument()
    // No hand-off from above the thread: Profile may hold an unsaved schedule draft.
    expect(notices.queryByRole('button', { name: /ask the agent/i })).toBeNull()
  })

  it('shows a patrol error when a refetch fails after a good cached read', async () => {
    const { queryClient } = await renderPage([row()], 'kirocrew', { route: '/members?member=oncall' })
    await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
    await waitFor(() => expect(api.autonudgeList).toHaveBeenCalled())

    vi.mocked(api.autonudgeList).mockRejectedValue(new Error('patrol unavailable'))
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['autonudge-loops'] }) })

    expect(await screen.findByTestId('member-main-patrol-error', undefined, PANE_READY)).toBeInTheDocument()
    expect(screen.getByTestId('member-roster-patrol-error')).toBeInTheDocument()
  })

  it('the teams read notice dismisses for the outage, returns on a failure after a good read, and clears on a good read', async () => {
    // The roster reads no default crewmate: a plain session is the default
    // custom agent (a template), so the teams read is the one notice here.
    vi.mocked(api.teams.list).mockRejectedValue(new Error('teams unavailable'))
    const { queryClient } = await renderPage([row()], 'kirocrew', { route: '/members?member=oncall' })
    await screen.findByTestId('member-main-teams-error', undefined, PANE_READY)

    // A real, named button; pressing it removes the notice everywhere.
    fireEvent.click(within(screen.getByTestId('member-main-teams-error')).getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(screen.queryByTestId('member-main-roster-errors')).toBeNull())
    expect(screen.queryByTestId('member-roster-teams-error')).toBeNull()

    // The same outage re-read (the 30s poll) fails again: the dismiss holds.
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['crew-teams'] }) })
    expect(screen.queryByTestId('member-main-teams-error')).toBeNull()

    // A good read ends the outage; a failure after it is new, and says so.
    vi.mocked(api.teams.list).mockResolvedValueOnce({ teams: [] })
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['crew-teams'] }) })
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['crew-teams'] }) })
    await screen.findByTestId('member-main-teams-error', undefined, PANE_READY)

    // A good read clears it with no press.
    vi.mocked(api.teams.list).mockResolvedValue({ teams: [] })
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['crew-teams'] }) })
    await waitFor(() => expect(screen.queryByTestId('member-main-teams-error')).toBeNull())
  })

  it('renders one row per member from the API', async () => {
    await renderPage([row(), row({ name: 'research', slug: 'research' })])
    expect(await rosterRow('oncall')).toBeInTheDocument()
    expect(roster().getByText('research')).toBeInTheDocument()
  })

  it('shows the empty-state hero when no crewmates exist', async () => {
    await renderPage([])
    // The hero is rendered twice: in the thread column (above md) and inside
    // the roster list (below md, `md:hidden`). CSS picks one per viewport, so
    // the DOM holds both; assert on the set, never on a single match.
    const heroes = await screen.findAllByTestId('crewmate-empty-hero')
    expect(heroes).toHaveLength(2)
    for (const title of screen.getAllByTestId('crewmate-empty-title')) {
      expect(title).toHaveTextContent('No crewmates yet')
    }
    expect(screen.getAllByText(/Give it a job; it keeps working while you are away/i)).toHaveLength(2)
    // The roster's old one-line copy and in-list button are gone: one call to
    // action per viewport besides the header "+".
    expect(screen.queryByTestId('member-empty-cta')).toBeNull()
  })
  it('treats the built-in default assistant as an empty crewmate roster (the Assistant member absent)', async () => {
    await renderPage([row({ name: 'default', slug: 'default', last_active_ts: 999 })])

    expect(await screen.findAllByTestId('crewmate-empty-hero')).toHaveLength(2)
    expect(screen.queryByTestId('member-add')).toBeNull()
    expect(api.memberThread).not.toHaveBeenCalledWith('default')
  })

  it('a default + Assistant roster opens the Assistant and asks for its first greeting, never the hero; default is untouched', async () => {
    await renderPage([defaultRow({ last_active_ts: 999 }), assistantRow()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-mate')
    expect(api.memberThread).toHaveBeenCalledWith('mate')
    expect(api.memberThread).not.toHaveBeenCalledWith('default')
    expect(screen.queryByTestId('crewmate-empty-hero')).toBeNull()
    // The Assistant is a crewmate: the "+" menu is the create door here.
    expect(screen.getByTestId('member-add')).toBeInTheDocument()
    // No static welcome card: Mate greets with a real first turn, asked for
    // once the confirmed thread is open.
    await waitFor(() => expect(api.memberGreet).toHaveBeenCalledWith('mate'))
    expect(api.memberGreet).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('assistant-welcome-stub')).toBeNull()
    expect(currentUrl()).toBe('/members?member=mate')
    // `default` keeps its ordinary row: its own name, not the Assistant's.
    expect(await rosterRow('default')).toBeInTheDocument()
  })

  it('a failed first greeting says nothing and leaves the Mate chat usable', async () => {
    ;(api.memberGreet as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('member_greet_failed'))
    await renderPage([defaultRow({ last_active_ts: 999 }), assistantRow()])
    await waitFor(() => expect(api.memberGreet).toHaveBeenCalledTimes(1), PANE_READY)
    expect((await screen.findByTestId('chat-pane-stub', undefined, PANE_READY))).toHaveTextContent('member-mate')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('only Mate\'s thread asks for a first welcome; a crewmate not created on this page asks for none', async () => {
    await renderPage([defaultRow(), row({ name: 'mate', slug: 'mate', kiro_agent: 'kirocrew', has_dm_message: true }), row({ name: 'alpha', slug: 'alpha', last_active_ts: 50 })])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-alpha')
    // The first crewmate is shown by its label, on whatever template it runs.
    fireEvent.click(await rosterRow('Mate'))
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-mate')
    await waitFor(() => expect(api.memberGreet).toHaveBeenCalledWith('mate'))
    expect(api.memberGreet).toHaveBeenCalledTimes(1)
    expect(api.memberGreet).not.toHaveBeenCalledWith('alpha')
    expect(screen.queryByTestId('assistant-welcome-stub')).toBeNull()
  })

  it('the default member, opened explicitly, asks for no Mate greeting', async () => {
    await renderPage([defaultRow(), assistantRow()], 'kirocrew', { route: '/members?member=default' })
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-default')
    expect(screen.queryByTestId('assistant-welcome-stub')).toBeNull()
    expect(api.memberGreet).not.toHaveBeenCalled()
    expect(screen.getByTestId('member-title-row')).toHaveTextContent('default')
  })

  it('shows the load-failure state when the roster call rejects', async () => {
    ;(api.members as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    renderWithProviders(<MembersPage />)
    // Both columns say it: the roster's own notice, and — since no chat can
    // open and the hero is gated on a good read — the chat column's, so a
    // wide window is never half blank.
    expect(await screen.findByTestId('member-roster-error')).toHaveTextContent(/Could not load your crewmates/i)
    const column = screen.getByTestId('member-column-load-error')
    expect(column).toHaveTextContent(/Could not load your crewmates/i)
    expect(column.className).toMatch(/\bmd:flex\b/)
    expect(screen.queryByTestId('crewmate-empty-hero')).toBeNull()
    // No roster to count: the header says so with a dash, never "0 members"
    // above a failure it would contradict.
    expect(screen.getByTestId('member-count')).toHaveTextContent('\u2014')
    expect(screen.getByTestId('member-count')).not.toHaveTextContent(/members/i)
    // Both placements offer the plain retry first, beside the hand-off link;
    // the retry repeats the roster read, and a good answer replaces the notice.
    expect(screen.getByTestId('member-roster-retry')).toHaveTextContent('Try again')
    expect(screen.getByTestId('member-column-load-retry')).toHaveTextContent('Try again')
    // The "+" menu's New crewmate item is held, and says why: with no roster
    // read the dialog cannot refuse a name that clashes with an unseen
    // crewmate (ONCALL beside oncall would share one slug and its chat could
    // never open). New team, which has no part in that check, stays live.
    expect(await probeAddCrewmate()).toEqual({ held: true, reason: 'Could not load your crewmates.' })
    ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: [row()], default_agent: 'kirocrew' })
    fireEvent.click(screen.getByTestId('member-column-load-retry'))
    expect(await rosterRow('oncall')).toBeInTheDocument()
    expect(screen.queryByTestId('member-roster-error')).toBeNull()
    expect(screen.queryByTestId('member-column-load-error')).toBeNull()
    expect(await probeAddCrewmate()).toEqual({ held: false, reason: null })
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
    expect(screen.queryByText(/No crewmates yet/i)).toBeNull()
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
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    expect(api.memberThread).toHaveBeenCalledTimes(1)
    utils.rerender(<LocationProbe />)
    // The re-open's POST hangs forever: if the thread column waited on the
    // network, "Opening the conversation…" would be all it shows.
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}))
    utils.rerender(page)
    expect(await screen.findByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
    // The cached chat is already usable; only the Dashboard may say it is reconnecting.
    expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
    // Every open still goes through the endpoint — the cache decides what to
    // render while the POST is out, it never replaces the POST.
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledTimes(2))
  })

  it('a failed repair over a cached thread keeps the thread up and says the RECONNECT failed, not the open', async () => {
    // A remembered member restores the thread on arrival (#11763); this test
    // is about a failed REPAIR over a cached thread, not the arrival rule.
    localStorage.setItem(LAST_MEMBER_KEY, 'oncall')
    const utils = await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    utils.rerender(<LocationProbe />)
    const rawReason = 'The recorded private memory is unavailable. token=repair-secret-value'
    const report = recordError({ source: 'api', message: rawReason, status: 409, code: 'memory_unavailable' })
    vi.mocked(api.memberThread).mockRejectedValue(new Error(rawReason))
    utils.rerender(page)
    const notice = await screen.findByTestId('member-thread-error')
    expect(notice).toHaveTextContent(/Couldn't reconnect this chat/i)
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
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall-confirmed'), PANE_READY)
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
    expect(screen.queryByText(/No crewmates yet/i)).toBeNull()
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
    expect(screen.queryByText(/Could not load your crewmates/i)).toBeNull()
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
    const notice = await screen.findByTestId('member-thread-error', undefined, PANE_READY)
    expect(notice).toHaveTextContent(/Could not open this crewmate's chat/i)
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
    CrewComposerRef.current = (await import('./CrewComposer')).default
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
    // The host passes its crewmate identity; ChatPane owns the responsive
    // send mode, leaving its default for ordinary session panes unchanged.
    expect(pane).toHaveAttribute('data-busy-mode', 'split')
    // The Crew page draws its own composer (no model / effort / permission /
    // context toolbar), not the ordinary chat one.
    expect(pane).toHaveAttribute('data-crew-composer', '1')
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
      await screen.findByText(/Could not open this crewmate's chat/i, undefined, PANE_READY),
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
    const notice = await screen.findByText(/shares its short name with/i)
    // It names both crewmates: the one whose chat this is goes, the owner stays.
    expect(notice).toHaveTextContent('delete “oncall” and create it again')
    expect(notice).toHaveTextContent('Keep “Oncall”.')
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
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-beta'), PANE_READY)
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
