/* MembersPage below md: the roster drawer and its gestures, Back over the
 * drawer, the shell bar slot, and the phone layout of Profile and the side
 * panel.
 * This file, MembersPage.test.tsx and the other files that import
 * src/test/MembersPageTestUtils.tsx (their shared fixtures) carry the same api
 * mock and module stubs below, word for word: change them together. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { Route, Routes, useNavigate } from 'react-router-dom'
import { renderWithProviders } from '../../test/helpers'
import { scopeOpen } from '../../guide/liveRegistry'
import { NavigationLeaveGuardProvider } from '../../components/NavigationLeaveGuard'
import { memberProjectionStore } from '../../state/memberProjectionStore'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
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
import { MobileRosterBarContext } from '../../components/MobileNavRailContext'
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
  echoThread,
  PANE_READY,
  realCreateClock,
  renderPage,
  LocationProbe,
  currentUrl,
  rosterRow,
  clickAddCrewmate,
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

describe('MembersPage default member, memory and URL', () => {
  const alphaBeta = () => [row({ name: 'alpha', slug: 'alpha' }), row({ name: 'beta', slug: 'beta' })]

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

    /** Slides the roster drawer in from the header's back control and returns
     *  the drawer (the roster aside is portaled into it). */
    const openRosterDrawer = async () => {
      fireEvent.click(screen.getByTestId('member-back'))
      return await screen.findByTestId('roster-drawer')
    }
    /** A horizontal drag across the page root, well inside both 24px platform
     *  edge bands and on one row, so the axis lock reads it as a swipe. */
    const drag = (from: number, to: number) => {
      const el = screen.getByTestId('members-page')
      const at = (x: number) => ({ clientX: x, clientY: 400 })
      const mid = from + (to - from) / 3
      fireEvent.touchStart(el, { touches: [at(from)] })
      fireEvent.touchMove(el, { touches: [at(mid)] })
      fireEvent.touchMove(el, { touches: [at(to)] })
      fireEvent.touchEnd(el, { changedTouches: [at(to)] })
    }

    it('a rightward drag over the chat reveals the roster drawer; a leftward one reveals the side panel', async () => {
      setWindowWidth(390)
      await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=alpha' })
      await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
      drag(120, 330)
      const drawer = await screen.findByTestId('roster-drawer')
      expect(within(drawer).getByTestId('member-roster')).toBeTruthy()
      expect(screen.queryByTestId('member-side-panel')).toBeNull()
      // Dragging it back shut, and only then the other way, opens the panel.
      drag(330, 60)
      await waitFor(() => expect(screen.queryByTestId('roster-drawer')).toBeNull(), { timeout: 3000 })
      drag(330, 80)
      const overlay = await screen.findByTestId('member-side-panel')
      expect(overlay).toHaveAttribute('data-placement', 'overlay')
      await screen.findByTestId('member-dashboard')
      expect(screen.queryByTestId('roster-drawer')).toBeNull()
      // And a rightward drag on the open panel puts it away again.
      drag(80, 330)
      await waitFor(() => expect(screen.queryByTestId('member-side-panel')).toBeNull(), { timeout: 3000 })
    })

    it('lands on the remembered crewmate like a wide window, with the roster waiting in a closed drawer', async () => {
      localStorage.setItem(LAST_MEMBER_KEY, 'beta')
      await renderPage(alphaBeta())
      expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-beta')
      expect(currentUrl()).toBe('/members?member=beta')
      // The chat is the screen: the roster renders only inside the drawer.
      expect(screen.queryByTestId('roster-drawer')).toBeNull()
      expect(screen.queryByTestId('member-roster')).toBeNull()
      // The page binds its own drawers, so it claims the drag from the shell's.
      expect(screen.getByTestId('members-page')).toHaveAttribute('data-owns-swipe', 'left right')
      // The header control opens that drawer, so it wears the Sessions page's
      // drawer toggle (a panel glyph), not a back arrow, and says it is closed.
      const toggle = screen.getByTestId('member-back')
      expect(toggle.querySelector('.pi-pane')).not.toBeNull()
      expect(toggle.querySelector('.lucide-arrow-left')).toBeNull()
      // A screen reader hears a toggle too, not "Back", in either state.
      expect(toggle).toHaveAccessibleName('Toggle crewmates')
      expect(toggle).toHaveAttribute('aria-expanded', 'false')
      fireEvent.click(toggle)
      await screen.findByTestId('roster-drawer')
      expect(toggle).toHaveAttribute('aria-expanded', 'true')
    })

    it('a never-chatted Mate opens below md too; its header Back slides the roster in and Mate stays open', async () => {
      localStorage.setItem(LAST_MEMBER_KEY, 'beta')
      await renderPage([...alphaBeta(), assistantRow({ has_dm_message: false })])
      expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-mate')
      expect(currentUrl()).toBe('/members?member=mate')
      const drawer = await openRosterDrawer()
      expect(within(drawer).getByTestId('member-roster')).toBeTruthy()
      expect(currentUrl()).toBe('/members?member=mate')
      expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-mate')
    })

    it('a drag the Dashboard frame relays closes the open side panel; nothing else does', async () => {
      // A touch over an iframe goes to the frame's own document and never reaches
      // the page root the swipe listens on, so the frame relays its touches
      // (`relayTouches`) and the page replays them onto the frame element.
      setWindowWidth(390)
      await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=alpha' })
      await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
      fireEvent.click(screen.getByTestId('member-panel-toggle'))
      const host = await screen.findByTestId('member-dashboard')
      const frame = document.createElement('iframe')
      host.appendChild(frame)
      const relay = (phase: string, x: number, source: Window | null = frame.contentWindow) =>
        act(() => {
          window.dispatchEvent(new MessageEvent('message', { data: { type: 'mc-frame-touch', phase, x, y: 300 }, source }))
        })
      // A window that is not a frame in the panel posting the same shape is ignored.
      const stranger = document.createElement('iframe')
      document.body.appendChild(stranger)
      for (const [phase, x] of [['start', 120], ['move', 200], ['move', 340], ['end', 340]] as const) relay(phase, x, stranger.contentWindow)
      expect(screen.getByTestId('member-side-panel')).toBeInTheDocument()
      for (const [phase, x] of [['start', 120], ['move', 200], ['move', 340], ['end', 340]] as const) relay(phase, x)
      await waitFor(() => expect(screen.queryByTestId('member-side-panel')).toBeNull(), { timeout: 3000 })
      stranger.remove()
    })

    it('the header back slides the drawer in on a duplicate entry; a pick spends that entry before switching', async () => {
      await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=alpha' })
      expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-alpha')
      navigateSpy.mockClear()
      await openRosterDrawer()
      // One push: a bare copy of the address, so Back has a drawer to dismiss.
      expect(navigateSpy).toHaveBeenCalledTimes(1)
      expect(navigateSpy.mock.calls[0][0]).toMatchObject({ pathname: '/members', search: '?member=alpha' })
      expect(currentUrl()).toBe('/members?member=alpha')
      fireEvent.click(await rosterRow('beta'))
      await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-beta'), PANE_READY)
      // The duplicate was popped FIRST and the switch replaced the entry under it,
      // so the page still holds one entry however many crewmates are visited.
      expect(navigateSpy).toHaveBeenCalledWith(-1)
      expect(currentUrl()).toBe('/members?member=beta')
      await waitFor(() => expect(screen.queryByTestId('roster-drawer')).toBeNull(), { timeout: 3000 })
      expect(localStorage.getItem(LAST_MEMBER_KEY)).toBe('beta')
    })

    it('Back with the drawer open closes it and stays on the crewmate; one more Back leaves the page', async () => {
      ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: alphaBeta(), default_agent: 'kirocrew' })
      ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementation(echoThread)
      function Elsewhere() {
        const nav = useNavigate()
        return (
          <button data-testid="go-members" onClick={() => nav('/members?member=alpha')}>
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
      expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-alpha')
      await openRosterDrawer()
      fireEvent.click(screen.getByTestId('history-back'))
      await waitFor(() => expect(screen.queryByTestId('roster-drawer')).toBeNull(), { timeout: 3000 })
      expect(currentUrl()).toBe('/members?member=alpha')
      expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-alpha')
      // The drawer's entry is spent: the next Back is off the page.
      fireEvent.click(screen.getByTestId('history-back'))
      await waitFor(() => expect(currentUrl()).toBe('/elsewhere'), PANE_READY)
    })

    it('a scrim tap closes the drawer and spends its entry without switching', async () => {
      await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=alpha' })
      await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
      await openRosterDrawer()
      navigateSpy.mockClear()
      fireEvent.click(screen.getByTestId('roster-drawer-backdrop'))
      expect(navigateSpy).toHaveBeenCalledWith(-1)
      await waitFor(() => expect(screen.queryByTestId('roster-drawer')).toBeNull(), { timeout: 3000 })
      expect(currentUrl()).toBe('/members?member=alpha')
    })

    it('a team header in the drawer opens the team view, and the view\'s Back slides the roster in again', async () => {
      vi.mocked(api.teams.list).mockResolvedValue({ teams: [{ id: 'abc123abc123', name: 'Triage', members: ['alpha'] }] })
      await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=alpha' })
      await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
      const drawer = await openRosterDrawer()
      fireEvent.click((await within(drawer).findAllByTestId('team-group-header'))[0])
      await screen.findByTestId('team-view')
      await waitFor(() => expect(currentUrl()).toBe('/members?team=abc123abc123'))
      await waitFor(() => expect(screen.queryByTestId('roster-drawer')).toBeNull(), { timeout: 3000 })
      fireEvent.click(screen.getByTestId('team-back'))
      expect(await screen.findByTestId('roster-drawer')).toBeTruthy()
      expect(currentUrl()).toBe('/members?team=abc123abc123')
    })

    it('with a team open the guide sees the phone roster only while its drawer is open', async () => {
      // The team view keeps the roster in the drawer, so a closed drawer is no
      // roster on screen even though no crewmate is open.
      vi.mocked(api.teams.list).mockResolvedValue({ teams: [{ id: 'abc123abc123', name: 'Triage', members: ['alpha'] }] })
      await renderPage(alphaBeta(), 'kirocrew', { route: '/members?team=abc123abc123' })
      await screen.findByTestId('team-view')
      expect(screen.queryByTestId('roster-drawer')).toBeNull()
      await waitFor(() => expect(scopeOpen('members.roster-phone')).toBe(false))
      fireEvent.click(screen.getByTestId('team-back'))
      expect(await screen.findByTestId('roster-drawer')).toBeTruthy()
      await waitFor(() => expect(scopeOpen('members.roster-phone')).toBe(true))
    })

    it('a New crewmate card takes the whole screen below md and its close returns to the originating chat', async () => {
      await renderPage([row({ name: 'alpha', slug: 'alpha' }), defaultRow()], 'kirocrew', { route: '/members?member=alpha' })
      expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-alpha')
      // The roster's "+" lives in the drawer below md.
      await openRosterDrawer()
      navigateSpy.mockClear()
      await clickAddCrewmate()
      // Opened on the drawer's own duplicate entry, so the close spends it.
      await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith(-1))
      // The modal is responsive: below md its DialogContent fills the frame
      // (w-[calc(100%-4rem)]). It renders as a role="dialog".
      const dialog = await screen.findByRole('dialog')
      // The creation surface retires the drawer and the roster yields with it.
      await waitFor(() => expect(screen.queryByTestId('roster-drawer')).toBeNull(), { timeout: 3000 })
      expect(screen.getByTestId('member-roster').className).toMatch(/(^|\s)hidden(\s|$)/)
      fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }))
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
      expect(currentUrl()).toBe('/members?member=alpha')
      expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-alpha')
    })

    it('a guide pushing the create link over the open drawer closes it without popping the guide\'s entry', async () => {
      await renderPage([row({ name: 'alpha', slug: 'alpha' }), defaultRow()], 'kirocrew', { route: '/members?member=alpha' })
      await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
      await openRosterDrawer()
      navigateSpy.mockClear()
      fireEvent.click(screen.getByTestId('guide-nav-granted'))
      await screen.findByRole('dialog')
      await waitFor(() => expect(screen.queryByTestId('roster-drawer')).toBeNull(), { timeout: 3000 })
      // The pop would have taken the guide's entry, not the drawer's duplicate.
      expect(navigateSpy).not.toHaveBeenCalledWith(-1)
    })

    /** A page before /members, so one Back can be seen leaving the page. */
    function renderFromElsewhere(go: { to: string; state?: unknown }) {
      function Elsewhere() {
        const nav = useNavigate()
        return (
          <button data-testid="go-members" onClick={() => nav(go.to, go.state ? { state: go.state } : undefined)}>
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
    }

    it('on the leave guard\'s own duplicate entry the drawer pushes and pops nothing, so the guard stays armed', async () => {
      // NavigationBackGuard parks a duplicate entry marked as its trap while a
      // draft is unsaved. A drawer entry above it would be popped back onto the
      // trap on close, which the guard reads as a press to carry past.
      ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: alphaBeta(), default_agent: 'kirocrew' })
      ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementation(echoThread)
      renderFromElsewhere({ to: '/members?member=alpha', state: { __navigationLeaveTrap: true } })
      expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-alpha')
      navigateSpy.mockClear()
      await openRosterDrawer()
      expect(navigateSpy).not.toHaveBeenCalled()
      fireEvent.click(screen.getByTestId('roster-drawer-backdrop'))
      await waitFor(() => expect(screen.queryByTestId('roster-drawer')).toBeNull(), { timeout: 3000 })
      expect(navigateSpy).not.toHaveBeenCalled()
      expect(currentUrl()).toBe('/members?member=alpha')
    })

    it('the first crewmate created from an empty roster opens on a replace, so one Back still leaves the page', async () => {
      // A pushed open would leave a bare /members under the chat, and the phone
      // lands a bare /members on a crewmate by itself: Back would bounce back in.
      const membersMock = api.members as ReturnType<typeof vi.fn>
      membersMock.mockResolvedValue({ members: [defaultRow()], default_agent: 'kirocrew' })
      ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementation(echoThread)
      renderFromElsewhere({ to: '/members' })
      fireEvent.click((await screen.findAllByTestId('crewmate-empty-advanced'))[0])
      await screen.findByTestId('crewmate-create-form')
      fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
      membersMock.mockResolvedValue({ members: [defaultRow(), row({ name: 'radar', slug: 'radar' })], default_agent: 'kirocrew' })
      fireEvent.click(screen.getByTestId('crewmate-create-submit'))
      await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-radar'), PANE_READY)
      expect(currentUrl()).toBe('/members?member=radar')
      fireEvent.click(screen.getByTestId('history-back'))
      await waitFor(() => expect(currentUrl()).toBe('/elsewhere'))
    })

    it('a stale ?member= opens a stand-in with the notice above the chat, as on a wide window', async () => {
      await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=ghost' })
      await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
      expect(currentUrl()).not.toBe('/members?member=ghost')
      expect(screen.getByTestId('member-gone-notice')).toHaveTextContent(/^Showing /)
      expect(screen.queryByTestId('member-gone-roster-notice')).toBeNull()
    })

    it('switching from the drawer with Profile open does not carry the card to the next crewmate', async () => {
      await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=alpha' })
      await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
      fireEvent.click(await screen.findByTestId('member-identity-pill'))
      await screen.findByTestId('crew-profile-modal')
      await openRosterDrawer()
      fireEvent.click(await rosterRow('beta'))
      await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-beta'), PANE_READY)
      // The card leaves through its exit animation.
      await waitFor(() => expect(screen.queryByTestId('crew-profile-modal')).toBeNull(), { timeout: 3000 })
      expect(screen.getByTestId('member-identity-pill')).toHaveAttribute('aria-expanded', 'false')
    })

    it('moves the thread header into the shell bar slot when the shell renders one', async () => {
      // The shell renders `#mobile-topbar-slot` on a phone with a crewmate open
      // (App's single bar); the page's header then sits there, not over the pane.
      const slot = document.createElement('div')
      slot.id = 'mobile-topbar-slot'
      document.body.appendChild(slot)
      try {
        await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=alpha' })
        await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
        const header = await screen.findByTestId('member-thread-header')
        expect(slot).toContainElement(header)
        expect(header).toHaveAttribute('data-in-topbar')
        expect(within(slot).getByTestId('member-back')).toBeInTheDocument()
        expect(within(slot).getByTestId('member-identity-pill')).toBeInTheDocument()
      } finally {
        slot.remove()
      }
    })

    it('tells the shell which bar it needs: open for a landing, roster only when it can open no one and on unmount', async () => {
      const opening = vi.fn()
      const first = await renderPage(alphaBeta(), 'kirocrew', { reportRosterBar: opening })
      await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
      // A landing that opens a crewmate never asks for the regular bar back.
      // A landing reports `pending` while it reads, then `open`, and never asks
      // for the regular bar back.
      expect(opening.mock.calls.map((c) => c[0])).toEqual(expect.arrayContaining(['open']))
      expect(opening).not.toHaveBeenCalledWith('roster')
      first.unmount()
      // Unmounting (an error boundary taking the page down, too) hands it back.
      expect(opening).toHaveBeenLastCalledWith('roster')
      const empty = vi.fn()
      await renderPage([defaultRow()], 'kirocrew', { reportRosterBar: empty })
      await screen.findAllByTestId('crewmate-empty-hero')
      expect(empty).toHaveBeenLastCalledWith('roster')
    })

    it('a ?team= link the teams read failed on opens nothing, so it asks for the regular bar', async () => {
      vi.mocked(api.teams.list).mockRejectedValue(new Error('teams unavailable'))
      const report = vi.fn()
      await renderPage(alphaBeta(), 'kirocrew', { route: '/members?team=ghost', reportRosterBar: report })
      await waitFor(() => expect(report).toHaveBeenLastCalledWith('roster'), PANE_READY)
    })

    it('looks the shell bar slot up only once a crewmate is open, so a bar swapped during a slow read is not kept', async () => {
      // The shell may re-render its bar while the roster read is pending,
      // replacing its slot; the crewmate's header must land in the slot that is
      // there once it opens. A slot latched while pending would be the detached
      // first one.
      const first = document.createElement('div')
      first.id = 'mobile-topbar-slot'
      document.body.appendChild(first)
      const second = document.createElement('div')
      let release: (v: unknown) => void = () => {}
      ;(api.members as ReturnType<typeof vi.fn>).mockImplementation(() => new Promise((r) => { release = r }))
      try {
        renderWithProviders(
          <MobileRosterBarContext.Provider value={() => {}}>
            <NavigationLeaveGuardProvider><MembersPage /></NavigationLeaveGuardProvider>
          </MobileRosterBarContext.Provider>,
          { route: '/members' },
        )
        await waitFor(() => expect(api.members).toHaveBeenCalled())
        first.remove()
        second.id = 'mobile-topbar-slot'
        document.body.appendChild(second)
        await act(async () => { release({ members: alphaBeta(), default_agent: 'kirocrew' }) })
        const header = await screen.findByTestId('member-thread-header', undefined, PANE_READY)
        await waitFor(() => expect(second).toContainElement(header))
      } finally {
        first.remove()
        second.remove()
      }
    })

    it('finds the shell bar slot again after the bare roster had the regular bar', async () => {
      // The empty roster gets the regular bar, so the shell's slot goes away;
      // the first crewmate then brings the single bar back with a NEW slot. A
      // page that kept the first slot would portal its header into a detached
      // node, out of sight.
      const first = document.createElement('div')
      first.id = 'mobile-topbar-slot'
      document.body.appendChild(first)
      const second = document.createElement('div')
      try {
        const utils = await renderPage([defaultRow()], 'kirocrew', { reportRosterBar: () => {} })
        await screen.findAllByTestId('crewmate-empty-hero')
        first.remove()
        second.id = 'mobile-topbar-slot'
        document.body.appendChild(second)
        ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: alphaBeta(), default_agent: 'kirocrew' })
        await act(async () => { await utils.queryClient.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY }) })
        const header = await screen.findByTestId('member-thread-header', undefined, PANE_READY)
        await waitFor(() => expect(second).toContainElement(header))
        expect(first).not.toContainElement(header)
      } finally {
        first.remove()
        second.remove()
      }
    })

    it('with nothing to open the roster is the page, and the page claims no drag', async () => {
      await renderPage([defaultRow()], 'kirocrew')
      await screen.findAllByTestId('crewmate-empty-hero')
      expect(screen.queryByTestId('roster-drawer')).toBeNull()
      expect(screen.getByTestId('members-page')).not.toHaveAttribute('data-owns-swipe')
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

    it('the side panel first mounts offscreen, so a non-gesture open never paints it at rest before it slides in', async () => {
      setWindowWidth(390)
      await renderPage(alphaBeta(), 'kirocrew', { route: '/members?member=beta' })
      expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-beta')
      // The transform the slide carries at the moment React inserts it, read
      // synchronously inside the commit: before the open effect re-seats the
      // offset, which for a non-discrete open runs only after a paint.
      let mountedTransform: string | null = null
      const capture = (n: Node) => {
        if (mountedTransform !== null || !(n instanceof HTMLElement)) return
        const slide = n.matches('[data-testid="member-side-panel-slide"]')
          ? n
          : n.querySelector<HTMLElement>('[data-testid="member-side-panel-slide"]')
        if (slide) mountedTransform = slide.style.transform
      }
      const realAppend = Node.prototype.appendChild
      const realInsert = Node.prototype.insertBefore
      const append = vi.spyOn(Node.prototype, 'appendChild').mockImplementation(function <T extends Node>(this: Node, n: T): T {
        capture(n)
        return realAppend.call(this, n) as T
      })
      const insert = vi.spyOn(Node.prototype, 'insertBefore').mockImplementation(function <T extends Node>(this: Node, n: T, ref: Node | null): T {
        capture(n)
        return realInsert.call(this, n, ref) as T
      })
      try {
        fireEvent.click(screen.getByTestId('member-panel-toggle'))
        await screen.findByTestId('member-side-panel')
      } finally {
        append.mockRestore()
        insert.mockRestore()
      }
      expect(mountedTransform).toMatch(/translate(X|3d)\(390px/)
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
