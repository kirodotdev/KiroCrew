/* MembersPage: create links that open the New crewmate card from a chat, a
 * guide or a deep link, what an open card does to the chat behind it, and a
 * create the user leaves running for another chat.
 * This file, MembersPage.test.tsx and the other files that import
 * src/test/MembersPageTestUtils.tsx (their shared fixtures) carry the same api
 * mock and module stubs below, word for word: change them together. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { ApiError } from '../../api/apiError'
import { memberProjectionStore } from '../../state/memberProjectionStore'
import { __resetPaneDraftsForTests } from '../../utils/chatPaneDrafts'
import { getViewedThreadSlot, _resetViewedThreadForTests } from '../../lib/viewedThread'
import { getMountedMemberThreadSlot } from '../../lib/mountedMemberThread'
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
  clickAddCrewmate,
  clickAddAdvanced,
  resetApiMocks,
} from '../../test/MembersPageTestUtils'
/* Follows a create link with no name or goal (a guide's, a crewmate
 * proposal's), which the page consumes and strips. */
const followBlankCreateLink = async () => {
  fireEvent.click(screen.getByTestId('guide-nav-blank'))
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

describe('MembersPage create links open the New mate card', () => {
  // `default` rides along untouched: the Assistant is its own member.
  const assistantAndAlpha = () => [defaultRow(), assistantRow(), row({ name: 'alpha', slug: 'alpha' })]
  /** True when the element, or an ancestor, is hidden by an inline display:none. */
  const hiddenByStyle = (el: Element) => {
    for (let n: Element | null = el; n; n = n.parentElement) {
      if (n instanceof HTMLElement && n.style.display === 'none') return true
    }
    return false
  }
  // The create form is now a Modal (`role="dialog"`), portaled over the chat
  // that stays mounted behind it — no `member-create-pane` wrapper, no
  // `crewmate-create-embedded` region.
  const cardOpen = () => screen.queryByRole('dialog') !== null
  const card = () => screen.getByRole('dialog')
  const nameField = () => within(card()).getByLabelText('Name') as HTMLInputElement
  /** "What it looks after": in the Advanced settings, so present only unfolded. */
  const goalField = () => within(card()).queryByLabelText('What it looks after') as HTMLTextAreaElement | null
  // The modal's own header X carries the "Close" name (components.modal.close),
  // the Modal path's dismiss affordance in place of the embedded card's Cancel.
  const closeCard = () => fireEvent.click(within(card()).getByRole('button', { name: 'Close' }))
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
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-mate')
  }

  it('a create link in Mate\'s chat opens the New mate card as a modal, prefilled: name in Name, goal in What it looks after; the chat stays MOUNTED and VISIBLE behind it; nothing is sent', async () => {
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
      expect(currentUrl()).toBe('/members?member=mate')
      expect(nameField().value).toBe('scout')
      // A goal was proposed, so Advanced settings is open and the goal is in view.
      expect(within(card()).getByTestId('crewmate-create-advanced-toggle')).toHaveAttribute('aria-expanded', 'true')
      expect(goalField()?.value).toBe('Watch the release train')
      noGuidedFlow()
      // Mounted AND visible: the modal is a portaled scrim over the chat, which
      // keeps its element, its mount, and its composer state behind it.
      expect(screen.getByTestId('chat-pane-stub')).toBe(stub)
      expect(hiddenByStyle(stub)).toBe(false)
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
    await waitFor(() => expect(currentUrl()).toBe('/members?member=alpha'), PANE_READY)
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

  it('Create on a prefilled card posts that name and goal, asks for the first greeting, and opens the new mate', async () => {
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
    await waitFor(() => expect(currentUrl()).toBe('/members?member=scout'), PANE_READY)
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-scout'), PANE_READY)
    // The modal closes through its exit motion once the create hands over.
    await waitFor(() => expect(cardOpen()).toBe(false))
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

  it('only Mate\'s chat intercepts: a mate chat\'s create link is left to the chat', async () => {
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
    await screen.findByRole('dialog')
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

  it('a chat behind a creation surface is not the viewed thread, and is again once the chat returns', async () => {
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    await openAssistant()
    await waitFor(() => expect(getViewedThreadSlot()).toBe('member-mate'))
    expect(getMountedMemberThreadSlot()).toBe('member-mate')
    await clickAddAdvanced()
    await screen.findByRole('dialog')
    // Messages arriving now must badge: the create surface is open over this
    // chat (keyed off createOpen, not off a hidden column), so it is not the
    // viewed thread even though the chat stays mounted behind the modal.
    await waitFor(() => expect(getViewedThreadSlot()).toBeNull())
    expect(getMountedMemberThreadSlot()).toBeNull()
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(getViewedThreadSlot()).toBe('member-mate'))
    expect(getMountedMemberThreadSlot()).toBe('member-mate')
  })

  it('Advanced opens the full form as a modal over the chat: chat still mounted and visible behind the scrim', async () => {
    await openAssistant()
    const stub = screen.getByTestId('chat-pane-stub')
    const mountsBefore = chatPaneMounts.count
    await clickAddAdvanced()
    const dialog = await screen.findByRole('dialog')
    // A modal now, not an in-place region: the chat is mounted AND visible
    // behind the portaled scrim, and the Advanced region is unfolded in it.
    expect(hiddenByStyle(stub)).toBe(false)
    expect(within(dialog).getByTestId('crewmate-create-advanced')).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.getByTestId('chat-pane-stub')).toBe(stub)
    expect(hiddenByStyle(stub)).toBe(false)
    expect(chatPaneMounts.count).toBe(mountsBefore)
  })

  it('the header shows only the display name, for Mate and for any labelled mate', async () => {
    const utils = await renderPage([defaultRow(), assistantRow(), row({ name: 'oncall', slug: 'oncall', display_name: 'Oncall Sentinel' })])
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-mate')
    const title = screen.getByTestId('member-title-row')
    expect(title).toHaveTextContent('Mate')
    expect(within(title).queryByTestId('member-pill-id')).toBeNull()
    expect(title).not.toHaveTextContent(/\bassistant\b/)
    utils.unmount()
    await renderPage([defaultRow(), assistantRow(), row({ name: 'oncall', slug: 'oncall', display_name: 'Oncall Sentinel' })], 'kirocrew', { route: '/members?member=oncall' })
    expect(await screen.findByTestId('chat-pane-stub', undefined, PANE_READY)).toHaveTextContent('member-oncall')
    expect(screen.getByTestId('member-title-row')).toHaveTextContent('Oncall Sentinel')
    expect(within(screen.getByTestId('member-title-row')).queryByTestId('member-pill-id')).toBeNull()
  })

  it('choosing a roster chat while an untouched card is up shows that chat without asking', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    try {
      await openAssistant()
      fireEvent.click(screen.getByTestId('chat-link-proposal'))
      await waitFor(() => expect(cardOpen()).toBe(true))
      fireEvent.click(await rosterRow('alpha'))
      await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-alpha'), PANE_READY)
      // The modal closes through its exit motion as the chat opens.
      await waitFor(() => expect(cardOpen()).toBe(false))
      expect(confirm).not.toHaveBeenCalled()
      expect(hiddenByStyle(screen.getByTestId('chat-pane-stub'))).toBe(false)
    } finally {
      confirm.mockRestore()
    }
  })

  it('choosing another chat while a create runs keeps that chat when the create lands', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    try {
      let finish: (v: unknown) => void = () => {}
      ;(api.createKirocrewAgent as ReturnType<typeof vi.fn>).mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
      await openAssistant()
      await clickAddCrewmate()
      await screen.findByRole('dialog')
      fireEvent.change(nameField(), { target: { value: 'radar' } })
      fireEvent.click(screen.getByTestId('crewmate-create-submit'))
      await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))
      // The user picks alpha mid-create and agrees to leave the card.
      fireEvent.click(await rosterRow('alpha'))
      await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-alpha'), PANE_READY)
      expect(confirm).toHaveBeenLastCalledWith(expect.stringMatching(/^Leave while the mate is being created\?/))
      const membersMock = api.members as ReturnType<typeof vi.fn>
      membersMock.mockResolvedValue({
        members: [...assistantAndAlpha(), row({ name: 'radar', slug: 'radar', dashboard_created: true })],
        default_agent: 'kirocrew',
      })
      const readsBefore = membersMock.mock.calls.length
      await act(async () => { finish({ ok: true }) })
      // The roster is re-read so the new crewmate is listed...
      await waitFor(() => expect(membersMock.mock.calls.length).toBeGreaterThan(readsBefore))
      expect(await rosterRow('radar')).toBeInTheDocument()
      // ...but the chat the user picked stays.
      expect(api.memberThread).not.toHaveBeenCalledWith('radar')
      expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-alpha')
      expect(currentUrl()).not.toContain('member=radar')
    } finally {
      confirm.mockRestore()
    }
  })

  it('New mate pressed again while a left create still runs does not reopen a card that create would then close', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    try {
      let finish: (v: unknown) => void = () => {}
      ;(api.createKirocrewAgent as ReturnType<typeof vi.fn>).mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
      await openAssistant()
      await clickAddCrewmate()
      await screen.findByRole('dialog')
      fireEvent.change(nameField(), { target: { value: 'radar' } })
      fireEvent.click(screen.getByTestId('crewmate-create-submit'))
      await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))
      fireEvent.click(await rosterRow('alpha'))
      await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-alpha'), PANE_READY)
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
      await clickAddCrewmate()
      expect(screen.queryByRole('dialog')).toBeNull()
      expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-alpha')
      await act(async () => { finish({ ok: true }) })
      expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-alpha')
    } finally {
      confirm.mockRestore()
    }
  })

  it('a create that fails after the user left it says so on the page, keeps the chosen chat, and the notice dismisses', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    try {
      let fail: (e: unknown) => void = () => {}
      ;(api.createKirocrewAgent as ReturnType<typeof vi.fn>).mockReturnValueOnce(new Promise((_, reject) => { fail = reject }))
      await openAssistant()
      await clickAddCrewmate()
      await screen.findByRole('dialog')
      fireEvent.change(nameField(), { target: { value: 'radar' } })
      fireEvent.click(screen.getByTestId('crewmate-create-submit'))
      await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))
      fireEvent.click(await rosterRow('alpha'))
      await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-alpha'), PANE_READY)
      await act(async () => { fail(new ApiError(500, 'boom')) })
      const notice = await screen.findByTestId('member-left-create-failed', undefined, PANE_READY)
      expect(notice).toHaveTextContent("Couldn't create the mate.")
      expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-alpha')
      fireEvent.click(within(notice).getByRole('button'))
      await waitFor(() => expect(screen.queryByTestId('member-left-create-failed')).toBeNull())
    } finally {
      confirm.mockRestore()
    }
  })

  it('a failed roster re-read after leaving a create says so, keeps the chosen chat, and its retry only re-reads', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    try {
      let finish: (v: unknown) => void = () => {}
      ;(api.createKirocrewAgent as ReturnType<typeof vi.fn>).mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
      await openAssistant()
      await clickAddCrewmate()
      await screen.findByRole('dialog')
      fireEvent.change(nameField(), { target: { value: 'radar' } })
      fireEvent.click(screen.getByTestId('crewmate-create-submit'))
      await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalledTimes(1))
      fireEvent.click(await rosterRow('alpha'))
      await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-alpha'), PANE_READY)
      const membersMock = api.members as ReturnType<typeof vi.fn>
      membersMock.mockRejectedValue(new Error('roster down'))
      await act(async () => { finish({ ok: true }) })
      expect(await screen.findByTestId('member-post-create-error', undefined, PANE_READY)).toBeInTheDocument()
      expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-alpha')
      membersMock.mockResolvedValue({
        members: [...assistantAndAlpha(), row({ name: 'radar', slug: 'radar', dashboard_created: true })],
        default_agent: 'kirocrew',
      })
      fireEvent.click(screen.getByTestId('member-post-create-retry'))
      expect(await rosterRow('radar')).toBeInTheDocument()
      await waitFor(() => expect(screen.queryByTestId('member-post-create-error')).toBeNull(), PANE_READY)
      // The retry listed it; the chat the user picked is still the one open.
      expect(api.memberThread).not.toHaveBeenCalledWith('radar')
      expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-alpha')
    } finally {
      confirm.mockRestore()
    }
  })

  it('New mate again over a typed blank card, once the user agrees to leave, starts empty', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    try {
      await openAssistant()
      await clickAddCrewmate()
      await screen.findByRole('dialog')
      fireEvent.change(nameField(), { target: { value: 'half-typed' } })
      await clickAddCrewmate()
      await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1))
      await waitFor(() => expect(nameField().value).toBe(''))
    } finally {
      confirm.mockRestore()
    }
  })
})
