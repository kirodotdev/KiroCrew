/* MembersPage: the open thread's live state — its viewed-thread registration,
 * the unread drain, the identity pill's activity line and the Profile entry, slug
 * collisions in live projections, and the opening welcome or greeting. The pill's
 * docking, name and chevron are in MembersPage.sidePanel.test.tsx.
 * This file, MembersPage.test.tsx and the other files that import
 * src/test/MembersPageTestUtils.tsx (their shared fixtures) carry the same api
 * mock and module stubs below, word for word: change them together. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { memberProjectionStore } from '../../state/memberProjectionStore'
import { markSlotUnread, sseSlots } from '../../store/dashboardSlice'
import { setSlotStatusDetail, sseChatMessage, sseToolActivity, sseToolResult, syncSlotRunningFromServer } from '../../store/chatSlice'
import { PILL_ACTIVITY_MAX_CHARS } from './pillActivity'
import { __resetPaneDraftsForTests } from '../../utils/chatPaneDrafts'
import { getViewedThreadSlot, _resetViewedThreadForTests } from '../../lib/viewedThread'
import { getMountedMemberThreadSlot } from '../../lib/mountedMemberThread'
import { bindSlotReadSender, emitSlotRead, _resetSlotReadRelayForTest } from '../../lib/slotReadRelay'
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
import { resetFirstGreetingRequests } from './useFirstGreeting'
import { __resetPanelTabs } from '../../hooks/usePanelTabs'
import {
  LAST_MEMBER_KEY,
  PANEL_OPEN_KEY,
  WIDE_WINDOW,
  setWindowWidth,
  row,
  PANE_READY,
  realCreateClock,
  renderPage,
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
    expect(getMountedMemberThreadSlot()).toBe('member-oncall')
  })

  it('window blur retires unread state but keeps visible mounted presence', async () => {
    await renderPage()
    fireEvent.click(await rosterRow('oncall'))
    await waitFor(() => expect(getViewedThreadSlot()).toBe('member-oncall'), PANE_READY)
    const focus = vi.spyOn(document, 'hasFocus').mockReturnValue(false)
    act(() => window.dispatchEvent(new Event('blur')))
    await waitFor(() => expect(getViewedThreadSlot()).toBeNull())
    expect(getMountedMemberThreadSlot()).toBe('member-oncall')
    focus.mockReturnValue(true)
    act(() => window.dispatchEvent(new Event('focus')))
    focus.mockRestore()
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
      // The mounted identity stays available for the synchronous return frame;
      // the hidden frame already cleared server presence.
      expect(getMountedMemberThreadSlot()).toBe('member-oncall')
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
    expect(getMountedMemberThreadSlot()).toBeNull()
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
    // sidebar's own dot was rebound in #10488 (chat-sidebar/SessionRow.tsx); this
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
  beforeEach(() => { localStorage.clear(); localStorage.setItem('mc-preview-crew', '1') })

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
    const { store } = await renderPage([row({ bound: true, slot_key: 'member-oncall', last_active_ts: Math.floor(Date.now() / 1000) - 180 })]) // flake-ok: a stamp three minutes back; the assertion reads only the 'Idle · ' prefix
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
      store.dispatch(setSlotStatusDetail({ slot: 'member-oncall', kind: 'tool', purpose: 'Look up the glass pill code', toolName: 'grep -n Glass', toolCallId: 't1', ts: Date.now() })) // flake-ok: no assertion here reads the stamp's value
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
      store.dispatch(setSlotStatusDetail({ slot: 'member-oncall', kind: 'tool', purpose: long, toolName: 'npm run build', toolCallId: 't2', ts: Date.now() })) // flake-ok: no assertion here reads the stamp's value
    })
    expect(line).toHaveAttribute('data-activity', 'tool')
    expect(Array.from(line.textContent ?? '').length).toBeLessThanOrEqual(PILL_ACTIVITY_MAX_CHARS)
    expect(line.textContent?.endsWith('…')).toBe(true)
    expect(line.textContent?.startsWith('Rebuild the whole site')).toBe(true)

    // Visible output streaming: the pill's own word, not the seam's
    // "Streaming" (transport jargon on a line that says what the crewmate
    // is doing).
    act(() => {
      store.dispatch(setSlotStatusDetail({ slot: 'member-oncall', kind: 'streaming', ts: Date.now() })) // flake-ok: no assertion here reads the stamp's value
    })
    expect(line).toHaveAttribute('data-activity', 'writing')
    expect(line.textContent).toBe('Writing…')

    // The turn ends — status idle, run state settled (as `chat_done` does),
    // slots frame no longer running: back to idle, whatever the last status
    // said.
    act(() => {
      store.dispatch(setSlotStatusDetail({ slot: 'member-oncall', kind: 'idle', ts: Date.now() })) // flake-ok: no assertion here reads the stamp's value
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

  it('the DM header floats over the thread: the conversation scrolls under the glass pill (#18325)', async () => {
    await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    const header = await screen.findByTestId('member-thread-header')
    const cls = header.className.split(/\s+/)
    // Out of flow over the column's top edge, no background of its own, so the
    // transcript passes under the pill the way it passes under the composer dock.
    expect(cls).toEqual(expect.arrayContaining(['absolute', 'top-0', 'inset-x-0']))
    expect(cls).not.toContain('bg-bg')
    expect(cls).not.toContain('bg-card')
    // Below the floating Profile card (z-30), whose scrim covers the header.
    expect(cls).toContain('z-20')
    // The row and its cells pass input through; only the controls catch it, so
    // a wheel beside the pill scrolls the transcript.
    expect(cls).toContain('pointer-events-none')
    const pill = screen.getByTestId('member-identity-pill')
    expect(pill.className.split(/\s+/)).toContain('pointer-events-auto')
    // The fade behind the controls fills the header's own box and is inert;
    // every other child is a cell that hands input to its controls.
    const fade = header.querySelector(':scope > [aria-hidden]') as HTMLElement
    expect(fade.className.split(/\s+/)).toEqual(expect.arrayContaining(['absolute', 'inset-0', 'pointer-events-none', 'bg-gradient-to-b']))
    for (const cell of Array.from(header.children).filter(c => c !== pill && c !== fade)) {
      expect(cell.className).toMatch(/\[&>\*\]:pointer-events-auto/)
    }
    // The side controls are glass like the pill, not bare text over the rows.
    expect(screen.getByTestId('crewmate-switcher').className.split(/\s+/)).toContain('glass-hover')
    const opener = screen.queryByTestId('member-panel-toggle')
    if (opener) expect(opener.className.split(/\s+/)).toContain('glass-hover')
    // The pane is told what the band costs (`topInset`, the header's measured
    // height — 0 here, happy-dom has no layout — never a constant), and the
    // block that can sit between header and pane is padded by the same number.
    const pane = await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
    expect(pane).toHaveAttribute('data-top-inset', '0')
    // Padding on the outer box, measurement on the inner one: the padding
    // never counts toward "is anything shown between", so a hidden
    // (`md:hidden`) child cannot reserve a blank band (Opus finding on R3).
    const between = header.nextElementSibling as HTMLElement
    expect(between.style.paddingTop).toBe('0px')
    expect(between.firstElementChild!.className.split(/\s+/)).toEqual(expect.arrayContaining(['flex', 'flex-col']))
    expect(between.className).not.toMatch(/empty:hidden/)
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

/** The greeting is drawn INSIDE the chat (ChatPane's `crewmateGreeting`), in
 *  the crewmate's own message bubble, not as a card above the transcript. */
function expectCrewmateMessage(card: HTMLElement) {
  expect(within(screen.getByTestId('chat-pane-stub')).getByTestId(card.dataset.testid!)).toBe(card)
  expect(card.closest('[data-testid="crewmate-message"]')).not.toBeNull()
  expect(card.querySelector('.message-bubble.crewmate-bubble')).not.toBeNull()
  // Never wider than the chat column: a long goal must not size it past a 320px chat.
  expect(within(card).getByTestId('mate-greeting-bubble').style.maxWidth).toBe('min(100%, 72ch)')
}

describe('MembersPage cold welcome (a new or long-idle thread)', () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear() })

  it('opening a long-idle crewmate with no goal in flight recaps its paused and recent work, once', async () => {
    vi.mocked(api.memberRecap).mockResolvedValue({
      slug: 'oncall', member: 'oncall',
      paused: [{ goal: 'Rotate the pager keys', next: 'confirm with Sam' }],
      recent: [{ title: 'Triage last night\'s alarms', ts: 1 }],
    })
    await renderPage([row({ last_active_ts: 1 })])
    fireEvent.click(await rosterRow('oncall'))
    const card = await screen.findByTestId('member-welcome-card', undefined, PANE_READY)
    // A message from the crewmate in its chat, not a card above it.
    expectCrewmateMessage(card)
    expect(api.memberRecap).toHaveBeenCalledExactlyOnceWith('oncall', 'oncall')
    expect(within(card).getByTestId('member-welcome-items')).toHaveTextContent('Paused: Rotate the pager keys (its next step: confirm with Sam)')
    expect(within(card).getByTestId('member-welcome-items')).toHaveTextContent("Recent session, may be finished: Triage last night's alarms")
    expect(screen.queryByTestId('member-resume-card')).toBeNull()
    expect(api.sendChat).not.toHaveBeenCalled()
    expect(localStorage.getItem('kc-mate-welcomed-member-oncall')).toBe('1')
  })
})

describe('MembersPage active task greeting', () => {
  const midGoal = {
    conductor: { schema: 1, slot_key: 'member-oncall', goal: 'Ship the crew page', round: 1, depth: 0, parent_item: null, created_at: '' },
    conductor_alive: 'idle',
    take_over_available: false,
    items: [
      { item_id: 'a', title: 'Mate list', state: 'accepted', status: 'done', terminal: true, alive: 'closed', outstanding: false, orphaned: false, stale: false, last_report_at: null },
      { item_id: 'b', title: 'Bubbles', state: 'open', status: 'progress', terminal: false, alive: 'running', outstanding: false, orphaned: false, stale: false, last_report_at: null },
      { item_id: 'c', title: 'Write the RFC', state: 'open', status: 'question', terminal: false, alive: 'idle', outstanding: true, orphaned: false, stale: false, last_report_at: null },
    ],
  }
  beforeEach(() => { sessionStorage.clear() })

  it('opening a crewmate mid-goal shows no immediate static warm card', async () => {
    vi.mocked(api.crewBoard).mockResolvedValue(midGoal as never)
    await renderPage([row()])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
    await waitFor(() => expect(api.crewBoard).toHaveBeenCalledExactlyOnceWith('member-oncall'))
    expect(screen.queryByTestId('member-resume-card')).toBeNull()
    expect(api.memberRecap).not.toHaveBeenCalled()
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it('return summary rows stay ordinary crewmate messages', async () => {
    vi.mocked(api.crewBoard).mockResolvedValue(midGoal as never)
    const { store } = await renderPage([row({ bound: true, slot_key: 'member-oncall' })])
    fireEvent.click(await rosterRow('oncall'))
    await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)
    act(() => {
      store.dispatch(sseChatMessage({
        slot: 'member-oncall',
        role: 'assistant',
        cls: 'msg msg-a',
        content: 'Preparing your current-task summary. You can wait here.',
        meta: { kind: 'warm_task_brief_preparing' },
      }))
      store.dispatch(sseChatMessage({
        slot: 'member-oncall',
        role: 'assistant',
        cls: 'msg msg-a',
        content: 'Problem\nFix the current task\n\nCurrent status\n- Ready\n\nSuggested actions\n1. Review it',
        meta: { kind: 'warm_task_brief_result' },
      }))
    })
    const messages = store.getState().chat.slotMessages['member-oncall']
    expect(messages.slice(-2).map(message => message.role)).toEqual(['assistant', 'assistant'])
    expect(screen.queryByTestId('warm-task-brief-card')).toBeNull()
  })

  it('a failed board read uses the cold-welcome error surface', async () => {
    vi.mocked(api.crewBoard).mockRejectedValue(Object.assign(new Error('ledger needs repair'), { status: 409 }))
    await renderPage([row()])
    fireEvent.click(await rosterRow('oncall'))
    expect(await screen.findByTestId('member-resume-error', undefined, PANE_READY)).toHaveTextContent("Couldn't read what oncall was working on.")
    expect(screen.queryByTestId('member-resume-card')).toBeNull()
  })

})
