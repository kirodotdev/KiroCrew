/* MembersPage: which crewmate a visit opens on a wide window —
 * resolveDefaultMember, the messages-app order, the default member, the
 * remembered one, a member named in the URL, the roster's sort and the history
 * entries a switch writes. Below md is MembersPage.narrow.test.tsx.
 * This file, MembersPage.test.tsx and the other files that import
 * src/test/MembersPageTestUtils.tsx (their shared fixtures) carry the same api
 * mock and module stubs below, word for word: change them together. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { Route, Routes, useNavigate } from 'react-router-dom'
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
import MembersPage, { resolveDefaultMember, resolveMateLanding } from './MembersPage'
import { resetFirstGreetingRequests } from './useFirstGreeting'
import { __resetPanelTabs } from '../../hooks/usePanelTabs'
import {
  LAST_MEMBER_KEY,
  WIDE_WINDOW,
  setWindowWidth,
  row,
  assistantRow,
  defaultRow,
  echoThread,
  PANE_READY,
  realCreateClock,
  renderPage,
  LocationProbe,
  currentUrl,
  roster,
  rosterRow,
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

describe('resolveDefaultMember', () => {
  const ordered = [
    row({ name: 'alpha', slug: 'alpha', last_active_ts: 100 }),
    row({ name: 'beta', slug: 'beta', last_active_ts: 200 }),
  ]

  it('nobody chatted -> the crewmate with the greatest last_active_ts', () => {
    // Product decision (CrewMates launch review): the "pick one" landing is
    // gone — the most recently USED crewmate opens by default.
    expect(resolveDefaultMember(ordered)?.name).toBe('beta')
  })

  it('a tie in last_active_ts keeps the first crewmate in `ordered`', () => {
    const tied = [
      row({ name: 'alpha', slug: 'alpha', last_active_ts: 100 }),
      row({ name: 'beta', slug: 'beta', last_active_ts: 100 }),
    ]
    expect(resolveDefaultMember(tied)?.name).toBe('alpha')
  })

  it('the built-in default crewmate is a row like any other: the newest conversation wins', () => {
    const withDefault = [row({ name: 'default', slug: 'default', last_active_ts: 999 }), ...ordered]
    expect(resolveDefaultMember(withDefault)?.name).toBe('default')
    // A default-only roster is the restore effect's hero case (`hasNoCrewmates`),
    // decided before this is asked; here it is just a one-row roster.
    expect(resolveDefaultMember([row({ name: 'default', slug: 'default' })])?.name).toBe('default')
  })

  it('Mate is the landing while no message was exchanged with it; then it is a crewmate like any other', () => {
    const withMate = [
      row({ name: 'alpha', slug: 'alpha', last_active_ts: 50 }),
      defaultRow({ last_active_ts: 999 }),
      assistantRow({ has_dm_message: false }),
    ]
    expect(resolveMateLanding(withMate)?.name).toBe('mate')
    // A thread with history (or a live preview of one) ends the landing.
    expect(resolveMateLanding([assistantRow({ has_dm_message: true })])).toBeUndefined()
    expect(resolveMateLanding([assistantRow({ last_message: 'Hi, I am Mate' })])).toBeUndefined()
    expect(resolveMateLanding([row({ name: 'alpha', slug: 'alpha' })])).toBeUndefined()
    // A claimed welcome marker counts as seen even with an empty thread (a
    // welcome that failed), and so does a row that does not say.
    expect(resolveMateLanding([assistantRow({ welcome_claimed: true })])).toBeUndefined()
    expect(resolveMateLanding([assistantRow({ welcome_claimed: undefined })])).toBeUndefined()
    // A welcome never owed is never sent: Mate is not waiting for it.
    expect(resolveMateLanding([assistantRow({ welcome_owed: false })])).toBeUndefined()
    expect(resolveMateLanding([assistantRow({ welcome_owed: undefined })])).toBeUndefined()
    // The usual rule carries no Mate special case: the newest conversation,
    // whoever it is, the built-in default included.
    expect(resolveDefaultMember(withMate)?.name).toBe('default')
    expect(resolveDefaultMember([defaultRow(), assistantRow()])?.name).toBe('default')
  })

  it('an empty roster resolves to undefined, never throws', () => {
    expect(resolveDefaultMember([])).toBeUndefined()
  })

  it('the thread with the newest message wins, whoever sent it; a stampless roster keeps the first', () => {
    const rows = [
      row({ name: 'alpha', slug: 'alpha', last_active_ts: 900 }),
      row({ name: 'beta', slug: 'beta', last_active_ts: 10 }),
      row({ name: 'default', slug: 'default', last_active_ts: 200 }),
    ]
    expect(resolveDefaultMember(rows)?.name).toBe('alpha')
    expect(resolveDefaultMember([row({ name: 'a', slug: 'a' }), row({ name: 'b', slug: 'b' })])?.name).toBe('a')
  })
})

describe('MembersPage lists conversations like a messages app', () => {
  it('opens the remembered thread, lists every thread that holds a message, newest first, hides empty ones', async () => {
    localStorage.setItem(LAST_MEMBER_KEY, 'older')
    await renderPage([
      // A scheduled run wrote the newest message: listed, and first.
      row({ name: 'bg-only', slug: 'bg-only', last_active_ts: 900, has_dm_message: true, dashboard_created: true }),
      // An empty thread: not listed until the search reaches it.
      row({ name: 'app-bot', slug: 'app-bot', last_active_ts: 800, has_dm_message: false }),
      row({ name: 'older', slug: 'older', last_active_ts: 1, has_dm_message: true }),
      row({ name: 'newest', slug: 'newest', last_active_ts: 2, has_dm_message: true }),
    ])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-older')
    expect(currentUrl()).toBe('/members?member=older')
    const names = () =>
      roster()
        .getAllByRole('listitem')
        .map((li) => within(li).queryByText(/^(bg-only|app-bot|older|newest)$/)?.textContent)
        .filter(Boolean)
    await waitFor(() => expect(names()).toEqual(['bg-only', 'newest', 'older']))
    // The search still reaches an empty thread.
    fireEvent.change(screen.getByTestId('member-search'), { target: { value: 'app' } })
    await waitFor(() => expect(names()).toEqual(['app-bot']))
  })

  it('nothing remembered: the newest conversation opens, whoever wrote its last message', async () => {
    await renderPage([
      row({ name: 'quiet', slug: 'quiet', last_active_ts: 1, has_dm_message: true }),
      row({ name: 'loud', slug: 'loud', last_active_ts: 500, has_dm_message: true }),
    ])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-loud')
    expect(currentUrl()).toBe('/members?member=loud')
  })
})

describe('MembersPage default member, memory and URL', () => {
  const alphaBeta = () => [row({ name: 'alpha', slug: 'alpha' }), row({ name: 'beta', slug: 'beta' })]

  it('a fresh visit with nothing remembered opens the most recently used crewmate', async () => {
    await renderPage([
      row({ name: 'zeta-quiet', slug: 'zeta-quiet' }),
      row({ name: 'fresh-talker', slug: 'fresh-talker', last_active_ts: 200 }),
      row({ name: 'old-talker', slug: 'old-talker', last_active_ts: 100 }),
    ])
    // No memory, no ?member=: the page follows the user's own history (the
    // greatest last_active_ts) rather than priming on whichever row the
    // 'recent' sort floated to the top (#11763) — the "Pick a member" landing
    // is gone. `fresh-talker` (ts 200) opens with no click, the URL is
    // rewritten, and the default open counts as a remembered choice.
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-fresh-talker')
    expect(api.memberThread).toHaveBeenCalledWith('fresh-talker')
    expect(currentUrl()).toBe('/members?member=fresh-talker')
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('fresh-talker')
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

  it('opening the built-in default by link remembers it like any crewmate', async () => {
    localStorage.setItem(LAST_MEMBER_KEY, 'alpha')
    await renderPage([
      row({ name: 'default', slug: 'default', last_active_ts: 900 }),
      row({ name: 'alpha', slug: 'alpha', last_active_ts: 500 }),
      row({ name: 'beta', slug: 'beta', last_active_ts: 700 }),
    ], 'kirocrew', { route: '/members?member=default' })

    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-default')
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('default')
  })

  it('re-clicking the open default row remembers it', async () => {
    localStorage.setItem(LAST_MEMBER_KEY, 'alpha')
    await renderPage([
      row({ name: 'default', slug: 'default' }),
      row({ name: 'alpha', slug: 'alpha' }),
    ], 'kirocrew', { route: '/members?member=default' })
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-default')
    fireEvent.click(await rosterRow('default'))
    await waitFor(() => expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('default'))
  })

  it('clicking the built-in default, leaving, and returning restores default', async () => {
    const rows = [
      row({ name: 'default', slug: 'default', last_active_ts: 100 }),
      row({ name: 'alpha', slug: 'alpha', last_active_ts: 200 }),
    ]
    const first = await renderPage(rows)
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-alpha')
    fireEvent.click(await rosterRow('default'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-default'), PANE_READY)
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('default')
    first.unmount()

    // Back via the rail: a bare `/members` with no `?member=`.
    await renderPage(rows)
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-default')
    expect(currentUrl()).toBe('/members?member=default')
  })

  it('a crewmate opened but not messaged since is the one a return lands on (#19341)', async () => {
    // alpha holds the newest message; the user clicked beta, sent nothing,
    // left the page, and came back. The memory, not the newest thread, decides.
    const rows = [
      row({ name: 'alpha', slug: 'alpha', last_active_ts: 500 }),
      row({ name: 'beta', slug: 'beta', last_active_ts: 10 }),
    ]
    const first = await renderPage(rows)
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-alpha')
    fireEvent.click(await rosterRow('beta'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-beta'))
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('beta')
    first.unmount()

    // Back via the rail: a bare `/members` with no `?member=`.
    await renderPage(rows)
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-beta')
    expect(currentUrl()).toBe('/members?member=beta')
    // A restore keeps the memory as it was.
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('beta')
  })

  it('the remembered thread wins over a newer message elsewhere, like a messages app', async () => {
    localStorage.setItem(LAST_MEMBER_KEY, 'alpha')
    await renderPage([
      row({ name: 'default', slug: 'default', last_active_ts: 900 }),
      row({ name: 'alpha', slug: 'alpha', last_active_ts: 500 }),
    ])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-alpha')
    expect(currentUrl()).toBe('/members?member=alpha')
  })

  it('with nothing remembered, the default DM opens when it holds the newest message', async () => {
    await renderPage([
      row({ name: 'default', slug: 'default', last_active_ts: 900 }),
      row({ name: 'alpha', slug: 'alpha', last_active_ts: 500 }),
    ])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-default')
    expect(currentUrl()).toBe('/members?member=default')
  })

  it('a remembered default on a default-only roster still shows the hero', async () => {
    localStorage.setItem(LAST_MEMBER_KEY, 'default')
    await renderPage([row({ name: 'default', slug: 'default', last_active_ts: 999 })])
    expect(await screen.findAllByTestId('crewmate-empty-hero')).toHaveLength(2)
    expect(api.memberThread).not.toHaveBeenCalled()
    expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
    expect(currentUrl()).toBe('/members')
  })

  it('opening the Assistant is a selection like any other: it is remembered and a returning visit reopens it', async () => {
    localStorage.setItem(LAST_MEMBER_KEY, 'alpha')
    const utils = await renderPage([
      defaultRow(),
      assistantRow(),
      row({ name: 'alpha', slug: 'alpha', last_active_ts: 500 }),
    ], 'kirocrew', { route: '/members?member=mate' })

    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-mate')
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('mate')
    // A returning bare visit: the remembered Assistant wins over the more
    // recently used crewmate.
    utils.unmount()
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockClear()
    await renderPage([
      defaultRow(),
      assistantRow(),
      row({ name: 'alpha', slug: 'alpha', last_active_ts: 500 }),
    ])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-mate')
    expect(api.memberThread).not.toHaveBeenCalledWith('alpha')
  })

  it('a never-chatted Mate is opened ahead of a remembered crewmate', async () => {
    localStorage.setItem(LAST_MEMBER_KEY, 'alpha')
    await renderPage([defaultRow(), assistantRow({ has_dm_message: false }), row({ name: 'alpha', slug: 'alpha', last_active_ts: 99 })])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-mate')
    expect(currentUrl()).toBe('/members?member=mate')
  })

  it('an existing user who chatted with a crewmate lands on an unseen Mate once', async () => {
    await renderPage([
      defaultRow(),
      assistantRow(),
      row({ name: 'oncall', slug: 'oncall', has_dm_message: true, last_active_ts: 300 }),
    ])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-mate')
    expect(currentUrl()).toBe('/members?member=mate')
  })

  it('after Mate\'s welcome row exists, the next bare visit lands on the newest conversation', async () => {
    await renderPage([
      defaultRow(),
      assistantRow({ has_dm_message: true, welcome_claimed: true }),
      row({ name: 'oncall', slug: 'oncall', has_dm_message: true, last_active_ts: 300 }),
    ])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    expect(api.memberThread).not.toHaveBeenCalledWith('mate')
  })

  it('a claimed welcome marker on an empty Mate thread (a failed welcome) lands on the newest conversation', async () => {
    await renderPage([
      defaultRow(),
      assistantRow({ welcome_claimed: true }),
      row({ name: 'oncall', slug: 'oncall', has_dm_message: true, last_active_ts: 300 }),
    ])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    expect(currentUrl()).toBe('/members?member=oncall')
    expect(api.memberThread).not.toHaveBeenCalledWith('mate')
    expect(api.memberGreet).not.toHaveBeenCalled()
    // Mate stays on the roster: the user can still open it.
    fireEvent.click(await rosterRow('Mate'))
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-mate')
  })

  it('an empty Mate with no owed welcome and no marker lands on the ordinary landing', async () => {
    await renderPage([
      defaultRow(),
      assistantRow({ welcome_owed: false }),
      row({ name: 'oncall', slug: 'oncall', has_dm_message: true, last_active_ts: 300 }),
    ])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    expect(api.memberThread).not.toHaveBeenCalledWith('mate')
  })

  it('a roster that cannot say whether Mate was seen uses the ordinary landing', async () => {
    await renderPage([
      defaultRow(),
      assistantRow({ welcome_claimed: undefined }),
      row({ name: 'oncall', slug: 'oncall', has_dm_message: true, last_active_ts: 300 }),
    ])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    expect(api.memberThread).not.toHaveBeenCalledWith('mate')
  })

  it('once Mate has a conversation, a returning user keeps the last chat', async () => {
    localStorage.setItem(LAST_MEMBER_KEY, 'alpha')
    await renderPage([defaultRow(), assistantRow({ has_dm_message: true }), row({ name: 'alpha', slug: 'alpha' })])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-alpha')
    expect(api.memberThread).not.toHaveBeenCalledWith('mate')
  })

  it('with the Crewmates preview off, opening the page neither lands on Mate nor asks for its welcome', async () => {
    localStorage.setItem('mc-preview-crew', '0')
    localStorage.setItem(LAST_MEMBER_KEY, 'alpha')
    await renderPage([defaultRow(), assistantRow({ has_dm_message: false }), row({ name: 'alpha', slug: 'alpha' })])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-alpha')
    expect(api.memberThread).not.toHaveBeenCalledWith('mate')
    // Only Mate's open thread ever asks, and Mate was never opened.
    expect(api.memberGreet).not.toHaveBeenCalled()
  })

  it('an explicit member link is not hijacked by the Assistant', async () => {
    await renderPage([defaultRow(), assistantRow(), row({ name: 'alpha', slug: 'alpha' })], 'kirocrew', { route: '/members?member=alpha' })
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-alpha')
    expect(currentUrl()).toBe('/members?member=alpha')
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

  it('the OPEN crewmate rising to the top is the one recency change that re-sorts', async () => {
    // #15276: sorted by Recent, typing into a crewmate's DM leaves the row in
    // its alphabetical place. The user's own send is not a background event — it
    // is the thing in front of them — so it is the one advance the order hold
    // must not swallow. A pushed `member_projection` frame carries it, so this
    // happens with NO roster refetch.
    await renderPage([
      row({ name: 'alpha', slug: 'alpha', last_active_ts: 900 }),
      row({ name: 'beta', slug: 'beta', last_active_ts: 100 }),
      row({ name: 'gamma', slug: 'gamma', last_active_ts: 500 }),
    ])
    const names = () =>
      roster()
        .getAllByRole('listitem')
        .map((li) => within(li).queryByText(/^(alpha|beta|gamma)$/)?.textContent)
        .filter(Boolean)
    await waitFor(() => expect(names()).toEqual(['alpha', 'gamma', 'beta']))
    const callsBefore = (api.members as ReturnType<typeof vi.fn>).mock.calls.length

    // Open beta, the coldest row. Opening alone must NOT re-sort: the hold's
    // whole point is that rows do not move as the user works the list.
    fireEvent.click(await rosterRow('beta'))
    await waitFor(
      () => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-beta'),
      PANE_READY,
    )
    expect(names()).toEqual(['alpha', 'gamma', 'beta'])

    // A background crewmate advances QUIETLY first, while the user works the
    // list. This is the row the hold exists for: it must not move, and — the
    // part a whole re-sort gets wrong — it must still not move when the open
    // row's own advance arrives next. gamma's 9000 outranks everything, so a
    // re-sort of the whole list would put gamma first, not beta.
    act(() => {
      memberProjectionStore.apply(
        'gamma',
        'roster',
        { name: 'gamma', slug: 'gamma', last_active_ts: 9_000 },
        4,
      )
    })
    await waitFor(() => expect(names()).toEqual(['alpha', 'gamma', 'beta']))

    // Now beta's recency advances — the user sent. seq > the baseline seed's
    // asOfSeq (1), so higher-seq-wins takes it.
    act(() => {
      memberProjectionStore.apply(
        'beta',
        'roster',
        { name: 'beta', slug: 'beta', last_active_ts: 4_000 },
        5,
      )
    })
    // ONE row moved. beta is first because the user messaged it; alpha and gamma
    // keep the positions they held relative to each other, even though gamma now
    // carries the greatest recency of the three.
    await waitFor(() => expect(names()).toEqual(['beta', 'alpha', 'gamma']))
    expect((api.members as ReturnType<typeof vi.fn>).mock.calls.length).toBe(callsBefore)

    // A further background advance still moves nothing.
    act(() => {
      memberProjectionStore.apply(
        'alpha',
        'roster',
        { name: 'alpha', slug: 'alpha', last_active_ts: 99_000 },
        6,
      )
    })
    await waitFor(() => expect(names()).toEqual(['beta', 'alpha', 'gamma']))
  })

  it('under the by-name sort a recency advance moves nothing', async () => {
    // Recency is not what the name sort orders by, so the open crewmate's own
    // send says nothing about where its row belongs. Lifting it to the top here
    // would break the one ordering the user explicitly chose.
    localStorage.setItem('mc-members-sort', 'name')
    await renderPage([
      row({ name: 'alpha', slug: 'alpha', last_active_ts: 100 }),
      row({ name: 'beta', slug: 'beta', last_active_ts: 200 }),
      row({ name: 'gamma', slug: 'gamma', last_active_ts: 300 }),
    ])
    const names = () =>
      roster()
        .getAllByRole('listitem')
        .map((li) => within(li).queryByText(/^(alpha|beta|gamma)$/)?.textContent)
        .filter(Boolean)
    await waitFor(() => expect(names()).toEqual(['alpha', 'beta', 'gamma']))
    fireEvent.click(await rosterRow('gamma'))
    await waitFor(
      () => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-gamma'),
      PANE_READY,
    )
    act(() => {
      memberProjectionStore.apply(
        'gamma',
        'roster',
        { name: 'gamma', slug: 'gamma', last_active_ts: 9_000 },
        5,
      )
    })
    await waitFor(() => expect(names()).toEqual(['alpha', 'beta', 'gamma']))
  })

  it('restores the remembered member on return (and after a reload)', async () => {
    localStorage.setItem(LAST_MEMBER_KEY, 'beta')
    await renderPage(alphaBeta())
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-beta')
    expect(api.memberThread).toHaveBeenCalledTimes(1)
    expect(api.memberThread).toHaveBeenCalledWith('beta')
    expect(currentUrl()).toBe('/members?member=beta')
  })

  it('a remembered member that was deleted or renamed falls back to the most-recently-used one, without an error', async () => {
    localStorage.setItem(LAST_MEMBER_KEY, 'ghost')
    await renderPage(alphaBeta())
    // The remembered member is gone (and the URL named no one), so there is
    // nothing to restore — but the roster is NOT empty, so the fallback is
    // the most-recently-used crewmate (#11763: no first-row/sort priming, but
    // an empty roster is the only case with nothing to open). alpha and beta
    // tie at ts=0, so the tie keeps alpha (first in `ordered`). Nothing is
    // announced (nobody named was on the roster to say "gone").
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-alpha')
    // The Dashboard tab beside the pane mounts on its own; an alert it raised
    // would land after the pane. The tab holds the crewmate's dynamic
    // dashboard, so the sentinel is that tab and not the webview drawer's
    // published document, which this tab no longer renders.
    await screen.findByTestId('crew-dashboard-stub', undefined, PANE_READY)
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByTestId('member-gone-notice')).toBeNull()
    expect(currentUrl()).toBe('/members?member=alpha')
  })

  it('a URL naming a member wins over the remembered one (shallow link)', async () => {
    localStorage.setItem(LAST_MEMBER_KEY, 'alpha')
    await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=beta' })
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-beta')
    expect(api.memberThread).toHaveBeenCalledTimes(1)
    // Opening via the link also becomes the memory for the next visit.
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('beta')
  })

  it('a URL naming a gone member with NOTHING remembered falls back to the most-recently-used one and SAYS so', async () => {
    await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=ghost' })
    // The user asked for a specific member, but there is nothing remembered to
    // stand in for them — the roster is not empty though, so the fallback is
    // the most-recently-used crewmate (alpha and beta tie at ts=0; the tie
    // keeps alpha, the first in `ordered`), with a notice naming the swap.
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-alpha')
    expect(screen.getByTestId('member-gone-notice')).toHaveTextContent(/^Showing alpha/)
    expect(currentUrl()).toBe('/members?member=alpha')
    // The Dashboard tab beside the pane settles on its own dashboard read. The
    // tab holds the crewmate's dynamic dashboard, so the sentinel is that tab
    // and not the webview drawer's published document.
    await screen.findByTestId('crew-dashboard-stub', undefined, PANE_READY)
    expect(screen.queryByRole('alert')).toBeNull()
    // The stand-in open is the page's choice, not the user's: one stale link
    // must not overwrite the memory (`activate(hit, !standIn)`).
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBeNull()
    // Re-clicking the stand-in acknowledges the swap, retires the notice, and
    // IS the user's choice — now it is remembered.
    fireEvent.click(await rosterRow('alpha'))
    await waitFor(() => expect(screen.queryByTestId('member-gone-notice')).toBeNull())
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('alpha')
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('alpha')
  })

  it('a URL naming a gone member on an EMPTY roster returns to the roster and SAYS so', async () => {
    await renderPage([], 'kirocrew', { route: '/members?member=ghost' })
    // An empty roster has nothing to stand in for the gone name at all — the
    // one case that still lands on the roster (the New crewmate hero) rather
    // than a stand-in chat.
    const notice = await screen.findByTestId('member-gone-roster-notice')
    expect(notice).toHaveTextContent('“ghost” is no longer on the roster')
    expect(notice).toHaveAttribute('role', 'status')
    expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
    expect(api.memberThread).not.toHaveBeenCalled()
    await screen.findAllByText(/No crewmates yet/i)
    expect(screen.queryByRole('alert')).toBeNull()
    // The return to `/members` is a navigate() issued from an effect once the
    // roster has loaded; on a slow runner the notice can render a tick before
    // that effect has run, so wait for the URL like the other redirect tests do.
    await waitFor(() => expect(currentUrl()).toBe('/members'), PANE_READY)
    // Nothing was opened, so nothing is remembered.
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBeNull()
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
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-alpha'), PANE_READY)
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('alpha')
  })

  it('clicking a member writes the URL and the memory', async () => {
    await renderPage(alphaBeta())
    // A fresh visit with nothing remembered opens the MRU crewmate (#11763,
    // alpha and beta tie at ts=0, so alpha).
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-alpha')
    fireEvent.click(await rosterRow('beta'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-beta'), PANE_READY)
    expect(currentUrl()).toBe('/members?member=beta')
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('beta')
    // The row reflects the selection the URL drove.
    expect(roster().getByText('beta').closest('button')).toHaveAttribute('aria-current', 'true')
  })

  it('returning to a bare /members with nothing remembered re-opens the most-recently-used crewmate, not the one left active', async () => {
    // `safeSetItem` returns false when storage is denied (a locked-down
    // embedding context, blocked cookies), so a click never persists and the
    // later read is null. Denied for this one key so every other raw read in
    // the shared providers still works — the page's own two storage calls are
    // both on it. With memory permanently empty, an empty-roster ONLY case is
    // "nothing to open" — this roster is not empty, so the desktop fallback is
    // the most-recently-used crewmate (alpha and beta tie at ts=0; the tie
    // keeps alpha).
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
      // The URL names no one and there is nothing REMEMBERED to restore, but
      // the roster is not empty, so the MRU fallback (alpha) opens rather than
      // leaving beta's thread standing over a bare URL.
      await waitFor(() => expect(currentUrl()).toBe('/members?member=alpha'), PANE_READY)
      expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-alpha')
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
      await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-beta'), PANE_READY)
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
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-alpha')
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
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-staging'), PANE_READY)
    expect(currentUrl()).toBe('/members?member=staging')
    expect(screen.queryByTestId('member-gone-notice')).toBeNull()
  })

  it('the auto-open from a bare desktop URL replaces, and a follow-up switch replaces too: Back leaves the page in one press', async () => {
    // A fresh visit with nothing remembered no longer leaves the URL bare
    // (#11763): the MRU crewmate (alpha and beta tie at ts=0, so alpha) opens
    // on arrival. Above md that arrival is not a navigation step — the
    // roster and the thread sit side by side — so it REPLACES the bare
    // `/members` entry, or Back would need two presses to leave the page.
    // Below md the first tap is the two-level step and does push (its own
    // case in MembersPage.narrow.test.tsx's below-md block).
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
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-alpha')
    expect(currentUrl()).toBe('/members?member=alpha')
    fireEvent.click(await rosterRow('beta'))
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-beta')
    expect(currentUrl()).toBe('/members?member=beta')
    // One Back: off the page. A pushed open at either step would have left an
    // intermediate entry behind it, costing extra presses.
    fireEvent.click(screen.getByTestId('history-back'))
    await waitFor(() => expect(currentUrl()).toBe('/elsewhere'), PANE_READY)
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
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-beta'), PANE_READY)
    fireEvent.click(await rosterRow('alpha'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-alpha'), PANE_READY)
    expect(currentUrl()).toBe('/members?member=alpha')
    // One Back: off the page — the switches replaced, they did not stack.
    fireEvent.click(screen.getByTestId('history-back'))
    await waitFor(() => expect(currentUrl()).toBe('/elsewhere'), PANE_READY)
    expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
    // The memory still holds the last member the user chose.
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('alpha')
  })
})
