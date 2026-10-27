/* MembersPage: a New crewmate create in progress — the leave guard over a
 * typed draft, the create path and its first greeting, the controls locked in
 * flight, the nested New workspace form, a create the connection drops, a create
 * or chat that lands after the user left the page, and a chat open that fails
 * right after the create. What a create leaves behind and the names it refuses
 * are in MembersPage.newCrewmateOutcomes.test.tsx.
 * This file, MembersPage.test.tsx and the other files that import
 * src/test/MembersPageTestUtils.tsx (their shared fixtures) carry the same api
 * mock and module stubs below, word for word: change them together. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { memberProjectionStore } from '../../state/memberProjectionStore'
import { __resetPaneDraftsForTests } from '../../utils/chatPaneDrafts'
import { _resetViewedThreadForTests } from '../../lib/viewedThread'
import {
  __resetErrorJournalForTests,
  __resetNavSeamForTests,
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
        schedules: { time: '09:00', timezone: '', daily: false, weekly: false, weekday: 1, monthly: false, month_day: 1 },
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
      <div data-testid="chat-pane-stub" data-agent-locked={agentLocked ? '1' : '0'} data-follow-content-width={followContentWidth ? '1' : '0'} data-busy-mode={busyMode ?? 'split'} data-top-inset={topInset ?? ''} data-crew-composer={composerInput === CrewComposerRef.current ? '1' : '0'} data-push-to-talk={rest.pushToTalk ? '1' : '0'}>
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
import NewCrewmateDialog, { RECONCILE_BOUND_MS } from './NewCrewmateDialog'
import { resetDashboardOnLoad } from './dashboardOnLoad'
import { resetFirstGreetingRequests } from './useFirstGreeting'
import { __resetPanelTabs } from '../../hooks/usePanelTabs'
import {
  WIDE_WINDOW,
  setWindowWidth,
  row,
  echoThread,
  PANE_READY,
  fakeCreateClock,
  realCreateClock,
  stepCreateClock,
  renderPage,
  currentUrl,
  rosterRow,
  clickAddCrewmate,
  clickAddAdvanced,
  unfoldAdvanced,
  waitForAddCrewmate,
  resetApiMocks,
} from '../../test/MembersPageTestUtils'
const clickRailMembers = () => fireEvent.click(screen.getByTestId('rail-members'))
const clickRawMembers = () => fireEvent.click(screen.getByTestId('rail-members-raw'))
const askToLeave = () => {
  fireEvent.click(screen.getByTestId('leave-probe'))
  return screen.getByTestId('leave-probe').textContent
}

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

describe('New mate dialog', () => {
  const openDialog = async () => {
    await clickAddAdvanced()
    return await screen.findByTestId('crewmate-create-form')
  }

  it('a typed draft vetoes an in-app navigation until the user confirms; an empty or closed dialog lets it through', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    try {
      await renderPage([row()])
      expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
      // Nothing open: leaving is free.
      expect(askToLeave()).toBe('true')
      expect(confirmSpy).not.toHaveBeenCalled()
      await openDialog()
      // Open but untouched: still free, no prompt.
      expect(askToLeave()).toBe('true')
      expect(confirmSpy).not.toHaveBeenCalled()
      // A typed name is a draft: the shell must ask, and a refusal keeps the page.
      fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
      expect(askToLeave()).toBe('false')
      // The modal's own leave-draft wording (the dialog closes; the page is left).
      expect(confirmSpy).toHaveBeenCalledWith('Leave this page? The New mate dialog closes and what you typed is lost.')
      expect(screen.getByTestId('crewmate-create-form')).toBeInTheDocument()
      // Accepting the prompt lets the navigation through.
      confirmSpy.mockReturnValue(true)
      expect(askToLeave()).toBe('true')
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('the rail\'s Mates row over a typed card: Stay keeps the card and its text, Leave closes it; an untouched card closes unasked', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    try {
      await renderPage([row()])
      expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
      await openDialog()
      fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
      // Stay: the row asked, the user kept the draft, nothing moved.
      clickRailMembers()
      expect(confirmSpy).toHaveBeenCalledWith('Leave this page? The New mate dialog closes and what you typed is lost.')
      expect(screen.getByTestId('crewmate-create-form')).toBeInTheDocument()
      expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('radar')
      expect(currentUrl()).toBe('/members?member=oncall')
      // Leave: the row navigates to this same page, which stays mounted -- the
      // card the user agreed to leave must close rather than sit there as if
      // the click did nothing. The chat is back.
      confirmSpy.mockReturnValue(true)
      clickRailMembers()
      await waitFor(() => expect(screen.queryByTestId('crewmate-create-form')).toBeNull())
      expect(screen.getByTestId('chat-pane-stub').closest('[style*="display: none"]')).toBeNull()
      // An untouched card: nothing to lose, so no question, and the click still
      // means "show me the page".
      confirmSpy.mockClear()
      await openDialog()
      clickRailMembers()
      await waitFor(() => expect(screen.queryByTestId('crewmate-create-form')).toBeNull())
      expect(confirmSpy).not.toHaveBeenCalled()
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('a push to /members that nobody guarded still asks before a typed card closes: Stay keeps it, Leave closes it', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    try {
      await renderPage([row()])
      expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
      await openDialog()
      fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
      // No `leaveGranted` on the entry: the landing is not proof anyone asked,
      // so the page asks the card's own guard before it would drop the text.
      clickRawMembers()
      await waitFor(() => expect(confirmSpy).toHaveBeenCalledTimes(1))
      expect(confirmSpy).toHaveBeenCalledWith('Leave this page? The New mate dialog closes and what you typed is lost.')
      expect(screen.getByTestId('crewmate-create-form')).toBeInTheDocument()
      expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('radar')
      confirmSpy.mockReturnValue(true)
      clickRawMembers()
      await waitFor(() => expect(screen.queryByTestId('crewmate-create-form')).toBeNull())
      expect(confirmSpy).toHaveBeenCalledTimes(2)
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('the rail row clicked while the address is already a bare /members (empty roster, card from the hero) asks before closing a typed card', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    try {
      await renderPage([])
      const [cta] = await screen.findAllByTestId('crewmate-empty-cta', undefined, PANE_READY)
      expect(currentUrl()).toBe('/members')
      fireEvent.click(cta)
      await screen.findByTestId('crewmate-create-form')
      fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
      // Same address: the rail asks nothing and navigates anyway (no mark), so
      // the page asks for itself; Stay keeps the card and its text.
      clickRailMembers()
      await waitFor(() => expect(confirmSpy).toHaveBeenCalledTimes(1))
      expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('radar')
      // Leave: the card closes; the hero is the page again.
      confirmSpy.mockReturnValue(true)
      clickRailMembers()
      await waitFor(() => expect(screen.queryByTestId('crewmate-create-form')).toBeNull())
      expect(screen.getAllByTestId('crewmate-empty-cta').length).toBeGreaterThan(0)
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('a create link to /members?create=1 opens the card and its own URL cleanup to a bare /members does not close it', async () => {
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    fireEvent.click(screen.getByTestId('guide-nav-granted'))
    const form = await screen.findByTestId('crewmate-create-form')
    // The link's params are cleaned off the address (a bare `/members` lands
    // before the page re-opens the remembered chat's `?member=`); neither
    // landing is a click on the page, so the card it opened stays open.
    await waitFor(() => expect(currentUrl()).not.toContain('create=1'))
    expect(screen.getByTestId('crewmate-create-form')).toBe(form)
    expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('scout')
  })

  it('a draft in the nested New workspace form counts toward the leave guard, and stops counting once that form is closed', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    try {
      await renderPage([row()])
      expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
      await openDialog()
      unfoldAdvanced()
      const workspace = await screen.findByRole('combobox', { name: 'Workspace' })
      fireEvent.keyDown(workspace, { key: 'ArrowDown' })
      fireEvent.click(await screen.findByRole('option', { name: '+ New workspace…' }))
      const wsName = await screen.findByPlaceholderText('e.g. oncall')
      // Crewmate fields untouched, workspace form empty: free to leave.
      expect(askToLeave()).toBe('true')
      // A typed workspace name is a draft the route change would destroy.
      fireEvent.change(wsName, { target: { value: 'staging' } })
      expect(askToLeave()).toBe('false')
      expect(confirmSpy).toHaveBeenCalledTimes(1)
      // Closing the workspace form (its Cancel) takes its draft out of the stake.
      const cancels = screen.getAllByRole('button', { name: 'Cancel' })
      fireEvent.click(cancels[cancels.length - 1])
      await waitFor(() => expect(screen.queryByPlaceholderText('e.g. oncall')).toBeNull())
      expect(askToLeave()).toBe('true')
      expect(confirmSpy).toHaveBeenCalledTimes(1)
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('a create in flight asks with its own words before an in-app navigation', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    try {
      let finish: (v: unknown) => void = () => {}
      ;(api.createKirocrewAgent as ReturnType<typeof vi.fn>).mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
      await renderPage([row()])
      expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
      await openDialog()
      fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
      fireEvent.click(screen.getByTestId('crewmate-create-submit'))
      await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))
      expect(askToLeave()).toBe('false')
      expect(confirmSpy).toHaveBeenLastCalledWith(expect.stringMatching(/^Leave while the mate is being created\?/))
      finish({ ok: true })
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('creates through the crew manager\'s write path, opens the new mate\'s chat and asks for its first greeting', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    await renderPage([row()])
    // The one row auto-opens on arrival (most recently used), so the page
    // starts with oncall's chat up and one thread POST made.
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    // Lower-case name: the thread stub answers `member: slug`, and the page
    // treats a name/answer mismatch as a slug collision (its own guard).
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    fireEvent.change(screen.getByLabelText('What it looks after'), { target: { value: 'Triage new issues' } })
    // The roster the page re-reads after the create carries the new row.
    membersMock.mockResolvedValue({
      members: [row(), row({ name: 'radar', slug: 'radar', dashboard_created: true })],
      default_agent: 'kirocrew',
    })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    // The SAME payload the crew manager's create form sends, plus the job as
    // the record's `description` and this page's
    // ask for a first greeting; no `model` key while inheriting.
    await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledWith({
      name: 'radar',
      kiro_agent: 'kirocrew',
      // '' asks the server for a workspace of the crewmate's own.
      workspace: '',
      memory_store: 'default',
      description: 'Triage new issues',
      triggers: '',
      session_color: '',
      first_greeting: true,
      // New mates default to a name-seeded icon avatar, never the trait ghost.
      avatar: expect.objectContaining({ kind: 'icon', pose: expect.any(String), bg: expect.stringMatching(/^#[0-9a-f]{6}$/i) }),
    }))
    // Roster re-read BEFORE the URL names the new crewmate, then its chat
    // opens through the verified thread endpoint, like any click would.
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledWith('radar'), PANE_READY)
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-radar'), PANE_READY)
    expect(currentUrl()).toBe('/members?member=radar')
    // The first greeting is the server's hidden turn: the page asks for it by
    // the crewmate's slug once the thread is confirmed, and sends nothing in
    // the user's name.
    await waitFor(() => expect(api.memberGreet).toHaveBeenCalledWith('radar'))
    expect(api.memberGreet).toHaveBeenCalledTimes(1)
    expect(api.sendChat).not.toHaveBeenCalled()
    // The dialog is gone (after its exit motion).
    await waitFor(() => expect(screen.queryByTestId('crewmate-create-form')).toBeNull())
  })

  it('the + New mate creates from a name alone: built from the built-in agent, wearing its name\'s own face, and its chat opens asking its goal through the hidden greeting', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await clickAddCrewmate()
    await screen.findByTestId('crewmate-create-look')
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    membersMock.mockResolvedValue({
      members: [row(), row({ name: 'radar', slug: 'radar', dashboard_created: true })],
      default_agent: 'kirocrew',
    })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))
    expect(api.createKirocrewAgent).toHaveBeenCalledWith(expect.objectContaining({
      name: 'radar',
      kiro_agent: 'kirocrew',
      description: '',
      first_greeting: true,
    }))
    // Built from a name alone, the new mate wears a name-seeded ICON (pose +
    // colour), not the trait ghost a missing avatar used to fall back to.
    const createdBody = vi.mocked(api.createKirocrewAgent).mock.calls[0][0] as { avatar: { kind: string; pose: string; bg: string } }
    expect(createdBody.avatar).toMatchObject({ kind: 'icon' })
    expect(createdBody.avatar.bg).toMatch(/^#[0-9a-f]{6}$/i)
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-radar'), PANE_READY)
    await waitFor(() => expect(api.memberGreet).toHaveBeenCalledWith('radar'), PANE_READY)
    // Once, and never as a message the user typed.
    await act(async () => {})
    expect(api.memberGreet).toHaveBeenCalledTimes(1)
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it('a just-created mate at work on its greeting moves as one picture in its row, with nothing animating inside it', async () => {
    // Right after create the greeting turn runs, so the new row is the first
    // place the working icon shows. An SVG that animates itself is re-rastered
    // in pieces on iOS Safari: head, tail and one eye out of step.
    const membersMock = api.members as ReturnType<typeof vi.fn>
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await clickAddCrewmate()
    await screen.findByTestId('crewmate-create-look')
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    membersMock.mockImplementation(() => {
      const sent = vi.mocked(api.createKirocrewAgent).mock.calls[0]?.[0] as { avatar?: unknown } | undefined
      return Promise.resolve({
        members: [
          row(),
          row({ name: 'radar', slug: 'radar', dashboard_created: true, avatar: sent?.avatar, running: true, bound: true, slot_key: 'member-radar' }),
        ],
        default_agent: 'kirocrew',
      })
    })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))
    const radar = (await rosterRow('radar')).closest('li') as HTMLElement
    // Running, as a fresh mate is while it greets: the presence dot is on.
    await within(radar).findByTestId('member-presence-dot', undefined, PANE_READY)
    const motion = await within(radar).findByTestId('crew-icon-motion', undefined, PANE_READY)
    // One moving root and one picture in the row: the silhouette and its eyes
    // are the same image, so they share every transform.
    expect(within(radar).getAllByTestId('crew-icon-motion')).toHaveLength(1)
    const imgs = radar.querySelectorAll('img')
    expect(imgs).toHaveLength(1)
    expect(motion.contains(imgs[0])).toBe(true)
    expect(decodeURIComponent(imgs[0].getAttribute('src') ?? '')).not.toMatch(/<style|animation|@keyframes/)
  })

  it('a mate not created on the dashboard asks for no greeting when opened', async () => {
    await renderPage([row({ name: 'synced', slug: 'synced' })], 'kirocrew', { route: '/members?member=synced' })
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-synced')
    await act(async () => {})
    expect(api.memberGreet).not.toHaveBeenCalled()
  })

  it('a free-form name opens the mate under the id the server answered and asks its greeting by that id', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Launch Notes' } })
    ;(api.createKirocrewAgent as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ ok: true, name: 'launch-notes' })
    membersMock.mockResolvedValue({
      members: [row(), row({ name: 'launch-notes', slug: 'launch-notes', display_name: 'Launch Notes', dashboard_created: true })],
      default_agent: 'kirocrew',
    })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledWith('launch-notes'), PANE_READY)
    await waitFor(() => expect(currentUrl()).toBe('/members?member=launch-notes'), PANE_READY)
    await waitFor(() => expect(api.memberGreet).toHaveBeenCalledWith('launch-notes'), PANE_READY)
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it('while the create is in flight every control locks — the Advanced toggle and its fields included', async () => {
    let finish: (v: { ok: true }) => void = () => {}
    ;(api.createKirocrewAgent as ReturnType<typeof vi.fn>).mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    unfoldAdvanced()
    const triggers = await screen.findByLabelText('Triggers')
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))
    // The body is sent; a value typed now would never reach it and would be
    // lost when success closes the dialog — so nothing under the form takes
    // input: one disabled fieldset covers the fields that have no `disabled`
    // prop of their own (workspace / model / triggers / colour) as well.
    expect(screen.getByTestId('crewmate-create-fieldset')).toBeDisabled()
    expect(triggers).toBeDisabled()
    expect(screen.getByTestId('crewmate-create-advanced-toggle')).toBeDisabled()
    expect(screen.getByLabelText('Name')).toBeDisabled()
    expect(screen.getByTestId('crewmate-create-submit')).toHaveTextContent('Creating…')
    finish({ ok: true })
    await waitFor(() => expect(screen.queryByTestId('crewmate-create-form')).toBeNull())
  })

  it('the nested New workspace form refuses Escape while it holds unsaved input, and its Cancel still closes it', async () => {
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    // Capture the create modal while it is still the active dialog: once the
    // nested New workspace form opens, the create modal goes inert
    // (aria-hidden), which removes its `dialog` role from the a11y tree.
    const outerDialog = screen.getByRole('dialog', { name: 'New mate' })
    unfoldAdvanced()
    const workspace = await screen.findByRole('combobox', { name: 'Workspace' })
    fireEvent.keyDown(workspace, { key: 'ArrowDown' })
    fireEvent.click(await screen.findByRole('option', { name: '+ New workspace…' }))
    // While the nested form is up the outer modal's own exit (its header X,
    // "Close") is disabled.
    expect(within(outerDialog).getByRole('button', { name: 'Close', hidden: true })).toBeDisabled()
    expect(document.querySelector('.z-\\[109\\]')).toBeInTheDocument()
    expect(screen.getByTestId('crewmate-create-submit')).toBeDisabled()
    const wsName = await screen.findByPlaceholderText('e.g. oncall')
    // Empty form: Escape is an ordinary dismissal.
    fireEvent.keyDown(wsName, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByPlaceholderText('e.g. oncall')).toBeNull())
    expect(within(outerDialog).getByRole('button', { name: 'Close' })).toBeEnabled()
    // The crewmate dialog underneath is untouched by that Escape.
    expect(screen.getByTestId('crewmate-create-form')).toBeInTheDocument()
    // Reopen, type a name: Escape is now refused, the draft stays.
    fireEvent.keyDown(screen.getByRole('combobox', { name: 'Workspace' }), { key: 'ArrowDown' })
    fireEvent.click(await screen.findByRole('option', { name: '+ New workspace…' }))
    const wsName2 = await screen.findByPlaceholderText('e.g. oncall')
    fireEvent.change(wsName2, { target: { value: 'staging' } })
    fireEvent.keyDown(wsName2, { key: 'Escape' })
    // flake-ok: pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
    // nosemgrep: semgrep.kirocrew.test-promise-sleep -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.getByPlaceholderText('e.g. oncall')).toHaveValue('staging')
    expect(screen.getByTestId('crewmate-create-form')).toBeInTheDocument()
    // The explicit Cancel is the deliberate way out.
    const cancels = screen.getAllByRole('button', { name: 'Cancel' })
    fireEvent.click(cancels[cancels.length - 1])
    await waitFor(() => expect(screen.queryByPlaceholderText('e.g. oncall')).toBeNull())
    expect(api.createWorkspace).not.toHaveBeenCalled()
  })

  it('a workspace created from Advanced is picked at once; a reopened dialog is not rewritten by the late refresh', async () => {
    const wsMock = api.workspaces as ReturnType<typeof vi.fn>
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    unfoldAdvanced()
    const workspace = await screen.findByRole('combobox', { name: 'Workspace' })
    expect(workspace).toHaveTextContent('Its own workspace (created automatically)')
    // The refresh after the create hangs: the pick must not wait for it.
    let finishRefresh: (v: { workspaces: { name: string }[] }) => void = () => {}
    wsMock.mockReturnValueOnce(new Promise((resolve) => { finishRefresh = resolve }))
    fireEvent.keyDown(workspace, { key: 'ArrowDown' })
    fireEvent.click(await screen.findByRole('option', { name: '+ New workspace…' }))
    const wsName = await screen.findByPlaceholderText('e.g. oncall')
    fireEvent.change(wsName, { target: { value: 'staging' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(api.createWorkspace).toHaveBeenCalled())
    // The workspace modal closes on success; the create dialog is reachable again.
    await waitFor(() => expect(screen.queryByPlaceholderText('e.g. oncall')).toBeNull())
    // Written immediately, visible in the select even before the list refreshes.
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Workspace' })).toHaveTextContent('staging'))
    // Dismiss and reopen while the refresh is still pending: the fresh form
    // says default, and the refresh landing later must not change that.
    fireEvent.click(within(screen.getByRole('dialog', { name: 'New mate' })).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByTestId('crewmate-create-form')).toBeNull())
    await openDialog()
    unfoldAdvanced()
    expect(await screen.findByRole('combobox', { name: 'Workspace' })).toHaveTextContent('Its own workspace (created automatically)')
    finishRefresh({ workspaces: [{ name: 'default' }, { name: 'staging' }] })
    // flake-ok: pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
    // nosemgrep: semgrep.kirocrew.test-promise-sleep -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
    await new Promise((r) => setTimeout(r, 20))
    expect(screen.getByRole('combobox', { name: 'Workspace' })).toHaveTextContent('Its own workspace (created automatically)')
  })

  // The nested form's create is an awaited POST; Radix unmounts the form on
  // close but the continuation still runs. These cases start a create that
  // hangs, retire its generation by each path — the nested form's Cancel, its
  // X, Escape, the parent's `open` dropping, the parent's reset-on-open, and
  // unmount — then let the POST answer: nothing about the parent draft, the
  // workspaces cache, or the list refetch may change. Call-count DELTAS on
  // `api.workspaces`, never its return value: the mock's default list would
  // let a refetch pass unnoticed.
  const startDeferredWorkspaceCreate = async () => {
    const createWs = api.createWorkspace as ReturnType<typeof vi.fn>
    const wsReads = api.workspaces as ReturnType<typeof vi.fn>
    let finishCreate: (v: { name: string }) => void = () => {}
    createWs.mockReturnValueOnce(new Promise((resolve) => { finishCreate = resolve }))
    unfoldAdvanced()
    const workspace = await screen.findByRole('combobox', { name: 'Workspace' })
    expect(workspace).toHaveTextContent('Its own workspace (created automatically)')
    fireEvent.keyDown(workspace, { key: 'ArrowDown' })
    fireEvent.click(await screen.findByRole('option', { name: '+ New workspace…' }))
    const wsName = await screen.findByPlaceholderText('e.g. oncall')
    fireEvent.change(wsName, { target: { value: 'staging' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(createWs).toHaveBeenCalledTimes(1))
    const readsBefore = wsReads.mock.calls.length
    return {
      wsName,
      nested: screen.getByRole('dialog', { name: 'Create Workspace' }),
      finish: () => finishCreate({ name: 'staging' }),
      /** How many workspace-list reads happened since the create was started. */
      readsSince: () => wsReads.mock.calls.length - readsBefore,
    }
  }
  const cachedWorkspaceNames = (queryClient: { getQueryData: (k: unknown[]) => unknown }) =>
    ((queryClient.getQueryData(['workspaces']) as { workspaces?: { name: string }[] } | undefined)?.workspaces ?? []).map((w) => w.name)
  // flake-ok: pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
  // nosemgrep: semgrep.kirocrew.test-promise-sleep -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
  const settle = () => new Promise((r) => setTimeout(r, 30))
  /** The stale-completion contract, whole: no pick, no cache entry, no refetch. */
  const expectStaleDropped = (queryClient: { getQueryData: (k: unknown[]) => unknown }, readsSince: () => number, readsAtClose: number) => {
    expect(screen.getByRole('combobox', { name: 'Workspace' })).toHaveTextContent('Its own workspace (created automatically)')
    expect(cachedWorkspaceNames(queryClient)).not.toContain('staging')
    expect(readsSince()).toBe(readsAtClose)
  }

  it('a workspace create that answers after the nested form\'s Cancel does not change the parent\'s pick, the cache, or refetch the list', async () => {
    const { queryClient } = await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    const { nested, finish, readsSince } = await startDeferredWorkspaceCreate()
    // The user gives up on the slow create through the form's own Cancel.
    fireEvent.click(within(nested).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByPlaceholderText('e.g. oncall')).toBeNull())
    const before = cachedWorkspaceNames(queryClient)
    const readsAtClose = readsSince()
    // The POST lands anyway. A stale generation: dropped whole.
    finish()
    await settle()
    expectStaleDropped(queryClient, readsSince, readsAtClose)
    expect(cachedWorkspaceNames(queryClient)).toEqual(before)
    // And the parent modal's own exit (its header X, "Close") is live again:
    // the nested dialog is closed for good.
    expect(within(screen.getByRole('dialog', { name: 'New mate' })).getByRole('button', { name: 'Close' })).toBeEnabled()
  })

  it('a workspace create that answers after the nested form\'s X does not change the parent\'s pick or the cache', async () => {
    const { queryClient } = await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    const { nested, finish, readsSince } = await startDeferredWorkspaceCreate()
    // The X is the other deliberate exit a DIRTY form still honours (Escape
    // and the backdrop are refused while it holds input).
    fireEvent.click(within(nested).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByPlaceholderText('e.g. oncall')).toBeNull())
    const readsAtClose = readsSince()
    finish()
    await settle()
    expectStaleDropped(queryClient, readsSince, readsAtClose)
    expect(within(screen.getByRole('dialog', { name: 'New mate' })).getByRole('button', { name: 'Close' })).toBeEnabled()
  })

  it('a workspace create that answers after the nested form was dismissed with Escape does not change the parent\'s pick', async () => {
    const { queryClient } = await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    const { wsName, finish, readsSince } = await startDeferredWorkspaceCreate()
    // Escape is refused while the form is dirty; clear the name first, then
    // Escape is an ordinary dismissal (the create stays in flight regardless).
    fireEvent.change(wsName, { target: { value: '' } })
    fireEvent.keyDown(wsName, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByPlaceholderText('e.g. oncall')).toBeNull())
    const readsAtClose = readsSince()
    finish()
    await settle()
    expectStaleDropped(queryClient, readsSince, readsAtClose)
  })

  it('a workspace create that answers while its nested form is still open is picked as before (same generation), and the list is re-read once', async () => {
    const { queryClient } = await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    const { finish, readsSince } = await startDeferredWorkspaceCreate()
    // The reconciling refetch answers with the server's list, which now
    // holds the new workspace. (The mock's default answer — `default` alone —
    // is the pre-create server state, and would overwrite the optimistic
    // entry before the pick observed it.)
    ;(api.workspaces as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ workspaces: [{ name: 'default' }, { name: 'staging' }] })
    // Nothing closed in between: the live generation's answer lands in full.
    finish()
    await waitFor(() => expect(screen.queryByPlaceholderText('e.g. oncall')).toBeNull())
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Workspace' })).toHaveTextContent('staging'))
    expect(cachedWorkspaceNames(queryClient)).toContain('staging')
    await waitFor(() => expect(readsSince()).toBe(1))
  })

  // The parent's `open` cannot drop from inside the page while the nested
  // form is up (its dismiss is disabled), but the prop is the page's to flip,
  // and a route change unmounts the whole dialog. Neither path exists in the
  // page harness, so the dialog is mounted on its own here with the same
  // providers, and `open` is driven directly.
  describe('with the parent driven directly', () => {
    const mountDialog = (onCreated = vi.fn()) => {
      const onClose = vi.fn()
      const utils = renderWithProviders(
        <NewCrewmateDialog open onClose={onClose} onCreated={onCreated} existingNames={['oncall']} />,
      )
      const rerenderOpen = (open: boolean) =>
        utils.rerender(<NewCrewmateDialog open={open} onClose={onClose} onCreated={onCreated} existingNames={['oncall']} />)
      return { ...utils, rerenderOpen }
    }

    it('a workspace create that answers after the parent\'s `open` dropped — nested form still up — mutates nothing, and a reopen starts clean', async () => {
      const { queryClient, rerenderOpen } = mountDialog()
      await screen.findByTestId('crewmate-create-form')
      const { finish, readsSince } = await startDeferredWorkspaceCreate()
      // The page closes the dialog from outside while the nested form is
      // still open and its POST is in flight. The retire is a layout effect,
      // so it has run by the time `act` returns; the Modal itself leaves
      // through its exit motion (AnimatePresence), so the form's absence is
      // awaited, not read in the same tick.
      act(() => { rerenderOpen(false) })
      await waitFor(() => expect(screen.queryByTestId('crewmate-create-form')).toBeNull())
      const readsAtClose = readsSince()
      finish()
      await settle()
      expect(cachedWorkspaceNames(queryClient)).not.toContain('staging')
      expect(readsSince()).toBe(readsAtClose)
      // Reopened: a fresh draft, and the stale name is nowhere in it.
      act(() => { rerenderOpen(true) })
      await screen.findByTestId('crewmate-create-form')
      unfoldAdvanced()
      expect(await screen.findByRole('combobox', { name: 'Workspace' })).toHaveTextContent('Its own workspace (created automatically)')
      expect(cachedWorkspaceNames(queryClient)).not.toContain('staging')
      fireEvent.keyDown(screen.getByRole('combobox', { name: 'Workspace' }), { key: 'ArrowDown' })
      expect(screen.queryByRole('option', { name: 'staging' })).toBeNull()
    })

    it('a workspace create that answers after the dialog unmounted mutates nothing', async () => {
      const { queryClient, unmount } = mountDialog()
      await screen.findByTestId('crewmate-create-form')
      const { finish, readsSince } = await startDeferredWorkspaceCreate()
      unmount()
      const readsAtClose = readsSince()
      finish()
      await settle()
      expect(cachedWorkspaceNames(queryClient)).not.toContain('staging')
      expect(readsSince()).toBe(readsAtClose)
    })

    it('with the parent kept open the same create still lands (the direct mount proves the guard, not the harness)', async () => {
      const { queryClient } = mountDialog()
      await screen.findByTestId('crewmate-create-form')
      const { finish, readsSince } = await startDeferredWorkspaceCreate()
      // The reconciling refetch returns the server's list with the new
      // workspace on it (see the page-harness twin of this case).
      ;(api.workspaces as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ workspaces: [{ name: 'default' }, { name: 'staging' }] })
      finish()
      await waitFor(() => expect(screen.getByRole('combobox', { name: 'Workspace' })).toHaveTextContent('staging'))
      expect(cachedWorkspaceNames(queryClient)).toContain('staging')
      await waitFor(() => expect(readsSince()).toBe(1))
    })
  })

  it('a create that fails below the API — a dropped connection — is said in the product\'s words, not the exception\'s', async () => {
    ;(api.createKirocrewAgent as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    const notice = await screen.findByTestId('crewmate-create-error')
    expect(notice).toHaveTextContent("Couldn't create the mate. Nothing was created — try again.")
    expect(notice).not.toHaveTextContent(/Failed to fetch/)
    // "Nothing was created" was CHECKED, not assumed: the roster was re-read
    // (arrival + reconcile) and radar is not on it.
    expect(api.members).toHaveBeenCalledTimes(2)
    // Still open with the draft; the user can try again.
    expect(screen.getByTestId('crewmate-create-form')).toBeInTheDocument()
    expect(screen.getByLabelText('Name')).toHaveValue('radar')
  })

  it('a dropped connection after the server committed the create is reconciled against the roster: the row is said as taken, nothing is claimed, sent or opened', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    ;(api.createKirocrewAgent as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    fireEvent.change(screen.getByLabelText('What it looks after'), { target: { value: 'Triage new issues' } })
    // The reconcile read finds radar — this request's, or another tab's; the
    // dialog cannot tell, so it claims neither.
    membersMock.mockResolvedValue({
      members: [row(), row({ name: 'radar', slug: 'radar', dashboard_created: true })],
      default_agent: 'kirocrew',
    })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    const notice = await screen.findByTestId('crewmate-create-error')
    expect(notice).toHaveTextContent('A mate named radar already exists.')
    // Dialog still open; no chat opened for radar, no greeting, one POST.
    expect(screen.getByTestId('crewmate-create-form')).toBeInTheDocument()
    expect(api.memberThread).not.toHaveBeenCalledWith('radar')
    expect(api.sendChat).not.toHaveBeenCalled()
    expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1)
    // The roster behind the dialog is refreshed so the row shows for picking.
    expect(await rosterRow('radar')).toBeInTheDocument()
  })

  it('a dropped connection whose reconcile read also fails is said as unconfirmed, never as "nothing was created"', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    ;(api.createKirocrewAgent as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    membersMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    // The roster invalidation that follows an unconfirmed create re-reads the
    // list: here the create DID land (the row is there), so the resubmit
    // below must be refused up front as taken instead of posting a namesake.
    membersMock.mockResolvedValue({
      members: [row(), row({ name: 'radar', slug: 'radar', dashboard_created: true })],
      default_agent: 'kirocrew',
    })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    // Its OWN test id and a bold lead: this is not one of the retry-safe
    // "nothing was created" notices, and neither a reader nor a test may take
    // it for one.
    const notice = await screen.findByTestId('crewmate-create-unconfirmed')
    expect(screen.queryByTestId('crewmate-create-error')).toBeNull()
    expect(within(notice).getByText('Not confirmed', { selector: 'strong' })).toBeInTheDocument()
    // The attempted name leads the sentence, so the notice cannot be read as
    // the nameless retry-safe "couldn't create the crewmate" one.
    expect(notice).toHaveTextContent("Couldn't confirm whether radar was created. Check the list before trying again.")
    expect(notice).not.toHaveTextContent(/the mate was created/)
    expect(notice).not.toHaveTextContent(/Nothing was created/)
    expect(screen.getByTestId('crewmate-create-form')).toBeInTheDocument()
    expect(api.sendChat).not.toHaveBeenCalled()
    // The roster behind the dialog was refreshed by the unconfirmed answer,
    // so radar is now on it and a second Create of the same name is a hint
    // under the field with NO request — one POST in total.
    expect(await rosterRow('radar')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    expect(await screen.findByTestId('crewmate-create-name-hint')).toHaveTextContent('A mate named radar already exists.')
    expect(screen.queryByTestId('crewmate-create-unconfirmed')).toBeNull()
    expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1)
  })

  it('a reconcile read that never answers does not hold the dialog on Creating…: at its bound the create is said as unconfirmed and the form unlocks', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    // The bound runs from the POST's failure, so the case fails it itself.
    let fail: (e: Error) => void = () => {}
    ;(api.createKirocrewAgent as ReturnType<typeof vi.fn>).mockReturnValueOnce(new Promise((_, reject) => { fail = reject }))
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    // Padded on purpose: the notice names the name as SENT (trimmed), which is
    // the row the list is then checked for, not the field's raw text.
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: '  radar  ' } })
    // The roster read the reconcile leans on stalls for good (a half-open
    // socket, a 429 ladder) — while the mutation is pending every exit is
    // refused, so an unbounded await here would be a dialog with no way out.
    const stalled = new Promise<never>(() => {})
    membersMock.mockReturnValueOnce(stalled)
    fakeCreateClock()
    await stepCreateClock(0, () => fireEvent.click(screen.getByTestId('crewmate-create-submit')))
    await stepCreateClock(0, () => fail(new TypeError('Failed to fetch')))
    // The POST got no answer and the reconcile read stalls: one millisecond
    // short of the bound the form is still locked on "Creating…".
    await stepCreateClock(RECONCILE_BOUND_MS - 1)
    expect(screen.queryByTestId('crewmate-create-unconfirmed')).toBeNull()
    expect(screen.getByTestId('crewmate-create-submit')).toHaveTextContent('Creating…')
    expect(screen.getByTestId('crewmate-create-fieldset')).toBeDisabled()
    await stepCreateClock(1)
    const notice = screen.getByTestId('crewmate-create-unconfirmed')
    // The reconcile read is the stalled one: only the bound can have ended it.
    expect(membersMock.mock.results.some((r) => r.value === stalled)).toBe(true)
    expect(notice).toHaveTextContent("Couldn't confirm whether radar was created. Check the list before trying again.")
    expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1)
    // Unlocked again: the mutation settled, so the modal's Close is live.
    expect(screen.getByTestId('crewmate-create-fieldset')).not.toBeDisabled()
    expect(within(screen.getByRole('dialog', { name: 'New mate' })).getByRole('button', { name: 'Close' })).toBeEnabled()
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it('a create that resolves after the user left the page does not pull them back: the unmounted dialog drops its continuation', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    const createMock = api.createKirocrewAgent as ReturnType<typeof vi.fn>
    let resolveCreate: (v: unknown) => void = () => {}
    createMock.mockReturnValueOnce(new Promise((resolve) => { resolveCreate = resolve }))
    const { unmount } = await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(createMock).toHaveBeenCalled())
    const rosterReads = membersMock.mock.calls.length
    // The user accepted "Leave this page?" and the page is gone.
    unmount()
    // The POST lands afterwards. `useMutation` still runs `onSuccess`; the
    // hand-over (roster re-read, chat open, URL write) must not.
    resolveCreate({ ok: true })
    // flake-ok: pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
    // nosemgrep: semgrep.kirocrew.test-promise-sleep -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
    await new Promise((r) => setTimeout(r, 50))
    expect(membersMock.mock.calls.length).toBe(rosterReads)
    expect(api.memberThread).not.toHaveBeenCalledWith('radar')
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it('a chat that opens after the user left the page asks for no greeting: the unmounted page drops the follow-up', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    const threadMock = api.memberThread as ReturnType<typeof vi.fn>
    let resolveThread: (v: unknown) => void = () => {}
    const { unmount } = await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    membersMock.mockResolvedValue({
      members: [row(), row({ name: 'radar', slug: 'radar', dashboard_created: true })],
      default_agent: 'kirocrew',
    })
    // The create and the roster re-read land; the new chat's open (the thread
    // POST) is the step still in flight when the user leaves.
    threadMock.mockImplementation((slug: string) =>
      slug === 'radar' ? new Promise((resolve) => { resolveThread = resolve }) : echoThread(slug),
    )
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(threadMock).toHaveBeenCalledWith('radar'), PANE_READY)
    // The user accepted "Leave this page?" and the page is gone.
    unmount()
    // The POST answers afterwards. `useMutation` still runs `onSuccess`, but
    // no page is left to ask for the greeting, so nothing is asked or sent:
    // the server keeps it owed for radar's next open.
    resolveThread({ slot_key: 'member-radar', slug: 'radar', member: 'radar', created: true })
    // flake-ok: pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
    // nosemgrep: semgrep.kirocrew.test-promise-sleep -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
    await new Promise((r) => setTimeout(r, 50))
    expect(api.memberGreet).not.toHaveBeenCalled()
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it('a chat open that fails right after the create frees the +, and the next successful open of that mate asks for its greeting', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    const threadMock = api.memberThread as ReturnType<typeof vi.fn>
    const sendMock = api.sendChat as ReturnType<typeof vi.fn>
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    membersMock.mockResolvedValue({
      members: [row(), row({ name: 'radar', slug: 'radar', dashboard_created: true })],
      default_agent: 'kirocrew',
    })
    // The create and the roster re-read land; the new chat's open (the
    // thread POST) is refused once — a transient failure, ordinary in real
    // operation right after a successful create.
    threadMock.mockImplementationOnce((slug: string) =>
      slug === 'radar' ? Promise.reject(new Error('gateway hiccup')) : echoThread(slug),
    )
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(threadMock).toHaveBeenCalledWith('radar'), PANE_READY)
    // The open is said as failed, nothing was sent, and the hold comes down:
    // the failure notice is the user's surface now.
    expect(await screen.findByTestId('member-thread-error')).toBeInTheDocument()
    expect(sendMock).not.toHaveBeenCalled()
    await waitForAddCrewmate(false)
    expect(api.memberGreet).not.toHaveBeenCalled()
    // The re-click is the repair gesture (a re-POST). This one succeeds, and
    // the confirmed thread asks for the greeting the server owes radar.
    fireEvent.click(await rosterRow('radar'))
    await waitFor(() => expect(threadMock).toHaveBeenCalledTimes(3))
    await waitFor(() => expect(api.memberGreet).toHaveBeenCalledWith('radar'), PANE_READY)
    // A further re-open asks nothing more in this tab, and nothing is ever
    // sent in the user's name.
    fireEvent.click(await rosterRow('radar'))
    await waitFor(() => expect(threadMock).toHaveBeenCalledTimes(4))
    // flake-ok: pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
    // nosemgrep: semgrep.kirocrew.test-promise-sleep -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
    await new Promise((r) => setTimeout(r, 50))
    expect(api.memberGreet).toHaveBeenCalledTimes(1)
    expect(sendMock).not.toHaveBeenCalled()
  })
})

describe('MembersPage: the dictation key under the New crewmate card (#24967)', () => {
  it('disarms the DM pane\'s dictation key while the card covers it, and re-arms it when the card closes', async () => {
    await renderPage([row()])
    const pane = await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
    await waitFor(() => expect(pane).toHaveAttribute('data-push-to-talk', '1'))
    await clickAddAdvanced()
    await screen.findByTestId('crewmate-create-form')
    // The key listens on the whole document: under the card it would dictate
    // into, and could send to, the hidden chat.
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveAttribute('data-push-to-talk', '0'))
    // An empty card closes on the rail's Crewmates entry (no draft to guard).
    clickRailMembers()
    await waitFor(() => expect(screen.queryByTestId('crewmate-create-form')).toBeNull())
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveAttribute('data-push-to-talk', '1'))
  })
})
