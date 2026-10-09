import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { PREVIEW_DASHBOARD, setPreviewFlag } from '../../utils/previewFlags'
import { useState } from 'react'
import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { namedCeiling } from '../../test/namedCeiling'
import { defaultScheduler, notifyManager } from '@tanstack/react-query'
import { Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { renderWithProviders } from '../../test/helpers'
import { NavigationLeaveGuardProvider, useMayLeaveForNavigation } from '../../components/NavigationLeaveGuard'
import { ApiError } from '../../api/apiError'
import { memberProjectionStore } from '../../state/memberProjectionStore'
import { markSlotUnread, sseConnected, sseDisconnected, sseSlots } from '../../store/dashboardSlice'
import { setSlotStatusDetail, sseChatMessage, sseToolActivity, sseToolResult, syncSlotRunningFromServer } from '../../store/chatSlice'
import { PILL_ACTIVITY_MAX_CHARS } from './pillActivity'
import { MEMBERS_ROSTER_QUERY_KEY, memberThreadQueryKey } from '../../api/membersQuery'
import { __resetPaneDraftsForTests, readPaneDraft, writePaneDraft } from '../../utils/chatPaneDrafts'
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
    // The roster's team grouping reads the team list; "no teams" keeps the
    // list flat, which is the shape every case here was written against.
    teams: { list: vi.fn(() => Promise.resolve({ teams: [] })), create: vi.fn(), update: vi.fn(), remove: vi.fn() },
    memberThread: vi.fn(),
    // Captain's first greeting: the server decides, so the page only asks.
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
    // The side panel's + menu gates its Summary row on this read; "disabled"
    // keeps the chat-style Summary row out of the menu, next to the Work log
    // chip it would be a second, unrelated summary of the same thread.
    sessionSummary: vi.fn(() => Promise.resolve({ enabled: false })),
    // The Dashboard tab's webview. Stubbed as "nothing published", which is the
    // state every case here is about: without it the reader rejects and the
    // tab raises a red alert, so a silent fallback (a remembered crew that
    // was renamed away) would read as an error on a page that is behaving.
    memberPanel: vi.fn(() => Promise.resolve({ panel: null, html: null })),
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
vi.mock('../../components/crew/CrewEditorDialog', () => ({
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
const chatPaneMounts = vi.hoisted(() => ({ count: 0 }))
vi.mock('../../components/ChatPane', async () => {
  const { useEffect } = await import('react')
  function ChatPaneStub({ slotKey, agentLocked, followContentWidth, busyMode, onOpenCommandCenter, ...rest }: { slotKey: string; agentLocked?: boolean; followContentWidth?: boolean; busyMode?: string; onOpenCommandCenter?: () => void } & Record<string, unknown>) {
    // Mount count: the New crewmate card must HIDE the pane, never unmount it.
    useEffect(() => { chatPaneMounts.count += 1 }, [])
    return (
      <div data-testid="chat-pane-stub" data-agent-locked={agentLocked ? '1' : '0'} data-follow-content-width={followContentWidth ? '1' : '0'} data-busy-mode={busyMode ?? 'split'}>
        {slotKey}
        {onOpenCommandCenter && <button onClick={onOpenCommandCenter}>Open task dashboard</button>}
        {/* A static opening card is gone: no host may pass one. */}
        {'assistantWelcome' in rest && <div data-testid="assistant-welcome-stub" />}
        {/* Stands in for rendered transcript links (markdown anchors). */}
        <a href="/members?create=1&name=scout&goal=Watch%20the%20release%20train" data-testid="chat-link-proposal">proposal</a>
        <a href="/members?create=1&goal=Second%20idea" data-testid="chat-link-proposal-2">second</a>
        <a href="http://[::1" data-testid="chat-link-malformed">bad</a>
        <a href="https://elsewhere.example/members?create=1&name=evil" data-testid="chat-link-foreign">foreign</a>
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
import NewCrewmateDialog, { CACHE_WARM_BOUND_MS, RECONCILE_BOUND_MS } from './NewCrewmateDialog'
import { seededTraits } from '../../components/CrewAvatar'
import MembersPage, { CREW_DASHBOARD_TAB_ID, CREW_PANEL_TAB_IDS, MEMBERS_UNCONFIRMED_WITHHELD_VIEWS, MEMBERS_UNFED_VIEWS, MEMBERS_WITHHELD_VIEWS, resolveDefaultMember } from './MembersPage'
import { resetMemberGreetingRequests } from './useMemberFirstGreeting'
import { __resetPanelTabs, VIEW_DATA_SOURCE } from '../../hooks/usePanelTabs'

/** The page's own memory key (mirrors the constant in MembersPage.tsx). */
const LAST_MEMBER_KEY = 'mc-members-last-member'
// Spelled out rather than imported: the value IS the contract with a returning
// browser, so a rename of the page's constant must fail here.
const PANEL_OPEN_KEY = 'mc-members-panel-open'

/** A window wide enough to dock the side panel BESIDE the thread (see
 *  panelSitsBeside): roster 264 + gaps 24 + shell reserve 560 + panel min 320
 *  = 1168. happy-dom's default is narrower, which would put every case in
 *  overlay mode with the panel closed. Narrow-window cases set their own. */
const WIDE_WINDOW = 1440
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

/** The built-in Assistant crewmate: its own member (`kirocrew-captain`, template
 *  `kirocrew-captain`), separate from the reserved `default` member. */
function assistantRow(overrides: Record<string, unknown> = {}) {
  return row({ name: 'kirocrew-captain', slug: 'kirocrew-captain', kiro_agent: 'kirocrew-captain', source: 'builtin', memory_store: 'default', ...overrides })
}
/** The reserved `default` member, as the backend lists it. */
function defaultRow(overrides: Record<string, unknown> = {}) {
  return row({ name: 'default', slug: 'default', ...overrides })
}

/** Echoes the requested slug back as the thread's member — the happy path for
 *  any roster, so an open (a click, or the restore of a remembered member)
 *  resolves cleanly for whichever member it names. Cases that need a collision
 *  or a failure pass `thread`. */
function echoThread(slug: string) {
  return Promise.resolve({ slot_key: 'member-' + slug, slug, member: slug, created: true })
}

/**
 * Ceiling for a wait on the chat pane, or on anything an open or a create's
 * follow-up produces (the pane, its viewed-thread registration, an open-failure
 * notice, the roster re-read, the first-greeting request, a team view's inbox cards
 * behind its thread POST and slot-detail read, the Dashboard tab's own
 * member-panel read). `renderPage`
 * returns once the roster fetch has been ISSUED; the pane sits behind a real
 * chain after that -- members resolve, the roster commits, the open (a
 * remembered restore, a ?member= URL, a click, or the crewmate a create hands
 * over) POSTs `memberThread`, that resolves, and the pane mounts. Under load (a
 * shared host, coverage instrumentation) that ran past the 1000ms default in one
 * of four full runs; a named ceiling, not a longer guess --
 * website/docs/testing.md. It goes in a findBy's THIRD argument: the second is
 * matcher options, and a timeout there is ignored.
 */
const PANE_READY = namedCeiling('PANE_READY', 5000)

/**
 * Put setTimeout/clearTimeout on a FAKE clock for a create whose read never
 * answers. NewCrewmateDialog races that read against a setTimeout of
 * CACHE_WARM_BOUND_MS (the warm-up) or RECONCILE_BOUND_MS (the reconcile), so a
 * case can stand one millisecond short of the bound and then on it. Install it
 * once the page has settled and before the submit that arms the bound. React
 * Query hands results to React on a setTimeout(0), which this clock would hold,
 * so those run as microtasks meanwhile. Until `realCreateClock`, step with
 * `stepCreateClock`, never waitFor/findBy: Testing Library's waits do not
 * advance vitest's fake clock.
 */
function fakeCreateClock() {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  notifyManager.setScheduler(queueMicrotask)
}
function realCreateClock() {
  vi.useRealTimers()
  notifyManager.setScheduler(defaultScheduler)
}
/** Run `action`, then move the fake clock on by `ms`, letting all it settles render. */
const stepCreateClock = (ms: number, action: () => void = () => {}) =>
  act(async () => { action(); await vi.advanceTimersByTimeAsync(ms) })

/** Renders the page at the URL and lets the roster load. `thread` replaces
 *  the thread-endpoint mock BEFORE mount: a remembered member (or a
 *  ?member= URL) opens a thread as soon as the roster is in, so a mock
 *  installed after render would miss that first POST. A fresh visit with
 *  nothing remembered opens no one (#11763). */
async function renderPage(
  members = [row()],
  defaultAgent = 'kirocrew',
  {
    route = '/members',
    thread,
    queryDefaults,
  }: { route?: string; thread?: Record<string, unknown> | Error; queryDefaults?: Record<string, unknown> } = {},
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
      <LeaveProbe />
      <GuideNavProbe />
    </NavigationLeaveGuardProvider>,
    { route, queryDefaults },
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

/** Stands in for a guide's `enter`: a navigation to the create link whose
 *  leave the guide's own `guardedLeave` already asked (`leaveGranted`), or,
 *  without it, one nothing asked. */
function GuideNavProbe() {
  const nav = useNavigate()
  const to = '/members?create=1&name=scout&goal=Watch%20the%20train'
  return (
    <>
      <button type="button" data-testid="guide-nav-granted" onClick={() => nav(to, { state: { leaveGranted: true } })}>guide nav (asked)</button>
      <button type="button" data-testid="guide-nav-plain" onClick={() => nav(to)}>guide nav (unasked)</button>
      <button type="button" data-testid="guide-nav-blank" onClick={() => nav('/members?create=1')}>guide nav (blank draft)</button>
    </>
  )
}

/** Stands in for an app-shell navigation surface (sidebar, palette, Back): asks
 *  the page's registered leave guard and records the answer. */
function LeaveProbe() {
  const mayLeave = useMayLeaveForNavigation()
  const [answer, setAnswer] = useState('')
  return (
    <button type="button" data-testid="leave-probe" onClick={() => setAnswer(String(mayLeave()))}>
      {answer}
    </button>
  )
}
const askToLeave = () => {
  fireEvent.click(screen.getByTestId('leave-probe'))
  return screen.getByTestId('leave-probe').textContent
}

/* The open member's name also renders in the thread header (and the panel's identity row),
 * so a bare screen query by name is ambiguous once a member is open (a click,
 * a remembered restore, or a ?member= URL). Scope name lookups to the roster
 * column. */
const roster = () => within(screen.getByTestId('member-roster'))
/* The roster rows are the members-resolve -> roster-commit leg of the chain
 * PANE_READY names, so they share its ceiling. */
const rosterRow = async (name: string) =>
  within(await screen.findByTestId('member-roster')).findByText(name, undefined, PANE_READY)

/* The roster header's "+" is a menu (New crewmate / New team). Radix opens
 * the dropdown on pointerdown (mouse), not click, so every case that wants
 * the crewmate item goes through here. Returns the item, which carries the
 * create hold (`aria-disabled` + the reason written under its label). */
const openAddMenu = async () => {
  fireEvent.pointerDown(screen.getByTestId('member-add'), { button: 0, ctrlKey: false, pointerType: 'mouse' })
  return await screen.findByTestId('member-add-crewmate')
}
/* Closes an open "+" menu (the trigger's pointerdown TOGGLES, so a second
 * open on an open menu would close it instead), and waits for it to go. */
const closeAddMenu = async () => {
  fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
  await waitFor(() => expect(screen.queryByTestId('member-add-menu')).toBeNull())
}
/* Selects New crewmate from the "+" menu: what one click on the old button
 * did. Radix closes the menu on select. */
const clickAddCrewmate = async () => {
  fireEvent.click(await openAddMenu())
}
/* Follows a create link with no name or goal (a guide's, a Captain
 * proposal's), which the page consumes and strips. */
const followBlankCreateLink = async () => {
  fireEvent.click(screen.getByTestId('guide-nav-blank'))
}
/* The "+" menu's New crewmate, then the card's own Advanced settings toggle:
 * the "+" menu holds no Advanced row of its own. */
const clickAddAdvanced = async () => {
  await clickAddCrewmate()
  const toggle = screen.queryByTestId('crewmate-create-advanced-toggle')
  if (toggle && toggle.getAttribute('aria-expanded') !== 'true') fireEvent.click(toggle)
}
/* Unfolds the card's Advanced settings unless the door already did. */
const unfoldAdvanced = () => {
  const toggle = screen.getByTestId('crewmate-create-advanced-toggle')
  if (toggle.getAttribute('aria-expanded') !== 'true') fireEvent.click(toggle)
}
/* Reads the New crewmate item's hold and closes the menu again: `held` is the
 * item's `aria-disabled` (a Radix item has no DOM `disabled` for
 * `toBeDisabled` to read), `reason` the text written under its label, `null`
 * while the item is live. */
const probeAddCrewmate = async () => {
  const item = await openAddMenu()
  const held = item.getAttribute('aria-disabled') === 'true'
  const reason = screen.queryByTestId('member-add-crewmate-held')?.textContent ?? null
  await closeAddMenu()
  return { held, reason }
}
/* Waits for the New crewmate item to reach `held` (the hold is released at
 * the follow-up's terminal points, so this is the cases' synchronisation
 * point), watching the one open menu rather than toggling it per attempt. */
const waitForAddCrewmate = async (held: boolean) => {
  const item = await openAddMenu()
  await waitFor(() => expect(item.getAttribute('aria-disabled') === 'true').toBe(held), PANE_READY)
  await closeAddMenu()
}

/** Put every api mock back to its factory default. A clear keeps each mock's
 *  persistent and queued-once implementations, so a rejection one case installs
 *  -- on the drawer's reads, the patrol registry, the editor roster -- would
 *  reach every later case, and a once-answer it never consumes would be served
 *  to the next case's first call. `members` and `memberThread` have no factory
 *  default, so after the reset they answer undefined until `renderPage`, or the
 *  case itself, seeds them. */
function resetApiMocks() {
  const owned = [api, api.teams] as unknown as Array<Record<string, unknown>>
  for (const group of owned) {
    for (const fn of Object.values(group)) {
      if (vi.isMockFunction(fn)) fn.mockReset()
    }
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  resetApiMocks()
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
  __resetPaneDraftsForTests()
  resetMemberGreetingRequests()
  // The remembered member must not leak between cases.
  localStorage.clear()
  // Nor the crewmate-created receipts (browser-session record).
  sessionStorage.clear()
  chatPaneMounts.count = 0
  // The Dashboard tab and the in-chat dock are a Feature Preview: these cases
  // exercise them, so the flag is on; the preview's own tests cover it off.
  localStorage.setItem(PREVIEW_DASHBOARD, '1')
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
    vi.mocked(api.defaultAgent).mockRejectedValue(new Error('default unavailable'))
    vi.mocked(api.teams.list).mockRejectedValue(new Error('teams unavailable'))

    await renderPage([row()], 'kirocrew', { route: '/members?member=oncall' })
    await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)

    const aside = screen.getByTestId('member-roster')
    expect(aside.className).toMatch(/\bhidden\b/)
    expect(aside.className).not.toMatch(/\bmd:flex\b/)
    const notices = within(await screen.findByTestId('member-main-roster-errors', undefined, PANE_READY))
    expect(notices.getByTestId('member-main-patrol-error')).toBeInTheDocument()
    expect(notices.getByTestId('member-main-default-agent-error')).toBeInTheDocument()
    expect(notices.getByTestId('member-main-teams-error')).toBeInTheDocument()
    // No hand-off from above the thread: Profile may hold an unsaved schedule draft.
    expect(notices.queryByRole('button', { name: /ask the agent/i })).toBeNull()
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
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-kirocrew-captain')
    expect(api.memberThread).toHaveBeenCalledWith('kirocrew-captain')
    expect(api.memberThread).not.toHaveBeenCalledWith('default')
    expect(screen.queryByTestId('crewmate-empty-hero')).toBeNull()
    // The Assistant is a crewmate: the "+" menu is the create door here.
    expect(screen.getByTestId('member-add')).toBeInTheDocument()
    // No static welcome card: Captain greets with a real first turn, asked for
    // once the confirmed thread is open.
    await waitFor(() => expect(api.memberGreet).toHaveBeenCalledWith('kirocrew-captain'))
    expect(api.memberGreet).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('assistant-welcome-stub')).toBeNull()
    expect(currentUrl()).toBe('/members?member=kirocrew-captain')
    // `default` keeps its ordinary row: its own name, not the Assistant's.
    expect(await rosterRow('default')).toBeInTheDocument()
  })

  it('a failed first greeting is said in the Captain column, without a hand-off, and Retry asks again', async () => {
    ;(api.memberGreet as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('member_greet_failed'))
    await renderPage([defaultRow({ last_active_ts: 999 }), assistantRow()])
    const notice = await screen.findByTestId('member-greeting-error')
    expect(notice).toHaveTextContent('The first greeting could not be started')
    // No hand-off: the Captain composer below may already hold a draft.
    expect(within(screen.getByTestId('member-greeting-error-row')).queryByRole('button', { name: /ask/i })).toBeNull()
    // The chat itself stays mounted and usable.
    expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-kirocrew-captain')
    fireEvent.click(screen.getByTestId('member-greeting-retry'))
    await waitFor(() => expect(api.memberGreet).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByTestId('member-greeting-error')).toBeNull())
  })

  it('a member merely NAMED assistant on another template is an ordinary crewmate: no greeting, no landing', async () => {
    await renderPage([defaultRow(), row({ name: 'kirocrew-captain', slug: 'kirocrew-captain', kiro_agent: 'kirocrew' }), row({ name: 'alpha', slug: 'alpha', last_active_ts: 50 })])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-alpha')
    fireEvent.click(await rosterRow('kirocrew-captain'))
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-kirocrew-captain')
    expect(screen.queryByTestId('assistant-welcome-stub')).toBeNull()
    expect(api.memberGreet).not.toHaveBeenCalled()
  })

  it('the default member, opened explicitly, asks for no Captain greeting', async () => {
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
    // A send while the member is working gets the main chat's Steer / Queue /
    // Jev auto split, so the Members page must NOT ask for 'steer-only'.
    expect(pane).toHaveAttribute('data-busy-mode', 'split')
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
    expect(screen.getByTestId('crewmate-switcher-count')).toHaveTextContent('1')
  })

  it('the Assistant lands with its docked panel hidden, remembered apart from every other crewmate', async () => {
    localStorage.removeItem('mc-members-assistant-panel-open')
    const rows = [defaultRow(), assistantRow(), row({ name: 'oncall', slug: 'oncall', bound: true, slot_key: 'member-oncall' })]
    const utils = await renderPage(rows)
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-kirocrew-captain')
    expect(screen.queryByTestId('member-dashboard')).toBeNull()
    // Hidden, not removed: the header opener brings it back, and it is remembered.
    fireEvent.click(screen.getByTestId('member-panel-toggle'))
    expect(await screen.findByTestId('member-dashboard')).toBeInTheDocument()
    expect(localStorage.getItem('mc-members-assistant-panel-open')).toBe('1')
    utils.unmount()
    // Another crewmate opens with the panel shown: the Assistant's choice is its own.
    localStorage.setItem('mc-members-assistant-panel-open', '0')
    await renderPage(rows, 'kirocrew', { route: '/members?member=oncall' })
    expect(await screen.findByTestId('member-dashboard')).toBeInTheDocument()
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
    // Reopened, the action now offers to fold it again.
    fireEvent.click(screen.getByTestId('crewmate-switcher'))
    expect(await screen.findByTestId('crewmate-switcher-roster')).toHaveTextContent('Hide the roster')
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('crewmate-switcher-list')).toBeNull())

    // New team is on the pinned roster's "+" menu.
    fireEvent.pointerDown(screen.getByTestId('member-add'), { button: 0, ctrlKey: false, pointerType: 'mouse' })
    expect(await screen.findByTestId('member-add-team')).toBeInTheDocument()
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
    fireEvent.click(screen.getByTestId('member-roster-hide'))
    await waitFor(() => expect(classes()).not.toContain('md:flex'))
    expect(classes()).toContain('hidden')
    expect(screen.queryByTestId('member-roster-hide')).toBeNull()
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

  it('the Dashboard tab stays across the preview flag and the chat has no dock opener', async () => {
    act(() => { setPreviewFlag(PREVIEW_DASHBOARD, false) })
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    // The tab is the crewmate's own published page, not the preview's surface.
    expect(await screen.findByTestId(`side-panel-leading-tab-${CREW_DASHBOARD_TAB_ID}`)).toHaveTextContent('Dashboard')
    expect(await screen.findByTestId('member-dashboard')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Open task dashboard' })).toBeNull()
    act(() => { setPreviewFlag(PREVIEW_DASHBOARD, true) })
    expect(screen.queryByRole('button', { name: 'Open task dashboard' })).toBeNull()
  })

  it('the Dashboard is the crewmate\'s generated page, with no repeated identity row', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    expect(await screen.findByTestId('member-dashboard')).toBeInTheDocument()
    await waitFor(() => expect(api.memberPanel).toHaveBeenCalledWith('oncall', 'oncall'), PANE_READY)
    expect(await screen.findByTestId('crew-webview-empty', undefined, PANE_READY)).toBeInTheDocument()
    expect(screen.queryByTestId('member-identity-row')).toBeNull()
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
  })

  it('switching crewmates drops an open Profile instead of reopening it over the next one on the old tab', async () => {
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
    fireEvent.click(screen.getByRole('tab', { name: 'Sessions' }))
    expect(screen.getByTestId('crew-profile-panel')).toHaveAttribute('data-tab', 'sessions')

    fireEvent.click(screen.getByTestId('crewmate-switcher'))
    const rows = await screen.findAllByTestId('crewmate-switcher-row')
    fireEvent.click(rows.find((r) => r.textContent?.includes('research'))!)
    await waitFor(() => expect(screen.getByTestId('member-identity-pill')).toHaveTextContent('research'))
    await waitFor(() => expect(screen.queryByTestId('crew-profile-docked')).toBeNull())
    expect(screen.queryByTestId('crew-profile-panel')).toBeNull()
    expect(screen.getByTestId('member-identity-pill')).toHaveAttribute('aria-expanded', 'false')
    // Opening it again starts on Profile, not where the last crewmate's card was left.
    fireEvent.click(screen.getByTestId('member-identity-pill'))
    expect(await screen.findByTestId('crew-profile-panel')).toHaveAttribute('data-tab', 'profile')
  })

  it('a window that narrows below md while Profile holds its column re-places the card as floating, keeping it open', async () => {
    // Placement is fixed at open time, so without this a phone-width viewport
    // kept a fixed-width in-flow aside beside the thread.
    localStorage.setItem(PANEL_OPEN_KEY, '0')
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    fireEvent.click(await screen.findByTestId('member-identity-pill'))
    await screen.findByTestId('crew-profile-docked')
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
      await waitFor(() => expect(screen.queryByTestId('crew-profile-docked')).toBeNull())
      expect(screen.getByTestId('crew-profile-modal')).toBeInTheDocument()
      expect(screen.getByTestId('crew-profile-panel')).toBeInTheDocument()
      expect(screen.getByTestId('member-identity-pill')).toHaveAttribute('aria-expanded', 'true')
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

  it('the identity pill withholds an ID that differs from the label only in case, keeping it in the tooltip', async () => {
    await renderPage([
      row({ name: 'kiro', slug: 'kiro', display_name: 'Kiro', bound: true, slot_key: 'member-kiro' }),
      row({ name: 'oncall', slug: 'oncall', display_name: 'Oncall Sentinel', bound: true, slot_key: 'member-oncall' }),
    ])
    fireEvent.click(await rosterRow('Kiro'))
    const pill = await screen.findByTestId('member-identity-pill')
    expect(within(pill).getByTestId('member-pill-name')).toHaveTextContent('Kiro')
    // "Kiro kiro" said the same thing twice; the ID still rides the name's title.
    expect(within(pill).queryByTestId('member-pill-id')).toBeNull()
    expect(within(pill).getByTestId('member-pill-name')).toHaveAttribute('title', expect.stringContaining('kiro'))
    // A label that COVERS the ID keeps the ID line: routes and crons address it.
    fireEvent.click(await rosterRow('Oncall Sentinel'))
    await waitFor(() => expect(screen.getByTestId('member-identity-pill')).toHaveTextContent('Oncall Sentinel'))
    expect(within(screen.getByTestId('member-identity-pill')).getByTestId('member-pill-id')).toHaveTextContent('oncall')
    expect(within(screen.getByTestId('member-identity-pill')).getByTestId('member-pill-name')).not.toHaveAttribute('title')
  })

  it('the profile pencil opens the existing editor for the exact crewmate', async () => {
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
    // The Profile's Workspace tile still names the workspace, as a name.
    fireEvent.click(await screen.findByTestId('member-identity-pill'))
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

  it('the "+" menu offers New crewmate and New team only: no Advanced or guided-flow rows', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    await rosterRow('oncall')
    await openAddMenu()
    const menu = await screen.findByTestId('member-add-menu')
    expect(within(menu).getAllByRole('menuitem').map(el => el.textContent?.trim())).toEqual(['New crewmate', 'New team'])
  })

  it('the roster header "+" menu\'s New crewmate opens the card in place with Advanced settings folded, and they unfold inline — no trip to the crew manager', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    await rosterRow('oncall')
    // Creating a crewmate IS creating a crew record, and the write path is
    // still `POST /api/agents`; what changed is the front door. The primary
    // row opens the New crewmate card inside the page with Advanced settings
    // folded; they unfold inline. Nothing navigates.
    expect(screen.getByTestId('member-add')).toHaveAttribute('aria-label', 'Add…')
    await clickAddCrewmate()
    expect(await screen.findByTestId('crewmate-create-look')).toBeInTheDocument()
    expect(screen.getByTestId('member-create-pane')).toContainElement(screen.getByTestId('crewmate-create-look'))
    expect(screen.queryByRole('combobox', { name: 'Built from' })).toBeNull()
    expect(screen.getByTestId('crewmate-create-advanced-toggle')).toHaveAttribute('aria-expanded', 'false')
    // Advanced settings unfolds inline, in the same card.
    fireEvent.click(screen.getByTestId('crewmate-create-advanced-toggle'))
    expect(await screen.findByRole('combobox', { name: 'Built from' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await clickAddAdvanced()
    expect(await screen.findByRole('combobox', { name: 'Built from' })).toBeInTheDocument()
    expect(screen.getByTestId('crewmate-create-advanced-toggle')).toHaveAttribute('aria-expanded', 'true')
    // In place, not a modal: a labelled page region where the chat was.
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByRole('region', { name: 'New crewmate' })).toBe(screen.getByTestId('crewmate-create-embedded'))
    expect(screen.getByTestId('member-create-pane')).toContainElement(screen.getByTestId('crewmate-create-embedded'))
    expect(navigateSpy).not.toHaveBeenCalledWith(expect.stringContaining('/capabilities'))
  })

  it('the "+" menu offers New team, which opens the team dialog with every crewmate listed', async () => {
    await renderPage([row({ name: 'oncall', slug: 'oncall' }), row({ name: 'docs', slug: 'docs' })])
    await rosterRow('oncall')
    // Radix opens the dropdown on pointerdown (mouse), not click.
    fireEvent.pointerDown(screen.getByTestId('member-add'), { button: 0, ctrlKey: false, pointerType: 'mouse' })
    fireEvent.click(await screen.findByTestId('member-add-team'))
    const body = await screen.findByTestId('team-dialog-body')
    expect(within(body).getAllByTestId('team-dialog-row')).toHaveLength(2)
    // Nothing on a team yet: every row says so, and the hint names the rule.
    expect(within(body).getAllByText('No team')).toHaveLength(2)
    expect(within(body).getByText(/on one team at a time/i)).toBeInTheDocument()
    // Create is gated on a name.
    expect(screen.getByTestId('team-dialog-save')).toBeDisabled()
    fireEvent.change(screen.getByTestId('team-dialog-name'), { target: { value: 'Triage' } })
    expect(screen.getByTestId('team-dialog-save')).toBeEnabled()
  })

  it('creates a team from the dialog form and writes the returned team into the roster', async () => {
    const created = { id: 'abc123abc123', name: 'Triage', members: ['oncall'] }
    vi.mocked(api.teams.create).mockResolvedValue({ team: created })
    vi.mocked(api.teams.list).mockResolvedValueOnce({ teams: [] }).mockResolvedValue({ teams: [created] })
    await renderPage([row({ name: 'oncall', slug: 'oncall' })])
    await rosterRow('oncall')
    fireEvent.pointerDown(screen.getByTestId('member-add'), { button: 0, ctrlKey: false, pointerType: 'mouse' })
    fireEvent.click(await screen.findByTestId('member-add-team'))
    const body = await screen.findByTestId('team-dialog-body')
    fireEvent.change(screen.getByTestId('team-dialog-name'), { target: { value: 'Triage' } })
    fireEvent.click(screen.getByLabelText('oncall'))
    fireEvent.submit(body)
    await waitFor(() => expect(api.teams.create).toHaveBeenCalledWith({ name: 'Triage', members: ['oncall'] }))
    await waitFor(() => expect(screen.queryByTestId('team-dialog-body')).toBeNull())
    expect((await screen.findAllByTestId('team-group-header'))[0]).toHaveTextContent('Triage')
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
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall'))
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
    await screen.findByTestId('member-roster-teams-error')
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
    await screen.findByTestId('member-roster-teams-error')
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
    const paneNotice = await screen.findByTestId('team-view-teams-error')
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
    const paneNotice = await screen.findByTestId('team-view-teams-error')
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

  it('a team header says it opens the team, and the New team dialog guards an unsaved draft against Escape', async () => {
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
    // The New team dialog: nothing typed -> Escape closes; a typed name -> Escape is ignored.
    // Radix opens the dropdown on pointerdown (mouse), not click.
    fireEvent.pointerDown(screen.getByTestId('member-add'), { button: 0, ctrlKey: false, pointerType: 'mouse' })
    fireEvent.click(await screen.findByTestId('member-add-team'))
    await screen.findByTestId('team-dialog-body')
    // A crewmate already on a team reads "On Triage", not a bare team name.
    expect(screen.getAllByTestId('team-dialog-row').map((r) => r.textContent)).toEqual(
      expect.arrayContaining([expect.stringContaining('On Triage')]),
    )
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('team-dialog-body')).toBeNull())
    // Radix opens the dropdown on pointerdown (mouse), not click.
    fireEvent.pointerDown(screen.getByTestId('member-add'), { button: 0, ctrlKey: false, pointerType: 'mouse' })
    fireEvent.click(await screen.findByTestId('member-add-team'))
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
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
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
    await waitFor(() => expect(getViewedThreadSlot()).toBe('member-oncall'), PANE_READY)
    fireEvent.click(await rosterRow('scout'))
    await waitFor(() => expect(getViewedThreadSlot()).toBe('member-scout'), PANE_READY)
  })

  it('retires the registration while the window is hidden, and restores it on reveal', async () => {
    await renderPage()
    fireEvent.click(await rosterRow('oncall'))
    await waitFor(() => expect(getViewedThreadSlot()).toBe('member-oncall'), PANE_READY)
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
    try {
      act(() => { document.dispatchEvent(new Event('visibilitychange')) })
      await waitFor(() => expect(getViewedThreadSlot()).toBeNull())
      hidden.mockReturnValue(false)
      act(() => { document.dispatchEvent(new Event('visibilitychange')) })
      await waitFor(() => expect(getViewedThreadSlot()).toBe('member-oncall'))
    } finally {
      // A hidden window left behind would hold every later case off its thread.
      hidden.mockRestore()
    }
  })

  it('flushes the pending trailing read synchronously when the registration retires', async () => {
    const { unmount } = await renderPage()
    fireEvent.click(await rosterRow('oncall'))
    await waitFor(() => expect(getViewedThreadSlot()).toBe('member-oncall'), PANE_READY)
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
    await waitFor(() => expect(getViewedThreadSlot()).toBe('member-oncall'), PANE_READY)
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

  it('the roster unread dot reads the status token, not brand accent', async () => {
    // #10479: an element that conveys STATE reads a semantic status token, so a
    // theme can keep the cue distinct from brand chrome. `var(--accent)` is the
    // hue of links, chips and the send button, so on a theme whose accent is its
    // status colour the "your turn" cue disappears into ordinary chrome. The
    // sidebar's own dot was rebound in #10488 (ChatSidebar.tsx:2197, :6550); this
    // pins the roster row to the same token so the two cannot drift again.
    localStorage.setItem(LAST_MEMBER_KEY, 'scout')
    const { store } = await renderPage([
      row({ bound: true, slot_key: 'member-oncall' }),
      row({ name: 'scout', slug: 'scout' }),
    ])
    await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
    act(() => {
      store.dispatch(markSlotUnread('member-oncall'))
    })
    // `style.background`, not `toHaveStyle`: the latter compares COMPUTED
    // values, and the test DOM resolves an unregistered custom property to the
    // empty string, so it would fail on both tokens alike. This is the spelling
    // ChatSidebar.statusMarker.test.tsx already uses for the sibling dot.
    const dot = await screen.findByTestId('member-unread-dot')
    expect(dot.style.background).toBe('var(--ok)')
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
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall'), PANE_READY)
    await waitFor(() => expect(screen.queryByTestId('member-unread-dot')).toBeNull())
  })
})

describe('MembersPage identity pill and Profile entry', () => {
  beforeEach(() => { localStorage.clear() })

  it('the identity pill opens the Profile card, not the editor', async () => {
    localStorage.setItem(PANEL_OPEN_KEY, '0')
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    const pill = await screen.findByTestId('member-identity-pill')
    expect(pill.tagName).toBe('BUTTON')
    expect(pill.className).toContain('liquid-glass')
    expect(pill).toHaveAccessibleName(expect.stringContaining('oncall'))
    expect(pill).toHaveAccessibleDescription('Profile')

    fireEvent.click(pill)
    expect(await screen.findByTestId('crew-profile-docked')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-editor-dialog-open')).toBeNull()
    expect(screen.getByTestId('crew-profile-name')).toHaveTextContent('oncall')
  })

  it('the pill\'s second line says what the crewmate is doing: text only, always present, out of the button\'s name', async () => {
    const { store } = await renderPage([row({ bound: true, slot_key: 'member-oncall', last_active_ts: Math.floor(Date.now() / 1000) - 180 })])
    act(() => {
      store.dispatch(sseSlots([{ key: 'member-oncall', mode: 'member', running: false, messages: 0 }] as never))
    })
    fireEvent.click(await rosterRow('oncall'))
    const pill = await screen.findByTestId('member-identity-pill')
    const line = screen.getByTestId('member-pill-activity')
    // Inside the pill, under the title row — the title row is not widened
    // sideways for it.
    expect(pill).toContainElement(line)
    expect(screen.getByTestId('member-title-row')).not.toContainElement(line)
    // Resting: the line is still there (one pill height in every state) and
    // says how long ago the thread last moved. No dot, no glyph: text only.
    expect(line).toHaveAttribute('data-activity', 'idle')
    expect(line.textContent).toMatch(/^Idle · /)
    expect(line.querySelector('svg, span')).toBeNull()
    // The button is still named by the crewmate alone — a line that changes
    // several times a turn is not part of WHO the thread is with. The same
    // text is in the reading order OUTSIDE the button for assistive tech, and
    // is not a live region.
    expect(line).toHaveAttribute('aria-hidden', 'true')
    expect(pill).toHaveAccessibleName(expect.not.stringContaining('Idle'))
    const sr = screen.getByTestId('member-pill-activity-sr')
    expect(pill).not.toContainElement(sr)
    expect(sr.className).toContain('sr-only')
    expect(sr).not.toHaveAttribute('aria-live')
    expect(sr.textContent).toBe(line.textContent)

    // A tool call: the line is the SHARED status label for that slot — the
    // same record the sessions sidebar renders — which in simplified mode is
    // the call's own purpose, verbatim when short.
    act(() => {
      store.dispatch(sseSlots([{ key: 'member-oncall', mode: 'member', running: true, messages: 1 }] as never))
      store.dispatch(sseToolActivity({ slot: 'member-oncall', tool: 'shell', kind: 'shell', purpose: 'Look up the glass pill code', input_preview: 'grep -n Glass', tool_call_id: 't1' }))
      store.dispatch(setSlotStatusDetail({ slot: 'member-oncall', kind: 'tool', purpose: 'Look up the glass pill code', toolName: 'grep -n Glass', toolCallId: 't1', ts: Date.now() }))
      store.dispatch(sseChatMessage({ slot: 'member-oncall', role: 'tool', content: '🔧 shell', meta: { tool_call_id: 't1' } }))
    })
    expect(line).toHaveAttribute('data-activity', 'tool')
    expect(line.textContent).toBe('Look up the glass pill code')
    expect(sr.textContent).toBe('Look up the glass pill code')

    // Once the call returns (matched by ITS id; an empty output still counts)
    // the model is reading it: thinking, not the stale purpose.
    act(() => {
      store.dispatch(sseToolResult({ slot: 'member-oncall', output: '', tool_call_id: 't1' }))
    })
    expect(line).toHaveAttribute('data-activity', 'thinking')
    expect(line.textContent).toBe('Thinking…')

    // A long purpose is cut at the cap with one ellipsis, so the centred pill
    // never grows to the header's width. At most the cap: a cut landing on a
    // space drops the space too.
    const long = 'Rebuild the whole site and then take a screenshot of every page in both themes'
    expect(Array.from(long).length).toBeGreaterThan(PILL_ACTIVITY_MAX_CHARS)
    act(() => {
      store.dispatch(setSlotStatusDetail({ slot: 'member-oncall', kind: 'tool', purpose: long, toolName: 'npm run build', toolCallId: 't2', ts: Date.now() }))
    })
    expect(line).toHaveAttribute('data-activity', 'tool')
    expect(Array.from(line.textContent ?? '').length).toBeLessThanOrEqual(PILL_ACTIVITY_MAX_CHARS)
    expect(line.textContent?.endsWith('…')).toBe(true)
    expect(line.textContent?.startsWith('Rebuild the whole site')).toBe(true)

    // Visible output streaming: the pill's own word, not the seam's
    // "Streaming" (transport jargon on a line that says what the crewmate
    // is doing).
    act(() => {
      store.dispatch(setSlotStatusDetail({ slot: 'member-oncall', kind: 'streaming', ts: Date.now() }))
    })
    expect(line).toHaveAttribute('data-activity', 'writing')
    expect(line.textContent).toBe('Writing…')

    // The turn ends — status idle, run state settled (as `chat_done` does),
    // slots frame no longer running: back to idle, whatever the last status
    // said.
    act(() => {
      store.dispatch(setSlotStatusDetail({ slot: 'member-oncall', kind: 'idle', ts: Date.now() }))
      store.dispatch(syncSlotRunningFromServer({ slot: 'member-oncall', running: false, stopping: false }))
      store.dispatch(sseSlots([{ key: 'member-oncall', mode: 'member', running: false, messages: 2 }] as never))
    })
    expect(line).toHaveAttribute('data-activity', 'idle')
  })

  it('keeps editing behind the Profile pencil, not the identity pill', async () => {
    localStorage.setItem(PANEL_OPEN_KEY, '0')
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    const header = await screen.findByTestId('member-thread-header')
    expect(within(header).queryByLabelText('Edit crewmate')).toBeNull()

    fireEvent.click(screen.getByTestId('member-identity-pill'))
    fireEvent.click(await screen.findByTestId('crew-profile-edit'))
    expect(await screen.findByTestId('crew-editor-dialog-open')).toHaveTextContent('oncall')
  })

  it('the chat surface\'s avatar is just an avatar: no scrim, no badge, no chip, no text "Edit avatar" button', async () => {
    // The #9116 shapes the user rejected: the face wrapped as an "Edit avatar"
    // button, a full-width "Edit avatar" text button in the summary and an
    // "Edit this avatar" chip beside the header face. The default-face
    // fixture (`{}`) is exactly the one that used to summon the chip. The face
    // now sits inside the identity pill, whose label names the EDITOR — it is
    // still not an avatar control of its own.
    await renderPage([row({ bound: true, slot_key: 'member-oncall', avatar: {} })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('member-identity-pill')
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

  it('opens the editor for the exact crew name, special characters and all', async () => {
    await renderPage([row({ name: 'on call/2', slug: 'on-call-2', bound: true, slot_key: 'member-on-call-2' })])
    fireEvent.click(await rosterRow('on call/2'))
    fireEvent.click(await screen.findByTestId('member-identity-pill'))
    fireEvent.click(await screen.findByTestId('crew-profile-edit'))
    // In-place editor, pointed at the crew's exact NAME (identity), no route
    // change and no URL-encoding round trip to get wrong.
    expect(await screen.findByTestId('crew-editor-dialog-open')).toHaveTextContent('on call/2')
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it('a failed editor roster read surfaces a notice instead of a silent dead click (F1)', async () => {
    // The editor's roster read is gated on the pill click; if GET
    // /api/kirocrew/agents rejects, the pill would otherwise do nothing forever
    // with no report. The notice near the header is what makes the failure
    // visible (Opus/F1 blocking finding).
    vi.mocked(api.kirocrewAgents).mockRejectedValue(new Error('roster boom'))
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    // Before the click the notice is absent — the read has not fired yet.
    expect(screen.queryByTestId('member-crew-roster-load-error')).toBeNull()
    fireEvent.click(await screen.findByTestId('member-identity-pill'))
    fireEvent.click(await screen.findByTestId('crew-profile-edit'))
    const notice = await screen.findByTestId('member-crew-roster-load-error')
    expect(notice).toBeInTheDocument()
    // Opus BLOCKING fix: this notice must NOT offer the "Ask the agent"
    // hand-off. It renders only when the roster read failed, so the editor is
    // unopened and its dirtyPanes is always empty here — a `dirtyPanes.size===0`
    // guard would be inert and leave the hand-off unconditionally on, silently
    // discarding the ChatPane DM draft and the Schedules create draft via the
    // raw /chat navigate that skips the leave guard.
    expect(within(notice).queryByText('Ask the agent')).toBeNull()
    expect(navigateSpy).not.toHaveBeenCalled()
    // And it is dismissable (clearing editingCrew), so it does not follow the
    // user onto other crewmates' threads nor pop the editor open later.
    fireEvent.click(within(notice).getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByTestId('member-crew-roster-load-error')).toBeNull()
  })
})

describe('New crewmate dialog', () => {
  const openDialog = async () => {
    await clickAddAdvanced()
    return await screen.findByTestId('crewmate-create-form')
  }

  it('Create is the form\'s submit button, nested as its first submit control, so Enter in a field submits', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    // Read after the dialog settled: the modal's opening render can replace
    // the form element the open helper first found.
    const form = screen.getByTestId('crewmate-create-form')
    const create = screen.getByTestId('crewmate-create-submit')
    // Associated by nesting, and the form's first submit button: implicit
    // submission (Enter in a field) goes through it.
    expect(create.closest('form')).toHaveAttribute('data-testid', 'crewmate-create-form')
    expect(form.querySelector('[type="submit"]')).toBe(create)
    expect(create).toHaveAttribute('type', 'submit')
    expect(create).not.toHaveAttribute('form')
    expect((create as HTMLButtonElement).form?.id).toBe(form.id)
    // The form's own submit event (what Enter in the Name field raises once the
    // form has a submit button) posts the create.
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    membersMock.mockResolvedValue({ members: [row(), row({ name: 'radar', slug: 'radar', dashboard_created: true })], default_agent: 'kirocrew' })
    fireEvent.submit(form)
    await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledWith(expect.objectContaining({ name: 'radar' })))
    expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1)
  })

  it('a successful create invalidates the crew registry and config caches, as the crew manager\'s own form does, so the pencil deep link finds the new crewmate', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    const { queryClient } = await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    // Warm the caches the crew manager reads; at staleTime Infinity nothing
    // but an explicit invalidation would ever refetch them.
    queryClient.setQueryData(['kirocrew-agents'], { agents: [], default_agent: 'kirocrew' })
    queryClient.setQueryData(['kirocrewConfig'], { agents: {} })
    const registryReadsBefore = (api.agentCatalog as ReturnType<typeof vi.fn>).mock.calls.length
    const configReadsBefore = (api.kirocrewConfig as ReturnType<typeof vi.fn>).mock.calls.length
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    membersMock.mockResolvedValue({ members: [row(), row({ name: 'radar', slug: 'radar', dashboard_created: true })], default_agent: 'kirocrew' })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(screen.queryByTestId('crewmate-create-form')).toBeNull(), PANE_READY)
    await waitFor(() => {
      expect((api.agentCatalog as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(registryReadsBefore)
      expect((api.kirocrewConfig as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(configReadsBefore)
    })
  })

  it('a successful create marks the shared sessionless agents-catalog cache stale, so a later reader (the command bar\'s crewmates view) lists the new crewmate without a reload and without a fetch per keystroke', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    const { queryClient } = await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    // A reader of the shared key holds its answer across a stale window (the
    // command bar serves from it at MATES_STALE_MS; nothing else invalidates
    // it). Seed it fresh, so only an explicit invalidation on create can turn
    // it stale.
    queryClient.setQueryData(['agents-catalog', 'global'], { agents: [], default_agent: 'kirocrew' })
    expect(queryClient.getQueryState(['agents-catalog', 'global'])?.isInvalidated).toBe(false)
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    membersMock.mockResolvedValue({ members: [row(), row({ name: 'radar', slug: 'radar', dashboard_created: true })], default_agent: 'kirocrew' })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(screen.queryByTestId('crewmate-create-form')).toBeNull(), PANE_READY)
    // Stale now: the next reader's first fetch refetches, so the crewmate is
    // listed. The command-bar view pays that once-per-entry fetch and then
    // filters a cached roster per keystroke (CommandBarOverlay.mates.test.tsx:
    // "pays for exactly one fetch"), so freshness does not buy a request per
    // keystroke.
    await waitFor(() =>
      expect(queryClient.getQueryState(['agents-catalog', 'global'])?.isInvalidated).toBe(true),
    )
  })

  it('a cache warm-up that never answers does not hold the dialog on Creating…: the create hands over at its bound', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    const { queryClient } = await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    queryClient.setQueryData(['kirocrew-agents'], { agents: [], default_agent: 'kirocrew' })
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    membersMock.mockResolvedValue({ members: [row(), row({ name: 'radar', slug: 'radar', dashboard_created: true })], default_agent: 'kirocrew' })
    // A registry read that never returns (stalled socket, 429 ladder). Stalled
    // at the api: the page's own observers of ['kirocrew-agents'] carry their
    // queryFn, so the warm-up refetch calls this, not a query default.
    vi.mocked(api.kirocrewAgents).mockImplementation(() => new Promise(() => {}))
    // The bound runs from the POST's answer, so the case answers it itself.
    let answer: (v: { ok: boolean }) => void = () => {}
    vi.mocked(api.createKirocrewAgent).mockReturnValueOnce(new Promise((resolve) => { answer = resolve }))
    fakeCreateClock()
    await stepCreateClock(0, () => fireEvent.click(screen.getByTestId('crewmate-create-submit')))
    expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1)
    await stepCreateClock(0, () => answer({ ok: true }))
    // The POST resolved and the warm-up stalls: one millisecond short of the
    // bound the dialog still holds "Creating…".
    await stepCreateClock(CACHE_WARM_BOUND_MS - 1)
    expect(screen.getByTestId('crewmate-create-submit')).toHaveTextContent('Creating…')
    expect(api.memberThread).not.toHaveBeenCalledWith('radar')
    // On the bound the stalled warm-up is abandoned and the create hands over.
    await stepCreateClock(1)
    expect(screen.queryByText('Creating…')).toBeNull()
    // Only the bound can have ended it: the warm-up is still in flight.
    expect(queryClient.getQueryState(['kirocrew-agents'])?.fetchStatus).toBe('fetching')
    realCreateClock()
    // What the hand-over starts: the dialog closes and radar's chat opens.
    await waitFor(() => expect(screen.queryByTestId('crewmate-create-form')).toBeNull(), PANE_READY)
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledWith('radar'), PANE_READY)
  })

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
      // The in-place form's own wording (no dialog closes; the page is left).
      expect(confirmSpy).toHaveBeenCalledWith('Leave this page? Your unsaved crewmate draft will be lost.')
      expect(screen.getByTestId('crewmate-create-form')).toBeInTheDocument()
      // Accepting the prompt lets the navigation through.
      confirmSpy.mockReturnValue(true)
      expect(askToLeave()).toBe('true')
    } finally {
      confirmSpy.mockRestore()
    }
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
      expect(confirmSpy).toHaveBeenLastCalledWith(expect.stringMatching(/^Leave while the crewmate is being created\?/))
      finish({ ok: true })
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('a blank name never leaves the browser: a hint under the field, no create call', async () => {
    await renderPage([row()])
    await rosterRow('oncall')
    await openDialog()
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    expect(await screen.findByText('Give your crewmate a name.')).toBeInTheDocument()
    expect(api.createKirocrewAgent).not.toHaveBeenCalled()
    // A hint is validation, not a failed request: no error notice renders.
    expect(screen.queryByTestId('crewmate-create-error')).toBeNull()
  })

  it('creates through the crew manager\'s write path, opens the new crewmate\'s chat and asks for its first greeting', async () => {
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
    // the record's `description`, the look the card showed, and this page's
    // ask for a first greeting; no `model` key while inheriting.
    await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledWith({
      name: 'radar',
      kiro_agent: 'kirocrew',
      workspace: 'default',
      memory_store: 'default',
      description: 'Triage new issues',
      triggers: '',
      session_color: '',
      avatar: { kind: 'ghost', traits: seededTraits('radar') },
      first_greeting: true,
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

  it('the + New crewmate creates from a name alone: built from the built-in agent, its look pinned, and its chat opens asking its goal through the hidden greeting', async () => {
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
      avatar: expect.objectContaining({ kind: 'ghost' }),
    }))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-radar'), PANE_READY)
    await waitFor(() => expect(api.memberGreet).toHaveBeenCalledWith('radar'), PANE_READY)
    // Once, and never as a message the user typed.
    await new Promise((r) => setTimeout(r, 50))
    expect(api.memberGreet).toHaveBeenCalledTimes(1)
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it('a crewmate not created on the dashboard asks for no greeting when opened', async () => {
    await renderPage([row({ name: 'synced', slug: 'synced' })], 'kirocrew', { route: '/members?member=synced' })
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-synced')
    await new Promise((r) => setTimeout(r, 50))
    expect(api.memberGreet).not.toHaveBeenCalled()
  })

  it('a free-form name opens the crewmate under the id the server answered and asks its greeting by that id', async () => {
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
    await waitFor(() => expect(currentUrl()).toBe('/members?member=launch-notes'))
    await waitFor(() => expect(api.memberGreet).toHaveBeenCalledWith('launch-notes'), PANE_READY)
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it('a name another crewmate shows is refused up front, like a taken key', async () => {
    await renderPage([row({ display_name: 'Oncall Sentinel' })])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Oncall Sentinel' } })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    expect(await screen.findByTestId('crewmate-create-name-hint')).toHaveTextContent('Oncall Sentinel')
    expect(api.createKirocrewAgent).not.toHaveBeenCalled()
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
    unfoldAdvanced()
    const workspace = await screen.findByRole('combobox', { name: 'Workspace' })
    fireEvent.keyDown(workspace, { key: 'ArrowDown' })
    fireEvent.click(await screen.findByRole('option', { name: '+ New workspace…' }))
    // The form is in place (no modal of its own); the nested form is the only
    // dialog, and the outer form's own exits wait for it.
    const outerDialog = screen.getByTestId('crewmate-create-embedded')
    expect(within(outerDialog).getByRole('button', { name: 'Cancel', hidden: true })).toBeDisabled()
    expect(document.querySelector('.z-\\[109\\]')).toBeInTheDocument()
    expect(screen.getByTestId('crewmate-create-submit')).toBeDisabled()
    const wsName = await screen.findByPlaceholderText('e.g. oncall')
    // Empty form: Escape is an ordinary dismissal.
    fireEvent.keyDown(wsName, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByPlaceholderText('e.g. oncall')).toBeNull())
    expect(within(outerDialog).getByRole('button', { name: 'Cancel' })).toBeEnabled()
    // The crewmate dialog underneath is untouched by that Escape.
    expect(screen.getByTestId('crewmate-create-form')).toBeInTheDocument()
    // Reopen, type a name: Escape is now refused, the draft stays.
    fireEvent.keyDown(screen.getByRole('combobox', { name: 'Workspace' }), { key: 'ArrowDown' })
    fireEvent.click(await screen.findByRole('option', { name: '+ New workspace…' }))
    const wsName2 = await screen.findByPlaceholderText('e.g. oncall')
    fireEvent.change(wsName2, { target: { value: 'staging' } })
    fireEvent.keyDown(wsName2, { key: 'Escape' })
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
    expect(workspace).toHaveTextContent('default')
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
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByTestId('crewmate-create-form')).toBeNull())
    await openDialog()
    unfoldAdvanced()
    expect(await screen.findByRole('combobox', { name: 'Workspace' })).toHaveTextContent('default')
    finishRefresh({ workspaces: [{ name: 'default' }, { name: 'staging' }] })
    await new Promise((r) => setTimeout(r, 20))
    expect(screen.getByRole('combobox', { name: 'Workspace' })).toHaveTextContent('default')
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
    expect(workspace).toHaveTextContent('default')
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
  const settle = () => new Promise((r) => setTimeout(r, 30))
  /** The stale-completion contract, whole: no pick, no cache entry, no refetch. */
  const expectStaleDropped = (queryClient: { getQueryData: (k: unknown[]) => unknown }, readsSince: () => number, readsAtClose: number) => {
    expect(screen.getByRole('combobox', { name: 'Workspace' })).toHaveTextContent('default')
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
    // And the parent's Cancel is live again: the nested dialog is closed for good.
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeEnabled()
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
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeEnabled()
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
      expect(await screen.findByRole('combobox', { name: 'Workspace' })).toHaveTextContent('default')
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
    expect(notice).toHaveTextContent("Couldn't create the crewmate. Nothing was created — try again.")
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
    expect(notice).toHaveTextContent('A crewmate named radar already exists.')
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
    expect(notice).not.toHaveTextContent(/the crewmate was created/)
    expect(notice).not.toHaveTextContent(/Nothing was created/)
    expect(screen.getByTestId('crewmate-create-form')).toBeInTheDocument()
    expect(api.sendChat).not.toHaveBeenCalled()
    // The roster behind the dialog was refreshed by the unconfirmed answer,
    // so radar is now on it and a second Create of the same name is a hint
    // under the field with NO request — one POST in total.
    expect(await rosterRow('radar')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    expect(await screen.findByTestId('crewmate-create-name-hint')).toHaveTextContent('A crewmate named radar already exists.')
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
    // Unlocked again: the mutation settled, so Cancel is live.
    expect(screen.getByTestId('crewmate-create-fieldset')).not.toBeDisabled()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeEnabled()
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
    await new Promise((r) => setTimeout(r, 50))
    expect(api.memberGreet).not.toHaveBeenCalled()
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it('a chat open that fails right after the create frees the +, and the next successful open of that crewmate asks for its greeting', async () => {
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
    await new Promise((r) => setTimeout(r, 50))
    expect(api.memberGreet).toHaveBeenCalledTimes(1)
    expect(sendMock).not.toHaveBeenCalled()
  })

  it('Built from is re-read on every open, so a template installed mid-session is offered without a reload', async () => {
    const catalogMock = api.agentCatalog as ReturnType<typeof vi.fn>
    // The shipped client's default (api/queryClient.ts): entries never go
    // stale on their own — under the test client's `staleTime: 0` default a
    // frozen list would re-read anyway and the case would prove nothing.
    await renderPage([row()], 'kirocrew', { queryDefaults: { staleTime: Infinity } })
    await rosterRow('oncall')
    await openDialog()
    await waitFor(() => expect(catalogMock).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByTestId('crewmate-create-form')).toBeNull())
    // A template was installed while the dialog was closed. No server event
    // invalidates this key, so only a per-open re-read can show it.
    catalogMock.mockResolvedValueOnce({
      agents: [
        { name: 'kirocrew', selection_kind: 'template', kiro_agent: 'kirocrew', scope: 'global' },
        { name: 'kirocrew-research', selection_kind: 'template', kiro_agent: 'kirocrew-research', scope: 'global' },
      ],
      default_agent: 'kirocrew',
    })
    await openDialog()
    await waitFor(() => expect(catalogMock).toHaveBeenCalledTimes(2))
    const dlg = within(screen.getByTestId('crewmate-create-embedded'))
    fireEvent.keyDown(dlg.getByRole('combobox', { name: 'Built from' }), { key: 'ArrowDown' })
    expect(await screen.findByRole('option', { name: 'kirocrew-research' })).toBeInTheDocument()
  })

  it('a 2xx whose body carries `error` is said in the product\'s words, not the body\'s', async () => {
    ;(api.createKirocrewAgent as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ error: 'config lock held' })
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    const notice = await screen.findByTestId('crewmate-create-error')
    expect(notice).toHaveTextContent("Couldn't create the crewmate. Nothing was created — try again.")
    expect(notice).not.toHaveTextContent(/lock held/)
    expect(screen.getByTestId('crewmate-create-form')).toBeInTheDocument()
  })

  it('while a create\'s follow-up step is still failed, the header + is held so a second create cannot drop the first one\'s retry', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    expect((await probeAddCrewmate()).held).toBe(false)
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    membersMock.mockRejectedValueOnce(new Error('roster down'))
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await screen.findByTestId('member-post-create-error', undefined, PANE_READY)
    // The cached roster has other rows, so this notice can be dismissed and
    // the hold says so; the empty-roster case (retry only) is tested below.
    // The hold is the New crewmate ITEM's, written under its label (a held
    // Radix item takes no pointer events, so a title would never show).
    expect(await probeAddCrewmate()).toEqual({
      held: true,
      reason: 'Retry or dismiss the notice about radar before adding another crewmate.',
    })
    // New team has no part in the follow-up, so the menu still offers it live.
    await openAddMenu()
    expect(screen.getByTestId('member-add-team')).not.toHaveAttribute('aria-disabled', 'true')
    await closeAddMenu()
    // The retry lands: the record clears and the + is a + again.
    membersMock.mockResolvedValueOnce({
      members: [row(), row({ name: 'radar', slug: 'radar', dashboard_created: true })],
      default_agent: 'kirocrew',
    })
    fireEvent.click(screen.getByTestId('member-post-create-retry'))
    await waitFor(() => expect(screen.queryByTestId('member-post-create-error')).toBeNull())
    await waitForAddCrewmate(false)
    expect(await probeAddCrewmate()).toEqual({ held: false, reason: null })
  })

  it('a second create is held until the first one\'s follow-up settles: the + waits through the thread open, then frees', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    const threadMock = api.memberThread as ReturnType<typeof vi.fn>
    const sendMock = api.sendChat as ReturnType<typeof vi.fn>
    await renderPage([])
    // Create alpha: its roster re-read lands, its thread POST is held open.
    let confirmAlpha: (v: unknown) => void = () => {}
    membersMock.mockResolvedValueOnce({ members: [row({ name: 'alpha', slug: 'alpha', dashboard_created: true })], default_agent: 'kirocrew' })
    threadMock.mockImplementationOnce(() => new Promise((resolve) => { confirmAlpha = resolve }))
    fireEvent.click((await screen.findAllByTestId('crewmate-empty-advanced'))[0])
    await screen.findByTestId('crewmate-create-form')
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'alpha' } })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(threadMock).toHaveBeenCalledWith('alpha'), PANE_READY)
    // Inside that window the header +'s New crewmate item is held and says
    // why: a second create here could overwrite the first one's retry.
    expect(await probeAddCrewmate()).toEqual({
      held: true,
      reason: "Finishing alpha's first message before adding another crewmate.",
    })
    expect(screen.queryByTestId('crewmate-empty-hero')).toBeNull()
    // Alpha's thread confirms: the follow-up is over and the + is a + again.
    confirmAlpha({ slot_key: 'member-alpha', slug: 'alpha', member: 'alpha', created: true })
    await waitForAddCrewmate(false)
    expect(await probeAddCrewmate()).toEqual({ held: false, reason: null })
    // Now beta can be created, and gets its own greeting.
    membersMock.mockResolvedValueOnce({
      members: [row({ name: 'alpha', slug: 'alpha', dashboard_created: true }), row({ name: 'beta', slug: 'beta', dashboard_created: true })],
      default_agent: 'kirocrew',
    })
    threadMock.mockResolvedValueOnce({ slot_key: 'member-beta', slug: 'beta', member: 'beta', created: true })
    await clickAddAdvanced()
    await screen.findByTestId('crewmate-create-form')
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'beta' } })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(api.memberGreet).toHaveBeenCalledWith('beta'), PANE_READY)
    expect(sendMock).not.toHaveBeenCalled()
  })

  it('the empty-roster hero is held too while the first create\'s roster re-read is still in flight, so it cannot open a second create', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    await renderPage([])
    const ctas = await screen.findAllByTestId('crewmate-empty-cta')
    for (const cta of ctas) expect(cta).toBeEnabled()
    for (const adv of screen.getAllByTestId('crewmate-empty-advanced')) expect(adv).toBeEnabled()
    // The re-read after the create is held open: the roster is still empty,
    // so the hero is still on screen — and must not be a second door.
    let finishReread: (v: unknown) => void = () => {}
    membersMock.mockImplementationOnce(() => new Promise((resolve) => { finishReread = resolve }))
    fireEvent.click(screen.getAllByTestId('crewmate-empty-advanced')[0])
    await screen.findByTestId('crewmate-create-form')
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'alpha' } })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(screen.queryByTestId('crewmate-create-form')).toBeNull(), PANE_READY)
    await waitFor(() => expect(membersMock).toHaveBeenCalledTimes(2))
    for (const cta of screen.getAllByTestId('crewmate-empty-cta')) {
      expect(cta).toBeDisabled()
      expect(cta).toHaveAttribute('title', "Finishing alpha's first message before adding another crewmate.")
    }
    // Both hero doors: the Advanced link is held with the primary.
    for (const adv of screen.getAllByTestId('crewmate-empty-advanced')) expect(adv).toBeDisabled()
    // The header + is not rendered while the hero is the door.
    expect(screen.queryByTestId('member-add')).toBeNull()
    // The re-read lands with alpha: the hero yields to the roster and the + appears, freed once its chat opens.
    finishReread({ members: [row({ name: 'alpha', slug: 'alpha', dashboard_created: true })], default_agent: 'kirocrew' })
    await waitFor(() => expect(screen.queryByTestId('crewmate-empty-cta')).toBeNull())
    await waitFor(() => expect(api.memberGreet).toHaveBeenCalledWith('alpha'), PANE_READY)
    await screen.findByTestId('member-add')
    await waitForAddCrewmate(false)
  })

  it('a roster re-read cancelled mid-flight (the star mutation\'s cancelQueries) is a failed re-read: the notice with its retry, no greeting dropped, no other chat opened', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    const { queryClient } = await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    // The re-read is held open; while it is in flight the roster query is
    // cancelled, which reverts it to the pre-create roster WITHOUT an error.
    let landReread: (v: unknown) => void = () => {}
    membersMock.mockImplementationOnce(() => new Promise((resolve) => { landReread = resolve }))
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(membersMock).toHaveBeenCalledTimes(2), PANE_READY)
    await act(async () => {
      await queryClient.cancelQueries({ queryKey: ['kirocrew-agents', 'members-roster'] })
    })
    landReread({ members: [row(), row({ name: 'radar', slug: 'radar', dashboard_created: true })], default_agent: 'kirocrew' })
    // Reported as the roster step failing, with its retry — never as "radar is gone".
    const notice = await screen.findByTestId('member-post-create-error', undefined, PANE_READY)
    expect(notice).toHaveTextContent("radar was created, but the list didn't refresh.")
    expect(screen.getByTestId('member-post-create-retry')).toHaveTextContent('Refresh your crewmates')
    expect(api.memberThread).not.toHaveBeenCalledWith('radar')
    expect(api.sendChat).not.toHaveBeenCalled()
    expect(screen.queryByTestId('member-gone-roster-notice')).toBeNull()
    // The retry lands a fresh roster: radar's chat opens and asks for its greeting.
    membersMock.mockResolvedValueOnce({ members: [row(), row({ name: 'radar', slug: 'radar', dashboard_created: true })], default_agent: 'kirocrew' })
    fireEvent.click(screen.getByTestId('member-post-create-retry'))
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledWith('radar'))
    await waitFor(() => expect(api.memberGreet).toHaveBeenCalledWith('radar'), PANE_READY)
  })

  it('a thread open that fails after the create releases the hold on the +', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    const threadMock = api.memberThread as ReturnType<typeof vi.fn>
    await renderPage([])
    membersMock.mockResolvedValueOnce({ members: [row({ name: 'alpha', slug: 'alpha' })], default_agent: 'kirocrew' })
    let failAlpha: (e: Error) => void = () => {}
    threadMock.mockImplementationOnce(() => new Promise((_resolve, reject) => { failAlpha = reject }))
    fireEvent.click((await screen.findAllByTestId('crewmate-empty-advanced', undefined, PANE_READY))[0])
    await screen.findByTestId('crewmate-create-form')
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'alpha' } })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(threadMock).toHaveBeenCalledWith('alpha'), PANE_READY)
    expect((await probeAddCrewmate()).held).toBe(true)
    failAlpha(new Error('boom'))
    await waitForAddCrewmate(false)
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  // A crewmate's name is free-form display text (`members.validate_member_name`
  // on the server): spaces, punctuation, any script, emoji. The dialog applies
  // no grammar of its own beyond the trim — the stable id, slug and slot key
  // are the server's to derive, and `Built from` is the one strict identifier
  // on the form. Each name goes out EXACTLY as typed (trimmed), and the
  // follow-up opens the chat under the identity the roster returns for it.
  // The slugs are deliberately NOT what a client-side slugifier would produce
  // (`dr-eggbot-2` is a namesake allocation, `m-7f3a` is opaque): the dialog
  // must open the chat by the slug the roster hands back, never by one it
  // derives from the display name.
  it.each([
    ['spaces and punctuation', '  Dr. Eggbot  ', 'Dr. Eggbot', 'dr-eggbot'],
    ['a namesake-allocated slug', 'Dr. Eggbot', 'Dr. Eggbot', 'dr-eggbot-2'],
    ['an accented name', 'Radär', 'Radär', 'radar'],
    ['a non-Latin script and an opaque slug', '雷达', '雷达', 'm-7f3a'],
    ['an emoji', 'Dr. Eggbot 🥚', 'Dr. Eggbot 🥚', 'dr-eggbot'],
    // A single emoji is a whole name (nothing Latin to slugify: the roster's
    // slug is opaque). Plain code points only — a ZWJ / VS16 sequence is not
    // claimed here, since the server's hidden-character guard may refuse it.
    ['an emoji-only name', '🥚', '🥚', 'm-e99a'],
    ['URL-hostile punctuation', 'Dr. & Co #1 100% +ok', 'Dr. & Co #1 100% +ok', 'dr-co-1'],
    // Exactly the server's cap (`members.MEMBER_NAME_MAX_CHARS`, 500): the
    // longest name that is accepted, sent whole.
    ['exactly 500 characters', 'R'.repeat(500), 'R'.repeat(500), 'm-500c'],
  ])('a free-form display name with %s is sent as typed (trimmed) and its chat opens under the returned identity', async (_label, typed, sent, slug) => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    const threadMock = api.memberThread as ReturnType<typeof vi.fn>
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: typed } })
    // The roster re-read lists the new crewmate under its exact display name
    // with the server-derived slug; the thread open goes by that slug.
    membersMock.mockResolvedValueOnce({
      members: [row(), row({ name: sent, slug, slot_key: `member-${slug}`, dashboard_created: true })],
      default_agent: 'kirocrew',
    })
    threadMock.mockResolvedValueOnce({ slot_key: `member-${slug}`, slug, member: sent, created: true })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))
    // No hint, no refusal: the request left with the display name untouched.
    expect(screen.queryByTestId('crewmate-create-name-hint')).toBeNull()
    expect(api.createKirocrewAgent).toHaveBeenCalledWith(expect.objectContaining({ name: sent, kiro_agent: 'kirocrew', first_greeting: true }))
    await waitFor(() => expect(threadMock).toHaveBeenCalledWith(slug), PANE_READY)
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent(`member-${slug}`), PANE_READY)
    // The URL names the exact display name (however the router encodes it).
    expect(new URLSearchParams((currentUrl() ?? '').split('?')[1] ?? '').get('member')).toBe(sent)
    // The first greeting is asked for by the slug the server derived, and
    // nothing is sent in the user's name.
    await waitFor(() => expect(api.memberGreet).toHaveBeenCalledWith(slug))
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it('a name of only whitespace is still refused as blank, with no request', async () => {
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: '   ' } })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    expect(await screen.findByTestId('crewmate-create-name-hint')).toHaveTextContent('Give your crewmate a name.')
    expect(screen.getByLabelText('Name')).toHaveAttribute('aria-invalid', 'true')
    expect(api.createKirocrewAgent).not.toHaveBeenCalled()
  })

  // Pins the retirement of the #1684 "must pick a template" guard. Before the
  // create doors converged on this dialog, create started with no template
  // selected and refused to submit until one was picked. The accepted
  // rfc-crewmates-launch.md rules the New crewmate dialog's "Built from" field
  // is "the default agent or a custom agent" — a default, not a required pick —
  // so submitting the untouched field posts `kiro_agent: 'kirocrew'` and there
  // is no template-required refusal. If someone restores the old guard, this
  // fails on the missing request.
  it('a create that never touches "Built from" defaults to the built-in agent, with no template-required refusal (RFC-ruled retirement of #1684)', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    membersMock.mockResolvedValueOnce({
      members: [row(), row({ name: 'radar', slug: 'radar', slot_key: 'member-radar' })],
      default_agent: 'kirocrew',
    })
    // Submit without ever interacting with the "Built from" select.
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))
    // No refusal hint was raised — the untouched field is a valid default,
    // not an unsatisfied requirement.
    expect(screen.queryByTestId('crewmate-create-name-hint')).toBeNull()
    // The request carries the built-in agent as the built-from value.
    expect(api.createKirocrewAgent).toHaveBeenCalledWith(expect.objectContaining({ name: 'radar', kiro_agent: 'kirocrew' }))
  })

  it('a free-form name already on the roster is refused as taken before any request, by exact display name', async () => {
    await renderPage([row({ name: 'Dr. Eggbot', slug: 'dr-eggbot', slot_key: 'member-dr-eggbot' })])
    await waitFor(() => expect(screen.getByTestId('member-roster')).toHaveTextContent('Dr. Eggbot'), PANE_READY)
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Dr. Eggbot' } })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    expect(await screen.findByTestId('crewmate-create-name-hint')).toHaveTextContent('Dr. Eggbot')
    expect(api.createKirocrewAgent).not.toHaveBeenCalled()
    // A different spelling is a different display name: the server decides
    // (it gives a colliding slug its own identity), not the dialog.
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Dr Eggbot' } })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledWith(expect.objectContaining({ name: 'Dr Eggbot' })))
  })

  it('a server failure other than a taken name is said in the product\'s words, not the server\'s sentence', async () => {
    ;(api.createKirocrewAgent as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new ApiError(500, 'Internal Server Error: config lock held', JSON.stringify({ error: 'config lock held' })),
    )
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    const notice = await screen.findByTestId('crewmate-create-error')
    expect(notice).toHaveTextContent("Couldn't create the crewmate. Nothing was created — try again.")
    expect(notice).not.toHaveTextContent(/lock held/)
  })

  it('a name the server refuses as credential-shaped (400) is said in the error notice with its cause and marks the Name field, never as "nothing was created — try again"', async () => {
    // The dialog applies no name grammar, so `aws_secret_access_key` goes out;
    // the server refuses it with 400 `credential_shaped_name` and deliberately
    // does not echo the name.
    // Collapsing that to the generic "try again" would send the user back to the
    // same name with the cause hidden. The server answered, so — like the 409 —
    // it is an ErrorNotice (the repo's one error surface, never a bare hint),
    // the Name field is marked, and the notice does not echo the name either.
    ;(api.createKirocrewAgent as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new ApiError(400, 'Bad Request', JSON.stringify({
        error: 'Agent name looks like a credential or a URL carrying one. Pick a name that identifies the crew instead.',
        code: 'credential_shaped_name',
      })),
    )
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'aws_secret_access_key' } })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    const notice = await screen.findByTestId('crewmate-create-error')
    expect(notice).toHaveTextContent('That name looks like a credential or a URL carrying one. Pick a name that says who the crewmate is.')
    expect(notice).not.toHaveTextContent(/aws_secret_access_key/)
    expect(notice).not.toHaveTextContent(/try again/)
    expect(screen.queryByTestId('crewmate-create-name-hint')).toBeNull()
    expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('crewmate-create-form')).toBeInTheDocument()
    expect(screen.getByLabelText('Name')).toHaveAttribute('aria-invalid', 'true')
    expect(api.sendChat).not.toHaveBeenCalled()
    // Editing the field clears the notice and the mark; a different name goes out.
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    expect(screen.queryByTestId('crewmate-create-error')).toBeNull()
    expect(screen.getByLabelText('Name')).not.toHaveAttribute('aria-invalid')
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(2))
  })

  // A display name the server cannot store as text (400 `invalid_member_name`
  // from `members.validate_member_name`): hidden / non-canonical characters, a
  // line break, over the length cap. The dialog has no pre-check for these —
  // two names can look identical and differ in invisible bytes — so the
  // verdict is said as the Name field's in the product's words — a MENU of
  // alternatives naming every rule the server applies (retype a paste or
  // remove hidden characters / line breaks / tabs, only periods, the exact
  // 500-character cap), not a checklist, so it never reads as "periods or
  // emoji are forbidden" — and the draft stays. The server's own sentence (which names the rule and
  // may quote the name) is never shown, and the copy is not the
  // credential-shaped one.
  it.each([
    ['a non-canonical (NFD) spelling', 'Rada\u0308r', 'Invalid Crew Member name: name must not contain hidden or non-canonical characters'],
    ['a line break', 'Radar\u2028One', 'Invalid Crew Member name: name must not contain line breaks or tabs'],
    ['an embedded tab', 'Radar\tOne', 'Invalid Crew Member name: name must not contain line breaks or tabs'],
    ['a period-only name', '...', 'Invalid Crew Member name: name must not consist only of periods'],
    ['an over-long name', 'R'.repeat(501), 'Invalid Crew Member name: name must be at most 500 characters'],
  ])('a name the server refuses as unusable text (%s) is said with its fix, marks the Name field, keeps the draft, and echoes neither name nor server reason', async (_label, typed, serverSentence) => {
    ;(api.createKirocrewAgent as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new ApiError(400, 'Bad Request', JSON.stringify({ error: serverSentence, code: 'invalid_member_name' })),
    )
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: typed } })
    fireEvent.change(screen.getByLabelText('What it looks after'), { target: { value: 'Watch the pager' } })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    const notice = await screen.findByTestId('crewmate-create-error')
    expect(notice).toHaveTextContent("That name can't be used as written. Try another version: retype pasted text or remove hidden characters, line breaks, or tabs; don't use only periods; or shorten it to 500 characters or fewer.")
    // The server's own rule sentence is never shown, and neither is the name.
    expect(notice).not.toHaveTextContent(/Invalid Crew Member name|non-canonical|line breaks or tabs|at most 500|only of periods/)
    expect(notice).not.toHaveTextContent(/Rada|Radar|\.\.\./)
    // Not the credential-shaped copy, and not the generic "nothing was created".
    expect(notice).not.toHaveTextContent(/credential|says who the crewmate is/)
    expect(notice).not.toHaveTextContent('Nothing was created')
    expect(screen.queryByTestId('crewmate-create-name-hint')).toBeNull()
    expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1)
    expect(api.createKirocrewAgent).toHaveBeenLastCalledWith(expect.objectContaining({ name: typed }))
    expect(api.sendChat).not.toHaveBeenCalled()
    // The draft is kept and the Name field is the marked thing to change.
    expect(screen.getByTestId('crewmate-create-form')).toBeInTheDocument()
    expect(screen.getByLabelText('Name')).toHaveValue(typed)
    expect(screen.getByLabelText('What it looks after')).toHaveValue('Watch the pager')
    expect(screen.getByLabelText('Name')).toHaveAttribute('aria-invalid', 'true')
    // Editing the field clears the notice and the mark; the corrected name goes out.
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Radär' } })
    expect(screen.queryByTestId('crewmate-create-error')).toBeNull()
    expect(screen.getByLabelText('Name')).not.toHaveAttribute('aria-invalid')
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(2))
    expect(api.createKirocrewAgent).toHaveBeenLastCalledWith(expect.objectContaining({ name: 'Radär' }))
  })

  it('a name already on the roster is refused before any request, in plain words, inside the dialog', async () => {
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'oncall' } })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    // Validation, not a failed request: the hint under the field, no ErrorNotice.
    const hint = await screen.findByTestId('crewmate-create-name-hint')
    expect(hint).toHaveTextContent('A crewmate named oncall already exists.')
    expect(screen.queryByTestId('crewmate-create-error')).toBeNull()
    // No request left the browser; nothing else moved — the one thread POST
    // is the arrival auto-open, no roster re-read, no greeting.
    expect(api.createKirocrewAgent).not.toHaveBeenCalled()
    expect(screen.getByTestId('crewmate-create-form')).toBeInTheDocument()
    expect(api.memberThread).toHaveBeenCalledTimes(1)
    expect(api.members).toHaveBeenCalledTimes(1)
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it('a name the roster did not know but the server has (stale roster) is said as taken from the 409', async () => {
    ;(api.createKirocrewAgent as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new ApiError(409, 'Conflict', JSON.stringify({ error: 'agent exists', code: 'agent_exists' })),
    )
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    const notice = await screen.findByTestId('crewmate-create-error')
    expect(notice).toHaveTextContent('A crewmate named radar already exists.')
    expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('crewmate-create-form')).toBeInTheDocument()
    expect(api.sendChat).not.toHaveBeenCalled()
    // The answer is about the Name field, so the field is marked too; editing it clears the mark.
    expect(screen.getByLabelText('Name')).toHaveAttribute('aria-invalid', 'true')
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar2' } })
    expect(screen.getByLabelText('Name')).not.toHaveAttribute('aria-invalid')
  })

  it('a dropped request for a name the roster already had never greets the old crewmate: it is refused before the request', async () => {
    // The reconcile's premise: a request only leaves for a name the roster
    // lacked, so a row found afterwards is this request's. With the name
    // present up front there is no request to reconcile and no greeting.
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'oncall' } })
    fireEvent.change(screen.getByLabelText('What it looks after'), { target: { value: 'A new job' } })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await screen.findByTestId('crewmate-create-name-hint')
    expect(api.createKirocrewAgent).not.toHaveBeenCalled()
    expect(api.sendChat).not.toHaveBeenCalled()
    expect(api.memberThread).toHaveBeenCalledTimes(1)
  })

  it('a failed roster re-read after the create is said above the chat column with a retry, never read as a gone name', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    // The create lands; the re-read that follows rejects. react-query keeps
    // the stale roster as `res.data`, so a plain "is radar on the list" check
    // would send the page down the gone-member path and open someone else.
    membersMock.mockRejectedValueOnce(new Error('roster down'))
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    const notice = await screen.findByTestId('member-post-create-error', undefined, PANE_READY)
    expect(notice).toHaveTextContent("radar was created, but the list didn't refresh.")
    // The retry names the one step it repeats — never a second "Create".
    expect(screen.getByTestId('member-post-create-retry')).toHaveTextContent('Refresh your crewmates')
    expect(screen.queryByTestId('member-gone-notice')).toBeNull()
    expect(screen.queryByTestId('member-gone-roster-notice')).toBeNull()
    // oncall's arrival open is the only thread POST; radar's never happened.
    expect(api.memberThread).not.toHaveBeenCalledWith('radar')
    expect(api.sendChat).not.toHaveBeenCalled()
    // Retry repeats exactly the step that failed; with the roster back, the
    // new crewmate's chat opens and asks for its greeting.
    membersMock.mockResolvedValue({
      members: [row(), row({ name: 'radar', slug: 'radar', dashboard_created: true })],
      default_agent: 'kirocrew',
    })
    fireEvent.click(screen.getByTestId('member-post-create-retry'))
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledWith('radar'))
    await waitFor(() => expect(api.memberGreet).toHaveBeenCalledWith('radar'), PANE_READY)
    expect(screen.queryByTestId('member-post-create-error')).toBeNull()
  })

  it('a failed re-read over a roster with other crewmates can be dismissed, so the existing chats stay reachable below md', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    const utils = await renderPage([row()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    membersMock.mockRejectedValueOnce(new Error('roster down'))
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    const notice = await screen.findByTestId('member-post-create-error', undefined, PANE_READY)
    // While the notice is up the roster yields below md (the notice column
    // is the screen there) — which is exactly why it must be closable here:
    // an undismissable notice would lock oncall's chat behind a failing
    // server for as long as it keeps failing.
    const aside = screen.getByTestId('member-roster')
    expect(aside.className).toMatch(/\bhidden\b/)
    const dismiss = within(notice).getByRole('button', { name: /dismiss|close/i })
    fireEvent.click(dismiss)
    expect(screen.queryByTestId('member-post-create-error')).toBeNull()
    // The old list is back (radar is not on it until the next read), the
    // create door is a door again, and the retry was not spent.
    expect(await rosterRow('oncall')).toBeInTheDocument()
    expect(roster().queryByText('radar')).toBeNull()
    expect(await probeAddCrewmate()).toEqual({ held: false, reason: null })
    expect(api.memberGreet).not.toHaveBeenCalled()
    // The greeting is the server's, owed to radar since its create: the next
    // roster read that lands lists radar, and radar's first open asks for it.
    membersMock.mockResolvedValue({
      members: [row(), row({ name: 'radar', slug: 'radar', dashboard_created: true })],
      default_agent: 'kirocrew',
    })
    await act(async () => { await utils.queryClient.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY }) })
    fireEvent.click(await rosterRow('radar'))
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledWith('radar'))
    await waitFor(() => expect(api.memberGreet).toHaveBeenCalledWith('radar'), PANE_READY)
    expect(api.memberGreet).toHaveBeenCalledTimes(1)
    expect(api.sendChat).not.toHaveBeenCalled()
    expect(screen.queryByTestId('member-post-create-error')).toBeNull()
  })

  it('a first create whose re-read fails: the roster stays visible on desktop and yields below md', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    // Empty roster: no chat opens, so the main column carries the notice below
    // md while the roster remains beside it on desktop. `rosterShown` reserves
    // that desktop width, so the aside must actually occupy it there.
    await renderPage([])
    // Both copies of the hero (roster below md, column above) share one testid.
    fireEvent.click((await screen.findAllByTestId('crewmate-empty-advanced', undefined, PANE_READY))[0])
    await screen.findByTestId('crewmate-create-form')
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    membersMock.mockRejectedValueOnce(new Error('roster down'))
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    const notice = await screen.findByTestId('member-post-create-error', undefined, PANE_READY)
    expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
    // "0 crewmates" under "radar was created" would contradict the notice: the
    // count reads as a dash — the same dash a failed arrival read shows — until
    // the retry refreshes the list.
    expect(screen.getByTestId('member-count')).toHaveTextContent('\u2014')
    // CSS picks per viewport: the roster yields below md but returns at md+
    // beside the visible notice column.
    const aside = screen.getByTestId('member-roster')
    expect(aside.className).toMatch(/\bhidden\b/)
    expect(aside.className).toMatch(/\bmd:flex\b/)
    const column = notice.closest('section') as HTMLElement
    expect(column.className).toMatch(/\bflex\b/)
    expect(column.className).not.toMatch(/\bhidden\b/)
    // No dismiss on a roster failure: the cached roster is still [] while
    // radar exists, so closing the notice would put "No crewmates yet" under
    // a crewmate that is there. The retry is the only way off it.
    expect(within(notice).queryByRole('button', { name: /dismiss|close/i })).toBeNull()
    membersMock.mockResolvedValue({ members: [row({ name: 'radar', slug: 'radar', dashboard_created: true })], default_agent: 'kirocrew' })
    fireEvent.click(screen.getByTestId('member-post-create-retry'))
    await waitFor(() => expect(screen.queryByTestId('member-post-create-error')).toBeNull())
    // With the roster back and radar's chat open, the roster stays hidden
    // below md for the usual reason (a chat is open), never for the notice.
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledWith('radar'))
    expect(screen.getByTestId('member-count')).toHaveTextContent('1 crewmate')
  })

  it('a first create over a default-only roster whose re-read fails is not dismissable either: the built-in default is not a crewmate', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    // The cached roster is NOT empty — it holds the built-in `default` row —
    // yet the hero is what it renders (`hasNoCrewmates`). Dismissability must
    // follow the same predicate: a `length > 0` test would offer a dismiss
    // here, and closing the notice would put "No crewmates yet" back under a
    // crewmate that exists.
    await renderPage([row({ name: 'default', slug: 'default', last_active_ts: 999 })])
    fireEvent.click((await screen.findAllByTestId('crewmate-empty-advanced', undefined, PANE_READY))[0])
    await screen.findByTestId('crewmate-create-form')
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    membersMock.mockRejectedValueOnce(new Error('roster down'))
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    const notice = await screen.findByTestId('member-post-create-error', undefined, PANE_READY)
    expect(notice).toHaveTextContent("radar was created, but the list didn't refresh.")
    expect(within(notice).queryByRole('button', { name: /dismiss|close/i })).toBeNull()
    // The header "+" stays absent (the hero is still the one create door on
    // a default-only roster), and the hero CTA itself is held by the retry-
    // only wording an undismissable roster notice carries.
    expect(screen.queryByTestId('member-add')).toBeNull()
    for (const cta of screen.getAllByTestId('crewmate-empty-cta')) {
      expect(cta).toBeDisabled()
      expect(cta).toHaveAttribute('title', 'Refresh the list for radar before adding another crewmate.')
    }
    // The retry is the way off it: the roster lands with radar, the chat
    // opens and asks for its greeting.
    membersMock.mockResolvedValue({
      members: [row({ name: 'default', slug: 'default', last_active_ts: 999 }), row({ name: 'radar', slug: 'radar', dashboard_created: true })],
      default_agent: 'kirocrew',
    })
    fireEvent.click(screen.getByTestId('member-post-create-retry'))
    await waitFor(() => expect(screen.queryByTestId('member-post-create-error')).toBeNull())
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledWith('radar'))
    await waitFor(() => expect(api.memberGreet).toHaveBeenCalledWith('radar'), PANE_READY)
  })

  it('a create over a default + Assistant roster whose re-read fails can be dismissed: the Assistant is a crewmate and its chat stays reachable', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    // The Assistant counts: the roster is NOT the empty state, so its chat
    // opens and a failed follow-up notice is dismissable (a "No crewmates
    // yet" hero would never come back under it).
    await renderPage([defaultRow({ last_active_ts: 999 }), assistantRow()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-kirocrew-captain')
    await clickAddAdvanced()
    await screen.findByTestId('crewmate-create-form')
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    membersMock.mockRejectedValueOnce(new Error('roster down'))
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    const notice = await screen.findByTestId('member-post-create-error')
    expect(notice).toHaveTextContent("radar was created, but the list didn't refresh.")
    expect(screen.queryByTestId('crewmate-empty-hero')).toBeNull()
    expect(within(notice).getByRole('button', { name: /dismiss|close/i })).toBeInTheDocument()
    expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-kirocrew-captain')
    // The retry is the way on: the roster lands with radar, its chat opens.
    membersMock.mockResolvedValue({
      members: [defaultRow({ last_active_ts: 999 }), assistantRow(), row({ name: 'radar', slug: 'radar', dashboard_created: true })],
      default_agent: 'kirocrew',
    })
    fireEvent.click(screen.getByTestId('member-post-create-retry'))
    await waitFor(() => expect(screen.queryByTestId('member-post-create-error')).toBeNull())
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledWith('radar'))
  })

  it('Built from lists the catalog\'s template rows with the built-in one labelled as the default — never a member row', async () => {
    // The same catalog the chat picker reads: members and templates are
    // separate rows tagged by kind. A member (the configured default crew
    // included) is never a thing to build from — its alias stored as
    // `kiro_agent` would boot a fallback — and the catalog has already dropped
    // background-only specs, fork copies and masked names on the server.
    ;(api.agentCatalog as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      agents: [
        { name: 'default', selection_kind: 'member', kiro_agent: 'kirocrew' },
        { name: 'oncall', selection_kind: 'member', kiro_agent: 'kirocrew' },
        { name: 'kirocrew', selection_kind: 'template', kiro_agent: 'kirocrew', scope: 'global' },
        { name: 'kirocrew-research', selection_kind: 'template', kiro_agent: 'kirocrew-research', scope: 'global' },
        { name: 'my-lite', selection_kind: 'template', kiro_agent: 'my-lite', scope: 'global' },
      ],
      default_agent: 'default',
    })
    await renderPage([row()])
    await rosterRow('oncall')
    await openDialog()
    const dlg = within(screen.getByTestId('crewmate-create-embedded'))
    await waitFor(() => expect(dlg.getByRole('combobox', { name: 'Built from' })).toHaveTextContent('kirocrew (default)'))
    // A crew alias (`default`) is not a template and must not be offered: a
    // record storing it as `kiro_agent` would boot a fallback, not that crew.
    expect(dlg.getByRole('combobox', { name: 'Built from' })).not.toHaveTextContent(/^default$/)
    // Only template rows are offered; member rows (default, oncall) are not.
    fireEvent.keyDown(dlg.getByRole('combobox', { name: 'Built from' }), { key: 'ArrowDown' })
    expect(await screen.findByRole('option', { name: 'kirocrew-research' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'my-lite' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'oncall' })).toBeNull()
    expect(screen.queryByRole('option', { name: 'default' })).toBeNull()
    fireEvent.keyDown(screen.getByRole('option', { name: 'kirocrew-research' }), { key: 'Escape' })
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Scout' } })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledWith(expect.objectContaining({ kiro_agent: 'kirocrew' })))
  })

  it('Advanced settings folds the rest of the crew manager\'s form behind one disclosure in the New crewmate card', async () => {
    await renderPage([row()])
    await rosterRow('oncall')
    await clickAddCrewmate()
    await screen.findByTestId('crewmate-create-form')
    const toggle = screen.getByTestId('crewmate-create-advanced-toggle')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('crewmate-create-advanced')).toBeNull()
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    const advanced = await screen.findByTestId('crewmate-create-advanced')
    // The same field components the editor mounts: workspace and model.
    expect(within(advanced).getByLabelText('Workspace')).toBeInTheDocument()
    const colorInput = advanced.querySelector('input[type="color"]')
    expect(colorInput).not.toBeNull()
    expect(colorInput).toHaveValue('#4f8ef7')
    expect(within(advanced).getByLabelText('Edit default model')).toBeInTheDocument()
  })

  it('the help "?" toggles inside Advanced explain their field; they never submit the form', async () => {
    // A <button> defaults to type="submit". The fields Advanced mounts carry
    // InfoTip toggles; inside the dialog's <form> an untyped one would post the
    // create (or paint the blank-name refusal) on a click meant to read help.
    await renderPage([row()])
    await rosterRow('oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    unfoldAdvanced()
    const advanced = await screen.findByTestId('crewmate-create-advanced')
    const tips = within(advanced).getAllByRole('button', { name: 'More information' })
    expect(tips.length).toBeGreaterThan(0)
    for (const tip of tips) expect(tip).toHaveAttribute('type', 'button')
    fireEvent.click(tips[0])
    expect(await screen.findByRole('tooltip')).toBeInTheDocument()
    // The dialog is still open with the draft intact and nothing was posted.
    expect(screen.getByLabelText('Name')).toHaveValue('radar')
    expect(api.createKirocrewAgent).not.toHaveBeenCalled()
    expect(screen.queryByTestId('crewmate-create-name-hint')).toBeNull()
  })
})

describe('MembersPage create links open the New crewmate card', () => {
  // `default` rides along untouched: the Assistant is its own member.
  const assistantAndAlpha = () => [defaultRow(), assistantRow(), row({ name: 'alpha', slug: 'alpha' })]
  /** True when the element, or an ancestor, is hidden by an inline display:none. */
  const hiddenByStyle = (el: Element) => {
    for (let n: Element | null = el; n; n = n.parentElement) {
      if (n instanceof HTMLElement && n.style.display === 'none') return true
    }
    return false
  }
  const pane = () => screen.getByTestId('member-create-pane')
  const isHiddenClass = (el: Element) => /(^|\s)hidden(\s|$)/.test(el.className)
  const cardOpen = () => !isHiddenClass(pane()) && !!within(pane()).queryByTestId('crewmate-create-embedded')
  const card = () => within(pane()).getByTestId('crewmate-create-embedded')
  const nameField = () => within(card()).getByLabelText('Name') as HTMLInputElement
  /** "What it looks after": in the Advanced settings, so present only unfolded. */
  const goalField = () => within(card()).queryByLabelText('What it looks after') as HTMLTextAreaElement | null
  const closeCard = () => fireEvent.click(within(card()).getByRole('button', { name: 'Cancel' }))
  /** No step of the removed guided flow, and no creation receipt, anywhere. */
  const noGuidedFlow = () => {
    expect(document.querySelector('[data-testid^="meet-crewmates-"]')).toBeNull()
    expect(screen.queryByTestId('member-create-guided')).toBeNull()
    expect(screen.queryByTestId('member-created-receipt')).toBeNull()
  }
  /** Records links the page let through (not intercepted) and stops the
   *  test DOM from following them. */
  const passedThrough = () => {
    const seen: string[] = []
    const onClick = (e: MouseEvent) => {
      const a = (e.target as Element | null)?.closest?.('a[href]')
      if (!a) return
      seen.push(a.getAttribute('href') ?? '')
      e.preventDefault()
    }
    document.addEventListener('click', onClick)
    return { seen, stop: () => document.removeEventListener('click', onClick) }
  }
  const openAssistant = async () => {
    await renderPage(assistantAndAlpha())
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-kirocrew-captain')
  }

  it('a create link in Captain\'s chat opens the New crewmate card in place, prefilled: name in Name, goal in What it looks after; the chat stays MOUNTED; nothing is sent', async () => {
    await openAssistant()
    const through = passedThrough()
    try {
      const stub = screen.getByTestId('chat-pane-stub')
      const mountsBefore = chatPaneMounts.count
      navigateSpy.mockClear()
      fireEvent.click(screen.getByTestId('chat-link-proposal'))
      await waitFor(() => expect(cardOpen()).toBe(true))
      expect(through.seen).toEqual([])
      expect(navigateSpy).not.toHaveBeenCalled()
      expect(currentUrl()).toBe('/members?member=kirocrew-captain')
      expect(nameField().value).toBe('scout')
      // A goal was proposed, so Advanced settings is open and the goal is in view.
      expect(within(card()).getByTestId('crewmate-create-advanced-toggle')).toHaveAttribute('aria-expanded', 'true')
      expect(goalField()?.value).toBe('Watch the release train')
      noGuidedFlow()
      // Hidden, not unmounted: same element, no new mount, composer state kept.
      expect(screen.getByTestId('chat-pane-stub')).toBe(stub)
      expect(hiddenByStyle(stub)).toBe(true)
      expect(chatPaneMounts.count).toBe(mountsBefore)
      closeCard()
      await waitFor(() => expect(cardOpen()).toBe(false))
      expect(screen.getByTestId('chat-pane-stub')).toBe(stub)
      expect(hiddenByStyle(stub)).toBe(false)
      expect(chatPaneMounts.count).toBe(mountsBefore)
      expect(api.createKirocrewAgent).not.toHaveBeenCalled()
      expect(api.sendChat).not.toHaveBeenCalled()
    } finally {
      through.stop()
    }
  })

  it('a create link with no goal opens the card folded and empty, from any chat, and its close returns to THAT chat', async () => {
    await renderPage(assistantAndAlpha(), 'kirocrew', { route: '/members?member=alpha' })
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-alpha')
    await followBlankCreateLink()
    await waitFor(() => expect(cardOpen()).toBe(true))
    expect(nameField().value).toBe('')
    expect(within(card()).getByTestId('crewmate-create-advanced-toggle')).toHaveAttribute('aria-expanded', 'false')
    expect(goalField()).toBeNull()
    noGuidedFlow()
    await waitFor(() => expect(currentUrl()).toBe('/members?member=alpha'))
    closeCard()
    await waitFor(() => expect(cardOpen()).toBe(false))
    expect(currentUrl()).toBe('/members?member=alpha')
    expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-alpha')
  })

  it('a deep link /members?create=1&name=&goal= opens an EDITABLE prefilled card, strips its params, and creates nothing', async () => {
    await renderPage(assistantAndAlpha(), 'kirocrew', {
      route: '/members?create=1&name=' + encodeURIComponent('scout') + '&goal=' + encodeURIComponent('Watch the release train & tag it'),
    })
    await waitFor(() => expect(cardOpen()).toBe(true))
    expect(nameField().value).toBe('scout')
    expect(goalField()?.value).toBe('Watch the release train & tag it')
    fireEvent.change(goalField()!, { target: { value: 'Edited goal' } })
    expect(goalField()?.value).toBe('Edited goal')
    await waitFor(() => expect(currentUrl()).not.toMatch(/create=|name=|goal=/))
    expect(api.createKirocrewAgent).not.toHaveBeenCalled()
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it('a deep link with a malformed percent-escape opens the card without throwing', async () => {
    await renderPage(assistantAndAlpha(), 'kirocrew', { route: '/members?create=1&goal=%E0%A4%A' })
    await waitFor(() => expect(cardOpen()).toBe(true))
    expect(api.createKirocrewAgent).not.toHaveBeenCalled()
  })

  it('Create on a prefilled card posts that name and goal, asks for the first greeting, and opens the new crewmate', async () => {
    await openAssistant()
    fireEvent.click(screen.getByTestId('chat-link-proposal'))
    await waitFor(() => expect(cardOpen()).toBe(true))
    ;(api.createKirocrewAgent as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ ok: true, name: 'scout', member_id: 'm-scout' })
    ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: [...assistantAndAlpha(), row({ name: 'scout', slug: 'scout' })], default_agent: 'kirocrew' })
    fireEvent.click(within(card()).getByTestId('crewmate-create-submit'))
    await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))
    expect(api.createKirocrewAgent).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'scout', description: 'Watch the release train', first_greeting: true }),
    )
    await waitFor(() => expect(currentUrl()).toBe('/members?member=scout'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-scout'))
    expect(cardOpen()).toBe(false)
    // No schedule is written by the card: when it runs is settled in the chat.
    expect(api.createCron).not.toHaveBeenCalled()
    // The greeting is the crewmate's own turn, never a message sent for the user.
    expect(api.sendChat).not.toHaveBeenCalled()
    noGuidedFlow()
  })

  it('malformed, foreign and modified clicks pass through untouched and never throw', async () => {
    await openAssistant()
    const through = passedThrough()
    try {
      expect(() => fireEvent.click(screen.getByTestId('chat-link-malformed'))).not.toThrow()
      fireEvent.click(screen.getByTestId('chat-link-foreign'))
      fireEvent.click(screen.getByTestId('chat-link-proposal'), { metaKey: true })
      expect(through.seen).toEqual(['http://[::1', 'https://elsewhere.example/members?create=1&name=evil', expect.stringContaining('/members?create=1')])
      expect(cardOpen()).toBe(false)
    } finally {
      through.stop()
    }
  })

  it('only Captain\'s chat intercepts: a crewmate chat\'s create link is left to the chat', async () => {
    await renderPage(assistantAndAlpha(), 'kirocrew', { route: '/members?member=alpha' })
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-alpha')
    const through = passedThrough()
    try {
      fireEvent.click(screen.getByTestId('chat-link-proposal'))
      expect(through.seen).toHaveLength(1)
      expect(cardOpen()).toBe(false)
    } finally {
      through.stop()
    }
  })

  it('a new proposal replaces an untouched card at once; an edited card is replaced only once the user agrees to leave it', async () => {
    await openAssistant()
    const through = passedThrough()
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    try {
      fireEvent.click(screen.getByTestId('chat-link-proposal'))
      await waitFor(() => expect(cardOpen()).toBe(true))
      // Untouched: the second proposal replaces the first, nothing asked.
      fireEvent.click(screen.getByTestId('chat-link-proposal-2'))
      await waitFor(() => expect(goalField()?.value).toBe('Second idea'))
      expect(nameField().value).toBe('')
      expect(confirm).not.toHaveBeenCalled()
      // Edited: the next proposal asks, and a refusal keeps the user's words.
      fireEvent.change(goalField()!, { target: { value: 'My own words' } })
      fireEvent.click(screen.getByTestId('chat-link-proposal'))
      await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1))
      expect(goalField()?.value).toBe('My own words')
      // Agreed: the proposal is shown.
      confirm.mockReturnValue(true)
      fireEvent.click(screen.getByTestId('chat-link-proposal'))
      await waitFor(() => expect(goalField()?.value).toBe('Watch the release train'))
      expect(nameField().value).toBe('scout')
      expect(confirm).toHaveBeenCalledTimes(2)
      expect(api.createKirocrewAgent).not.toHaveBeenCalled()
    } finally {
      confirm.mockRestore()
      through.stop()
    }
  })

  it('a create link the guide already asked about replaces an edited card without asking again', async () => {
    await openAssistant()
    await clickAddAdvanced()
    await screen.findByTestId('crewmate-create-embedded')
    fireEvent.change(nameField(), { target: { value: 'half-typed' } })
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    try {
      // Unasked: the page asks once, and a refusal keeps the card.
      fireEvent.click(screen.getByTestId('guide-nav-plain'))
      await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1))
      expect(nameField().value).toBe('half-typed')
      // Asked already by the guide: no second question, the proposal fills the card.
      fireEvent.click(screen.getByTestId('guide-nav-granted'))
      await waitFor(() => expect(nameField().value).not.toBe('half-typed'))
      expect(confirm).toHaveBeenCalledTimes(1)
      noGuidedFlow()
    } finally {
      confirm.mockRestore()
    }
  })

  it('a hidden chat behind a creation surface is not the viewed thread, and is again once the chat returns', async () => {
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    await openAssistant()
    await waitFor(() => expect(getViewedThreadSlot()).toBe('member-kirocrew-captain'))
    await clickAddAdvanced()
    await screen.findByTestId('crewmate-create-embedded')
    // Messages arriving now must badge: nobody is looking at this chat.
    await waitFor(() => expect(getViewedThreadSlot()).toBeNull())
    fireEvent.click(within(screen.getByTestId('crewmate-create-embedded')).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(getViewedThreadSlot()).toBe('member-kirocrew-captain'))
  })

  it('Advanced opens the full form IN PLACE of the chat: chat and side panel hidden, chat still mounted', async () => {
    await openAssistant()
    const stub = screen.getByTestId('chat-pane-stub')
    const mountsBefore = chatPaneMounts.count
    await clickAddAdvanced()
    await screen.findByTestId('crewmate-create-embedded')
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(hiddenByStyle(stub)).toBe(true)
    const panel = screen.queryByTestId('member-side-panel')
    if (panel) expect(panel.closest('.hidden')).not.toBeNull()
    expect(isHiddenClass(screen.getByTestId('member-create-advanced'))).toBe(false)
    fireEvent.click(within(screen.getByTestId('crewmate-create-embedded')).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByTestId('crewmate-create-embedded')).toBeNull())
    expect(isHiddenClass(pane())).toBe(true)
    expect(screen.getByTestId('chat-pane-stub')).toBe(stub)
    expect(hiddenByStyle(stub)).toBe(false)
    expect(chatPaneMounts.count).toBe(mountsBefore)
  })

  it('the Assistant\'s header shows its label without the raw `kirocrew-captain` ID; a labelled crewmate still shows its ID', async () => {
    const utils = await renderPage([defaultRow(), assistantRow(), row({ name: 'oncall', slug: 'oncall', display_name: 'Oncall Sentinel' })])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-kirocrew-captain')
    const title = screen.getByTestId('member-title-row')
    expect(title).toHaveTextContent('Captain')
    expect(within(title).queryByTestId('member-pill-id')).toBeNull()
    expect(title).not.toHaveTextContent(/\bassistant\b/)
    utils.unmount()
    await renderPage([defaultRow(), assistantRow(), row({ name: 'oncall', slug: 'oncall', display_name: 'Oncall Sentinel' })], 'kirocrew', { route: '/members?member=oncall' })
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    expect(within(screen.getByTestId('member-title-row')).getByTestId('member-pill-id')).toHaveTextContent('oncall')
  })

  it('choosing a roster chat while an untouched card is up shows that chat without asking', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    try {
      await openAssistant()
      fireEvent.click(screen.getByTestId('chat-link-proposal'))
      await waitFor(() => expect(cardOpen()).toBe(true))
      fireEvent.click(await rosterRow('alpha'))
      await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-alpha'))
      expect(cardOpen()).toBe(false)
      expect(confirm).not.toHaveBeenCalled()
      expect(hiddenByStyle(screen.getByTestId('chat-pane-stub'))).toBe(false)
    } finally {
      confirm.mockRestore()
    }
  })
})

describe('resolveDefaultMember', () => {
  const ordered = [
    row({ name: 'alpha', slug: 'alpha', last_active_ts: 100 }),
    row({ name: 'beta', slug: 'beta', last_active_ts: 200 }),
  ]

  it('nothing remembered -> the crewmate with the greatest last_active_ts', () => {
    // Product decision (CrewMates launch review): the "pick one" landing is
    // gone — the most recently USED crewmate opens by default.
    expect(resolveDefaultMember(null, ordered)?.name).toBe('beta')
    expect(resolveDefaultMember('', ordered)?.name).toBe('beta')
  })

  it('a tie in last_active_ts keeps the first crewmate in `ordered`', () => {
    const tied = [
      row({ name: 'alpha', slug: 'alpha', last_active_ts: 100 }),
      row({ name: 'beta', slug: 'beta', last_active_ts: 100 }),
    ]
    expect(resolveDefaultMember(null, tied)?.name).toBe('alpha')
  })

  it('restore: the remembered crewmate wins over the most-recently-used one', () => {
    // alpha is remembered even though beta has the greater last_active_ts.
    expect(resolveDefaultMember('alpha', ordered)?.name).toBe('alpha')
  })

  it('stale: a remembered crewmate that is gone falls back to the most-recently-used one', () => {
    expect(resolveDefaultMember('ghost', ordered)?.name).toBe('beta')
  })
  it('does not auto-open the built-in default member as a crewmate', () => {
    const defaultOnly = [row({ name: 'default', slug: 'default', last_active_ts: 999 })]
    expect(resolveDefaultMember(null, defaultOnly)).toBeUndefined()
    expect(resolveDefaultMember('default', defaultOnly)).toBeUndefined()
  })

  it('the built-in Assistant is the landing when no crewmate has been used, and a remembered or more recent one wins', () => {
    const withAssistant = [
      row({ name: 'alpha', slug: 'alpha' }),
      defaultRow({ last_active_ts: 999 }),
      assistantRow(),
      row({ name: 'beta', slug: 'beta' }),
    ]
    expect(resolveDefaultMember(null, withAssistant)?.name).toBe('kirocrew-captain')
    expect(resolveDefaultMember('beta', withAssistant)?.name).toBe('beta')
    // `default` is never remembered or landed on, even when it is the most used.
    expect(resolveDefaultMember('default', withAssistant)?.name).toBe('kirocrew-captain')
    const used = [assistantRow({ last_active_ts: 10 }), row({ name: 'alpha', slug: 'alpha', last_active_ts: 20 })]
    expect(resolveDefaultMember(null, used)?.name).toBe('alpha')
    expect(resolveDefaultMember(null, [defaultRow(), assistantRow()])?.name).toBe('kirocrew-captain')
    // A same-named member on another template is not the Assistant.
    const impostor = [row({ name: 'alpha', slug: 'alpha' }), row({ name: 'kirocrew-captain', slug: 'kirocrew-captain', kiro_agent: 'kirocrew' })]
    expect(resolveDefaultMember(null, impostor)?.name).toBe('alpha')
  })

  it('an empty roster resolves to undefined, never throws', () => {
    expect(resolveDefaultMember('beta', [])).toBeUndefined()
    expect(resolveDefaultMember(null, [])).toBeUndefined()
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

  it('opening the built-in default does not replace the remembered crewmate', async () => {
    localStorage.setItem(LAST_MEMBER_KEY, 'alpha')
    await renderPage([
      row({ name: 'default', slug: 'default' }),
      row({ name: 'alpha', slug: 'alpha' }),
    ], 'kirocrew', { route: '/members?member=default' })

    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-default')
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('alpha')
  })

  it('opening the Assistant is a selection like any other: it is remembered and a returning visit reopens it', async () => {
    localStorage.setItem(LAST_MEMBER_KEY, 'alpha')
    const utils = await renderPage([
      defaultRow(),
      assistantRow(),
      row({ name: 'alpha', slug: 'alpha', last_active_ts: 500 }),
    ], 'kirocrew', { route: '/members?member=kirocrew-captain' })

    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-kirocrew-captain')
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('kirocrew-captain')
    // A returning bare visit: the remembered Assistant wins over the more
    // recently used crewmate.
    utils.unmount()
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockClear()
    await renderPage([
      defaultRow(),
      assistantRow(),
      row({ name: 'alpha', slug: 'alpha', last_active_ts: 500 }),
    ])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-kirocrew-captain')
    expect(api.memberThread).not.toHaveBeenCalledWith('alpha')
  })

  it('a returning user keeps the last chat: a remembered crewmate is not hijacked by the Assistant', async () => {
    localStorage.setItem(LAST_MEMBER_KEY, 'alpha')
    await renderPage([defaultRow(), assistantRow(), row({ name: 'alpha', slug: 'alpha' })])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-alpha')
    expect(api.memberThread).not.toHaveBeenCalledWith('kirocrew-captain')
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
    // The Dashboard tab beside the pane settles on its own read; an alert it
    // raised would land after the pane.
    await screen.findByTestId('crew-webview-empty', undefined, PANE_READY)
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
    // The Dashboard tab beside the pane settles on its own member-panel read.
    await screen.findByTestId('crew-webview-empty', undefined, PANE_READY)
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
    await waitFor(() => expect(currentUrl()).toBe('/members'))
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
      await waitFor(() => expect(currentUrl()).toBe('/members?member=alpha'))
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
    // case in the below-md block).
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
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-beta'), PANE_READY)
    fireEvent.click(await rosterRow('alpha'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-alpha'), PANE_READY)
    expect(currentUrl()).toBe('/members?member=alpha')
    // One Back: off the page — the switches replaced, they did not stack.
    fireEvent.click(screen.getByTestId('history-back'))
    await waitFor(() => expect(currentUrl()).toBe('/elsewhere'))
    expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
    // The memory still holds the last member the user chose.
    expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('alpha')
  })

  describe('below md', () => {
    // Restored BY VALUE, so the override never outlives its case: happy-dom
    // exposes `window.matchMedia` through an accessor whose setter the
    // assignment writes through, so re-defining a saved descriptor keeps the
    // mock and every later desktop case reads as a phone (useIsMobile re-keys on
    // the function's identity).
    let realMatchMedia: typeof window.matchMedia
    beforeEach(() => {
      realMatchMedia = window.matchMedia
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
      window.matchMedia = realMatchMedia
    })

    it('a bare visit opens the remembered chat like md+; an explicit ?view=roster is the roster and never auto-opens', async () => {
      localStorage.setItem(LAST_MEMBER_KEY, 'beta')
      const utils = await renderPage(alphaBeta())
      expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-beta')
      expect(currentUrl()).toBe('/members?member=beta')
      utils.unmount()
      ;(api.memberThread as ReturnType<typeof vi.fn>).mockClear()
      await renderPage(alphaBeta(), 'kirocrew', { route: '/members?view=roster' })
      await rosterRow('alpha')
      // Let the roster settle: nothing may open behind it.
      await act(async () => { await new Promise((r) => setTimeout(r, 50)) })
      expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
      expect(api.memberThread).not.toHaveBeenCalled()
      expect(currentUrl()).toBe('/members?view=roster')
    })

    it('a bare first visit with nothing used opens the Assistant', async () => {
      await renderPage([row({ name: 'alpha', slug: 'alpha' }), defaultRow(), assistantRow()])
      expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-kirocrew-captain')
      expect(currentUrl()).toBe('/members?member=kirocrew-captain')
    })

    it('a save from the team view keeps the pushed entry, so Back still leaves the page in one press', async () => {
      // Driven history: a page before /members, a real push into it, the team
      // header's PUSH (fromRoster), the Edit team dialog's Save -- which
      // re-opens the saved team with a REPLACE -- then the view's own Back and
      // one more. The replace must keep `fromRoster`: without it the view's
      // Back writes a second roster entry instead of popping, and leaving the
      // page takes two presses.
      vi.mocked(api.teams.list).mockResolvedValue({ teams: [{ id: 'abc123abc123', name: 'Triage', members: ['oncall'] }] })
      vi.mocked(api.teams.update).mockResolvedValue({ team: { id: 'abc123abc123', name: 'Release', members: ['oncall'] } })
      ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: [row({ name: 'oncall', slug: 'oncall' })], default_agent: 'kirocrew' })
      function Elsewhere() {
        const nav = useNavigate()
        return (
          <button data-testid="go-members" onClick={() => nav('/members?view=roster')}>
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
        <NavigationLeaveGuardProvider>
          <Routes>
            <Route path="/elsewhere" element={<Elsewhere />} />
            <Route path="/members" element={<MembersPage />} />
          </Routes>
          <BackProbe />
          <LocationProbe />
        </NavigationLeaveGuardProvider>,
        { route: '/elsewhere' },
      )
      fireEvent.click(screen.getByTestId('go-members'))
      await rosterRow('oncall')
      fireEvent.click((await screen.findAllByTestId('team-group-header'))[0])
      const view = await screen.findByTestId('team-view')
      expect(currentUrl()).toBe('/members?team=abc123abc123')
      fireEvent.click(within(view).getByTestId('team-edit'))
      await screen.findByTestId('team-dialog-body')
      fireEvent.change(screen.getByTestId('team-dialog-name'), { target: { value: 'Release' } })
      fireEvent.click(screen.getByTestId('team-dialog-save'))
      await waitFor(() => expect(api.teams.update).toHaveBeenCalledTimes(1))
      await waitFor(() => expect(screen.queryByTestId('team-dialog-body')).toBeNull())
      expect(currentUrl()).toBe('/members?team=abc123abc123')
      // The view's Back pops the pushed entry: the explicit roster, still on the page.
      fireEvent.click(screen.getByTestId('team-back'))
      await waitFor(() => expect(currentUrl()).toBe('/members?view=roster'))
      expect(screen.queryByTestId('team-view')).toBeNull()
      // One more Back: off the page -- the save did not stack a second roster entry.
      fireEvent.click(screen.getByTestId('history-back'))
      await waitFor(() => expect(currentUrl()).toBe('/elsewhere'))
    })

    it('a create link\'s card takes the whole screen below md and its close returns to the originating chat', async () => {
      await renderPage([row({ name: 'alpha', slug: 'alpha' }), defaultRow(), assistantRow()])
      expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-kirocrew-captain')
      await followBlankCreateLink()
      const pane = screen.getByTestId('member-create-pane')
      await waitFor(() => expect(pane.className).not.toMatch(/(^|\s)hidden(\s|$)/))
      expect(screen.getByTestId('member-roster').className).toMatch(/(^|\s)hidden(\s|$)/)
      fireEvent.click(within(await within(pane).findByTestId('crewmate-create-embedded')).getByRole('button', { name: 'Cancel' }))
      await waitFor(() => expect(pane.className).toMatch(/(^|\s)hidden(\s|$)/))
      expect(currentUrl()).toBe('/members?member=kirocrew-captain')
      expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-kirocrew-captain')
    })

    it('a stale ?member= returns to the roster and says where the member went', async () => {
      await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=ghost' })
      await rosterRow('alpha')
      await waitFor(() => expect(currentUrl()).toBe('/members?view=roster'))
      expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
      expect(api.memberThread).not.toHaveBeenCalled()
      // The roster is the answer surface here, so the notice sits above it.
      const notice = screen.getByTestId('member-gone-roster-notice')
      expect(notice).toHaveTextContent('“ghost” is no longer on the roster')
      expect(notice).toHaveAttribute('role', 'status')
      // Tapping a member retires it.
      fireEvent.click(await rosterRow('beta'))
      await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-beta'), PANE_READY)
      expect(screen.queryByTestId('member-gone-roster-notice')).toBeNull()
    })

    it('tapping a member opens it; the header back POPS the entry the roster pushed', async () => {
      await renderPage(alphaBeta(), 'kirocrew', { route: '/members?view=roster' })
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
      // Back is an explicit roster, so the arrival auto-open cannot reopen it.
      expect(currentUrl()).toBe('/members?view=roster')
      expect(navigateSpy).not.toHaveBeenCalledWith(-1)
      await act(async () => { await new Promise((r) => setTimeout(r, 50)) })
      expect(screen.queryByTestId('chat-pane-stub')).toBeNull()
      expect(currentUrl()).toBe('/members?view=roster')
    })

    it('leaving the thread with Profile open does not carry the card to the next crewmate', async () => {
      // The phone's Back clears the member param; `profile` stayed non-null
      // across it, so the next crewmate opened with the card already up.
      await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=alpha' })
      await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
      fireEvent.click(await screen.findByTestId('member-identity-pill'))
      await screen.findByTestId('crew-profile-modal')
      fireEvent.click(screen.getByTestId('member-back'))
      // Back writes the explicit roster view, which never auto-opens a chat.
      await waitFor(() => expect(currentUrl()).toBe('/members?view=roster'))
      fireEvent.click(await rosterRow('beta'))
      await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-beta'), PANE_READY)
      expect(screen.queryByTestId('crew-profile-modal')).toBeNull()
      expect(screen.getByTestId('member-identity-pill')).toHaveAttribute('aria-expanded', 'false')
    })

    it('floats Profile instead of adding its fixed-width column on a 320px phone', async () => {
      setWindowWidth(320)
      await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=beta' })
      await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
      fireEvent.click(await screen.findByTestId('member-identity-pill'))

      expect(await screen.findByTestId('crew-profile-modal')).toBeInTheDocument()
      expect(screen.getByTestId('crew-profile-card')).toBeInTheDocument()
      expect(screen.queryByTestId('crew-profile-docked')).toBeNull()
    })

    it('the overlay fills the phone: the panel is handed the window width, not left to a 100% it cannot resolve (#9979)', async () => {
      // 390 is the audit's phone frame. Below SIDE_PANEL_MIN_W the panel would
      // clamp up to its minimum instead; 390 is above it, so the width the
      // panel carries must be the window's own.
      setWindowWidth(390)
      await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=beta' })
      expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-beta')
      fireEvent.click(screen.getByTestId('member-panel-toggle'))
      const summary = await screen.findByTestId('member-dashboard')
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
