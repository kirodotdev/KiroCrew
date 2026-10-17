/* MembersPage: teams in the roster and the team view (its inbox, edits and
 * refetch failures), the empty-roster hero and page-header doors, and the roster
 * rows — message preview, Stopped chip, presence dot and search. These cases keep
 * the describe title they share with MembersPage.sidePanel.test.tsx.
 * This file, MembersPage.test.tsx and the other files that import
 * src/test/MembersPageTestUtils.tsx (their shared fixtures) carry the same api
 * mock and module stubs below, word for word: change them together. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { NavigationLeaveGuardProvider } from '../../components/NavigationLeaveGuard'
import { memberProjectionStore } from '../../state/memberProjectionStore'
import { sseConnected, sseDisconnected, sseSlots } from '../../store/dashboardSlice'
import { __resetPaneDraftsForTests, readPaneDraft, writePaneDraft } from '../../utils/chatPaneDrafts'
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
  WIDE_WINDOW,
  setWindowWidth,
  row,
  PANE_READY,
  realCreateClock,
  renderPage,
  currentUrl,
  roster,
  rosterRow,
  openAddMenu,
  closeAddMenu,
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

describe('MembersPage side panel (Dashboard / Work log / Notes / Schedules) and edit jump', () => {

  it('offers NO team door while team creation is hidden for the phase, and never opens the dialog in create mode', async () => {
    await renderPage([row({ name: 'oncall', slug: 'oncall' }), row({ name: 'docs', slug: 'docs' })])
    await rosterRow('oncall')
    // The one door the "+" has is the crewmate one, and it is the only
    // menuitem: a team cannot be created from anywhere on this page. The
    // dialog's create mode is still covered by `TeamDialog.test.tsx`, so the
    // door returning is a revert rather than a rewrite.
    const item = await openAddMenu()
    expect(item).toBeInTheDocument()
    // New crewmate only; no team door.
    expect(within(screen.getByTestId('member-add-menu')).getAllByRole('menuitem')).toHaveLength(1)
    expect(screen.queryByTestId('member-add-team')).toBeNull()
    await closeAddMenu()
    expect(screen.queryByTestId('team-dialog-body')).toBeNull()
    expect(api.teams.create).not.toHaveBeenCalled()
  })

  it('deletes a team only after confirmation and removes it from the roster cache', async () => {
    const triage = { id: 'abc123abc123', name: 'Triage', members: ['oncall'] }
    vi.mocked(api.teams.list).mockResolvedValueOnce({ teams: [triage] }).mockResolvedValue({ teams: [] })
    vi.mocked(api.teams.remove).mockResolvedValue({ ok: true })
    await renderPage([row({ name: 'oncall', slug: 'oncall' })])
    await rosterRow('oncall')
    fireEvent.click((await screen.findAllByTestId('team-group-header'))[0])
    fireEvent.click(within(await screen.findByTestId('team-view')).getByTestId('team-edit'))
    await screen.findByTestId('team-dialog-body')
    fireEvent.click(screen.getByTestId('team-dialog-delete'))
    fireEvent.click(screen.getByTestId('team-dialog-delete-confirm'))
    await waitFor(() => expect(api.teams.remove).toHaveBeenCalledWith('abc123abc123'))
    await waitFor(() => expect(screen.queryByTestId('team-dialog-body')).toBeNull())
  })

  it('titles a failed delete as delete, never as save', async () => {
    const triage = { id: 'abc123abc123', name: 'Triage', members: ['oncall'] }
    vi.mocked(api.teams.list).mockResolvedValue({ teams: [triage] })
    vi.mocked(api.teams.remove).mockRejectedValue(new Error('disk full'))
    await renderPage([row({ name: 'oncall', slug: 'oncall' })])
    await rosterRow('oncall')
    fireEvent.click((await screen.findAllByTestId('team-group-header'))[0])
    fireEvent.click(within(await screen.findByTestId('team-view')).getByTestId('team-edit'))
    await screen.findByTestId('team-dialog-body')
    fireEvent.click(screen.getByTestId('team-dialog-delete'))
    fireEvent.click(screen.getByTestId('team-dialog-delete-confirm'))
    const notice = await screen.findByTestId('team-dialog-error')
    expect(notice).toHaveTextContent('Could not delete the team')
    expect(notice).not.toHaveTextContent('Could not save the team')
  })

  it('groups the roster by team with a trailing "No team" group, and a team header opens the team view', async () => {
    vi.mocked(api.teams.list).mockResolvedValue({ teams: [{ id: 'abc123abc123', name: 'Triage', members: ['oncall'] }] })
    await renderPage([row({ name: 'oncall', slug: 'oncall' }), row({ name: 'docs', slug: 'docs' })])
    await rosterRow('oncall')
    const headers = await screen.findAllByTestId('team-group-header')
    // Teams in their stored order, the unlisted rows last under "No team".
    expect(headers.map((h) => h.dataset.team)).toEqual(['abc123abc123', 'no-team'])
    expect(headers[0]).toHaveTextContent('Triage')
    expect(headers[1]).toHaveTextContent('No team')
    // Selecting the header opens the team view where a chat would be: the URL
    // names the team, no member is open, and the empty-pane sentence is gone.
    fireEvent.click(headers[0])
    const view = await screen.findByTestId('team-view')
    expect(view.dataset.team).toBe('abc123abc123')
    expect(currentUrl()).toBe('/members?team=abc123abc123')
    expect(within(view).getAllByTestId('team-status-row')).toHaveLength(1)
    expect(within(view).getByTestId('team-status-row')).toHaveTextContent('oncall')
    expect(screen.queryByText(/Pick a member/i)).toBeNull()
    expect(screen.queryByTestId('member-thread-header')).toBeNull()
    // Never talked to: nothing can be waiting, and the week is empty.
    expect(await within(view).findByTestId('team-inbox-empty')).toBeInTheDocument()
    expect(await within(view).findByTestId('team-week-empty')).toBeInTheDocument()
  })

  it('the team view lists an unanswered question with at most two answer controls and says a chip only drafts', async () => {
    vi.mocked(api.teams.list).mockResolvedValue({ teams: [{ id: 'abc123abc123', name: 'Triage', members: ['oncall'] }] })
    vi.mocked(api.chatSlotDetail).mockResolvedValue({
      messages: [
        { role: 'user', content: 'Fix the flake.', ts: '2026-09-22T10:00:00Z' },
        { role: 'assistant', content: 'Merge as is, keep digging, or park it?\n\n[OPTIONS: Merge as is | Find the race | Park it]', ts: '2026-09-22T10:05:00Z' },
      ],
    } as never)
    await renderPage([row({ name: 'oncall', slug: 'oncall', bound: true, slot_key: 'member-oncall' })])
    await rosterRow('oncall')
    fireEvent.click((await screen.findAllByTestId('team-group-header'))[0])
    const view = await screen.findByTestId('team-view')
    const card = await within(view).findByTestId('team-inbox-card', undefined, PANE_READY)
    // Three options: one chip plus ONE overflow menu holding the rest -- never
    // three peer buttons in a row.
    expect(within(card).getAllByTestId('team-inbox-option')).toHaveLength(1)
    expect(within(card).getByTestId('team-inbox-option')).toHaveTextContent('Merge as is')
    expect(within(card).getByTestId('team-inbox-option-more')).toBeInTheDocument()
    // The chip's effect is written where the reader looks, not only in a tooltip.
    expect(within(card).getByTestId('team-inbox-option-lead')).toHaveTextContent(/drafts a reply in oncall's chat; nothing is sent until you do/i)
    writePaneDraft('member-oncall', { text: 'Find the race', files: ['/workspace/note.txt'], pastes: [] })
    fireEvent.click(within(card).getByTestId('team-inbox-option'))
    expect(readPaneDraft('member-oncall')).toEqual({
      text: 'Find the race\n\nMerge as is',
      files: ['/workspace/note.txt'],
      pastes: [],
    })
  })

  it('two answers render as two chips with no overflow menu', async () => {
    vi.mocked(api.teams.list).mockResolvedValue({ teams: [{ id: 'abc123abc123', name: 'Triage', members: ['oncall'] }] })
    vi.mocked(api.chatSlotDetail).mockResolvedValue({
      messages: [{ role: 'assistant', content: 'Ship it?\n\n[OPTIONS: Yes | No]', ts: '2026-09-22T10:05:00Z' }],
    } as never)
    await renderPage([row({ name: 'oncall', slug: 'oncall', bound: true, slot_key: 'member-oncall' })])
    await rosterRow('oncall')
    fireEvent.click((await screen.findAllByTestId('team-group-header'))[0])
    const card = await within(await screen.findByTestId('team-view')).findByTestId('team-inbox-card', undefined, PANE_READY)
    expect(within(card).getAllByTestId('team-inbox-option').map((b) => b.textContent)).toEqual(['Draft “Yes”', 'Draft “No”'])
    expect(within(card).queryByTestId('team-inbox-option-more')).toBeNull()
  })

  it('reopening the same team waits for this opening to confirm its slot instead of using the prior opening cache', async () => {
    vi.mocked(api.teams.list).mockResolvedValue({ teams: [{ id: 'abc123abc123', name: 'Triage', members: ['oncall'] }] })
    const tails: Record<string, string> = {
      'member-oncall': 'Old opening: merge?\n\n[OPTIONS: Yes | No]',
      'slot-reopened': 'New opening: ship?\n\n[OPTIONS: Ship | Hold]',
    }
    vi.mocked(api.chatSlotDetail).mockImplementation((key) =>
      Promise.resolve({ messages: [{ role: 'assistant', content: tails[key] ?? '', ts: '2026-09-22T10:05:00Z' }] } as never),
    )
    await renderPage([row({ name: 'oncall', slug: 'oncall', bound: true, slot_key: 'member-oncall' })])
    await rosterRow('oncall')
    fireEvent.click((await screen.findAllByTestId('team-group-header'))[0])
    const firstView = await screen.findByTestId('team-view')
    expect(await within(firstView).findByTestId('team-inbox-card', undefined, PANE_READY)).toHaveTextContent('Old opening: merge?')
    const oldReads = () => vi.mocked(api.chatSlotDetail).mock.calls.filter(([key]) => key === 'member-oncall').length
    const readsBefore = oldReads()
    const threadPosts = () => vi.mocked(api.memberThread).mock.calls.length

    // Back to the bare roster reopens the most recently used chat (oncall's),
    // and THAT open confirms the thread once for itself. Let it settle before
    // arming the held answer, so the held answer is the TEAM reopen's own.
    const postsBeforeBack = threadPosts()
    fireEvent.click(within(firstView).getByTestId('team-back'))
    await waitFor(() => expect(screen.queryByTestId('team-view')).toBeNull())
    await waitFor(() => expect(threadPosts()).toBe(postsBeforeBack + 1))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall'), PANE_READY)
    const postsBeforeReopen = threadPosts()
    let confirm!: (value: Awaited<ReturnType<typeof api.memberThread>>) => void
    vi.mocked(api.memberThread).mockReturnValueOnce(new Promise((resolve) => { confirm = resolve }))
    fireEvent.click((await screen.findAllByTestId('team-group-header'))[0])
    const reopened = await screen.findByTestId('team-view')
    // Exactly one more POST: the reopened team view's own confirmation.
    await waitFor(() => expect(threadPosts()).toBe(postsBeforeReopen + 1))
    expect(within(reopened).queryByTestId('team-inbox-card')).toBeNull()
    expect(oldReads()).toBe(readsBefore)

    await act(async () => {
      confirm({ slot_key: 'slot-reopened', slug: 'oncall', member: 'oncall', created: false })
    })
    expect(await within(reopened).findByTestId('team-inbox-card')).toHaveTextContent('New opening: ship?')
    expect(oldReads()).toBe(readsBefore)
  })

  it('a thread key confirmed before a gateway reconnect feeds nothing until the reconnected gateway confirms again: a reassigned slot never shows the old chat', async () => {
    vi.mocked(api.teams.list).mockResolvedValue({ teams: [{ id: 'abc123abc123', name: 'Triage', members: ['oncall'] }] })
    const tails: Record<string, string> = {
      'member-oncall': 'Old slot: merge?\n\n[OPTIONS: Yes | No]',
      'slot-reassigned': 'New slot: ship?\n\n[OPTIONS: Ship | Hold]',
    }
    vi.mocked(api.chatSlotDetail).mockImplementation((key) =>
      Promise.resolve({ messages: [{ role: 'assistant', content: tails[key] ?? '', ts: '2026-09-22T10:05:00Z' }] } as never),
    )
    const { store } = await renderPage([row({ name: 'oncall', slug: 'oncall', bound: true, slot_key: 'member-oncall' })])
    await rosterRow('oncall')
    // The first connect after a load is not a reconnect: nothing is asked twice.
    act(() => { store.dispatch(sseConnected()) })
    // The arrival opened oncall's own chat (the most recently used one) and
    // confirmed its thread; the team view's confirmations are counted from here.
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledTimes(1))
    const threadPosts = () => vi.mocked(api.memberThread).mock.calls.length
    const postsBeforeTeam = threadPosts()
    fireEvent.click((await screen.findAllByTestId('team-group-header'))[0])
    const view = await screen.findByTestId('team-view')
    expect(await within(view).findByTestId('team-inbox-card', undefined, PANE_READY)).toHaveTextContent('Old slot: merge?')
    expect(threadPosts()).toBe(postsBeforeTeam + 1)
    const oldKeyReads = () => vi.mocked(api.chatSlotDetail).mock.calls.filter(([k]) => k === 'member-oncall').length
    const readsBefore = oldKeyReads()

    // The gateway restarts under the view and hands oncall a different slot.
    // Its answer is held back, so the window between the drop and the new
    // confirmation -- where the old key used to keep feeding the tail -- is
    // what this asserts on.
    let confirm!: (value: Awaited<ReturnType<typeof api.memberThread>>) => void
    vi.mocked(api.memberThread).mockReturnValueOnce(new Promise((resolve) => { confirm = resolve }))
    act(() => { store.dispatch(sseDisconnected()) })
    // The drop alone retires the key: while the gateway is unreachable the
    // card is gone -- nothing to answer into a slot that may already be
    // someone else's -- and the old slot is not read once more.
    const disconnectedView = await screen.findByTestId('team-view')
    expect(within(disconnectedView).queryByTestId('team-inbox-card')).toBeNull()
    expect(within(disconnectedView).queryByTestId('team-inbox-waiting')).toBeNull()
    expect(within(disconnectedView).queryByTestId('team-inbox-empty')).toBeNull()
    expect(oldKeyReads()).toBe(readsBefore)
    expect(threadPosts()).toBe(postsBeforeTeam + 1)
    act(() => { store.dispatch(sseConnected()) })
    // The reconnect re-confirms once, from the team view alone: no chat is
    // open beside it, so the page's own reconnect re-POST has no member to ask for.
    await waitFor(() => expect(threadPosts()).toBe(postsBeforeTeam + 2))
    // Unconfirmed by the gateway that is serving now: no card from the old
    // slot, and not one more read of it.
    const reconnected = await screen.findByTestId('team-view')
    expect(within(reconnected).queryByTestId('team-inbox-card')).toBeNull()
    expect(oldKeyReads()).toBe(readsBefore)

    await act(async () => {
      confirm({ slot_key: 'slot-reassigned', slug: 'oncall', member: 'oncall', created: false })
    })
    expect(await within(reconnected).findByTestId('team-inbox-card')).toHaveTextContent('New slot: ship?')
    await waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalledWith('slot-reassigned', expect.anything()))
    // The old slot was never read again -- not while waiting, not after.
    expect(oldKeyReads()).toBe(readsBefore)
  })

  it('the status strip counts calendar today from recent entries, not the rolling projection count', async () => {
    // The fixture's midnight and TeamView's own are read from one clock, pinned
    // at midday, so a run that straddles midnight cannot put both entries
    // before the component's "today". Date only: waitFor and React Query keep
    // real timers. The top-level afterEach puts the real clock back.
    vi.useFakeTimers({ toFake: ['Date'], now: new Date(2026, 8, 22, 12, 0, 0) })
    const midnight = new Date()
    midnight.setHours(0, 0, 0, 0)
    memberProjectionStore.apply('oncall', 'activity', {
      recent: [
        { ts: midnight.getTime() / 1000 - 60, via: 'chat', project: '' },
        { ts: midnight.getTime() / 1000 + 60, via: 'chat', project: '' },
      ],
      today: 2,
      week: 2,
    }, 1)
    vi.mocked(api.teams.list).mockResolvedValue({ teams: [{ id: 'abc123abc123', name: 'Triage', members: ['oncall'] }] })
    await renderPage([row({ name: 'oncall', slug: 'oncall' })])
    await rosterRow('oncall')
    fireEvent.click((await screen.findAllByTestId('team-group-header'))[0])
    const counts = await within(await screen.findByTestId('team-view')).findByTestId('team-status-counts')
    expect(counts).toHaveTextContent('Runs: 1 today · 2 this week')
    expect(counts.className.split(/\s+/)).toEqual(expect.arrayContaining(['hidden', 'sm:inline']))
  })

  it('a team with no crewmates keeps its header and opens an empty team view', async () => {
    vi.mocked(api.teams.list).mockResolvedValue({ teams: [{ id: 'abc123abc123', name: 'Release', members: [] }] })
    await renderPage([row({ name: 'oncall', slug: 'oncall' })])
    await rosterRow('oncall')
    const headers = await screen.findAllByTestId('team-group-header')
    // The empty team is still the user's team: its header is the way back to
    // Edit team, so it stays -- at "0 crewmates" -- above the No-team group.
    expect(headers.map((h) => h.dataset.team)).toEqual(['abc123abc123', 'no-team'])
    expect(headers[0]).toHaveTextContent('Release')
    expect(headers[0]).toHaveTextContent('0 crewmates')
    fireEvent.click(headers[0])
    const view = await screen.findByTestId('team-view')
    expect(within(view).getByTestId('team-empty')).toBeInTheDocument()
    expect(within(view).getByTestId('team-edit')).toBeInTheDocument()
  })

  it('a crewmate waiting on the user with no question in its tail gets a plain waiting card', async () => {
    vi.mocked(api.teams.list).mockResolvedValue({ teams: [{ id: 'abc123abc123', name: 'Triage', members: ['oncall'] }] })
    vi.mocked(api.chatSlotDetail).mockResolvedValue({
      messages: [{ role: 'assistant', content: 'Running the approval now.', ts: '2026-09-22T10:05:00Z' }],
    } as never)
    const { store } = await renderPage([row({ name: 'oncall', slug: 'oncall', bound: true, slot_key: 'member-oncall' })])
    await rosterRow('oncall')
    act(() => {
      store.dispatch(sseConnected())
      store.dispatch(sseSlots([{ key: 'member-oncall', mode: 'member', running: false, needs_input: true, messages: 3 }] as never))
    })
    fireEvent.click((await screen.findAllByTestId('team-group-header'))[0])
    const view = await screen.findByTestId('team-view')
    // The strip says waiting, so the inbox must not say "nothing waiting":
    // a card without a bubble names the state and offers the chat.
    const card = await within(view).findByTestId('team-inbox-card', undefined, PANE_READY)
    expect(within(card).getByTestId('team-inbox-waiting')).toHaveTextContent(/waiting on your reply/i)
    // No question to quote: the strip says it waits, and does not count a
    // question nobody can find.
    const statusRow = within(view).getByTestId('team-status-row')
    expect(statusRow).toHaveTextContent(/Waiting on you/)
    expect(statusRow).not.toHaveTextContent(/question/)
    expect(within(card).queryByTestId('team-inbox-bubble')).toBeNull()
    expect(within(card).getByTestId('team-inbox-open')).toBeInTheDocument()
    expect(within(view).queryByTestId('team-inbox-empty')).toBeNull()
  })

  it('the inbox reads a chat only through the thread endpoint, never the roster binding', async () => {
    vi.mocked(api.teams.list).mockResolvedValue({ teams: [{ id: 'abc123abc123', name: 'Triage', members: ['oncall'] }] })
    // The roster binding names one slot; the thread endpoint confirms another.
    vi.mocked(api.chatSlotDetail).mockResolvedValue({ messages: [{ role: 'assistant', content: 'Ship it?', ts: '2026-09-22T10:05:00Z' }] } as never)
    await renderPage([row({ name: 'oncall', slug: 'oncall', bound: true, slot_key: 'member-oncall-stale' })], 'kirocrew', {
      thread: { slot_key: 'member-oncall-confirmed', slug: 'oncall', member: 'oncall' },
    })
    await rosterRow('oncall')
    fireEvent.click((await screen.findAllByTestId('team-group-header'))[0])
    const view = await screen.findByTestId('team-view')
    await within(view).findByTestId('team-inbox-card')
    expect(api.memberThread).toHaveBeenCalledWith('oncall')
    const tailKeys = vi.mocked(api.chatSlotDetail).mock.calls.map((c) => c[0])
    expect(tailKeys).toContain('member-oncall-confirmed')
    expect(tailKeys).not.toContain('member-oncall-stale')
  })

  it('the Edit team dialog sends only the field it changed, so a stale dialog cannot clobber the other', async () => {
    vi.mocked(api.teams.list).mockResolvedValue({ teams: [{ id: 'abc123abc123', name: 'Triage', members: ['oncall'] }] })
    const update = vi.mocked(api.teams.update)
    update.mockResolvedValue({ team: { id: 'abc123abc123', name: 'Release', members: ['oncall'] } })
    await renderPage([row({ name: 'oncall', slug: 'oncall' }), row({ name: 'scribe', slug: 'scribe' })])
    await rosterRow('oncall')
    fireEvent.click((await screen.findAllByTestId('team-group-header'))[0])
    const view = await screen.findByTestId('team-view')
    fireEvent.click(within(view).getByTestId('team-edit'))
    await screen.findByTestId('team-dialog-body')
    // Nothing changed yet: an edit with nothing to send stays disabled.
    expect(screen.getByTestId('team-dialog-save')).toBeDisabled()
    fireEvent.change(screen.getByTestId('team-dialog-name'), { target: { value: 'Release' } })
    expect(screen.getByTestId('team-dialog-save')).toBeEnabled()
    fireEvent.click(screen.getByTestId('team-dialog-save'))
    await waitFor(() => expect(update).toHaveBeenCalledTimes(1))
    // The rename alone travels; membership is omitted so the route keeps whatever is current.
    expect(update).toHaveBeenCalledWith('abc123abc123', { name: 'Release' })
  })

  it('a refetch that fails after a save is said on the roster, which already shows the saved team', async () => {
    // First read answers; every later one (the refetch the save starts) fails.
    vi.mocked(api.teams.list)
      .mockResolvedValueOnce({ teams: [{ id: 'abc123abc123', name: 'Triage', members: ['oncall'] }] })
      .mockRejectedValue(new Error('boom'))
    vi.mocked(api.teams.update).mockResolvedValue({
      team: { id: 'abc123abc123', name: 'Release', members: ['oncall'] },
    })
    await renderPage([row({ name: 'oncall', slug: 'oncall' }), row({ name: 'scribe', slug: 'scribe' })])
    await rosterRow('oncall')
    expect(screen.queryByTestId('member-roster-teams-error')).toBeNull()
    fireEvent.click((await screen.findAllByTestId('team-group-header'))[0])
    const view = await screen.findByTestId('team-view')
    fireEvent.click(within(view).getByTestId('team-edit'))
    await screen.findByTestId('team-dialog-body')
    fireEvent.change(screen.getByTestId('team-dialog-name'), { target: { value: 'Release' } })
    fireEvent.click(screen.getByTestId('team-dialog-save'))
    // The failed refetch is a notice, not silence -- the cached list is on
    // screen and must be said to be possibly stale ...
    await screen.findByTestId('member-roster-teams-error', undefined, PANE_READY)
    // ... and that list is the route's answer to the save, not the mount-time one.
    expect(screen.getAllByTestId('team-group-header')[0]).toHaveTextContent('Release')
  })

  it('a crewmate moved between teams leaves its old team in the cache too, so a failed refetch cannot show it twice', async () => {
    const triage = { id: 'aaaaaaaaaaaa', name: 'Triage', members: ['oncall', 'scribe'] }
    const release = { id: 'bbbbbbbbbbbb', name: 'Release', members: ['fixer'] }
    // First read answers; the refetch the save starts fails, so what the
    // roster shows afterwards is exactly what the dialog wrote into the cache.
    vi.mocked(api.teams.list)
      .mockResolvedValueOnce({ teams: [triage, release] })
      .mockRejectedValue(new Error('boom'))
    // The store moved scribe: it is on Release now, and NOT on Triage.
    vi.mocked(api.teams.update).mockResolvedValue({ team: { ...release, members: ['fixer', 'scribe'] } })
    await renderPage([
      row({ name: 'oncall', slug: 'oncall' }),
      row({ name: 'scribe', slug: 'scribe' }),
      row({ name: 'fixer', slug: 'fixer' }),
    ])
    await rosterRow('oncall')
    fireEvent.click((await screen.findAllByTestId('team-group-header'))[1])
    const view = await screen.findByTestId('team-view')
    fireEvent.click(within(view).getByTestId('team-edit'))
    await screen.findByTestId('team-dialog-body')
    fireEvent.click(screen.getByLabelText('scribe'))
    fireEvent.click(screen.getByTestId('team-dialog-save'))
    await screen.findByTestId('member-roster-teams-error', undefined, PANE_READY)
    // One scribe row, and it sits under the Release header -- the Triage
    // group lost it in the same cache write that gave it to Release.
    const roster = screen.getByTestId('member-roster')
    const rows = within(roster).getAllByText('scribe')
    expect(rows).toHaveLength(1)
    let li: Element | null = rows[0].closest('li')
    while (li && li.getAttribute('data-testid') !== 'team-group') li = li.previousElementSibling
    expect(li?.getAttribute('data-team')).toBe('bbbbbbbbbbbb')
  })

  it('an initial roster failure is said in an open team pane instead of calling the team empty', async () => {
    vi.mocked(api.members).mockRejectedValue(new Error('boom'))
    vi.mocked(api.teams.list).mockResolvedValue({ teams: [{ id: 'abc123abc123', name: 'Triage', members: ['oncall'] }] })
    renderWithProviders(
      <NavigationLeaveGuardProvider><MembersPage /></NavigationLeaveGuardProvider>,
      { route: '/members?team=abc123abc123' },
    )
    const notice = await screen.findByTestId('team-view-roster-error')
    expect(notice).toHaveTextContent(/Could not load your crewmates/i)
    // The roster column stands beside a team view from md up and carries its
    // own copy of the notice, so the pane's copy is the below-md one.
    expect(notice.closest('.md\\:hidden')).not.toBeNull()
    expect(screen.getByTestId('member-roster').className.split(/\s+/)).toContain('md:flex')
    expect(screen.queryByTestId('team-empty')).toBeNull()
  })

  it('a failed team read is said in the open team pane too, where the roster that carries the notice is hidden below md', async () => {
    const triage = { id: 'aaaaaaaaaaaa', name: 'Triage', members: ['oncall'] }
    // First read answers; the refetch the rename starts fails. Below md the
    // roster (and its notice) is display:none while a team is open, so the
    // only visible surface is the team pane -- it must say the team may be stale.
    vi.mocked(api.teams.list)
      .mockResolvedValueOnce({ teams: [triage] })
      .mockRejectedValue(new Error('boom'))
    vi.mocked(api.teams.update).mockResolvedValue({ team: { ...triage, name: 'Release' } })
    await renderPage([row({ name: 'oncall', slug: 'oncall' })])
    await rosterRow('oncall')
    expect(screen.queryByTestId('team-view-teams-error')).toBeNull()
    fireEvent.click((await screen.findAllByTestId('team-group-header'))[0])
    const view = await screen.findByTestId('team-view')
    fireEvent.click(within(view).getByTestId('team-edit'))
    await screen.findByTestId('team-dialog-body')
    fireEvent.change(screen.getByTestId('team-dialog-name'), { target: { value: 'Release' } })
    fireEvent.click(screen.getByTestId('team-dialog-save'))
    // Both surfaces carry the notice; the pane's copy is the one a narrow
    // viewport can see (the roster's is inside the `hidden md:flex` aside),
    // and it steps aside at md where the roster's own notice is beside it.
    const paneNotice = await screen.findByTestId('team-view-teams-error', undefined, PANE_READY)
    expect(screen.getByTestId('team-view')).toBeTruthy()
    expect(screen.getByTestId('member-roster-teams-error')).toBeTruthy()
    expect(screen.getByTestId('member-roster').className.split(/\s+/)).toContain('hidden')
    expect(screen.getByTestId('member-roster').className.split(/\s+/)).toContain('md:flex')
    expect(paneNotice.closest('.md\\:hidden')).not.toBeNull()
    expect(screen.getByTestId('member-roster').contains(paneNotice)).toBe(false)
  })

  it('with the roster pinned beside the team pane, the pane\'s notice steps aside at md and up only', async () => {
    const triage = { id: 'aaaaaaaaaaaa', name: 'Triage', members: ['oncall'] }
    vi.mocked(api.teams.list)
      .mockResolvedValueOnce({ teams: [triage] })
      .mockRejectedValue(new Error('boom'))
    vi.mocked(api.teams.update).mockResolvedValue({ team: { ...triage, name: 'Release' } })
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    // Pinning lives on the thread header's switcher: open a thread, pin, then
    // open the team from the roster the pin brought back.
    fireEvent.click(await rosterRow('oncall'))
    fireEvent.click(await screen.findByTestId('crewmate-switcher'))
    fireEvent.click(await screen.findByTestId('crewmate-switcher-roster'))
    await waitFor(() => expect(screen.getByTestId('member-roster').className.split(/\s+/)).toContain('md:flex'))
    fireEvent.click((await screen.findAllByTestId('team-group-header'))[0])
    const view = await screen.findByTestId('team-view')
    fireEvent.click(within(view).getByTestId('team-edit'))
    await screen.findByTestId('team-dialog-body')
    fireEvent.change(screen.getByTestId('team-dialog-name'), { target: { value: 'Release' } })
    fireEvent.click(screen.getByTestId('team-dialog-save'))
    const paneNotice = await screen.findByTestId('team-view-teams-error', undefined, PANE_READY)
    // The roster's own copy is on screen beside the pane from md up, so the
    // pane's copy is the below-md one — exactly the roster's hidden range.
    expect(screen.getByTestId('member-roster').className.split(/\s+/)).toContain('md:flex')
    expect(paneNotice.closest('.md\\:hidden')).not.toBeNull()
    expect(screen.getByTestId('member-roster-teams-error')).toBeTruthy()
  })

  it('a membership edit travels as add / remove deltas, never as the dialog\'s whole snapshot', async () => {
    vi.mocked(api.teams.list).mockResolvedValue({ teams: [{ id: 'abc123abc123', name: 'Triage', members: ['oncall'] }] })
    const update = vi.mocked(api.teams.update)
    update.mockResolvedValue({ team: { id: 'abc123abc123', name: 'Triage', members: ['scribe'] } })
    await renderPage([row({ name: 'oncall', slug: 'oncall' }), row({ name: 'scribe', slug: 'scribe' })])
    await rosterRow('oncall')
    fireEvent.click((await screen.findAllByTestId('team-group-header'))[0])
    const view = await screen.findByTestId('team-view')
    fireEvent.click(within(view).getByTestId('team-edit'))
    await screen.findByTestId('team-dialog-body')
    fireEvent.click(screen.getByLabelText('scribe'))
    fireEvent.click(screen.getByLabelText('oncall'))
    fireEvent.click(screen.getByTestId('team-dialog-save'))
    await waitFor(() => expect(update).toHaveBeenCalledTimes(1))
    // Two toggles, two deltas; no `members` list, so another tab's move of a
    // third crewmate is left exactly where that tab put it.
    expect(update).toHaveBeenCalledWith('abc123abc123', { add: ['scribe'], remove: ['oncall'] })
  })

  it('the team view confirms a bound crewmate\'s thread once per open and never again on a window focus', async () => {
    vi.mocked(api.teams.list).mockResolvedValue({ teams: [{ id: 'abc123abc123', name: 'Triage', members: ['oncall'] }] })
    vi.mocked(api.chatSlotDetail).mockResolvedValue({ messages: [] } as never)
    await renderPage([row({ name: 'oncall', slug: 'oncall', bound: true, slot_key: 'member-oncall' })])
    await rosterRow('oncall')
    // The arrival opened oncall's own chat and confirmed its thread; the
    // team view's confirmations are counted from here.
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledTimes(1))
    const threadPosts = () => vi.mocked(api.memberThread).mock.calls.length
    const postsBeforeTeam = threadPosts()
    fireEvent.click((await screen.findAllByTestId('team-group-header'))[0])
    await screen.findByTestId('team-view')
    // The open confirms the thread (one POST) and reads its tail once.
    await waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalledTimes(1), PANE_READY)
    expect(threadPosts()).toBe(postsBeforeTeam + 1)
    // Long past every stale window the window regains focus. The tail is a
    // READ and refetches -- proof the focus was seen -- while the thread
    // confirm is a WRITE (the slot creator / repairer) and is not re-issued.
    const later = Date.now() + 10 * 60_000
    vi.useFakeTimers({ toFake: ['Date'] })
    try {
      vi.setSystemTime(later)
      act(() => {
        window.dispatchEvent(new Event('visibilitychange'))
      })
      await waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalledTimes(2))
      expect(threadPosts()).toBe(postsBeforeTeam + 1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('a team header says it opens the team, and the team dialog guards an unsaved draft against Escape', async () => {
    vi.mocked(api.teams.list).mockResolvedValue({ teams: [{ id: 'abc123abc123', name: 'Triage', members: ['oncall'] }] })
    await renderPage([row({ name: 'oncall', slug: 'oncall' }), row({ name: 'docs', slug: 'docs' })])
    await rosterRow('oncall')
    const header = (await screen.findAllByTestId('team-group-header'))[0]
    // The header's click opens rather than folds, so it says so.
    expect(within(header).getByTestId('team-group-open')).toHaveTextContent('Open team')
    expect(header).toHaveAttribute('aria-label', 'Open team Triage')
    // The "Open team ›" chevron sits in the row's right padding, so the header
    // must WIN the padding merge against ROW_BOX_CLS's `pr-3`: with `pr-3` kept
    // the absolute chevron lands on the label's tail and reads "Open te ›".
    expect(header.className.split(/\s+/)).toContain('pr-8')
    expect(header.className.split(/\s+/)).not.toContain('pr-3')
    expect(header.className.split(/\s+/)).toContain('py-1.5')
    expect(header.className.split(/\s+/)).not.toContain('py-2')
    const noTeamHeader = (await screen.findAllByTestId('team-group-header'))[1]
    expect(noTeamHeader.className.split(/\s+/)).toContain('py-1.5')
    expect(noTeamHeader.className.split(/\s+/)).not.toContain('py-2')
    // The team dialog, reached the way it still can be (the group header's
    // team view, then Edit): nothing changed -> Escape closes; a typed name ->
    // Escape is ignored.
    fireEvent.click(header)
    fireEvent.click(within(await screen.findByTestId('team-view')).getByTestId('team-edit'))
    await screen.findByTestId('team-dialog-body')
    // A crewmate on the team being edited reads "On this team"; one on another
    // team reads "On <name>", which create mode pins (`TeamDialog.test.tsx`).
    expect(screen.getAllByTestId('team-dialog-row').map((r) => r.textContent)).toEqual(
      expect.arrayContaining([expect.stringContaining('On this team')]),
    )
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('team-dialog-body')).toBeNull())
    fireEvent.click(within(await screen.findByTestId('team-view')).getByTestId('team-edit'))
    await screen.findByTestId('team-dialog-body')
    fireEvent.change(screen.getByTestId('team-dialog-name'), { target: { value: 'Release' } })
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.getByTestId('team-dialog-body')).toBeInTheDocument()
    // Cancel is the deliberate exit and still works.
    fireEvent.click(screen.getByTestId('team-dialog-cancel'))
    await waitFor(() => expect(screen.queryByTestId('team-dialog-body')).toBeNull())
  })

  it('a collapsed team hides its rows and the fold persists per team', async () => {
    vi.mocked(api.teams.list).mockResolvedValue({ teams: [{ id: 'abc123abc123', name: 'Triage', members: ['oncall'] }] })
    await renderPage([row({ name: 'oncall', slug: 'oncall' }), row({ name: 'docs', slug: 'docs' })])
    await rosterRow('oncall')
    fireEvent.click(screen.getByTestId('team-group-toggle'))
    await waitFor(() => expect(within(screen.getByTestId('member-roster')).queryByText('oncall')).toBeNull())
    // The other group is untouched, and the fold is remembered by team id.
    expect(await rosterRow('docs')).toBeInTheDocument()
    expect(JSON.parse(localStorage.getItem('mc-members-teams-collapsed') ?? '[]')).toEqual(['abc123abc123'])
  })

  it('the empty-state hero\'s "New crewmate" opens the card folded; its Advanced link opens it with Advanced settings unfolded', async () => {
    await renderPage([])
    const [cta] = await screen.findAllByTestId('crewmate-empty-cta')
    expect(cta).toHaveTextContent('New crewmate')
    fireEvent.click(cta)
    expect(await screen.findByTestId('crewmate-create-look')).toBeInTheDocument()
    expect(screen.getByTestId('crewmate-create-advanced-toggle')).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('combobox', { name: 'Built from' })).toBeNull()
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Close' }))
    fireEvent.click((await screen.findAllByTestId('crewmate-empty-advanced'))[0])
    expect(await screen.findByRole('combobox', { name: 'Built from' })).toBeInTheDocument()
    expect(screen.getByTestId('crewmate-create-advanced-toggle')).toHaveAttribute('aria-expanded', 'true')
    expect(navigateSpy).not.toHaveBeenCalledWith(expect.stringContaining('/capabilities'))
  })

  it('the page header draws the same two-ghost brand mark as the nav rail, and both add entries a bare plus', async () => {
    await renderPage([])
    const roster = await screen.findByTestId('member-roster')
    // The nav rail names this page with `CrewMemberMark` (surfaces/builtins),
    // so the header that opens under that row must not switch to Lucide's
    // person-pair `Users` — one glyph for one thing. The mark is a CSS mask
    // over currentColor, so it is found by its test id, not an svg class.
    expect(within(roster).getByTestId('crew-member-mark')).toBeInTheDocument()
    // On an empty roster the hero is the one create door: no header "+".
    expect(screen.queryByTestId('member-add')).toBeNull()
    // Every "add" entry carries a bare `Plus`, not `UserPlus`: the page
    // icon already says "members", and `UserPlus` would put the one Lucide
    // person figure on a page whose members are ghosts. Asserting on the
    // rendered svg class pins the glyph, not just that some icon rendered.
    for (const el of screen.getAllByTestId('crewmate-empty-cta')) {
      const icon = el.querySelector('svg')
      expect(icon).toHaveClass('lucide-plus')
      expect(icon).not.toHaveClass('lucide-user-plus')
    }
  })

  it('roster rows show the last message preview, not an Idle/Working label', async () => {
    await renderPage([
      row({ last_message: 'Six new issues triaged.' }),
      row({ name: 'quiet', slug: 'quiet' }),
    ])
    await rosterRow('oncall')
    // The most recently used crewmate opens by default, and its identity pill
    // in the thread header says "Idle" by design (once visibly, once for
    // screen readers). Wait for that pill so the case always runs against the
    // page it is checking, not only when the header happens to render late.
    await screen.findByTestId('member-identity-pill')
    // The preview is the row's sub-line, like a session row. Presence rides
    // the avatar dot, so a textual status label must not come back in the
    // ROSTER. The header's activity line is not a roster row, so the label
    // check is scoped to the roster column.
    expect(roster().getByText('Six new issues triaged.')).toBeTruthy()
    expect(roster().queryByText(/^(idle|working)$/i)).toBeNull()
    expect(roster().getByText('quiet')).toBeTruthy()
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
    act(() => {
      store.dispatch(sseSlots([{
        key: 'member-oncall', mode: 'member', running: false,
        subagents_running: true, messages: 0,
      }] as never))
    })
    expect(screen.getAllByTestId('member-presence-dot')).toHaveLength(1)
    act(() => {
      store.dispatch(sseSlots([{
        key: 'member-oncall', mode: 'member', running: false,
        subagents_running: false, messages: 0,
      }] as never))
    })
    expect(screen.queryByTestId('member-presence-dot')).toBeNull()
  })

  it('the search box filters the roster by name', async () => {
    await renderPage([
      row({ name: 'radar', slug: 'radar', dashboard_created: true }),
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
