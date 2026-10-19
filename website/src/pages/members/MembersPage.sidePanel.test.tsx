/* MembersPage: the roster's Starred filter count, the switcher the roster folds
 * into while a thread is open, the standing Dashboard and Files tabs, Profile
 * docking and its flights, the identity pill's name and chevron and the Profile
 * pencil, the withheld views and the roster header's "+" menu. The pill's
 * activity line and the Profile entry are in MembersPage.openThread.test.tsx.
 * This file, MembersPage.test.tsx and the other files that import
 * src/test/MembersPageTestUtils.tsx (their shared fixtures) carry the same api
 * mock and module stubs below, word for word: change them together. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { QueryClient } from '@tanstack/react-query'
import { memberProjectionStore } from '../../state/memberProjectionStore'
import { sseConnected, sseSlots } from '../../store/dashboardSlice'
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
    // The side panel opens Files only for a slot confirmed to run locally.
    chatSlotEnvironment: vi.fn(() => Promise.resolve({ profile: null, profiles: [] })),
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
import { CREW_DASHBOARD_TAB_ID, CREW_PANEL_TAB_IDS, MEMBERS_UNCONFIRMED_WITHHELD_VIEWS, MEMBERS_UNFED_VIEWS, MEMBERS_WITHHELD_VIEWS } from './MembersPage'
import { resetFirstGreetingRequests } from './useFirstGreeting'
import { __resetPanelTabs, VIEW_DATA_SOURCE } from '../../hooks/usePanelTabs'
import {
  PANEL_OPEN_KEY,
  WIDE_WINDOW,
  setWindowWidth,
  row,
  assistantRow,
  defaultRow,
  PANE_READY,
  realCreateClock,
  renderPage,
  currentUrl,
  rosterRow,
  openAddMenu,
  closeAddMenu,
  clickAddCrewmate,
  clickAddAdvanced,
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

  it('folds the roster into a switcher while a thread is open', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-identity-pill')
    expect(screen.getByTestId('member-roster')).toHaveClass('hidden')
    expect(screen.getByTestId('crewmate-switcher')).toBeInTheDocument()
    // No count on the chip (issue #19045): the open list says how many. The
    // unread column hangs from the header's bottom edge, absolutely, so it adds
    // nothing to the header's measured height: the thread is never pushed down.
    expect(screen.queryByTestId('crewmate-switcher-count')).toBeNull()
    const header = screen.getByTestId('member-thread-header')
    const slot = screen.getByTestId('crewmate-unread-column-slot')
    expect(header.contains(slot)).toBe(true)
    expect(slot.className).toContain('absolute')
    expect(slot.className).toContain('top-full')
  })

  it('the Assistant lands with its docked panel hidden, remembered apart from every other mate', async () => {
    localStorage.removeItem('mc-members-assistant-panel-open')
    const rows = [defaultRow(), assistantRow(), row({ name: 'oncall', slug: 'oncall', bound: true, slot_key: 'member-oncall' })]
    const utils = await renderPage(rows)
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-mate')
    expect(screen.queryByTestId('member-dashboard')).toBeNull()
    // Hidden, not removed: the header opener brings it back, and it is remembered.
    fireEvent.click(screen.getByTestId('member-panel-toggle'))
    expect(await screen.findByTestId('member-dashboard', undefined, PANE_READY)).toBeInTheDocument()
    expect(localStorage.getItem('mc-members-assistant-panel-open')).toBe('1')
    utils.unmount()
    // Another crewmate opens with the panel shown: the Assistant's choice is its own.
    localStorage.setItem('mc-members-assistant-panel-open', '0')
    await renderPage(rows, 'kirocrew', { route: '/members?member=oncall' })
    expect(await screen.findByTestId('member-dashboard', undefined, PANE_READY)).toBeInTheDocument()
  })

  it('the switcher\'s "Show the full roster" pins the roster beside the thread on desktop, where team headers and New team are reachable', async () => {
    // The switcher lists crewmates only. Team headers, New team, the star, the
    // filters and the sort live on the roster column, which a DM folds away on
    // desktop (the thread's back control is md:hidden and a bare /members
    // reopens a crewmate) — so the switcher's footer action is the one path to
    // them there. The pin is md+ only (`hidden md:flex`), survives a row pick
    // and a team open, and lifts from the roster header's own close.
    const triage = { id: 'abc123abc123', name: 'Triage', members: ['oncall'] }
    vi.mocked(api.teams.list).mockResolvedValue({ teams: [triage] })
    await renderPage([
      row({ name: 'oncall', slug: 'oncall', bound: true, slot_key: 'member-oncall' }),
      row({ name: 'research', slug: 'research' }),
    ])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-identity-pill')
    const classes = () => screen.getByTestId('member-roster').className.split(/\s+/)
    expect(classes()).toContain('hidden')
    expect(classes()).not.toContain('md:flex')
    expect(screen.queryByTestId('member-roster-hide')).toBeNull()

    fireEvent.click(screen.getByTestId('crewmate-switcher'))
    const action = await screen.findByTestId('crewmate-switcher-roster')
    expect(action).toHaveTextContent('Show the full roster')
    fireEvent.click(action)
    await waitFor(() => expect(classes()).toContain('md:flex'))
    // Still folded below md: the back control is the phone's way to the roster.
    expect(classes()).toContain('hidden')
    expect(screen.getByTestId('member-roster-hide')).toHaveAccessibleName('Hide the roster')
    // The open column IS the list the chip folds, so the chip fades out in
    // place while the pin holds; the identity pill stays.
    const slotClasses = () => screen.getByTestId('crewmate-switcher-slot').className.split(/\s+/)
    await waitFor(() => expect(slotClasses()).toContain('invisible'))
    expect(slotClasses()).toContain('opacity-0')
    expect(screen.getByTestId('member-identity-pill')).toBeVisible()
    // Keyboard focus follows into the column's search, not onto the page body.
    await waitFor(() => expect(screen.getByTestId('member-search')).toHaveFocus())

    // The pinned roster's "+" offers the crewmate door and nothing else:
    // team creation is hidden for the phase.
    fireEvent.pointerDown(screen.getByTestId('member-add'), { button: 0, ctrlKey: false, pointerType: 'mouse' })
    const addMenu = await screen.findByTestId('member-add-menu')
    // New crewmate and Import from file only; no team door.
    expect(within(addMenu).getAllByRole('menuitem')).toHaveLength(2)
    expect(screen.queryByTestId('member-add-team')).toBeNull()
    await closeAddMenu()

    // A row pick switches the thread and keeps the roster pinned.
    fireEvent.click(await rosterRow('research'))
    await waitFor(() => expect(screen.getByTestId('member-identity-pill')).toHaveTextContent('research'))
    expect(classes()).toContain('md:flex')

    // The team header opens the team view; the roster stays beside it at md+
    // on its own there (the team header draws no switcher and its Back is
    // md:hidden, so the column is the desktop way back), and the pin's own
    // close is withheld — lifting the pin would change nothing on screen.
    fireEvent.click((await screen.findAllByTestId('team-group-header'))[0])
    await screen.findByTestId('team-view')
    expect(classes()).toContain('md:flex')
    expect(screen.queryByTestId('member-roster-hide')).toBeNull()

    // Back in a thread the pin still holds, and the roster's own close lifts it.
    fireEvent.click(await rosterRow('research'))
    await waitFor(() => expect(screen.getByTestId('member-identity-pill')).toHaveTextContent('research'))
    expect(classes()).toContain('md:flex')
    // A keyboard press: focus sits on the X, which the close unmounts.
    screen.getByTestId('member-roster-hide').focus()
    fireEvent.click(screen.getByTestId('member-roster-hide'))
    await waitFor(() => expect(classes()).not.toContain('md:flex'))
    expect(classes()).toContain('hidden')
    expect(screen.queryByTestId('member-roster-hide')).toBeNull()
    // The column closed, the chip is back, and focus returns to it from the
    // X that just unmounted.
    expect(slotClasses()).toContain('visible')
    expect(slotClasses()).not.toContain('invisible')
    await waitFor(() => expect(screen.getByTestId('crewmate-switcher')).toHaveFocus())
  })

  it('a team view keeps the roster column beside it on desktop, pinned or not — it is the only way back', async () => {
    // TeamView's Back is md:hidden and the team header carries no switcher, so
    // a desktop /members?team=<id> with the roster folded had no exit at all.
    // The column is `hidden md:flex` in a team view regardless of the pin,
    // exactly as it was before the roster folded behind the switcher.
    vi.mocked(api.teams.list).mockResolvedValue({ teams: [{ id: 'abc123abc123', name: 'Triage', members: ['oncall'] }] })
    await renderPage([row({ name: 'oncall', slug: 'oncall' })], 'kirocrew', { route: '/members?team=abc123abc123' })
    await screen.findByTestId('team-view')
    const classes = () => screen.getByTestId('member-roster').className.split(/\s+/)
    expect(classes()).toContain('hidden')
    expect(classes()).toContain('md:flex')
    // No pin to lift, so no close control either.
    expect(screen.queryByTestId('member-roster-hide')).toBeNull()
    // The column is live: a row pick leaves the team view for the thread, which folds it.
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-identity-pill')
    expect(screen.queryByTestId('team-view')).toBeNull()
    expect(classes()).not.toContain('md:flex')
  })

  it('the side panel has Dashboard and Files as its two standing tabs', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-dashboard')

    const leading = within(screen.getByTestId('side-panel-leading-tabs')).getAllByRole('tab')
    expect(leading).toHaveLength(1)
    expect(leading[0]).toHaveAccessibleName('Dashboard')
    expect(screen.getByRole('tab', { name: 'Files' })).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: 'Notes' })).toBeNull()
    expect(screen.queryByRole('tab', { name: 'Work log' })).toBeNull()
    expect(screen.queryByRole('tab', { name: 'Schedules' })).toBeNull()
    // Side Chat remains dynamic rather than standing.
    expect(screen.queryByRole('tab', { name: 'Side Chat' })).toBeNull()
  })

  it('the Dashboard is the mate\'s generated page, with no repeated identity row', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    expect(await screen.findByTestId('member-dashboard')).toBeInTheDocument()
    // The tab holds the crewmate's own dynamic dashboard, carrying that
    // crewmate's identity.
    const stub = await screen.findByTestId('crew-dashboard-stub', undefined, PANE_READY)
    expect(stub).toHaveAttribute('data-slug', 'oncall')
    expect(screen.queryByTestId('member-identity-row')).toBeNull()
  })

  describe('a refresh opens the Dashboard tab, not the last focused one', () => {
    const dashTab = () => screen.getByTestId(`side-panel-leading-tab-${CREW_DASHBOARD_TAB_ID}`)
    const openOncall = async () => {
      const utils = await renderPage([row({ bound: true, slot_key: 'member-oncall' })], 'kirocrew', { route: '/members?member=oncall' })
      await screen.findByTestId('side-panel-leading-tabs', undefined, PANE_READY)
      return utils
    }

    it('reopens on Dashboard after a reload even when Files was focused', async () => {
      const first = await openOncall()
      fireEvent.click(screen.getByRole('tab', { name: 'Files' }))
      await waitFor(() => expect(screen.getByRole('tab', { name: 'Files' })).toHaveAttribute('aria-selected', 'true'))
      first.unmount()
      // The strip keeps its stored focus on Files, as the persisted bucket does
      // across a reload; the reload itself is the module state starting over.
      resetDashboardOnLoad()
      await openOncall()
      await waitFor(() => expect(dashTab()).toHaveAttribute('aria-selected', 'true'))
      expect(screen.getByRole('tab', { name: 'Files' })).toHaveAttribute('aria-selected', 'false')
    })

    it('keeps the tab the person picked on an in-app return', async () => {
      const first = await openOncall()
      fireEvent.click(screen.getByRole('tab', { name: 'Files' }))
      await waitFor(() => expect(screen.getByRole('tab', { name: 'Files' })).toHaveAttribute('aria-selected', 'true'))
      first.unmount()
      await openOncall()
      await waitFor(() => expect(screen.getByRole('tab', { name: 'Files' })).toHaveAttribute('aria-selected', 'true'))
      expect(dashTab()).toHaveAttribute('aria-selected', 'false')
    })
  })

  describe('a preview link opens the Dashboard tab on the STAGED page', () => {
    // The link `dashboard_preview` hands a crewmate to show the person:
    // `instance.preview_url` builds exactly this shape.
    const PREVIEW_ROUTE = '/members?member=oncall&dashboard=preview'

    it('opens a closed panel on the Dashboard tab and asks the frame for the preview', async () => {
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      await renderPage([row({ bound: true, slot_key: 'member-oncall' })], 'kirocrew', { route: PREVIEW_ROUTE })
      const stub = await screen.findByTestId('crew-dashboard-stub', undefined, PANE_READY)
      expect(stub).toHaveAttribute('data-preview', 'true')
      expect(stub).toHaveAttribute('data-member', 'oncall')
      expect(screen.getByTestId(`side-panel-leading-tab-${CREW_DASHBOARD_TAB_ID}`)).toHaveAttribute('aria-selected', 'true')
      // The one preview label the chat panel uses, above the staged page.
      expect(screen.getByTestId('dashboard-preview-badge')).toHaveTextContent('Preview, not applied')
    })

    it.each([
      ['the page link', 'chat-open-preview-member'],
      ['the older JSON-read link', 'chat-open-preview-slug'],
    ])('%s clicked in the thread opens the Dashboard tab on the staged page, not a second tab', async (_name, control) => {
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      await renderPage([row({ name: 'oncall', slug: 'oncall', bound: true, slot_key: 'member-oncall' })], 'kirocrew', { route: '/members?member=oncall' })
      fireEvent.click(await screen.findByTestId(control, undefined, PANE_READY))
      await waitFor(() => expect(currentUrl()).toBe('/members?member=oncall&dashboard=preview'))
      const stub = await screen.findByTestId('crew-dashboard-stub', undefined, PANE_READY)
      expect(stub).toHaveAttribute('data-preview', 'true')
      expect(screen.getByTestId(`side-panel-leading-tab-${CREW_DASHBOARD_TAB_ID}`)).toHaveAttribute('aria-selected', 'true')
      // One preview surface: the chat page's side-panel preview never mounts here.
      expect(screen.queryByTestId('dashboard-preview-panel')).toBeNull()
    })

    it('a preview link clicked in the thread re-reads the staged page, so a restaged page is never shown stale', async () => {
      const invalidate = vi.spyOn(QueryClient.prototype, 'invalidateQueries')
      await renderPage([row({ name: 'oncall', slug: 'oncall', bound: true, slot_key: 'member-oncall' })], 'kirocrew', { route: '/members?member=oncall' })
      invalidate.mockClear()
      fireEvent.click(await screen.findByTestId('chat-open-preview-member', undefined, PANE_READY))
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['member-dashboard', 'oncall'] })
      invalidate.mockRestore()
    })

    it('a staged page the agent applied (or that expired) hands over to the live page, with a line saying so', async () => {
      await renderPage([row({ bound: true, slot_key: 'member-oncall' })], 'kirocrew', { route: '/members?member=oncall&dashboard=preview' })
      expect(await screen.findByTestId('dashboard-preview-badge', undefined, PANE_READY)).toBeInTheDocument()
      fireEvent.click(await screen.findByTestId('crew-dashboard-stub-gone', undefined, PANE_READY))
      await waitFor(() => expect(screen.queryByTestId('dashboard-preview-badge')).toBeNull())
      // The URL stops asking for the preview, so the tab reads the live page.
      await waitFor(() => expect(currentUrl()).toBe('/members?member=oncall'))
      expect(screen.getByTestId('crew-dashboard-stub')).toHaveAttribute('data-preview', 'false')
      expect(screen.getByTestId('dashboard-preview-outcome')).toHaveAttribute('data-outcome', 'closed')
      expect(screen.getByTestId('crew-dashboard-tab')).toBeInTheDocument()
    })

    it('a plain member link is the live page', async () => {
      await renderPage([row({ bound: true, slot_key: 'member-oncall' })], 'kirocrew', { route: '/members?member=oncall' })
      expect(await screen.findByTestId('crew-dashboard-stub', undefined, PANE_READY)).toHaveAttribute('data-preview', 'false')
      expect(screen.queryByTestId('dashboard-preview-badge')).toBeNull()
    })
  })

  it('with the panel closed, the identity pill opens Profile in its own column and leaves one face', async () => {
    localStorage.setItem(PANEL_OPEN_KEY, '0')
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    fireEvent.click(await screen.findByTestId('member-identity-pill'))

    expect(await screen.findByTestId('crew-profile-docked')).toBeInTheDocument()
    expect(screen.queryByTestId('member-identity-pill')).toBeNull()
    expect(screen.getAllByTestId('crew-profile-face')).toHaveLength(1)
    expect(screen.getByTestId('crew-profile-name')).toHaveTextContent('oncall')
    expect(screen.getByTestId('crew-profile-tabs')).toBeInTheDocument()
    // jsdom lays nothing out, so the pill's face had no box to depart from: a
    // plain swap, no flight copy — the one-face invariant holds the cheap way.
    expect(screen.queryByTestId('crew-face-flight')).toBeNull()
    expect(screen.getByTestId('crew-profile-face')).not.toHaveStyle({ visibility: 'hidden' })
  })

  it('docking Profile flies the face as its own copy above the page, then lands it in the card', async () => {
    // #18236: the face used to be a framer `layoutId` shared by the pill and
    // the card head, travelling INSIDE the card — clipped by its rounded shell
    // and scrolling body, and under the thread on the way back. The flight is
    // now a portaled copy, and both real faces hold their place unpainted while
    // it is up, so there is still exactly one face on screen.
    localStorage.setItem(PANEL_OPEN_KEY, '0')
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    const pillFace = await screen.findByTestId('member-pill-face')
    // Give the departing face the box a laid-out pill has.
    vi.spyOn(pillFace, 'getBoundingClientRect').mockReturnValue(
      { x: 640, y: 14, left: 640, top: 14, right: 670, bottom: 44, width: 30, height: 30, toJSON: () => ({}) } as DOMRect,
    )
    fireEvent.click(screen.getByTestId('member-identity-pill'))

    const flight = await screen.findByTestId('crew-face-flight')
    // Above every surface, outside the card's clip chain, and not a target.
    expect(flight.parentElement).toBe(document.body)
    expect(flight).toHaveAttribute('aria-hidden', 'true')
    expect(flight.className).toMatch(/\bfixed\b/)
    expect(flight.className).toMatch(/pointer-events-none/)
    // The copy is the crewmate's face (decorative img, as the real ones are).
    expect(flight.querySelector('img')).not.toBeNull()
    // One face: the card's own is unpainted until the copy lands on it.
    expect(screen.getByTestId('crew-profile-face')).toHaveStyle({ visibility: 'hidden' })

    await waitFor(() => expect(screen.queryByTestId('crew-face-flight')).toBeNull(), { timeout: 3000 })
    expect(screen.getByTestId('crew-profile-face')).not.toHaveStyle({ visibility: 'hidden' })
  })

  it('re-docking during the undock exit leaves the card face painted once the flight lands', async () => {
    // The undock hides the LEAVING card's face by an inline style, since props
    // cannot reach an exiting AnimatePresence child. A re-dock opens a new card
    // (keyed on its open nonce), so that node, and its style, never comes back.
    localStorage.setItem(PANEL_OPEN_KEY, '0')
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    const box = (left: number, size: number) =>
      ({ x: left, y: 14, left, top: 14, right: left + size, bottom: 14 + size, width: size, height: size, toJSON: () => ({}) }) as DOMRect
    vi.spyOn(await screen.findByTestId('member-pill-face'), 'getBoundingClientRect').mockReturnValue(box(640, 30))
    fireEvent.click(screen.getByTestId('member-identity-pill'))
    await screen.findByTestId('crew-face-flight')
    await waitFor(() => expect(screen.queryByTestId('crew-face-flight')).toBeNull(), { timeout: 3000 })
    // Undock with a measurable card face, then re-dock before anything settles.
    vi.spyOn(screen.getByTestId('crew-profile-face'), 'getBoundingClientRect').mockReturnValue(box(900, 84))
    fireEvent.click(screen.getByTestId('crew-profile-close'))
    await screen.findByTestId('crew-face-flight')
    vi.spyOn(await screen.findByTestId('member-pill-face'), 'getBoundingClientRect').mockReturnValue(box(640, 30))
    fireEvent.click(screen.getByTestId('member-identity-pill'))
    await waitFor(() => expect(screen.queryByTestId('crew-face-flight')).toBeNull(), { timeout: 3000 })
    const faces = screen.getAllByTestId('crew-profile-face')
    expect(faces).toHaveLength(1)
    expect(faces[0]).not.toHaveStyle({ visibility: 'hidden' })
  })

  it('leaving the mate mid-flight drops the flight, so the next open starts with a painted pill face', async () => {
    // The copy unmounts with the crewmate before its onComplete; without the
    // clear, the stale flight replayed the old face and hid the next pill face.
    localStorage.setItem(PANEL_OPEN_KEY, '0')
    await renderPage([
      row({ name: 'oncall', slug: 'oncall', bound: true, slot_key: 'member-oncall' }),
      row({ name: 'research', slug: 'research', bound: true, slot_key: 'member-research' }),
    ])
    fireEvent.click(await rosterRow('oncall'))
    const pillFace = await screen.findByTestId('member-pill-face')
    vi.spyOn(pillFace, 'getBoundingClientRect').mockReturnValue(
      { x: 640, y: 14, left: 640, top: 14, right: 670, bottom: 44, width: 30, height: 30, toJSON: () => ({}) } as DOMRect,
    )
    fireEvent.click(screen.getByTestId('member-identity-pill'))
    await screen.findByTestId('crew-face-flight')

    fireEvent.click(await rosterRow('research'))
    await waitFor(() => expect(screen.getByTestId('member-identity-pill')).toHaveTextContent('research'))
    expect(screen.queryByTestId('crew-face-flight')).toBeNull()
    expect(screen.getByTestId('member-pill-face')).not.toHaveStyle({ visibility: 'hidden' })
  })

  it('the profile settings write only the confirmed thread slot', async () => {
    localStorage.setItem(PANEL_OPEN_KEY, '0')
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
    fireEvent.click(await screen.findByTestId('member-identity-pill'))
    // The card opens on Sessions (#18245); the settings live on the Profile tab.
    fireEvent.click(await screen.findByRole('tab', { name: 'Profile' }))
    // Awaits the lazy CrewProfileSettings import under the opened Profile card.
    const settings = await screen.findByTestId('profile-settings-slot', undefined, PANE_READY)
    expect(settings).toHaveTextContent('member-oncall')
    expect(settings).toHaveAttribute('data-waiting', '0')
  })

  it('a refused thread hands the profile settings no slot, even with a cached key', async () => {
    localStorage.setItem(PANEL_OPEN_KEY, '0')
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })], 'kirocrew', { thread: new Error('member_slot_conflict') })
    fireEvent.click(await rosterRow('oncall'))
    fireEvent.click(await screen.findByTestId('member-identity-pill'))
    // The card opens on Sessions (#18245); the settings live on the Profile tab.
    fireEvent.click(await screen.findByRole('tab', { name: 'Profile' }))
    // Awaits the lazy CrewProfileSettings import, then the refused POST.
    await screen.findByTestId('profile-settings-slot', undefined, PANE_READY)
    await waitFor(() => expect(screen.getByTestId('profile-settings-slot')).toBeEmptyDOMElement(), PANE_READY)
    // No confirmed thread, so the pickers wait instead of saving a record the
    // thread would not follow.
    expect(screen.getByTestId('profile-settings-slot')).toHaveAttribute('data-waiting', '1')
  })

  it('a first open still in flight makes the profile settings wait', async () => {
    localStorage.setItem(PANEL_OPEN_KEY, '0')
    // Never answers: the thread stays unconfirmed for the whole test.
    const pending = new Promise<never>(() => {})
    await renderPage([row({ bound: false, slot_key: '' })], 'kirocrew', { thread: pending as unknown as Record<string, unknown> })
    fireEvent.click(await rosterRow('oncall'))
    fireEvent.click(await screen.findByTestId('member-identity-pill'))
    // The card opens on Sessions (#18245); the settings live on the Profile tab.
    fireEvent.click(await screen.findByRole('tab', { name: 'Profile' }))
    // Awaits the lazy CrewProfileSettings import under the opened Profile card.
    const settings = await screen.findByTestId('profile-settings-slot', undefined, PANE_READY)
    expect(settings).toBeEmptyDOMElement()
    expect(settings).toHaveAttribute('data-waiting', '1')
  })

  it('opening the side panel folds a docked profile away and restores the pill', async () => {
    localStorage.setItem(PANEL_OPEN_KEY, '0')
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    fireEvent.click(await screen.findByTestId('member-identity-pill'))
    await screen.findByTestId('crew-profile-docked')

    fireEvent.click(await screen.findByTestId('member-panel-toggle'))
    expect(await screen.findByTestId('side-panel-root')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('crew-profile-docked')).toBeNull())
    expect(await screen.findByTestId('member-identity-pill')).toBeInTheDocument()
  })

  it('with the side panel open, Profile floats over chat and both remain visible', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('side-panel-root')
    fireEvent.click(await screen.findByTestId('member-identity-pill'))

    expect(await screen.findByTestId('crew-profile-modal')).toBeInTheDocument()
    expect(screen.getByTestId('side-panel-root')).toBeInTheDocument()
    expect(screen.getByTestId('member-identity-pill')).toBeInTheDocument()
    // The face is IN the card now: the pill keeps its slot but paints no face.
    expect(screen.getByTestId('member-pill-face')).toHaveStyle({ visibility: 'hidden' })
    expect(screen.getByTestId('crew-profile-face')).not.toHaveStyle({ visibility: 'hidden' })
  })

  /** The New goal modal's one button: the opening message into this mate's thread. */
  const NEW_GOAL_MESSAGE = 'I want to set a goal for you. Ask me what matters, then write it down for me to approve.'
  async function sendNewGoal() {
    fireEvent.click(screen.getByRole('tab', { name: 'Goals' }))
    fireEvent.click(await screen.findByTestId('crew-goals-add'))
    const dialog = await screen.findByRole('dialog', { name: 'New goal' })
    fireEvent.click(within(dialog).getByTestId('crew-goals-new-goal-send'))
  }

  it("Goals' New goal modal sends a visible message to the mate and closes the floating card", async () => {
    const sendMock = api.sendChat as ReturnType<typeof vi.fn>
    sendMock.mockClear()
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('side-panel-root')
    fireEvent.click(await screen.findByTestId('member-identity-pill'))
    await screen.findByTestId('crew-profile-modal')
    await sendNewGoal()
    await waitFor(() => expect(sendMock).toHaveBeenCalledTimes(1))
    const [message, slot, , , meta] = sendMock.mock.calls[0]
    // The ordinary send path: the message as the user's own, no hidden nudge.
    expect(message).toBe(NEW_GOAL_MESSAGE)
    expect(slot).toBe('member-oncall')
    expect(meta).toBeUndefined()
    // Delivered: the modal closes, and so does the card floating over the thread.
    await waitFor(() => expect(screen.queryByTestId('crew-profile-modal')).toBeNull(), { timeout: 3000 })
    expect(screen.queryByRole('dialog', { name: 'New goal' })).toBeNull()
  })

  it("Goals' New goal modal leaves the docked card open", async () => {
    const sendMock = api.sendChat as ReturnType<typeof vi.fn>
    sendMock.mockClear()
    localStorage.setItem(PANEL_OPEN_KEY, '0')
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    fireEvent.click(await screen.findByTestId('member-identity-pill'))
    await screen.findByTestId('crew-profile-docked')
    await sendNewGoal()
    await waitFor(() => expect(sendMock).toHaveBeenCalledTimes(1))
    const [message, slot, , , meta] = sendMock.mock.calls[0]
    expect(message).toBe(NEW_GOAL_MESSAGE)
    expect(slot).toBe('member-oncall')
    expect(meta).toBeUndefined()
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New goal' })).toBeNull())
    expect(screen.getByTestId('crew-profile-docked')).toBeInTheDocument()
  })

  it('with the side panel open, the pill face flies into the floating card and back on close', async () => {
    // #18329: the floating card used to appear in place with no tie to the pill
    // that opened it, and vanish in one frame. Same flight as the docked card.
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('side-panel-root')
    const box = (left: number, size: number) =>
      ({ x: left, y: 14, left, top: 14, right: left + size, bottom: 14 + size, width: size, height: size, toJSON: () => ({}) }) as DOMRect
    vi.spyOn(await screen.findByTestId('member-pill-face'), 'getBoundingClientRect').mockReturnValue(box(640, 30))
    fireEvent.click(screen.getByTestId('member-identity-pill'))

    const flight = await screen.findByTestId('crew-face-flight')
    expect(flight.parentElement).toBe(document.body)
    expect(screen.getByTestId('crew-profile-modal')).toBeInTheDocument()
    expect(screen.getByTestId('crew-profile-face')).toHaveStyle({ visibility: 'hidden' })
    await waitFor(() => expect(screen.queryByTestId('crew-face-flight')).toBeNull(), { timeout: 3000 })
    expect(screen.getByTestId('crew-profile-face')).not.toHaveStyle({ visibility: 'hidden' })
    expect(screen.getByTestId('member-pill-face')).toHaveStyle({ visibility: 'hidden' })

    // Close: the face flies back to the pill while the card fades out.
    vi.spyOn(screen.getByTestId('crew-profile-face'), 'getBoundingClientRect').mockReturnValue(box(900, 84))
    fireEvent.click(screen.getByTestId('crew-profile-close'))
    await screen.findByTestId('crew-face-flight')
    await waitFor(() => expect(screen.queryByTestId('crew-face-flight')).toBeNull(), { timeout: 3000 })
    await waitFor(() => expect(screen.queryByTestId('crew-profile-modal')).toBeNull(), { timeout: 3000 })
    expect(screen.getByTestId('member-pill-face')).not.toHaveStyle({ visibility: 'hidden' })
  })

  it('switching mates drops an open Profile instead of reopening it over the next one on the old tab', async () => {
    // `profile` used to survive the switch, and the card is keyed on name + tab:
    // the next crewmate came up with the card already open — on Sessions.
    localStorage.setItem(PANEL_OPEN_KEY, '0')
    await renderPage([
      row({ name: 'oncall', slug: 'oncall', bound: true, slot_key: 'member-oncall' }),
      row({ name: 'research', slug: 'research', bound: true, slot_key: 'member-research' }),
    ])
    fireEvent.click(await rosterRow('oncall'))
    fireEvent.click(await screen.findByTestId('member-identity-pill'))
    await screen.findByTestId('crew-profile-docked')
    fireEvent.click(screen.getByRole('tab', { name: 'Goals' }))
    expect(screen.getByTestId('crew-profile-panel')).toHaveAttribute('data-tab', 'goals')

    fireEvent.click(screen.getByTestId('crewmate-switcher'))
    const rows = await screen.findAllByTestId('crewmate-switcher-row')
    fireEvent.click(rows.find((r) => r.textContent?.includes('research'))!)
    await waitFor(() => expect(screen.getByTestId('member-identity-pill')).toHaveTextContent('research'))
    await waitFor(() => expect(screen.queryByTestId('crew-profile-docked')).toBeNull())
    expect(screen.queryByTestId('crew-profile-panel')).toBeNull()
    expect(screen.getByTestId('member-identity-pill')).toHaveAttribute('aria-expanded', 'false')
    // Opening it again starts on Sessions, not where the last crewmate's card was left.
    fireEvent.click(screen.getByTestId('member-identity-pill'))
    expect(await screen.findByTestId('crew-profile-panel')).toHaveAttribute('data-tab', 'sessions')
  })

  it('a window that narrows below md while Profile holds its column re-places the card as floating, keeping it open', async () => {
    // Placement is fixed at open time, so without this a phone-width viewport
    // kept a fixed-width in-flow aside beside the thread.
    localStorage.setItem(PANEL_OPEN_KEY, '0')
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    fireEvent.click(await screen.findByTestId('member-identity-pill'))
    await screen.findByTestId('crew-profile-docked')
    // Give the docked card's face a box: a re-placement must not read it as a
    // close and fly the face out (the floating card takes over the same ref).
    vi.spyOn(screen.getByTestId('crew-profile-face'), 'getBoundingClientRect').mockReturnValue(
      { x: 900, y: 60, left: 900, top: 60, right: 984, bottom: 144, width: 84, height: 84, toJSON: () => ({}) } as DOMRect,
    )
    // useIsMobile re-reads matchMedia on a window resize and re-keys on the
    // function's identity, so swapping the function and firing `resize` is a
    // live breakpoint crossing. Restored BY VALUE below: happy-dom exposes
    // `window.matchMedia` through an accessor whose setter the assignment
    // writes through, so re-defining the saved descriptor would keep the mock
    // and leave every later desktop case reading as a phone.
    const orig = window.matchMedia
    try {
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
      setWindowWidth(390)
      act(() => { window.dispatchEvent(new Event('resize')) })
      // The column is dropped AT ONCE, without its width exit: a phone-width
      // viewport never holds the in-flow column beside the floating card, not
      // even for the exit's 0.26s, and only one card and one card face exist.
      await screen.findByTestId('crew-profile-modal')
      expect(screen.queryByTestId('crew-profile-docked')).toBeNull()
      expect(screen.getAllByTestId('crew-profile-card')).toHaveLength(1)
      expect(screen.getAllByTestId('crew-profile-face')).toHaveLength(1)
      expect(screen.getByTestId('crew-profile-modal')).toBeInTheDocument()
      expect(screen.getByTestId('crew-profile-panel')).toBeInTheDocument()
      expect(screen.getByTestId('member-identity-pill')).toHaveAttribute('aria-expanded', 'true')
      // Still one card, no flight: the floating card's face is painted and no
      // copy is in the air. The pill's face slot is held empty, as it is for
      // every floating card — the face is in the card.
      expect(screen.queryByTestId('crew-face-flight')).toBeNull()
      expect(screen.getByTestId('crew-profile-face')).not.toHaveStyle({ visibility: 'hidden' })
      expect(screen.getByTestId('member-pill-face')).toHaveStyle({ visibility: 'hidden' })
    } finally {
      window.matchMedia = orig
      setWindowWidth(WIDE_WINDOW)
    }
  })

  it('the full roster row carries the switcher\'s needs-you cue, read from the same resolver', async () => {
    const { store } = await renderPage([
      row({ name: 'oncall', slug: 'oncall', bound: true, slot_key: 'member-oncall' }),
      row({ name: 'research', slug: 'research', bound: true, slot_key: 'member-research' }),
    ])
    await rosterRow('oncall')
    expect(screen.queryByTestId('member-needs-you')).toBeNull()
    act(() => {
      store.dispatch(sseConnected())
      store.dispatch(sseSlots([{ key: 'member-oncall', mode: 'member', running: false, needs_input: true, messages: 3 }] as never))
    })
    const cue = await screen.findByTestId('member-needs-you')
    // Only the parked crewmate's row, named like the switcher's dot and
    // carrying the same muted word beside it.
    expect(screen.getAllByTestId('member-needs-you')).toHaveLength(1)
    expect(cue.closest('li')).toHaveTextContent('oncall')
    expect(within(cue).getByTestId('member-needs-you-dot')).toHaveAccessibleName('Needs your approval or answer')
    expect(cue).toHaveTextContent('Needs you')
    // The two views agree: with research open, the folded switcher lights for oncall.
    fireEvent.click(await rosterRow('research'))
    expect(await screen.findByTestId('crewmate-switcher-needs-you')).toBeInTheDocument()
  })

  it('the identity pill shows only the display name, never the ID', async () => {
    await renderPage([
      row({ name: 'kiro', slug: 'kiro', display_name: 'Kiro', bound: true, slot_key: 'member-kiro' }),
      row({ name: 'oncall', slug: 'oncall', display_name: 'Oncall Sentinel', bound: true, slot_key: 'member-oncall' }),
    ])
    fireEvent.click(await rosterRow('Kiro'))
    const pill = await screen.findByTestId('member-identity-pill')
    expect(within(pill).getByTestId('member-pill-name')).toHaveTextContent('Kiro')
    expect(within(pill).queryByTestId('member-pill-id')).toBeNull()
    fireEvent.click(await rosterRow('Oncall Sentinel'))
    await waitFor(() => expect(screen.getByTestId('member-identity-pill')).toHaveTextContent('Oncall Sentinel'))
    expect(within(screen.getByTestId('member-identity-pill')).queryByTestId('member-pill-id')).toBeNull()
    expect(screen.getByTestId('member-identity-pill')).not.toHaveTextContent(/\boncall\b/)
  })

  it('the profile pencil opens the existing editor for the exact mate', async () => {
    localStorage.setItem(PANEL_OPEN_KEY, '0')
    await renderPage([row({ name: 'on call/2', slug: 'on-call-2', bound: true, slot_key: 'member-on-call-2' })])
    fireEvent.click(await rosterRow('on call/2'))
    fireEvent.click(await screen.findByTestId('member-identity-pill'))
    fireEvent.click(await screen.findByTestId('crew-profile-edit'))

    expect(await screen.findByTestId('crew-editor-dialog-open')).toHaveTextContent('on call/2')
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it('withholds only the unfed views, Command Center and Artifacts: Dashboard + Files stand, the + menu keeps its dynamic views', async () => {
    // The set is the contract: every view fed by ChatPage-owned transcript
    // indexes plus the session Summary (MEMBERS_UNFED_VIEWS), the chat page's
    // Command Center (the Dashboard tab is the one dashboard entrance here) and
    // Artifacts — pinned on the chat page, but this page's standing set is
    // Dashboard + Files. Nothing else: Terminal, Browser, Git, Subagents,
    // Workflows, the Developer-Mode views and app tabs stay reachable from +.
    expect([...MEMBERS_UNFED_VIEWS].sort()).toEqual(['changes', 'issues', 'links', 'pins', 'summary'])
    expect([...MEMBERS_WITHHELD_VIEWS].sort()).toEqual([...MEMBERS_UNFED_VIEWS, 'command-center', 'artifacts'].sort())
    expect(MEMBERS_WITHHELD_VIEWS).not.toContain('terminal')
    expect(MEMBERS_WITHHELD_VIEWS).not.toContain('app')
    expect(MEMBERS_WITHHELD_VIEWS).not.toContain('side')
    // While the thread is unconfirmed EVERY classified view is withheld, plus
    // Terminal and app tabs — derived from the classification, so a new
    // ViewKind lands in this set without anyone listing it.
    expect([...MEMBERS_UNCONFIRMED_WITHHELD_VIEWS].sort()).toEqual(
      [...(Object.keys(VIEW_DATA_SOURCE) as string[]), 'terminal', 'app'].sort(),
    )
    expect(MEMBERS_UNCONFIRMED_WITHHELD_VIEWS).toEqual(expect.arrayContaining([...MEMBERS_WITHHELD_VIEWS]))
    expect(CREW_PANEL_TAB_IDS).toEqual([CREW_DASHBOARD_TAB_ID])

    const { store } = await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-dashboard')
    // The slots frame carries the confirmed slot's record, so Terminal's cwd is known.
    act(() => {
      store.dispatch(sseSlots([{ key: 'member-oncall', mode: 'member', running: false, messages: 0 }] as never))
    })
    // Standing tabs: Dashboard (leading) + Files (pinned) — no Artifacts, no Changes.
    expect(screen.getAllByRole('tab').map((t) => t.getAttribute('aria-label'))).toEqual(['Dashboard', 'Files'])
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'Open side panel tab' }),
      { button: 0, ctrlKey: false, pointerType: 'mouse' },
    )
    const menu = await screen.findByRole('menu')
    for (const name of ['Side Chat', 'Terminal', 'Browser', 'Git', 'Subagents', 'Workflows']) {
      expect(within(menu).getByRole('menuitem', { name })).toBeInTheDocument()
    }
    for (const name of ['Pins', 'Issues', 'Links', 'Summary', 'Artifacts', 'Changes', 'Command Center']) {
      expect(within(menu).queryByRole('menuitem', { name })).toBeNull()
    }
  })

  it('Files is rooted in the slot\'s resolved project directory, not the crew\'s workspace name', async () => {
    // `workspace` on the roster row is the configured workspace's NAME — a key
    // into the workspaces list, "default" for most crewmates. The directory it
    // resolves to arrives on the member slot's WS frame as `project`. Rooting
    // Files in the name opened a folder literally called "default".
    const { store } = await renderPage([row({ bound: true, slot_key: 'member-oncall', workspace: 'default' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-dashboard')
    act(() => {
      store.dispatch(sseSlots([{ key: 'member-oncall', mode: 'member', running: false, messages: 0, project: '/home/me/repos/oncall-desk' }] as never))
    })
    fireEvent.click(screen.getByRole('tab', { name: 'Files' }))
    const files = await screen.findByTestId('files-home-stub')
    expect(files).toHaveAttribute('data-project-dir', '/home/me/repos/oncall-desk')
    // The Profile's Workspace tile still names the workspace, as a name. The
    // card opens on Sessions, so the Profile tab is selected first.
    fireEvent.click(await screen.findByTestId('member-identity-pill'))
    fireEvent.click(await screen.findByRole('tab', { name: 'Profile' }))
    expect(await screen.findByTestId('crew-profile-workspace')).toHaveTextContent('default')
  })

  it('the identity pill carries a chevron, so it reads as a door and not as a second switcher chip', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    const pill = await screen.findByTestId('member-identity-pill')
    const chevron = within(pill).getByTestId('member-identity-pill-chevron')
    expect(chevron).toHaveAttribute('aria-hidden', 'true')
    // Decorative: the pill's name stays the crewmate's.
    expect(pill).toHaveAccessibleName(/oncall/)
  })

  it('the "+" menu offers New mate and Import from file only: no Advanced or guided-flow rows', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    await rosterRow('oncall')
    await openAddMenu()
    const menu = await screen.findByTestId('member-add-menu')
    expect(within(menu).getAllByRole('menuitem').map(el => el.textContent?.trim())).toEqual(['New mate', 'Import from file'])
  })

  it('the roster header "+" menu\'s New mate opens the card as a modal with Advanced settings folded, and they unfold inline — no trip to the crew manager', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    await rosterRow('oncall')
    // Creating a crewmate IS creating a crew record, and the write path is
    // still `POST /api/agents`; what changed is the front door. The primary
    // row opens the New crewmate card as a modal with Advanced settings
    // folded; they unfold inline. Nothing navigates.
    expect(screen.getByTestId('member-add')).toHaveAttribute('aria-label', 'Add…')
    await clickAddCrewmate()
    expect(await screen.findByTestId('crewmate-create-look')).toBeInTheDocument()
    // The card is now a Radix-less Modal (role="dialog"), holding the look.
    const dialog = screen.getByRole('dialog')
    expect(dialog).toContainElement(screen.getByTestId('crewmate-create-look'))
    expect(screen.queryByRole('combobox', { name: 'Built from' })).toBeNull()
    expect(screen.getByTestId('crewmate-create-advanced-toggle')).toHaveAttribute('aria-expanded', 'false')
    // Advanced settings unfolds inline, in the same card.
    fireEvent.click(screen.getByTestId('crewmate-create-advanced-toggle'))
    expect(await screen.findByRole('combobox', { name: 'Built from' })).toBeInTheDocument()
    // The modal's own header X ("Close") is the way out, in place of the old
    // in-place card's Cancel.
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }))
    await clickAddAdvanced()
    expect(await screen.findByRole('combobox', { name: 'Built from' })).toBeInTheDocument()
    expect(screen.getByTestId('crewmate-create-advanced-toggle')).toHaveAttribute('aria-expanded', 'true')
    // A modal named "New crewmate", holding the form — no in-place page region.
    expect(screen.getByRole('dialog')).toHaveAccessibleName('New mate')
    expect(screen.getByRole('dialog')).toContainElement(screen.getByTestId('crewmate-create-form'))
    expect(navigateSpy).not.toHaveBeenCalledWith(expect.stringContaining('/capabilities'))
  })
})
