/* MembersPage: what a New crewmate create leaves behind and checks — the
 * caches it marks stale, the + held until its follow-up settles, the names
 * refused before or after the request, a failed roster re-read, and the card's
 * Built from and Advanced settings fields. A create in progress is in
 * MembersPage.newCrewmate.test.tsx.
 * This file, MembersPage.test.tsx and the other files that import
 * src/test/MembersPageTestUtils.tsx (their shared fixtures) carry the same api
 * mock and module stubs below, word for word: change them together. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { ApiError } from '../../api/apiError'
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
import { CACHE_WARM_BOUND_MS } from './NewCrewmateDialog'
import { resetDashboardOnLoad } from './dashboardOnLoad'
import { resetFirstGreetingRequests } from './useFirstGreeting'
import { __resetPanelTabs } from '../../hooks/usePanelTabs'
import {
  WIDE_WINDOW,
  setWindowWidth,
  row,
  assistantRow,
  defaultRow,
  PANE_READY,
  fakeCreateClock,
  realCreateClock,
  stepCreateClock,
  renderPage,
  currentUrl,
  roster,
  rosterRow,
  clickAddCrewmate,
  clickAddAdvanced,
  unfoldAdvanced,
  probeAddCrewmate,
  waitForAddCrewmate,
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

describe('New mate dialog', () => {
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

  it('a successful create invalidates the crew registry and config caches, as the crew manager\'s own form does, so the pencil deep link finds the new mate', async () => {
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

  it('a successful create marks the shared sessionless agents-catalog cache stale, so a later reader (the command bar\'s mates view) lists the new mate without a reload and without a fetch per keystroke', async () => {
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

  it('a blank name never leaves the browser: a hint under the field, no create call', async () => {
    await renderPage([row()])
    await rosterRow('oncall')
    await openDialog()
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    expect(await screen.findByText('Give your mate a name.')).toBeInTheDocument()
    expect(api.createKirocrewAgent).not.toHaveBeenCalled()
    // A hint is validation, not a failed request: no error notice renders.
    expect(screen.queryByTestId('crewmate-create-error')).toBeNull()
  })

  it('a name another mate shows is refused up front, like a taken key', async () => {
    await renderPage([row({ display_name: 'Oncall Sentinel' })])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    await openDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Oncall Sentinel' } })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    expect(await screen.findByTestId('crewmate-create-name-hint')).toHaveTextContent('Oncall Sentinel')
    expect(api.createKirocrewAgent).not.toHaveBeenCalled()
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
    fireEvent.click(within(screen.getByRole('dialog', { name: 'New mate' })).getByRole('button', { name: 'Close' }))
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
    const dlg = within(screen.getByRole('dialog', { name: 'New mate' }))
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
    expect(notice).toHaveTextContent("Couldn't create the mate. Nothing was created — try again.")
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
      reason: 'Retry or dismiss the notice about radar before adding another mate.',
    })
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
      reason: "Finishing alpha's first message before adding another mate.",
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
      expect(cta).toHaveAttribute('title', "Finishing alpha's first message before adding another mate.")
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
    expect(screen.getByTestId('member-post-create-retry')).toHaveTextContent('Refresh your mates')
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
    expect(await screen.findByTestId('crewmate-create-name-hint')).toHaveTextContent('Give your mate a name.')
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
    expect(notice).toHaveTextContent("Couldn't create the mate. Nothing was created — try again.")
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
    expect(notice).toHaveTextContent('That name looks like a credential or a URL carrying one. Pick a name that says who the mate is.')
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
    expect(notice).not.toHaveTextContent(/credential|says who the mate is/)
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
    expect(hint).toHaveTextContent('A mate named oncall already exists.')
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
    expect(notice).toHaveTextContent('A mate named radar already exists.')
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
    expect(screen.getByTestId('member-post-create-retry')).toHaveTextContent('Refresh your mates')
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

  it('a failed re-read over a roster with other mates can be dismissed, so the existing chats stay reachable below md', async () => {
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
    expect(screen.getByTestId('member-count')).toHaveTextContent('1 mate')
  })

  it('a first create over a default-only roster whose re-read fails is not dismissable either: the built-in default is not a mate', async () => {
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
      expect(cta).toHaveAttribute('title', 'Refresh the list for radar before adding another mate.')
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

  it('a create over a default + Assistant roster whose re-read fails can be dismissed: the Assistant is a mate and its chat stays reachable', async () => {
    const membersMock = api.members as ReturnType<typeof vi.fn>
    // The Assistant counts: the roster is NOT the empty state, so its chat
    // opens and a failed follow-up notice is dismissable (a "No crewmates
    // yet" hero would never come back under it).
    await renderPage([defaultRow({ last_active_ts: 999 }), assistantRow()])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-mate')
    await clickAddAdvanced()
    await screen.findByTestId('crewmate-create-form', undefined, PANE_READY)
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    membersMock.mockRejectedValueOnce(new Error('roster down'))
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    const notice = await screen.findByTestId('member-post-create-error', undefined, PANE_READY)
    expect(notice).toHaveTextContent("radar was created, but the list didn't refresh.")
    expect(screen.queryByTestId('crewmate-empty-hero')).toBeNull()
    expect(within(notice).getByRole('button', { name: /dismiss|close/i })).toBeInTheDocument()
    expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-mate')
    // The retry is the way on: the roster lands with radar, its chat opens.
    membersMock.mockResolvedValue({
      members: [defaultRow({ last_active_ts: 999 }), assistantRow(), row({ name: 'radar', slug: 'radar', dashboard_created: true })],
      default_agent: 'kirocrew',
    })
    fireEvent.click(screen.getByTestId('member-post-create-retry'))
    await waitFor(() => expect(screen.queryByTestId('member-post-create-error')).toBeNull(), PANE_READY)
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledWith('radar'), PANE_READY)
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
    const dlg = within(screen.getByRole('dialog', { name: 'New mate' }))
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

  it('Advanced settings folds the rest of the crew manager\'s form behind one disclosure in the New mate card', async () => {
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
