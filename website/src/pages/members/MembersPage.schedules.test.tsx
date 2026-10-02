import { describe, it, expect, vi, beforeEach } from 'vitest'
import { PREVIEW_DASHBOARD } from '../../utils/previewFlags'
import { useState } from 'react'
import { screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { NavigationLeaveGuardProvider, useMayLeaveForNavigation, useRegisterNavigationLeaveGuard } from '../../components/NavigationLeaveGuard'
import { __resetPanelTabs } from '../../hooks/usePanelTabs'

/* CREW-18721 — the Schedules chip in the crewmate side panel.
 *
 * What wakes a crewmate used to be readable only from the crew editor, two
 * navigations away from the crewmate you were looking at. The panel now carries
 * the crew editor's OWN pane as a fourth chip; these cases pin the three facts
 * that would otherwise regress silently.
 *
 *   - The chip's count is live-over-total across the jobs bound to THIS
 *     crewmate, in the crew editor rail's shape, so one crewmate reads the same
 *     either place.
 *   - An unreadable cron list drops the badge rather than rendering `0/0`: that
 *     would state that nothing wakes this crewmate on the strength of a request
 *     that failed. A crewmate that genuinely has none drops it too — a quiet
 *     empty pane says it without a number on every unscheduled crewmate.
 *   - A job belonging to NO crewmate is not this crewmate's business: the tab
 *     lists only what is attributed to the open crewmate, the default crew
 *     included, and unowned jobs stay on `/schedule`.
 *
 * Its own file rather than a block in `MembersPage.test.tsx`: the `below md`
 * describe there leaves `useIsMobile` answering mobile (its own comment says the
 * hook caches on the mock's identity), so a later case that needs the DOCKED
 * panel finds no strip at all. Same reason `MembersPage.sideChat.test.tsx` and
 * `MembersPage.filters.test.tsx` stand alone.
 */

vi.mock('../../api/client', () => ({
  api: {
    members: vi.fn(),
    teams: { list: vi.fn(() => Promise.resolve({ teams: [] })) },
    memberThread: vi.fn(),
    memberActivity: vi.fn(() => Promise.resolve({ slug: '', member: '', capped: false, entries: [] })),
    memberBriefing: vi.fn(() => Promise.resolve({ slug: '', member: '', supported: true, text: '', updated_ts: null, redacted: false, truncated: false })),
    sessionCrewLogProjections: vi.fn(() => Promise.resolve({ folds: {}, resolved: true, writesDrained: true })),
    memberPanel: vi.fn(() => Promise.resolve({ panel: null, html: null })),
    autonudgeList: vi.fn(() => Promise.resolve({ enabled: true, loops: [] })),
    // The chip's count and the tab body read one cron list and filter it per
    // crewmate. Each case sets its own jobs.
    crons: vi.fn(() => Promise.resolve({ jobs: [] })),
    // The create the draft-guard cases drive; each one controls its own resolution.
    createCron: vi.fn(() => Promise.resolve({ ok: true, id: 'j-new' })),
    updateCron: vi.fn(() => Promise.resolve({ ok: true })),
    // `wakesCrew`'s default-crew fallback needs to know which crew is default.
    defaultAgent: vi.fn(() => Promise.resolve({ default_agent: 'kirocrew' })),
    // Reached by the schedule row's pause / run controls via `useCronActions`.
    toggleCron: vi.fn(() => Promise.resolve({})),
    runCron: vi.fn(() => Promise.resolve({})),
    cancelCron: vi.fn(() => Promise.resolve({})),
    cronToChat: vi.fn(() => Promise.resolve({})),
    models: vi.fn(() => Promise.resolve([])),
    // Reached only by the post-create veto case below, which drives the real New
    // crewmate dialog rather than a stub: stubbing it would move the page's own
    // leave guard ahead of the dialog's, which the veto-ordering case above relies on.
    agentCatalog: vi.fn(() => Promise.resolve({ agents: [], default_agent: 'kirocrew' })),
    workspaces: vi.fn(() => Promise.resolve({ workspaces: [] })),
    createKirocrewAgent: vi.fn(() => Promise.resolve({ ok: true, name: 'radar' })),
    kirocrewConfig: vi.fn(() => Promise.resolve({ agents: {} })),
  },
}))

/* Panel bodies that are not this file's business. */
vi.mock('../chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../chat/FilesHomePanel', () => ({ default: () => null }))
vi.mock('../chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../../components/WebPreviewPanel', () => ({ default: () => null }))
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
vi.mock('../../components/ChatPane', () => ({
  // The dock the real pane renders is exposed here because it OPENS A PANEL TAB, which
  // makes it one more exit from the Schedules tab and so one more thing that must ask.
  default: ({ slotKey, onOpenCommandCenter, onFileOpen, onSessionOpen }: { slotKey: string; onOpenCommandCenter?: () => void; onFileOpen?: (p: string) => void; onSessionOpen?: (k: string) => void }) => (
    <div data-testid="chat-pane-stub">
      {slotKey}
      <button onClick={onOpenCommandCenter}>Open task dashboard</button>
      <button onClick={() => onFileOpen?.('notes.md')}>Open file link</button>
      {/* A driving-session row. It leaves `/members` for `/chat` outright, so it is one
          more exit from the Schedules tab and one more thing that must ask. */}
      <button onClick={() => onSessionOpen?.('chat-77')}>Open driving session</button>
    </div>
  ),
}))

const navigateSpy = vi.fn()
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>()
  return { ...actual, useNavigate: () => navigateSpy }
})

import { api } from '../../api/client'
import MembersPage, { CREW_SCHEDULES_TAB_ID } from './MembersPage'
import { wakesCrew } from '../../components/crew/wakesCrew'

/** Wide enough to dock the panel beside the thread — see `panelSitsBeside`. */
const WIDE_WINDOW = 1440

function row(overrides: Record<string, unknown> = {}) {
  return {
    name: 'oncall', slug: 'oncall', bound: true, slot_key: 'member-oncall', running: false,
    kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '',
    ...overrides,
  }
}

/** Two jobs on `oncall`, one of them paused, plus one belonging to nobody. */
const JOBS = [
  { id: 'j1', name: 'triage new issues', message: 'go', enabled: true, schedule: '0 9 * * *', agent: 'shared-template', member_id: 'oncall' },
  { id: 'j2', name: 'weekly digest', message: 'go', enabled: false, schedule: 'every 7d', agent: 'shared-template', member_id: 'oncall' },
  { id: 'j3', name: 'nightly backup', message: 'go', enabled: true, schedule: 'every 24h', agent: '', member_id: '' },
]

async function openCrewmate(name = 'oncall', alsoRoster: string[] = []) {
  ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({
    members: [name, ...alsoRoster].map(n => row({ name: n, slug: n, slot_key: `member-${n}` })),
    default_agent: 'kirocrew',
  })
  // Echo the requested slug: a fixed answer would report `member: <first>` for every
  // crewmate, which the page reads as a slug collision and renders instead of the thread.
  ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementation((slug: string) =>
    Promise.resolve({ slot_key: `member-${slug}`, slug, member: slug, created: false }),
  )
  renderWithProviders(
    <NavigationLeaveGuardProvider>
      <MembersPage />
      <LeaveProbe />
    </NavigationLeaveGuardProvider>,
  )
  fireEvent.click(await screen.findByText(name))
  await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent(`member-${name}`))
  await screen.findByTestId('member-dashboard')
}

/** A second dirty surface that refuses every navigation, rendered as a SIBLING after the
 *  page so its guard registers after the page's own -- the order that lets it answer a
 *  question the page has already said yes to. The page's real sibling in production is
 *  `NewCrewmateDialog`, which is a CHILD and therefore always asked first. */
function VetoSurface() {
  useRegisterNavigationLeaveGuard(() => false)
  return null
}

/** `openCrewmate` with a veto surface behind the page. */
async function openCrewmateWithVeto(name = 'oncall') {
  ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({
    members: [row({ name, slug: name, slot_key: `member-${name}` })],
    default_agent: 'kirocrew',
  })
  ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementation((slug: string) =>
    Promise.resolve({ slot_key: `member-${slug}`, slug, member: slug, created: false }),
  )
  renderWithProviders(
    <NavigationLeaveGuardProvider>
      <MembersPage />
      <VetoSurface />
      <LeaveProbe />
    </NavigationLeaveGuardProvider>,
  )
  fireEvent.click(await screen.findByText(name))
  await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent(`member-${name}`))
  await screen.findByTestId('member-dashboard')
}

const chip = () => screen.getByTestId(`side-panel-leading-tab-${CREW_SCHEDULES_TAB_ID}`)

/** Stands in for an app-shell navigation surface (sidebar, palette, Back): asks the
 *  page's registered leave guards and records the answer. Its own copy rather than an
 *  import, for the same reason this whole file stands alone. */
function LeaveProbe() {
  const mayLeave = useMayLeaveForNavigation()
  const [answer, setAnswer] = useState('')
  return (
    <button type="button" data-testid="leave-probe" onClick={() => setAnswer(String(mayLeave()))}>
      {answer}
    </button>
  )
}
/** Does the page hold the document open right now? The `beforeunload` listener and the
 *  published navigation stake are armed off ONE flag on adjacent lines, and this is the
 *  half a test can see: a cancelled unload event means the flag is up. */
const holdsDocument = () =>
  !window.dispatchEvent(new Event('beforeunload', { cancelable: true }))
const askToLeave = () => {
  fireEvent.click(screen.getByTestId('leave-probe'))
  return screen.getByTestId('leave-probe').textContent
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  // The Dashboard tab and the in-chat dock are a Feature Preview, on here.
  localStorage.setItem(PREVIEW_DASHBOARD, '1')
  __resetPanelTabs()
  Object.defineProperty(window, 'innerWidth', { value: WIDE_WINDOW, configurable: true, writable: true })
  vi.mocked(api.crons).mockResolvedValue({ jobs: JOBS } as never)
  vi.mocked(api.defaultAgent).mockResolvedValue({ default_agent: 'kirocrew' } as never)
})

describe('MembersPage Schedules chip', () => {
  it('sits last in the leading block, after Dashboard / Work log / Notes', async () => {
    await openCrewmate()
    await waitFor(() => expect(chip()).toBeInTheDocument())
    // The leading block alone: the pinned views (Artifacts, Files) follow it and
    // belong to the panel, not to the crewmate.
    const leading = screen.getByTestId('side-panel-leading-tabs')
    expect(within(leading).getAllByRole('tab').map((t) => t.getAttribute('aria-label')))
      .toEqual(['Dashboard', 'Work log', 'Notes', 'Schedules'])
  })

  it('matches a private schedule on the crewmate\'s IMMUTABLE id, not its display name', async () => {
    // The fixtures above all have name === slug, which cannot tell the two identities
    // apart. A private schedule's `member_id` is the slug: the client's value is
    // rewritten to the canonical id before the record is persisted. So for a crewmate
    // whose display name is not already its own slug, matching on the name showed
    // nothing -- including a job just created from this very tab.
    ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({
      members: [row({ name: 'Radar One', slug: 'radar-one', slot_key: 'member-radar-one' })],
      default_agent: 'kirocrew',
    })
    // `member` is the crewmate's NAME, not its slug -- the page compares the two and
    // reads a mismatch as a slug collision. That distinction is the point of this case.
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementation((slug: string) =>
      Promise.resolve({ slot_key: `member-${slug}`, slug, member: 'Radar One', created: false }),
    )
    vi.mocked(api.crons).mockResolvedValue({
      jobs: [
        { id: 'p1', name: 'triage', message: 'go', enabled: true, schedule: '0 9 * * *', agent: 'shared-template', member_id: 'radar-one' },
        { id: 'p2', name: 'sweep', message: 'go', enabled: false, schedule: 'every 6h', agent: 'shared-template', member_id: 'radar-one' },
      ],
    } as never)
    renderWithProviders(
      <NavigationLeaveGuardProvider>
        <MembersPage />
        <LeaveProbe />
      </NavigationLeaveGuardProvider>,
    )
    fireEvent.click(await screen.findByText('Radar One'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-radar-one'))
    await screen.findByTestId('member-dashboard')
    await waitFor(() => expect(screen.getByTestId('member-schedules-count')).toHaveTextContent('1/2'))
    fireEvent.click(chip())
    const body = await screen.findByTestId('member-schedules')
    await waitFor(() => expect(within(body).getAllByTestId('wake-row')).toHaveLength(2))
  })

  it('counts only this crewmate\'s schedules, live over total', async () => {
    await openCrewmate()
    // 2 bound to `oncall`, 1 of them enabled. The ownerless job is not counted:
    // `oncall` is not the default crew, so it is not one of its wakes.
    await waitFor(() => expect(screen.getByTestId('member-schedules-count')).toHaveTextContent('1/2'))
  })

  it('opens the crew editor\'s own pane, listing this crewmate\'s jobs and no others', async () => {
    await openCrewmate()
    fireEvent.click(chip())
    const body = await screen.findByTestId('member-schedules')
    // The SAME section the crew editor mounts, not a look-alike list.
    expect(within(body).getByTestId('crew-wake-section')).toBeInTheDocument()
    await waitFor(() => expect(within(body).getAllByTestId('wake-row')).toHaveLength(2))
    expect(within(body).getAllByTestId('wake-row').map(r => r.textContent).join(' '))
      .not.toContain('nightly backup')
  })

  it('will not leave the tab while a create is in flight, and asks before discarding a draft', async () => {
    // Only the active leading tab's body is mounted, so every other chip is a
    // destruction path for the create form. A POST already sent cannot be cancelled by
    // unmounting, so that window refuses outright; typed-but-unsent work asks first.
    let release: (v: unknown) => void = () => {}
    vi.mocked(api.createCron).mockReturnValue(new Promise(r => { release = r }) as never)
    await openCrewmate()
    fireEvent.click(chip())
    const body = await screen.findByTestId('member-schedules')
    await within(body).findByTestId('crew-wake-section')

    fireEvent.click(within(body).getByTestId('crew-wake-add'))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Check the board' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Read the board.' } })

    // Dirty, not yet saving: leaving asks, and answering no keeps the draft.
    fireEvent.click(screen.getByTestId('side-panel-leading-tab-crew-notes'))
    const ask = await screen.findByRole('dialog')
    // Scoped to the dialog: the section's own "Cancel new schedule" toggle is on screen
    // at the same time and matches the same name.
    fireEvent.click(within(ask).getByRole('button', { name: /Cancel/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.getByTestId('member-schedules')).toBeInTheDocument()

    // Saving: the switch is refused with no prompt at all.
    fireEvent.click(within(body).getByTestId('crew-wake-create-submit'))
    await waitFor(() => expect(api.createCron).toHaveBeenCalled())
    fireEvent.click(screen.getByTestId('side-panel-leading-tab-crew-notes'))
    await new Promise(r => setTimeout(r, 120))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByTestId('member-schedules')).toBeInTheDocument()
    act(() => { release({ ok: true, id: 'j-new' }) })
  })

  it('asks before a crewmate switch discards a draft, and remounts the section once it does', async () => {
    // Two rules meet here. `key={activeMemberName}` means a switch REMOUNTS the section,
    // so the form cannot survive with its `memberId` silently rebound to the new
    // crewmate. And because that remount destroys typed work, the switch asks first --
    // it used to discard in silence, which is the one exit that made "every exit asks"
    // untrue.
    await openCrewmate('oncall', ['scribe'])
    fireEvent.click(chip())
    const body = await screen.findByTestId('member-schedules')
    await within(body).findByTestId('crew-wake-section')
    fireEvent.click(within(body).getByTestId('crew-wake-add'))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: "oncall's draft" } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Read the board.' } })

    // Refused: still on the same crewmate, with what was typed.
    fireEvent.click(screen.getByText('scribe'))
    const ask = await screen.findByRole('dialog')
    fireEvent.click(within(ask).getByRole('button', { name: /Cancel/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
    expect(screen.getByDisplayValue("oncall's draft")).toBeInTheDocument()

    // Confirmed: the switch happens and the form is gone rather than carried over.
    fireEvent.click(screen.getByText('scribe'))
    const ask2 = await screen.findByRole('dialog')
    fireEvent.click(within(ask2).getByRole('button', { name: /Discard/i }))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-scribe'))
    await waitFor(() => expect(screen.queryByDisplayValue("oncall's draft")).toBeNull())
  })

  it('asks before the side-panel chord hides a draft', async () => {
    // Hiding the panel unmounts the tab body exactly as closing it from the strip does.
    // The header opener is hidden while the docked panel is open (the panel's own close
    // control owns that gesture there), so the chord is the other way to hide it.
    await openCrewmate()
    fireEvent.click(chip())
    const body = await screen.findByTestId('member-schedules')
    await within(body).findByTestId('crew-wake-section')
    fireEvent.click(within(body).getByTestId('crew-wake-add'))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Check the board' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Read the board.' } })

    act(() => { window.dispatchEvent(new Event('toggle-activity-panel')) })
    const ask = await screen.findByRole('dialog')
    fireEvent.click(within(ask).getByRole('button', { name: /Cancel/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.getByTestId('member-schedules')).toBeInTheDocument()

    // Confirmed, the panel goes.
    act(() => { window.dispatchEvent(new Event('toggle-activity-panel')) })
    const ask2 = await screen.findByRole('dialog')
    fireEvent.click(within(ask2).getByRole('button', { name: /Discard/i }))
    await waitFor(() => expect(screen.queryByTestId('member-schedules')).toBeNull())
  })

  /* The Ask-about-this and Work-log jumps (`openMemberSideChat`, `openCrewWorkLog`) are
   * guarded through the same two refs every case below exercises, but their real entry
   * points are a text selection inside the transcript and a line in the quiet-chat card,
   * neither drivable here without stubbing the thing under test. A case that passed
   * whether or not the guard fired would prove nothing, so they are covered by the three
   * families below instead: `setSearchParams` (team row), panel visibility (chord), and
   * `openView` / `setActive` (the + menu). */

  it('asks before a team header row discards a draft', async () => {
    // Opening a team clears the open crewmate, unmounting the whole panel subtree.
    ;(api.teams.list as ReturnType<typeof vi.fn>).mockResolvedValue({
      teams: [{ id: 't1', name: 'Ops', members: ['oncall'] }],
    })
    await openCrewmate()
    fireEvent.click(chip())
    const body = await screen.findByTestId('member-schedules')
    await within(body).findByTestId('crew-wake-section')
    fireEvent.click(within(body).getByTestId('crew-wake-add'))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Check the board' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Read the board.' } })

    const header = await screen.findByText('Ops')
    fireEvent.click(header)
    const ask = await screen.findByRole('dialog')
    fireEvent.click(within(ask).getByRole('button', { name: /Cancel/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    // Refused: still on the crewmate, with the draft.
    expect(screen.getByTestId('member-schedules')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()
  })

  it('asks before the + menu opens another tab over a draft', async () => {
    // Opening any other tab makes it active, which unmounts the Schedules body just as a
    // chip click does. The + menu is the only door to that here: the panel's launcher
    // cards render only for a host supplying no leading tabs, and this one supplies four.
    await openCrewmate()
    fireEvent.click(chip())
    const body = await screen.findByTestId('member-schedules')
    await within(body).findByTestId('crew-wake-section')
    fireEvent.click(within(body).getByTestId('crew-wake-add'))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Check the board' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Read the board.' } })

    // Radix opens the dropdown on pointerdown (mouse), not click.
    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'Open side panel tab' }),
      { button: 0, ctrlKey: false, pointerType: 'mouse' },
    )
    const menu = await screen.findByRole('menu')
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Subagents' }))
    const ask = await screen.findByRole('dialog')
    fireEvent.click(within(ask).getByRole('button', { name: /Cancel/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.getByTestId('member-schedules')).toBeInTheDocument()
  })

  it('holds a reload and the navigation stake while a draft is open, so Back and unload arm too', async () => {
    // Registering a leave guard is not enough: a reload never reaches it, and
    // `NavigationBackGuard` arms off the published STAKE rather than the guard, so
    // without both of these Back and a reload discarded the draft silently while every
    // wired in-app exit asked -- on the same page where the New crewmate dialog does
    // publish, which made the gap uneven rather than merely absent. Both are armed off
    // one flag (`MembersPage.tsx`: `usePublishNavigationStake(schedAtStake)` and the
    // `beforeunload` effect on the next line), and the unload is the arm a test can see.
    await openCrewmate()
    fireEvent.click(chip())
    const body = await screen.findByTestId('member-schedules')
    await within(body).findByTestId('crew-wake-section')
    expect(holdsDocument()).toBe(false)

    fireEvent.click(within(body).getByTestId('crew-wake-add'))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Check the board' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Read the board.' } })
    await waitFor(() => expect(holdsDocument()).toBe(true))

    // Discarding the draft drops it again, so neither Back nor a reload asks once there
    // is nothing to lose. The section's own toggle is the cancel, and it routes through
    // the host's confirm.
    fireEvent.click(within(body).getByTestId('crew-wake-add'))
    const ask = await screen.findByRole('dialog')
    fireEvent.click(within(ask).getByRole('button', { name: /Discard/i }))
    await waitFor(() => expect(holdsDocument()).toBe(false))
  })

  it('vetoes leaving the route while a draft is open, and lets a clean tab through', async () => {
    // The navigation-leave registry is synchronous, so this exit uses `window.confirm`,
    // the same path the New crewmate dialog's own guard takes on this page. Both guards
    // are registered at once, which is why the registry holds a set rather than one slot.
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    try {
      await openCrewmate()
      fireEvent.click(chip())
      const body = await screen.findByTestId('member-schedules')
      await within(body).findByTestId('crew-wake-section')
      // Clean: the shell is allowed to leave without a prompt at all.
      expect(askToLeave()).toBe('true')
      expect(confirmSpy).not.toHaveBeenCalled()

      fireEvent.click(within(body).getByTestId('crew-wake-add'))
      fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Check the board' } })
      fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Read the board.' } })
      expect(askToLeave()).toBe('false')
      expect(confirmSpy).toHaveBeenCalledWith(expect.stringMatching(/lose the schedule/i))
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('opens the editor in place over a Schedules draft without discarding it (CREW-18688)', async () => {
    // The pill IS the crewmate's edit entry. It used to NAVIGATE to the crew
    // manager, which unmounted this page and so had to prompt before discarding
    // a Schedules draft. It now opens the editor as a MODAL IN PLACE (CREW-18688):
    // the panel subtree holding the draft stays mounted behind the modal, so there
    // is nothing to discard and no prompt — the draft is still there afterwards.
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    try {
      await openCrewmate()
      fireEvent.click(chip())
      const body = await screen.findByTestId('member-schedules')
      await within(body).findByTestId('crew-wake-section')
      fireEvent.click(within(body).getByTestId('crew-wake-add'))
      fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Check the board' } })
      fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Read the board.' } })

      fireEvent.click(screen.getByTestId('member-identity-pill'))
      // No discard prompt: opening the modal does not leave the route.
      expect(confirmSpy).not.toHaveBeenCalled()
      // Still on the crewmate, with the draft intact behind the modal.
      expect(screen.getByTestId('member-schedules')).toBeInTheDocument()
      expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('keeps the draft through a resize across the docking boundary, which no guard can decline', async () => {
    // `beside` is recomputed from the live window width, so dragging the window narrow
    // turns the docked panel into an overlay that starts closed -- on its own, with no
    // gesture to intercept. Asking is not available (declining cannot un-resize a
    // window), so the panel stays mounted and hidden instead of unmounting the form.
    await openCrewmate()
    fireEvent.click(chip())
    const body = await screen.findByTestId('member-schedules')
    await within(body).findByTestId('crew-wake-section')
    fireEvent.click(within(body).getByTestId('crew-wake-add'))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Check the board' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Read the board.' } })

    Object.defineProperty(window, 'innerWidth', { value: 900, configurable: true, writable: true })
    fireEvent(window, new Event('resize'))
    // No confirm was raised and nothing was thrown away: the typed text is still here.
    await waitFor(() => expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument())
    expect(screen.queryByRole('dialog')).toBeNull()

    // Still guarded while hidden. Keeping the form mounted IS the only copy of that
    // text, so a gate on VISIBILITY would answer "nothing at stake" here and let the
    // next sidebar click or Back press discard it without asking -- a hole the retention
    // opened rather than closed. The confirm having been ASKED is the assertion: a
    // refusal alone would also be what an unguarded page returns by default.
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    try {
      expect(askToLeave()).toBe('false')
      expect(confirmSpy).toHaveBeenCalledWith(expect.stringMatching(/lose the schedule/i))
    } finally {
      confirmSpy.mockRestore()
    }

    // Widening back shows the same form, still holding what was typed.
    Object.defineProperty(window, 'innerWidth', { value: WIDE_WINDOW, configurable: true, writable: true })
    fireEvent(window, new Event('resize'))
    await waitFor(() => expect(screen.getByTestId('member-schedules')).toBeInTheDocument())
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()
  })

  it('writes the provider TEMPLATE for a crewmate whose identity persists, and lists it back', async () => {
    // `agent` and `member_id` are different fields and the tab has to pass both. For a
    // crewmate with a persisted identity the server keeps `member_id`, so `wakesCrew`
    // matches on that and `agent` is free to carry the template -- which is what
    // `/schedule` labels the job by. Omitting it persisted no agent at all and the job
    // read as the default crew's.
    ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({
      members: [row({
        name: 'radar', slug: 'radar', slot_key: 'member-radar', kiro_agent: 'kirocrew-worker',
        memory_version: 2, memory_owner: 'radar',
      })],
      default_agent: 'kirocrew',
    })
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementation((slug: string) =>
      Promise.resolve({ slot_key: `member-${slug}`, slug, member: slug, created: false }),
    )
    renderWithProviders(
      <NavigationLeaveGuardProvider>
        <MembersPage />
        <LeaveProbe />
      </NavigationLeaveGuardProvider>,
    )
    fireEvent.click(await screen.findByText('radar'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-radar'))
    await screen.findByTestId('member-dashboard')
    fireEvent.click(chip())
    const body = await screen.findByTestId('member-schedules')
    await within(body).findByTestId('crew-wake-section')

    fireEvent.click(within(body).getByTestId('crew-wake-add'))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Check the board' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Read the board.' } })
    fireEvent.click(within(body).getByTestId('crew-wake-create-submit'))

    await waitFor(() => expect(api.createCron).toHaveBeenCalled())
    const submitted = vi.mocked(api.createCron).mock.calls[0][0] as { agent?: string; member_id?: string }
    expect(submitted.agent).toBe('kirocrew-worker')
    // And the record the server would keep is listed back under this crewmate: its
    // `member_id` survives, which is the branch `wakesCrew` takes first.
    expect(wakesCrew(
      { id: 'j-new', name: 'Check the board', member_id: 'radar', agent: 'kirocrew-worker' } as never,
      'radar', false, 'radar',
    )).toBe(true)
  })

  it('writes the DISPLAY NAME for a crewmate whose identity does not persist, and lists it back', async () => {
    // A legacy crewmate has no persisted identity, so the server CLEARS `member_id` as
    // the job is created and `wakesCrew` falls through to comparing `agent` against the
    // display name. Writing the provider template there matched neither field and the new
    // schedule vanished from the very tab that created it.
    ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({
      members: [row({
        name: 'Radar One', slug: 'radar-one', slot_key: 'member-radar-one',
        kiro_agent: 'kirocrew-worker', memory_version: 1, memory_owner: '',
      })],
      default_agent: 'kirocrew',
    })
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementation((slug: string) =>
      Promise.resolve({ slot_key: `member-${slug}`, slug, member: 'Radar One', created: false }),
    )
    renderWithProviders(
      <NavigationLeaveGuardProvider>
        <MembersPage />
        <LeaveProbe />
      </NavigationLeaveGuardProvider>,
    )
    fireEvent.click(await screen.findByText('Radar One'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-radar-one'))
    await screen.findByTestId('member-dashboard')
    fireEvent.click(chip())
    const body = await screen.findByTestId('member-schedules')
    await within(body).findByTestId('crew-wake-section')

    fireEvent.click(within(body).getByTestId('crew-wake-add'))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Check the board' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Read the board.' } })
    fireEvent.click(within(body).getByTestId('crew-wake-create-submit'))

    await waitFor(() => expect(api.createCron).toHaveBeenCalled())
    const submitted = vi.mocked(api.createCron).mock.calls[0][0] as { agent?: string }
    expect(submitted.agent).toBe('Radar One')
    // With `member_id` cleared, that is exactly the value the tab's own filter reads, so
    // the schedule is listed under this crewmate instead of disappearing.
    expect(wakesCrew(
      { id: 'j-new', name: 'Check the board', member_id: '', agent: 'Radar One' } as never,
      'Radar One', false, 'radar-one',
    )).toBe(true)
  })

  it('files a created schedule under the crewmate\'s NAME, never a derived id that can collide', async () => {
    // The tab MATCHES on the immutable id, but it must not SUBMIT one: a crewmate whose
    // id was never persisted gets an id derived from its name, slugification is lossy, and
    // member resolution reads an agent name before a stored id. So a derived id could file
    // this schedule against a different crewmate that happens to be named it. The name is
    // the roster row's own identity and cannot collide that way.
    ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({
      members: [row({ name: 'Radar One', slug: 'radar-one', slot_key: 'member-radar-one' })],
      default_agent: 'kirocrew',
    })
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementation((slug: string) =>
      Promise.resolve({ slot_key: `member-${slug}`, slug, member: 'Radar One', created: false }),
    )
    renderWithProviders(<MembersPage />)
    fireEvent.click(await screen.findByText('Radar One'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-radar-one'))
    await screen.findByTestId('member-dashboard')
    fireEvent.click(chip())
    const body = await screen.findByTestId('member-schedules')
    await within(body).findByTestId('crew-wake-section')

    fireEvent.click(within(body).getByTestId('crew-wake-add'))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Check the board' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Read the board.' } })
    fireEvent.click(within(body).getByTestId('crew-wake-create-submit'))

    await waitFor(() => expect(api.createCron).toHaveBeenCalled())
    const submitted = vi.mocked(api.createCron).mock.calls[0][0] as { member_id?: string }
    expect(submitted.member_id).toBe('Radar One')
  })

  it('asks before the narrow-window Back button drops the member param over a draft', async () => {
    // Below md the header carries a Back button that clears `?member=` with a REPLACE for
    // a deep-linked crewmate. A replace raises no `popstate`, so neither the published
    // stake nor the browser Back trap can see it, and clearing the param unmounts the
    // panel subtree -- the draft went with it, silently, on an ordinary tap.
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    try {
      await openCrewmate()
      fireEvent.click(chip())
      const body = await screen.findByTestId('member-schedules')
      await within(body).findByTestId('crew-wake-section')
      fireEvent.click(within(body).getByTestId('crew-wake-add'))
      fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Check the board' } })
      fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Read the board.' } })

      fireEvent.click(screen.getByTestId('member-back'))
      const ask = await screen.findByRole('dialog')
      fireEvent.click(within(ask).getByRole('button', { name: /Cancel/i }))
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
      // Refused: still on the crewmate, with the draft.
      expect(screen.getByTestId('member-schedules')).toBeInTheDocument()
      expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('keeps the draft guarded when a LATER guard vetoes the exit it just agreed to', async () => {
    // Accepting the discard stands retention down so the panel can actually unmount. But
    // the channel asks every registered guard, and a guard registered AFTER this page can
    // then refuse the same navigation -- leaving the form on screen. It must still be a
    // draft. The first version of this cleared the dirty flags on the accept, which left
    // a visible, UNGUARDED draft for some later exit to throw away.
    await openCrewmateWithVeto()
    fireEvent.click(chip())
    const body = await screen.findByTestId('member-schedules')
    await within(body).findByTestId('crew-wake-section')
    fireEvent.click(within(body).getByTestId('crew-wake-add'))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Check the board' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Read the board.' } })

    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    try {
      // This page says yes; the veto surface behind it says no, so nothing navigates.
      expect(askToLeave()).toBe('false')
      expect(confirmSpy).toHaveBeenCalledTimes(1)
      // The form is still here, so it is still a draft and the next exit must ask again.
      expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()
      expect(askToLeave()).toBe('false')
      expect(confirmSpy).toHaveBeenCalledTimes(2)
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('releases the create hold when a draft veto blocks the open of a crewmate just created', async () => {
    // The "+" hold and the parked greeting are both released by the new crewmate's thread
    // OPENING. That open goes through the same guard every other exit does, so it can be
    // REFUSED -- and then nothing opens, nothing releases, and the create button stays
    // disabled for the rest of the page's life with the greeting dropped on unmount. The
    // refusal has to travel back to the caller that parked them.
    ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({
      members: [row({ name: 'oncall', slug: 'oncall', slot_key: 'member-oncall' })],
      default_agent: 'kirocrew',
    })
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementation((slug: string) =>
      Promise.resolve({ slot_key: `member-${slug}`, slug, member: slug, created: false }),
    )
    renderWithProviders(<MembersPage />, { route: '/members' })
    await screen.findByTestId('chat-pane-stub')
    fireEvent.click(chip())
    const body = await screen.findByTestId('member-schedules')
    await within(body).findByTestId('crew-wake-section')
    fireEvent.click(within(body).getByTestId('crew-wake-add'))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'nightly sweep' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Read the board.' } })

    // Create a second crewmate. The post-create re-read must LAND and list it, so that
    // `openCreated` takes its open-the-thread branch rather than the parked-notice one.
    ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({
      members: [
        row({ name: 'oncall', slug: 'oncall', slot_key: 'member-oncall' }),
        row({ name: 'radar', slug: 'radar', slot_key: 'member-radar' }),
      ],
      default_agent: 'kirocrew',
    })
    fireEvent.pointerDown(screen.getByTestId('member-add'), { button: 0, pointerType: 'mouse' })
    fireEvent.click(await screen.findByTestId('member-add-crewmate'))
    const createForm = await screen.findByTestId('crewmate-create-form')
    fireEvent.change(within(createForm).getByLabelText('Name'), { target: { value: 'radar' } })
    fireEvent.submit(createForm)
    await waitFor(() => expect(api.createKirocrewAgent).toHaveBeenCalled())

    // The open of `radar` asks about the draft, and the answer is no.
    const ask = await screen.findByRole('dialog')
    fireEvent.click(within(ask).getByRole('button', { name: /Cancel/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    // Refused, so `oncall` is still open with its draft intact -- and the hold is GONE:
    // the "+" is usable again rather than stuck on a thread that will never open.
    expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
    expect(screen.getByDisplayValue('nightly sweep')).toBeInTheDocument()
    fireEvent.pointerDown(screen.getByTestId('member-add'), { button: 0, pointerType: 'mouse' })
    const addItem = await screen.findByTestId('member-add-crewmate')
    await waitFor(() => expect(addItem.getAttribute('aria-disabled')).not.toBe('true'))
  })

  it('asks before the in-chat Command Center dock opens the Dashboard over a draft', async () => {
    // The dock sits in the thread, not in the panel, and it makes the Crew Dashboard tab
    // active -- which unmounts the Schedules body. It used to call the tab store's raw
    // `setActive`, which is the strip's guard bypassed, and it is clickable in exactly the
    // state the draft is most fragile in: a hidden panel keeps the form mounted.
    await openCrewmate()
    fireEvent.click(chip())
    const body = await screen.findByTestId('member-schedules')
    await within(body).findByTestId('crew-wake-section')
    fireEvent.click(within(body).getByTestId('crew-wake-add'))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Check the board' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Read the board.' } })

    fireEvent.click(screen.getByRole('button', { name: 'Open task dashboard' }))
    const ask = await screen.findByRole('dialog')
    fireEvent.click(within(ask).getByRole('button', { name: /Cancel/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    // Refused: still on Schedules, with the draft.
    expect(screen.getByTestId('member-schedules')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()
  })

  it('reads the tab without asking which crew is the default', async () => {
    // The tab lists only what is attributed to the open crewmate, so which crew is the
    // default changes nothing here. A failing read of it must therefore cost the tab
    // nothing: no error, no withheld pane, no missing count.
    vi.mocked(api.defaultAgent).mockRejectedValue(new Error('boom'))
    await openCrewmate()
    fireEvent.click(chip())
    const body = await screen.findByTestId('member-schedules')
    await waitFor(() => expect(within(body).getAllByTestId('wake-row')).toHaveLength(2))
    expect(screen.getByTestId('member-schedules-count')).toHaveTextContent('1/2')
  })

  it('asks before the panel\'s own close control discards a draft', async () => {
    // Closing the panel destroys the tab body exactly as switching chips does, so it
    // asks the same question. Without the guard this was the one way out that dropped
    // typed work silently.
    await openCrewmate()
    fireEvent.click(chip())
    const body = await screen.findByTestId('member-schedules')
    await within(body).findByTestId('crew-wake-section')
    fireEvent.click(within(body).getByTestId('crew-wake-add'))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Check the board' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Read the board.' } })

    fireEvent.click(screen.getByRole('button', { name: /close panel/i }))
    const ask = await screen.findByRole('dialog')
    fireEvent.click(within(ask).getByRole('button', { name: /Cancel/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    // Refused: the panel is still there and so is what was typed.
    expect(screen.getByTestId('member-schedules')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()
  })

  it('asks before the overlay scrim discards a draft', async () => {
    // The scrim dismisses the panel without passing through its close control, so on a
    // phone a tap beside an open create form was a second silent discard path.
    Object.defineProperty(window, 'innerWidth', { value: 900, configurable: true, writable: true })
    // At this width the panel is a dismissable overlay and starts closed, so the
    // shared opener does not apply — open it from the header first.
    ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({
      members: [row()], default_agent: 'kirocrew',
    })
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementation((slug: string) =>
      Promise.resolve({ slot_key: `member-${slug}`, slug, member: slug, created: false }),
    )
    renderWithProviders(<MembersPage />)
    fireEvent.click(await screen.findByText('oncall'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall'))
    fireEvent.click(await screen.findByTestId('member-panel-toggle'))
    await screen.findByTestId('member-dashboard')
    fireEvent.click(chip())
    const body = await screen.findByTestId('member-schedules')
    await within(body).findByTestId('crew-wake-section')
    const panel = screen.getByTestId('member-side-panel')
    expect(panel).toHaveAttribute('data-placement', 'overlay')

    fireEvent.click(within(body).getByTestId('crew-wake-add'))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Check the board' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Read the board.' } })

    fireEvent.click(panel)
    const ask = await screen.findByRole('dialog')
    fireEvent.click(within(ask).getByRole('button', { name: /Cancel/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.getByTestId('member-schedules')).toBeInTheDocument()
  })

  it('drops the count when the cron list cannot be read, and keeps the chip', async () => {
    vi.mocked(api.crons).mockRejectedValue(new Error('boom'))
    await openCrewmate()
    // The chip stays reachable — the failure is about the list, not the surface —
    // but it states no count: absence of an answer is not an answer of none.
    await waitFor(() => expect(chip()).toBeInTheDocument())
    expect(screen.queryByTestId('member-schedules-count')).toBeNull()
  })

  it('asks before a transcript file link opens a panel tab over a draft', async () => {
    // A file link in the DM transcript opens a panel tab, which unmounts the Schedules
    // body, and `tabsCtl.openFile` focuses that tab directly without consulting any
    // `onBeforeLeave`. Third surface to reach this unmount around the guard, after the
    // Command Center dock and the narrow-window Back.
    await openCrewmate()
    fireEvent.click(chip())
    const body = await screen.findByTestId('member-schedules')
    await within(body).findByTestId('crew-wake-section')
    fireEvent.click(within(body).getByTestId('crew-wake-add'))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Check the board' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Read the board.' } })

    fireEvent.click(screen.getByRole('button', { name: 'Open file link' }))
    const ask = await screen.findByRole('dialog')
    fireEvent.click(within(ask).getByRole('button', { name: /Cancel/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    // Refused: still on Schedules, with the draft, and no file read was started.
    expect(screen.getByTestId('member-schedules')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()
  })

  it('asks before a driving-session row leaves for the chat page over a draft', async () => {
    // The thread lists the sessions this crewmate is driving, and a row opens one on
    // `/chat` -- leaving `/members` entirely by a raw `navigate`, which the leave channel
    // never sees. Same class as the identity pill, and the newest of these exits.
    await openCrewmate()
    fireEvent.click(chip())
    const body = await screen.findByTestId('member-schedules')
    await within(body).findByTestId('crew-wake-section')
    fireEvent.click(within(body).getByTestId('crew-wake-add'))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Check the board' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Read the board.' } })

    navigateSpy.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'Open driving session' }))
    const ask = await screen.findByRole('dialog')
    fireEvent.click(within(ask).getByRole('button', { name: /Cancel/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    // Refused: still on Schedules with the draft, and the route never changed.
    expect(screen.getByTestId('member-schedules')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()
    expect(navigateSpy).not.toHaveBeenCalledWith(expect.stringContaining('/chat'))
  })

  it('drops the count when a REFETCH fails, not just a first read', async () => {
    // A failed refetch keeps the last successful answer in the query's `data`, so a
    // check for absent data alone let the chip go on stating a count that was read
    // before the failure -- the same false claim as "0 schedules" on a failed request,
    // one keystroke later.
    await openCrewmate()
    await waitFor(() => expect(screen.getByTestId('member-schedules-count')).toHaveTextContent('1/2'))

    // The count query is gated on the panel being visible, so hiding and reshowing it
    // is a real gesture that refetches. This time the read fails.
    vi.mocked(api.crons).mockRejectedValue(new Error('boom'))
    act(() => { window.dispatchEvent(new Event('toggle-activity-panel')) })
    act(() => { window.dispatchEvent(new Event('toggle-activity-panel')) })

    await waitFor(() => expect(screen.queryByTestId('member-schedules-count')).toBeNull())
    // The chip itself stays: the failure is about the list, not the surface.
    expect(chip()).toBeInTheDocument()
  })

  it('does not hand the default crewmate a job that belongs to nobody', async () => {
    // `kirocrew` IS the default crew, and the crew editor's pane WOULD list the
    // ownerless job there (`wakesCrew`'s last fallback). This tab does not: it answers
    // what wakes this crewmate, and a schedule with no crewmate is nobody's. It stays
    // on `/schedule`. So the default crewmate reads as having none.
    await openCrewmate('kirocrew')
    fireEvent.click(chip())
    const body = await screen.findByTestId('member-schedules')
    await within(body).findByTestId('crew-wake-section')
    await waitFor(() => expect(within(body).queryAllByTestId('wake-row')).toHaveLength(0))
    expect(screen.queryByTestId('member-schedules-count')).toBeNull()
  })

  it('a crewmate nothing wakes gets a quiet pane and no badge', async () => {
    vi.mocked(api.crons).mockResolvedValue({ jobs: [] } as never)
    await openCrewmate()
    await waitFor(() => expect(chip()).toBeInTheDocument())
    // No `0/0`: every unscheduled crewmate would carry that forever.
    expect(screen.queryByTestId('member-schedules-count')).toBeNull()
    fireEvent.click(chip())
    const body = await screen.findByTestId('member-schedules')
    await within(body).findByTestId('crew-wake-section')
    expect(within(body).queryAllByTestId('wake-row')).toHaveLength(0)
    // The panel's own wording, not the editor's: most crewmates have none, so this is
    // the line the reader usually gets, and the editor's copy calls it an "agent".
    expect(within(body).getByText(/Nothing wakes this crewmate on its own yet/i)).toBeInTheDocument()
  })
})
