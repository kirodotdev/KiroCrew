/* MembersPage: the desktop two-finger trackpad swipe on the side panel (the
 * drag, its commit rule and settle, the Dashboard frame relay, the native
 * Browser view mark). Split out of MembersPage.sidePanel.test.tsx for the
 * line cap, with its setup. That file, MembersPage.test.tsx and the other
 * files that import src/test/MembersPageTestUtils.tsx (their shared
 * fixtures) carry the same api mock and module stubs below, word for word:
 * change them together. */
import { describe, it, expect, vi, beforeEach, afterEach, onTestFinished } from 'vitest'
import { screen, fireEvent, waitFor, act } from '@testing-library/react'
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
  function ChatPaneStub({ slotKey, agentLocked, followContentWidth, busyMode, topInset, composerInput, ...rest }: { slotKey: string; agentLocked?: boolean; followContentWidth?: boolean; busyMode?: string; topInset?: number; composerInput?: unknown } & Record<string, unknown>) {
    // Mount count: the New crewmate card must HIDE the pane, never unmount it.
    useEffect(() => { chatPaneMounts.count += 1 }, [])
    return (
      <div data-testid="chat-pane-stub" data-agent-locked={agentLocked ? '1' : '0'} data-follow-content-width={followContentWidth ? '1' : '0'} data-busy-mode={busyMode ?? 'split'} data-top-inset={topInset ?? ''} data-crew-composer={composerInput === CrewComposerRef.current ? '1' : '0'}>
        {slotKey}
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

import { resetDashboardOnLoad } from './dashboardOnLoad'
import { swipeTiming, SWIPE_GESTURE_IDLE_MS } from '../../hooks/useTrackpadSwipe'
import { MotionGlobalConfig, frame, frameData } from 'framer-motion'
/** The event time `swipe()` stamps on every event (see there). */
const SWIPE_AT = 1
import { CREW_DASHBOARD_TAB_ID } from './MembersPage'
import { resetFirstGreetingRequests } from './useFirstGreeting'
import { __resetPanelTabs } from '../../hooks/usePanelTabs'
import {
  PANEL_OPEN_KEY,
  WIDE_WINDOW,
  setWindowWidth,
  row,
  PANE_READY,
  realCreateClock,
  renderPage,
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
  describe('a two-finger trackpad swipe moves the side panel on desktop', () => {
    // These cases look at the screen between two halves of one swipe; a slow
    // assertion must not read as the fingers lifting (the real gap is 70ms).
    beforeEach(() => { swipeTiming.idleMs = 400 })
    afterEach(() => { swipeTiming.idleMs = SWIPE_GESTURE_IDLE_MS })
    const dashTab = () => screen.getByTestId(`side-panel-leading-tab-${CREW_DASHBOARD_TAB_ID}`)
    /** A trackpad run: `n` wheel events on `target`, each `dx`/`dy`. Returns
     *  how many the page cancelled. */
    const swipe = (target: Element, dx: number, n = 12, init: WheelEventInit = {}, dy = 0) => {
      let cancelled = 0
      act(() => {
        for (let i = 0; i < n; i++) {
          const ev = new WheelEvent('wheel', { deltaX: dx, deltaY: dy, bubbles: true, cancelable: true, ...init })
          // The test DOM drops modifier keys from a WheelEvent's init.
          if (init.ctrlKey) Object.defineProperty(ev, 'ctrlKey', { value: true })
          // One event time for every call: a batch has no speed, so it never
          // commits mid-drag on speed (rule C); the tests that mean speed stamp it.
          Object.defineProperty(ev, 'timeStamp', { value: SWIPE_AT })
          target.dispatchEvent(ev)
          if (ev.defaultPrevented) cancelled++
        }
      })
      return cancelled
    }
    const openOncall = async () => {
      await renderPage([row({ bound: true, slot_key: 'member-oncall' })], 'kirocrew', { route: '/members?member=oncall' })
      return screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
    }

    /** The sheet's live offset while it slides, px from its resting place. */
    const slideX = () => {
      const el = screen.getByTestId('member-side-panel-slide') as HTMLElement
      const m = /translate(?:X|3d)\((-?[\d.]+)px/.exec(el.style.transform)
      return m ? Number(m[1]) : 0
    }
    const sliding = () => screen.queryByTestId('member-side-panel')?.getAttribute('data-sliding') === 'true'

    it('a leftward run drags a hidden panel in under the fingers, then settles open on Dashboard', async () => {
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      expect(screen.queryByTestId('member-side-panel')).toBeNull()
      // Fingers moving left: a trackpad reports a positive deltaX.
      expect(swipe(chat, 20, 5)).toBe(5)
      // Mid-drag: the sheet is out, and sits as far in as the fingers took it.
      expect(sliding()).toBe(true)
      const travel = window.innerWidth
      await waitFor(() => expect(slideX()).toBeCloseTo(travel - 100, 0))
      swipe(chat, 20, 5)
      await waitFor(() => expect(slideX()).toBeCloseTo(travel - 200, 0))
      // Past the commit share, then the fingers lift: it settles open, docked.
      swipe(chat, 20, 20)
      await waitFor(() => expect(sliding()).toBe(false), { timeout: 3000 })
      expect(screen.getByTestId('member-side-panel')).toHaveAttribute('data-placement', 'docked')
      await waitFor(() => expect(dashTab()).toHaveAttribute('aria-selected', 'true'))
      expect(localStorage.getItem(PANEL_OPEN_KEY)).toBe('1')
    })

    /** The width the chat gives up for the sheet during a desktop drag, px. */
    const spacerW = () => {
      const el = screen.queryByTestId('member-side-panel-spacer') as HTMLElement | null
      return el ? parseFloat(el.style.width || '0') : null
    }

    it('opening: the chat gives up exactly the visible part of the panel', async () => {
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      expect(spacerW()).toBeNull()
      swipe(chat, 20, 5)
      await waitFor(() => expect(spacerW()).toBeCloseTo(100, 0))
      swipe(chat, 20, 5)
      await waitFor(() => expect(spacerW()).toBeCloseTo(200, 0))
      swipe(chat, 20, 20)
      // Settled: the docked column takes the spacer's place.
      await waitFor(() => expect(sliding()).toBe(false), { timeout: 3000 })
      expect(spacerW()).toBeNull()
      expect(screen.getByTestId('member-side-panel')).toHaveAttribute('data-placement', 'docked')
    })

    it("closing: the chat takes the panel's width back as the drag goes", async () => {
      const chat = await openOncall()
      await screen.findByTestId('member-side-panel', undefined, PANE_READY)
      expect(spacerW()).toBeNull()
      // The spacer starts at the whole column and shrinks with the drag.
      swipe(chat, -20, 5)
      await waitFor(() => expect(spacerW()).toBeCloseTo(window.innerWidth - 100, 0))
      swipe(chat, -20, 25)
      await waitFor(() => expect(screen.queryByTestId('member-side-panel')).toBeNull(), { timeout: 3000 })
      expect(spacerW()).toBeNull()
    })

    it('a slide marks the page while it runs (utils/panelSlide), and the mark goes when the panel rests', async () => {
      const chat = await openOncall()
      await screen.findByTestId('member-side-panel', undefined, PANE_READY)
      expect(document.documentElement.hasAttribute('data-panel-sliding')).toBe(false)
      swipe(chat, -20, 5)
      expect(sliding()).toBe(true)
      expect(document.documentElement.hasAttribute('data-panel-sliding')).toBe(true)
      swipe(chat, -20, 25)
      await waitFor(() => expect(screen.queryByTestId('member-side-panel')).toBeNull(), { timeout: 3000 })
      expect(document.documentElement.hasAttribute('data-panel-sliding')).toBe(false)
    })

    it('a new run grabs an opening settle mid-way: it stops there, follows from that offset, and the rule decides again', async () => {
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      const travel = window.innerWidth
      const paint = manualClocks()
      const lift = () => act(() => {
        vi.advanceTimersByTime(swipeTiming.idleMs)
        vi.advanceTimersToNextFrame()
      })
      let t = performance.now()
      // 400px in, slowly (no speed commit), then the fingers lift: past the
      // share, so it settles open.
      wheelSeq(chat, steps(20, 20, t, 80))
      t += 20 * 80
      await paint()
      lift()
      // One frame into the settle.
      await paint(1000 / 60)
      const mid = slideX()
      expect(sliding()).toBe(true)
      expect(mid).toBeLessThan(travel - 400)
      expect(mid).toBeGreaterThan(0)
      // A rightward run now, in two halves, slowly: it stops the settle and
      // follows the fingers 1:1 from where it was (input lands in a frame not
      // yet painted, so the first may still carry one frame of the settle).
      frameData.timestamp += 1000 / 60
      wheelSeq(chat, steps(-20, 5, t, 80))
      t += 5 * 80
      await paint()
      const first = slideX()
      // 100px out from where the settle stood (at most one frame past `mid`).
      expect(first).toBeLessThanOrEqual(mid + 100 + 1)
      expect(first).toBeGreaterThan(100)
      frameData.timestamp += 1000 / 60
      wheelSeq(chat, steps(-20, 10, t, 80))
      await paint()
      expect(slideX() - first).toBeCloseTo(200, 0)
      expect(spacerW()).toBeCloseTo(travel - slideX(), 0)
      // Past the share toward closed, lifted with no speed: it closes.
      lift()
      for (let i = 0; i < 60 && sliding(); i++) await paint(1000 / 60)
      expect(screen.queryByTestId('member-side-panel')).toBeNull()
      expect(localStorage.getItem(PANEL_OPEN_KEY)).toBe('0')
    })

    it("closing: the spacer starts at the docked column's own width, so the chat does not collapse", async () => {
      const chat = await openOncall()
      const panel = await screen.findByTestId('member-side-panel', undefined, PANE_READY)
      // A real layout: the docked column is 818px wide on a wider window. (This
      // test DOM has no layout, so the measure is stood in for.)
      const COLUMN = 818
      const realRect = panel.getBoundingClientRect.bind(panel)
      panel.getBoundingClientRect = () => ({ ...realRect().toJSON(), width: COLUMN }) as DOMRect
      swipe(chat, -20, 5)
      // The chat gives back exactly what the panel has moved, from the column's
      // width: never the window's, which collapsed the chat on every close.
      await waitFor(() => expect(spacerW()).toBeCloseTo(COLUMN - 100, 0))
      expect(slideX()).toBeCloseTo(100, 0)
    })

    /** Stand in for the slide's laid-out width (this test DOM has no layout).
     *  On the prototype, because the drag start mounts the slide afresh. */
    const slideWidth = (w: number) => {
      const real = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth')!
      Object.defineProperty(HTMLElement.prototype, 'offsetWidth', {
        configurable: true,
        get(this: HTMLElement) { return this.dataset.testid === 'member-side-panel-slide' ? w : real.get!.call(this) },
      })
      onTestFinished(() => { Object.defineProperty(HTMLElement.prototype, 'offsetWidth', real) })
    }

    it("overlay placement: the travel is the panel's own width, not the scrim's, so a 39% drag closes", async () => {
      // Narrower than a docked column needs: the panel is an overlay, and
      // #member-side-panel is the full-window scrim around it.
      setWindowWidth(900)
      const chat = await openOncall()
      fireEvent.click(await screen.findByTestId('member-panel-toggle'))
      const panel = await screen.findByTestId('member-side-panel', undefined, PANE_READY)
      expect(panel).toHaveAttribute('data-placement', 'overlay')
      // A real layout (this test DOM has none): the scrim spans the window, the
      // panel inside it is 384px.
      const PANEL = 384
      const realRect = panel.getBoundingClientRect.bind(panel)
      panel.getBoundingClientRect = () => ({ ...realRect().toJSON(), width: 900 }) as DOMRect
      slideWidth(PANEL)
      // 150px of 384 is 39%, past the share: it must close, not spring back.
      swipe(chat, -15, 10)
      await waitFor(() => expect(slideX()).toBeCloseTo(150, 0))
      await waitFor(() => expect(screen.queryByTestId('member-side-panel')).toBeNull(), { timeout: 3000 })
    })

    it('overlay placement: a long drag stops at the panel width', async () => {
      setWindowWidth(900)
      const chat = await openOncall()
      fireEvent.click(await screen.findByTestId('member-panel-toggle'))
      const panel = await screen.findByTestId('member-side-panel', undefined, PANE_READY)
      const realRect = panel.getBoundingClientRect.bind(panel)
      panel.getBoundingClientRect = () => ({ ...realRect().toJSON(), width: 900 }) as DOMRect
      slideWidth(384)
      swipe(chat, -20, 30)
      await waitFor(() => expect(slideX()).toBeCloseTo(384, 0))
    })

    it('a swipe that starts inside a dialog does nothing to the panel', async () => {
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      const dialog = document.createElement('div')
      dialog.setAttribute('role', 'dialog')
      const text = document.createElement('p')
      dialog.appendChild(text)
      chat.appendChild(dialog)
      expect(swipe(text, 20, 30)).toBe(0)
      expect(sliding()).toBe(false)
      expect(screen.queryByTestId('member-side-panel')).toBeNull()
      expect(localStorage.getItem(PANEL_OPEN_KEY)).toBe('0')
    })

    it('a short leftward run lets the panel fall back out', async () => {
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      swipe(chat, 4, 3)
      expect(sliding()).toBe(true)
      await waitFor(() => expect(screen.queryByTestId('member-side-panel')).toBeNull(), { timeout: 3000 })
      expect(localStorage.getItem(PANEL_OPEN_KEY)).toBe('0')
    })

    it('a leftward run brings the Dashboard forward when another tab is showing', async () => {
      const chat = await openOncall()
      await screen.findByTestId('side-panel-leading-tabs', undefined, PANE_READY)
      fireEvent.click(screen.getByRole('tab', { name: 'Workspace' }))
      await waitFor(() => expect(dashTab()).toHaveAttribute('aria-selected', 'false'))
      swipe(chat, 15)
      await waitFor(() => expect(dashTab()).toHaveAttribute('aria-selected', 'true'))
      expect(sliding()).toBe(false)
    })

    it('a rightward run drags the open panel out with the fingers, then settles closed', async () => {
      const chat = await openOncall()
      await screen.findByTestId('member-side-panel', undefined, PANE_READY)
      swipe(chat, -20, 5)
      expect(sliding()).toBe(true)
      await waitFor(() => expect(slideX()).toBeCloseTo(100, 0))
      swipe(chat, -20, 25)
      await waitFor(() => expect(screen.queryByTestId('member-side-panel')).toBeNull(), { timeout: 3000 })
      expect(localStorage.getItem(PANEL_OPEN_KEY)).toBe('0')
    })

    it('a short rightward run lets the open panel settle back in', async () => {
      const chat = await openOncall()
      await screen.findByTestId('member-side-panel', undefined, PANE_READY)
      swipe(chat, -4, 3)
      expect(sliding()).toBe(true)
      await waitFor(() => expect(sliding()).toBe(false), { timeout: 3000 })
      expect(screen.getByTestId('member-side-panel')).toHaveAttribute('data-placement', 'docked')
      expect(localStorage.getItem(PANEL_OPEN_KEY)).not.toBe('0')
    })

    it('a short rightward run over Workspace falls back open and stays on Workspace', async () => {
      const chat = await openOncall()
      await screen.findByTestId('side-panel-leading-tabs', undefined, PANE_READY)
      fireEvent.click(screen.getByRole('tab', { name: 'Workspace' }))
      await waitFor(() => expect(dashTab()).toHaveAttribute('aria-selected', 'false'))
      swipe(chat, -4, 3)
      expect(sliding()).toBe(true)
      await waitFor(() => expect(sliding()).toBe(false), { timeout: 3000 })
      expect(screen.getByTestId('member-side-panel')).toBeInTheDocument()
      expect(screen.getByRole('tab', { name: 'Workspace' })).toHaveAttribute('aria-selected', 'true')
      expect(dashTab()).toHaveAttribute('aria-selected', 'false')
    })

    it('a run still drags with text selected: a trackpad swipe is never a selection drag', async () => {
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      const text = document.createElement('p')
      text.textContent = 'some selected words'
      chat.appendChild(text)
      onTestFinished(() => { window.getSelection()?.removeAllRanges(); text.remove() })
      const range = document.createRange()
      range.selectNodeContents(text)
      window.getSelection()?.removeAllRanges()
      window.getSelection()?.addRange(range)
      expect(window.getSelection()?.isCollapsed).toBe(false)
      swipe(chat, 20, 5)
      expect(sliding()).toBe(true)
      await waitFor(() => expect(slideX()).toBeCloseTo(window.innerWidth - 100, 0))
      swipe(chat, 20, 20)
      await waitFor(() => expect(sliding()).toBe(false), { timeout: 3000 })
      await waitFor(() => expect(dashTab()).toHaveAttribute('aria-selected', 'true'))
    })

    it('a genuine click right after a trackpad drag lets go is not swallowed', async () => {
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      const btn = document.createElement('button')
      let clicks = 0
      btn.addEventListener('click', () => { clicks++ })
      chat.appendChild(btn)
      onTestFinished(() => btn.remove())
      // A click during the drag and one the moment it has settled both land.
      swipe(chat, 20, 20)
      expect(sliding()).toBe(true)
      fireEvent.click(btn)
      await waitFor(() => expect(sliding()).toBe(false), { timeout: 3000 })
      fireEvent.click(btn)
      expect(clicks).toBe(2)
    })

    /** Wheel events at explicit event times, as a trackpad stamps them. */
    const wheelSeq = (target: Element, steps: Array<[dx: number, at: number]>) => act(() => {
      for (const [dx, at] of steps) {
        const ev = new WheelEvent('wheel', { deltaX: dx, deltaY: 0, bubbles: true, cancelable: true })
        Object.defineProperty(ev, 'timeStamp', { value: at })
        target.dispatchEvent(ev)
      }
    })
    /** `n` steps of `dx`, `gap` ms apart, from `t0`. */
    const steps = (dx: number, n: number, t0: number, gap = 16): Array<[number, number]> =>
      Array.from({ length: n }, (_, i) => [dx, t0 + i * gap])
    /** A trackpad momentum tail: `n` steps decaying by `ratio`, 16ms apart. */
    const tailSteps = (dx: number, n: number, t0: number, ratio = 0.85): Array<[number, number]> =>
      Array.from({ length: n }, (_, i) => [dx * ratio ** (i + 1), t0 + (i + 1) * 16])

    /** Both clocks under the test's hand: the reader's timers, and framer's. */
    const manualClocks = () => {
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'requestAnimationFrame', 'cancelAnimationFrame'] })
      frameData.timestamp = performance.now()
      MotionGlobalConfig.useManualTiming = true
      onTestFinished(() => {
        MotionGlobalConfig.useManualTiming = false
        vi.useRealTimers()
      })
      return (ms = 0) => act(async () => {
        frameData.timestamp += ms
        await new Promise<void>((resolve) => { frame.postRender(() => resolve()) })
      })
    }

    it('rule C: a fast drag commits before any lift-off and settles open within 150ms', async () => {
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      const paint = manualClocks()
      wheelSeq(chat, steps(10, 4, performance.now()))
      let frames = 0
      while (sliding() && frames < 60) { await paint(1000 / 60); frames += 1 }
      expect(frames).toBeLessThanOrEqual(10)
      expect(screen.getByTestId('member-side-panel')).toHaveAttribute('data-placement', 'docked')
    })

    it('no cooldown: a swipe back during the open momentum, within 150ms, closes it again', async () => {
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      const paint = manualClocks()
      const t0 = performance.now()
      wheelSeq(chat, [
        ...steps(10, 4, t0),
        ...tailSteps(10, 4, t0 + 3 * 16),
        ...steps(-10, 4, t0 + 8 * 16),
      ])
      for (let i = 0; i < 60 && sliding(); i++) await paint(1000 / 60)
      expect(screen.queryByTestId('member-side-panel')).toBeNull()
      expect(localStorage.getItem(PANEL_OPEN_KEY)).toBe('0')
    })

    it('no cooldown: a new same-way burst during the momentum starts a new run', async () => {
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      const paint = manualClocks()
      const t0 = performance.now()
      // Open fast (it commits mid-drag), then its momentum decaying, then new
      // fingers the same way, growing again: 7, 10, 14 (two steps each 1.3x
      // or more), slowly enough not to commit on speed.
      const tail = tailSteps(10, 4, t0 + 3 * 16)
      const last = tail[tail.length - 1][1]
      wheelSeq(chat, [
        ...steps(10, 4, t0),
        ...tail,
        [7, last + 60], [10, last + 120], [14, last + 180],
      ])
      // The new run is a drag: it grabbed the settle and holds the sheet under
      // the fingers. Swallowed as the old run's momentum, the settle would
      // have finished and docked the panel.
      for (let i = 0; i < 30; i++) await paint(1000 / 60)
      expect(sliding()).toBe(true)
      expect(slideX()).toBeGreaterThan(0)
    })

    it('rule C: a slow drag past 20% waits for the release, and dragged back under it falls back', async () => {
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      const travel = window.innerWidth
      const paint = manualClocks()
      const t0 = performance.now()
      const n = Math.round((travel * 0.3) / 3)
      wheelSeq(chat, steps(3, n, t0))
      await paint()
      expect(sliding()).toBe(true)
      expect(slideX()).toBeCloseTo(travel - 3 * n, 0)
      const back = Math.round((travel * 0.2) / 3)
      // The next frame: framer schedules one render per frame time, so input
      // must land in a frame not yet painted.
      frameData.timestamp += 1000 / 60
      wheelSeq(chat, steps(-3, back, t0 + n * 16))
      await paint()
      expect(slideX()).toBeCloseTo(travel - 3 * (n - back), 0)
      act(() => {
        vi.advanceTimersByTime(swipeTiming.idleMs)
        vi.advanceTimersToNextFrame()
      })
      for (let i = 0; i < 60 && sliding(); i++) await paint(1000 / 60)
      expect(screen.queryByTestId('member-side-panel')).toBeNull()
      expect(localStorage.getItem(PANEL_OPEN_KEY)).toBe('0')
    })

    it('a slow 30% drag that commits settles open within the trackpad settle (150ms), on a wide panel', async () => {
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      const travel = window.innerWidth
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'requestAnimationFrame', 'cancelAnimationFrame'] })
      frameData.timestamp = performance.now()
      MotionGlobalConfig.useManualTiming = true
      onTestFinished(() => {
        MotionGlobalConfig.useManualTiming = false
        vi.useRealTimers()
      })
      const paint = (ms = 0) => act(async () => {
        frameData.timestamp += ms
        await new Promise<void>((resolve) => { frame.postRender(() => resolve()) })
      })
      const n = Math.round((travel * 0.3) / 3)
      wheelSeq(chat, steps(3, n, performance.now()))
      await paint()
      act(() => {
        vi.advanceTimersByTime(swipeTiming.idleMs)
        vi.advanceTimersToNextFrame()
      })
      let frames = 0
      while (sliding() && frames < 60) { await paint(1000 / 60); frames += 1 }
      expect(travel).toBeGreaterThan(800)
      // 150ms is 9 frames; one more for the hand-back. The phone settle took 27.
      expect(frames).toBeLessThanOrEqual(10)
      expect(screen.getByTestId('member-side-panel')).toHaveAttribute('data-placement', 'docked')
    })

    it('a slow 15% drag that lifts off snaps back: the panel follows, then falls back out', async () => {
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      const travel = window.innerWidth
      const n = Math.round((travel * 0.15) / 3)
      const t0 = performance.now()
      wheelSeq(chat, steps(3, n, t0))
      // It follows the fingers 1:1 and holds there.
      await waitFor(() => expect(slideX()).toBeCloseTo(travel - 3 * n, 0))
      expect(spacerW()).toBeCloseTo(3 * n, 0)
      // Lifted with no momentum: the phone's rule sends 15% back.
      await waitFor(() => expect(screen.queryByTestId('member-side-panel')).toBeNull(), { timeout: 3000 })
      expect(localStorage.getItem(PANEL_OPEN_KEY)).toBe('0')
    })

    it('a slow drag with a 60ms pause keeps following through the pause', async () => {
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      const travel = window.innerWidth
      const t0 = performance.now()
      wheelSeq(chat, steps(3, 20, t0))
      await waitFor(() => expect(slideX()).toBeCloseTo(travel - 60, 0))
      wheelSeq(chat, steps(3, 20, t0 + 19 * 16 + 60))
      expect(sliding()).toBe(true)
      await waitFor(() => expect(slideX()).toBeCloseTo(travel - 120, 0))
    })

    it('a fast 10% flick with a momentum tail opens the panel', async () => {
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      const travel = window.innerWidth
      const per = Math.round((travel * 0.1) / 4)
      const t0 = performance.now()
      wheelSeq(chat, [...steps(per, 4, t0), ...tailSteps(per, 12, t0 + 3 * 16)])
      await waitFor(() => expect(sliding()).toBe(false), { timeout: 3000 })
      expect(screen.getByTestId('member-side-panel')).toHaveAttribute('data-placement', 'docked')
      expect(localStorage.getItem(PANEL_OPEN_KEY)).toBe('1')
    })

    /** A trackpad turning back: the fingers go one way, then come back with
     *  steps that shrink by 0.8, the size pattern of a tail. */
    const REVERSAL = [20, 20, 20, 20, -16, -12.8, -10.24]

    it('a drag that turns back with shrinking steps follows the turn: 40.96px in, no speed, it falls back out', async () => {
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      const t0 = performance.now()
      wheelSeq(chat, REVERSAL.map((dx, i) => [dx, t0 + i * 80] as [number, number]))
      // Read as a tail, the turn was dropped and the drag released on the way
      // in, at the opening speed: it settled open.
      await waitFor(() => expect(screen.queryByTestId('member-side-panel')).toBeNull(), { timeout: 3000 })
      expect(localStorage.getItem(PANEL_OPEN_KEY)).toBe('0')
    })

    it('under reduced motion, a turn back counts against the swipe: 60.96px is not a clear swipe', async () => {
      const real = window.matchMedia
      window.matchMedia = ((q: string) => ({
        matches: q.includes('prefers-reduced-motion'), media: q, onchange: null,
        addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {},
        dispatchEvent: () => false,
      })) as typeof window.matchMedia
      onTestFinished(() => { window.matchMedia = real })
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      // The run ends on the quiet timer, then one frame: both stepped by hand.
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'requestAnimationFrame', 'cancelAnimationFrame'] })
      onTestFinished(() => { vi.useRealTimers() })
      const t0 = performance.now()
      // Read as a tail, the turn was dropped: a 100px swipe, which opened.
      wheelSeq(chat, [20, ...REVERSAL].map((dx, i) => [dx, t0 + i * 16] as [number, number]))
      act(() => {
        vi.advanceTimersByTime(swipeTiming.idleMs)
        vi.advanceTimersToNextFrame()
      })
      expect(screen.queryByTestId('member-side-panel')).toBeNull()
      expect(localStorage.getItem(PANEL_OPEN_KEY)).toBe('0')
    })

    it("widening mid phone close stops the close: its end never writes the phone's offset onto the docked panel", async () => {
      // A compositor settle, stood in for (this test DOM has no WAAPI): each
      // animation is recorded, and finishes only when the test says.
      type FakeAnim = { cancel: ReturnType<typeof vi.fn>; onfinish: (() => void) | null; oncancel: (() => void) | null; to: string }
      const anims: FakeAnim[] = []
      const proto = HTMLElement.prototype as unknown as { animate?: unknown }
      const realAnimate = proto.animate
      proto.animate = function (frames: Keyframe[]) {
        const a: FakeAnim = { cancel: vi.fn(), onfinish: null, oncancel: null, to: String(frames[frames.length - 1]?.transform ?? '') }
        anims.push(a)
        return a
      }
      const real = window.matchMedia
      const phone = ((q: string) => ({
        matches: q.includes('max-width'), media: q, onchange: null,
        addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {},
        dispatchEvent: () => false,
      })) as typeof window.matchMedia
      onTestFinished(() => {
        proto.animate = realAnimate
        window.matchMedia = real
        setWindowWidth(WIDE_WINDOW)
      })
      setWindowWidth(390)
      window.matchMedia = phone
      await openOncall()
      fireEvent.click(screen.getByTestId('member-panel-toggle'))
      const overlay = await screen.findByTestId('member-side-panel', undefined, PANE_READY)
      expect(overlay).toHaveAttribute('data-placement', 'overlay')
      // The scrim closes it: a compositor settle out to the phone's travel.
      const before = anims.length
      fireEvent.click(overlay)
      await waitFor(() => expect(anims.length).toBeGreaterThan(before))
      const closing = anims[anims.length - 1]
      expect(closing.to).toContain('390px')
      // Wider than a docked column needs, before that settle ends.
      act(() => {
        setWindowWidth(WIDE_WINDOW)
        window.matchMedia = real
        window.dispatchEvent(new Event('resize'))
      })
      await waitFor(() => expect(screen.getByTestId('member-side-panel')).toHaveAttribute('data-placement', 'docked'))
      expect(closing.cancel).toHaveBeenCalled()
      // Its end, had it been left running: nothing may land on the panel.
      act(() => { closing.onfinish?.() })
      expect(screen.getByTestId('member-side-panel-slide').style.transform).not.toContain('390px')
    })

    it('crossing to the phone layout mid-drag retires the desktop drag, so widening again redocks the panel', async () => {
      const chat = await openOncall()
      await screen.findByTestId('member-side-panel', undefined, PANE_READY)
      swipe(chat, -20, 5)
      expect(sliding()).toBe(true)
      const real = window.matchMedia
      const phone = ((q: string) => ({
        matches: q.includes('max-width'), media: q, onchange: null,
        addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {},
        dispatchEvent: () => false,
      })) as typeof window.matchMedia
      onTestFinished(() => { window.matchMedia = real; window.dispatchEvent(new Event('resize')) })
      act(() => { window.matchMedia = phone; window.dispatchEvent(new Event('resize')) })
      act(() => { window.matchMedia = real; window.dispatchEvent(new Event('resize')) })
      // Back on desktop: the docked column, not a sheet stranded mid-slide.
      await waitFor(() => expect(screen.getByTestId('member-side-panel')).toHaveAttribute('data-placement', 'docked'))
      expect(sliding()).toBe(false)
      expect(spacerW()).toBeNull()
    })

    it('under reduced motion, [20,20,20,15,12] then silence is an 87px swipe and opens', async () => {
      const real = window.matchMedia
      window.matchMedia = ((q: string) => ({
        matches: q.includes('prefers-reduced-motion'), media: q, onchange: null,
        addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {},
        dispatchEvent: () => false,
      })) as typeof window.matchMedia
      onTestFinished(() => { window.matchMedia = real })
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      const t0 = performance.now()
      wheelSeq(chat, [[20, t0], [20, t0 + 30], [20, t0 + 60], [15, t0 + 90], [12, t0 + 120]])
      await screen.findByTestId('member-side-panel', undefined, { timeout: 3000 })
      await waitFor(() => expect(dashTab()).toHaveAttribute('aria-selected', 'true'))
    })

    it('arms the Dashboard frame relay only while the desktop swipe listens', async () => {
      await openOncall()
      const host = await screen.findByTestId('member-dashboard')
      const said: Array<{ type: string; on: boolean }> = []
      const frame = document.createElement('iframe')
      // Our relaying Dashboard frame carries the mark the host arms by.
      frame.setAttribute('data-wheel-relay', '')
      const win = { postMessage: (m: { type: string; on: boolean }) => { said.push(m) } }
      Object.defineProperty(frame, 'contentWindow', { value: win })
      host.appendChild(frame)
      // A foreign iframe under the same root is never told anything, not even
      // when it sends the hello itself.
      const heard: unknown[] = []
      const foreign = document.createElement('iframe')
      const foreignWin = { postMessage: (m: unknown) => { heard.push(m) } }
      Object.defineProperty(foreign, 'contentWindow', { value: foreignWin })
      host.appendChild(foreign)
      act(() => {
        const ev = new MessageEvent('message', { data: { type: 'mc-frame-wheel-hello' } })
        Object.defineProperty(ev, 'source', { value: foreignWin })
        window.dispatchEvent(ev)
      })
      // The frame says hello as it starts; a listening host arms it.
      act(() => {
        const ev = new MessageEvent('message', { data: { type: 'mc-frame-wheel-hello' } })
        Object.defineProperty(ev, 'source', { value: win })
        window.dispatchEvent(ev)
      })
      expect(said.at(-1)).toEqual({ type: 'mc-frame-wheel-armed', on: true })
      // Below the desktop width the swipe stops listening: the frame is disarmed,
      // so it no longer cancels the browser's own swipe over it.
      const real = window.matchMedia
      onTestFinished(() => { window.matchMedia = real; window.dispatchEvent(new Event('resize')) })
      act(() => {
        window.matchMedia = ((q: string) => ({
          matches: q.includes('max-width'), media: q, onchange: null,
          addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {},
          dispatchEvent: () => false,
        })) as typeof window.matchMedia
        window.dispatchEvent(new Event('resize'))
      })
      await waitFor(() => expect(said.at(-1)).toEqual({ type: 'mc-frame-wheel-armed', on: false }))
      expect(heard).toEqual([])
    })

    it('a run over a horizontal scroller inside a shadow root scrolls it instead of opening', async () => {
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      const host = document.createElement('div')
      chat.appendChild(host)
      onTestFinished(() => host.remove())
      const shadow = host.attachShadow({ mode: 'open' })
      const scroller = document.createElement('div')
      scroller.style.overflowX = 'auto'
      Object.defineProperty(scroller, 'scrollWidth', { value: 900 })
      Object.defineProperty(scroller, 'clientWidth', { value: 300 })
      const code = document.createElement('pre')
      scroller.appendChild(code)
      shadow.appendChild(scroller)
      let cancelled = 0
      let reached = 0
      const page = screen.getByTestId('members-page')
      const count = () => { reached++ }
      page.addEventListener('wheel', count)
      onTestFinished(() => page.removeEventListener('wheel', count))
      act(() => {
        for (let i = 0; i < 12; i++) {
          const ev = new WheelEvent('wheel', { deltaX: 15, deltaY: 0, bubbles: true, cancelable: true, composed: true })
          // A browser retargets the event to the shadow HOST for a listener
          // outside the root; this test DOM does not, so do it here. Only the
          // composed path still reaches the scroller.
          Object.defineProperty(ev, 'target', { get: () => host })
          code.dispatchEvent(ev)
          if (ev.defaultPrevented) cancelled++
        }
      })
      // The events did cross the shadow boundary to the page, and were left alone.
      expect(reached).toBe(12)
      expect(cancelled).toBe(0)
      expect(screen.queryByTestId('member-side-panel')).toBeNull()
    })

    it('under reduced motion, a run ended by the next (vertical) event still switches the panel', async () => {
      const real = window.matchMedia
      window.matchMedia = ((q: string) => ({
        matches: q.includes('prefers-reduced-motion'), media: q, onchange: null,
        addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {},
        dispatchEvent: () => false,
      })) as typeof window.matchMedia
      onTestFinished(() => { window.matchMedia = real })
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      const at0 = performance.now()
      const wheelAt = (dx: number, dy: number, t: number) => {
        const ev = new WheelEvent('wheel', { deltaX: dx, deltaY: dy, bubbles: true, cancelable: true })
        Object.defineProperty(ev, 'timeStamp', { value: t })
        chat.dispatchEvent(ev)
      }
      act(() => {
        // A clear 120px leftward swipe, then a vertical scroll just past the gap:
        // the scroll is what ends the swipe, before the quiet timer can.
        for (let i = 0; i < 6; i++) wheelAt(20, 0, at0 + i * 16)
        wheelAt(0, 30, at0 + 5 * 16 + swipeTiming.idleMs + 2)
      })
      await screen.findByTestId('member-side-panel', undefined, { timeout: 3000 })
      await waitFor(() => expect(dashTab()).toHaveAttribute('aria-selected', 'true'))
    })

    it('under reduced motion there is no live drag: a clear swipe switches once at the end', async () => {
      const real = window.matchMedia
      window.matchMedia = ((q: string) => ({
        matches: q.includes('prefers-reduced-motion'), media: q, onchange: null,
        addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {},
        dispatchEvent: () => false,
      })) as typeof window.matchMedia
      onTestFinished(() => { window.matchMedia = real })
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      swipe(chat, 20, 10)
      expect(screen.queryByTestId('member-side-panel')).toBeNull()
      await screen.findByTestId('member-side-panel', undefined, { timeout: 3000 })
      expect(sliding()).toBe(false)
      await waitFor(() => expect(dashTab()).toHaveAttribute('aria-selected', 'true'))
    })

    // Each case on its own page: one run that is not ours would otherwise hold
    // the next run that follows it within the gesture gap.
    it('leaves a vertical scroll alone, even one drifting sideways', async () => {
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      expect(swipe(chat, 6, 12, {}, 30)).toBe(0)
      expect(screen.queryByTestId('member-side-panel')).toBeNull()
    })

    it('leaves a pinch zoom (ctrlKey wheel) alone', async () => {
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      expect(swipe(chat, 15, 12, { ctrlKey: true })).toBe(0)
      expect(screen.queryByTestId('member-side-panel')).toBeNull()
    })

    it('leaves a run over a sideways scroller that can still move to that scroller', async () => {
      localStorage.setItem(PANEL_OPEN_KEY, '0')
      const chat = await openOncall()
      // A wide code block that can still scroll right takes the run itself.
      const scroller = document.createElement('div')
      scroller.style.overflowX = 'auto'
      Object.defineProperty(scroller, 'scrollWidth', { value: 900 })
      Object.defineProperty(scroller, 'clientWidth', { value: 300 })
      const code = document.createElement('pre')
      scroller.appendChild(code)
      chat.appendChild(scroller)
      expect(swipe(code, 15)).toBe(0)
      expect(screen.queryByTestId('member-side-panel')).toBeNull()
      scroller.remove()
    })

    /** An iframe with its own stand-in window: the test DOM gives every frame a
     *  null `contentWindow`, which would make any two frames look the same. */
    const frameIn = (parent: Element) => {
      const frame = document.createElement('iframe')
      Object.defineProperty(frame, 'contentWindow', { value: { name: 'frame-window' } })
      parent.appendChild(frame)
      return frame
    }
    const relayFrom = (frame: HTMLIFrameElement, dx: number, n = 30) => act(() => {
      for (let i = 0; i < n; i++) {
        const ev = new MessageEvent('message', { data: { type: 'mc-frame-wheel', dx, dy: 0, mode: 0 } })
        Object.defineProperty(ev, 'source', { value: frame.contentWindow })
        // One event time, as `swipe()` stamps: a batch has no speed, so the
        // first event's slow mount never reads as a flick that commits.
        Object.defineProperty(ev, 'timeStamp', { value: SWIPE_AT })
        window.dispatchEvent(ev)
      }
    })

    it('a relayed drag that turns back with shrinking steps follows the turn and stays open', async () => {
      await openOncall()
      const frame = frameIn(await screen.findByTestId('member-dashboard'))
      const t0 = performance.now()
      act(() => {
        REVERSAL.forEach((dx, i) => {
          const ev = new MessageEvent('message', { data: { type: 'mc-frame-wheel', dx: -dx, dy: 0, mode: 0 } })
          Object.defineProperty(ev, 'source', { value: frame.contentWindow })
          Object.defineProperty(ev, 'timeStamp', { value: t0 + i * 80 })
          window.dispatchEvent(ev)
        })
      })
      expect(sliding()).toBe(true)
      // Read as a tail, the turn was dropped and the drag released at the
      // closing speed: it shut. Followed, it is 40.96px out with no speed.
      await waitFor(() => expect(sliding()).toBe(false), { timeout: 3000 })
      expect(screen.getByTestId('member-side-panel')).toHaveAttribute('data-placement', 'docked')
      expect(localStorage.getItem(PANEL_OPEN_KEY)).not.toBe('0')
    })

    it('a relayed burst delivered late is read at the wheels\' own times: slow fingers fall back open', async () => {
      await openOncall()
      const frame = frameIn(await screen.findByTestId('member-dashboard'))
      // Five -3px steps 16ms apart (0.19px/ms), then a momentum tail, all
      // stamped by the frame 300ms ago and delivered in one burst after a stall,
      // 1ms apart here.
      const wheelAt = performance.now() - 300
      const finger = Array.from({ length: 5 }, (_, i) => [-3, wheelAt + i * 16] as const)
      const tail = Array.from({ length: 4 }, (_, k) => [-3 * 0.85 ** (k + 1), wheelAt + 64 + (k + 1) * 16] as const)
      const received = performance.now()
      act(() => {
        ;[...finger, ...tail].forEach(([dx, at], i) => {
          const ev = new MessageEvent('message', { data: { type: 'mc-frame-wheel', dx, dy: 0, mode: 0, t: performance.timeOrigin + at } })
          Object.defineProperty(ev, 'source', { value: frame.contentWindow })
          Object.defineProperty(ev, 'timeStamp', { value: received + i })
          window.dispatchEvent(ev)
        })
      })
      // Read at the arrival times, the same 12px took 4ms (3px/ms), a flick
      // that shut the panel.
      await waitFor(() => expect(sliding()).toBe(false), { timeout: 3000 })
      expect(screen.getByTestId('member-side-panel')).toHaveAttribute('data-placement', 'docked')
      expect(localStorage.getItem(PANEL_OPEN_KEY)).not.toBe('0')
    })

    it('a rightward run relayed from the Dashboard frame drags the panel shut; a stranger frame cannot', async () => {
      await openOncall()
      const frame = frameIn(await screen.findByTestId('member-dashboard'))
      const stranger = frameIn(document.body)
      onTestFinished(() => stranger.remove())
      relayFrom(stranger, -20)
      expect(sliding()).toBe(false)
      expect(localStorage.getItem(PANEL_OPEN_KEY)).not.toBe('0')
      relayFrom(frame, -20, 5)
      expect(sliding()).toBe(true)
      await waitFor(() => expect(slideX()).toBeCloseTo(100, 0))
      relayFrom(frame, -20, 25)
      await waitFor(() => expect(screen.queryByTestId('member-side-panel')).toBeNull(), { timeout: 3000 })
    })

    /** The Dashboard stays mounted behind Files once visited, frame and all. */
    const dashboardFrameBehindFiles = async () => {
      await openOncall()
      const frame = frameIn(await screen.findByTestId('member-dashboard'))
      fireEvent.click(screen.getByRole('tab', { name: 'Workspace' }))
      await waitFor(() => expect(dashTab()).toHaveAttribute('aria-selected', 'false'))
      expect(frame.isConnected).toBe(true)
      return frame
    }

    // One run per page: a run the page declined holds every event that follows
    // it within the gesture gap, so a second run here would prove nothing.
    it('a hidden Dashboard frame cannot pull the panel back to its own tab', async () => {
      const frame = await dashboardFrameBehindFiles()
      relayFrom(frame, 20)
      expect(dashTab()).toHaveAttribute('aria-selected', 'false')
      expect(sliding()).toBe(false)
    })

    it('a hidden Dashboard frame cannot drag the panel shut', async () => {
      const frame = await dashboardFrameBehindFiles()
      relayFrom(frame, -20)
      expect(sliding()).toBe(false)
      expect(screen.getByTestId('member-side-panel')).toBeInTheDocument()
    })
  })
})
