import { describe, it, expect, vi, beforeEach } from 'vitest'
import { PREVIEW_DASHBOARD } from '../../utils/previewFlags'
import { useState } from 'react'
import { screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { NavigationLeaveGuardProvider, useMayLeaveForNavigation } from '../../components/NavigationLeaveGuard'
import { __resetPanelTabs } from '../../hooks/usePanelTabs'

/* The crewmate's schedules, on the Profile card (crewmate-panel IA).
 *
 * The side panel used to carry the crew editor's Schedules pane as a fourth
 * chip with a live/total badge. The panel is the Dashboard and the Workspace
 * file browser now; what wakes a crewmate is the card's Schedules tab — a
 * READABLE list, one row per schedule, no count anywhere — and "New schedule"
 * pushes the crew editor's OWN pane (`CrewWakeSection`) as a page over the
 * card, so there is still exactly one schedules editor in the product. These
 * cases pin:
 *
 *   - The strip has no Schedules chip and no badge; the card's tab lists only
 *     what is attributed to THIS crewmate (immutable id first), and a job
 *     belonging to nobody is nobody's — the default crewmate included.
 *   - New schedule mounts the real `CrewWakeSection`, and a schedule created
 *     there is filed exactly as before: `member_id` is the crewmate's NAME,
 *     `agent` the template for a persisted identity, the display name for a
 *     legacy one.
 *   - The unsaved-draft guards that matter on this surface still ask: the
 *     card's own close / back / outside-click, a crewmate switch from the
 *     switcher, a team row, the narrow-window Back, a driving-session row, the
 *     route itself, and a reload — and an in-flight create is never discardable.
 *     Panel-only hides, file opens, Side Chat switches, and Workspace from a
 *     FLOATING card keep the Profile draft mounted and therefore do not ask;
 *     Workspace from the DOCKED column unmounts the card and does.
 *
 * Its own file rather than a block in `MembersPage.test.tsx`: the `below md`
 * describe there leaves `useIsMobile` answering mobile, so a later case that
 * needs the wide layout finds no header switcher at all.
 */

vi.mock('../../api/client', () => ({
  api: {
    members: vi.fn(),
    teams: { list: vi.fn(() => Promise.resolve({ teams: [] })) },
    // The warm greeting's read of the crewmate's work ledger. No ledger is the
    // state every case not about the greeting wants: the chat opens bare.
    crewBoard: vi.fn(() => Promise.reject(Object.assign(new Error('no_ledger'), { status: 404 }))),
    memberRecap: vi.fn(() => Promise.reject(new Error('no recap in this test'))),
    memberThread: vi.fn(),
    memberActivity: vi.fn(() => Promise.resolve({ slug: '', member: '', capped: false, entries: [] })),
    memberProjections: vi.fn(() => Promise.resolve({ asOfSeq: 0, values: {} })),
    memberBriefing: vi.fn(() => Promise.resolve({ slug: '', member: '', supported: true, text: '', updated_ts: null, redacted: false, truncated: false })),
    memberPanel: vi.fn(() => Promise.resolve({ panel: null, html: null })),
    autonudgeList: vi.fn(() => Promise.resolve({ enabled: true, loops: [] })),
    // The card's description comes from the crew registry; none of these cases need one.
    kirocrewAgents: vi.fn(() => Promise.resolve({ agents: [], default_agent: 'kirocrew' })),
    // The card's list and the pushed section read one cron list and filter it per
    // crewmate. Each case sets its own jobs.
    crons: vi.fn(() => Promise.resolve({ jobs: [] })),
    // The create the draft-guard cases drive; each one controls its own resolution.
    createCron: vi.fn(() => Promise.resolve({ ok: true, id: 'j-new' })),
    updateCron: vi.fn(() => Promise.resolve({ ok: true })),
    defaultAgent: vi.fn(() => Promise.resolve({ default_agent: 'kirocrew' })),
    // Reached by the pushed section's row controls via `useCronActions`.
    toggleCron: vi.fn(() => Promise.resolve({})),
    runCron: vi.fn(() => Promise.resolve({})),
    cancelCron: vi.fn(() => Promise.resolve({})),
    cronToChat: vi.fn(() => Promise.resolve({})),
    models: vi.fn(() => Promise.resolve([])),
    agentCatalog: vi.fn(() => Promise.resolve({ agents: [], default_agent: 'kirocrew' })),
    workspaces: vi.fn(() => Promise.resolve({ workspaces: [] })),
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
  // Driving-session and quiet-chat links are page-owned exits from the pushed
  // schedule form, so the stub exposes both callbacks directly. File and Side
  // Chat actions stay inside the Members page and must leave Profile mounted.
  default: ({
    slotKey,
    onSessionOpen,
    onOpenCrewWorkLog,
    openSideChat,
    onFileOpen,
  }: {
    slotKey: string
    onSessionOpen?: (k: string) => void
    onOpenCrewWorkLog?: () => void
    openSideChat?: (slot: string) => boolean
    onFileOpen?: (path: string) => void
  }) => (
    <div data-testid="chat-pane-stub">
      {slotKey}
      <button onClick={() => onSessionOpen?.('chat-77')}>Open driving session</button>
      <button onClick={onOpenCrewWorkLog}>Open quiet sessions</button>
      <button onClick={() => openSideChat?.(slotKey)}>Open side chat</button>
      <button onClick={() => onFileOpen?.('/tmp/readme.md')}>Open file</button>
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
const NARROW_WINDOW = 900

function row(overrides: Record<string, unknown> = {}) {
  return {
    name: 'oncall', slug: 'oncall', bound: true, slot_key: 'member-oncall', running: false,
    kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '',
    ...overrides,
  }
}

/** Two jobs on `oncall`, one of them paused, plus one belonging to nobody. */
const JOBS = [
  { id: 'j1', name: 'triage new issues', message: 'go', enabled: true, schedule: '0 9 * * *', last_status: 'ok', agent: 'shared-template', member_id: 'oncall' },
  { id: 'j2', name: 'weekly digest', message: 'go', enabled: false, schedule: 'every 7d', last_status: '', agent: 'shared-template', member_id: 'oncall' },
  { id: 'j3', name: 'nightly backup', message: 'go', enabled: true, schedule: 'every 24h', last_status: 'ok', agent: '', member_id: '' },
]

function setWindowWidth(px: number) {
  Object.defineProperty(window, 'innerWidth', { value: px, configurable: true, writable: true })
}

function mockRoster(members: Array<Record<string, unknown>>) {
  ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members, default_agent: 'kirocrew' })
  // Echo the requested slug: a fixed answer would report `member: <first>` for every
  // crewmate, which the page reads as a slug collision and renders instead of the thread.
  ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementation((slug: string) =>
    Promise.resolve({ slot_key: `member-${slug}`, slug, member: members.find((m) => m.slug === slug)?.name ?? slug, created: false }),
  )
}

/** Opens `name`'s thread. The roster rows are the only `name` text on screen then. */
async function openCrewmate(name = 'oncall', alsoRoster: string[] = []) {
  mockRoster([name, ...alsoRoster].map((n) => row({ name: n, slug: n, slot_key: `member-${n}` })))
  renderWithProviders(
    <NavigationLeaveGuardProvider>
      <MembersPage />
      <LeaveProbe />
    </NavigationLeaveGuardProvider>,
  )
  fireEvent.click(await screen.findByText(name))
  await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent(`member-${name}`))
  await screen.findByTestId('member-identity-pill')
}

/** The header pill opens the card; its Schedules tab is the list. */
async function openSchedulesTab() {
  fireEvent.click(screen.getByTestId('member-identity-pill'))
  const card = await screen.findByTestId('crew-profile-panel')
  fireEvent.click(within(card).getByRole('tab', { name: 'Schedules' }))
  return await screen.findByTestId('crew-schedule-list')
}

/** "New schedule" pushes the crew editor's pane with its form already open. */
async function openCreateForm() {
  fireEvent.click(screen.getByTestId('crew-schedule-create'))
  const page = await screen.findByTestId('crew-profile-page-new-schedule')
  const section = await within(page).findByTestId('crew-wake-section')
  await screen.findByLabelText('Name')
  return section
}

async function typeDraft(name = 'Check the board') {
  fireEvent.change(await screen.findByLabelText('Name'), { target: { value: name } })
  fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Read the board.' } })
}

/** Open the thread, the card, the Schedules tab, the create form, and type a draft. */
async function openDraft(name = 'oncall', alsoRoster: string[] = []) {
  await openCrewmate(name, alsoRoster)
  await openSchedulesTab()
  const section = await openCreateForm()
  await typeDraft()
  return section
}

/** Answer the discard question. Scoped to the dialog: the section's own "Cancel new
 *  schedule" toggle is on screen at the same time and matches the same name. */
async function answer(choice: 'Cancel' | 'Discard') {
  const ask = await screen.findByRole('dialog')
  fireEvent.click(within(ask).getByRole('button', { name: new RegExp(choice, 'i') }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
}

const rows = () => screen.getAllByTestId('crew-schedule-row')
const strip = () =>
  within(screen.getByTestId('side-panel-leading-tabs')).getAllByRole('tab').map((t) => t.getAttribute('aria-label'))

/** Stands in for an app-shell navigation surface (sidebar, palette, Back): asks the
 *  page's registered leave guards and records the answer. */
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
  delete (window as unknown as { __kirocrewPluginHandlesFiles?: boolean }).__kirocrewPluginHandlesFiles
  // The Dashboard tab and the in-chat dock are a Feature Preview, on here.
  localStorage.setItem(PREVIEW_DASHBOARD, '1')
  __resetPanelTabs()
  setWindowWidth(WIDE_WINDOW)
  vi.mocked(api.crons).mockResolvedValue({ jobs: JOBS } as never)
  vi.mocked(api.defaultAgent).mockResolvedValue({ default_agent: 'kirocrew' } as never)
})

describe('MembersPage Profile card Schedules tab', () => {
  it('the side panel carries no Schedules chip and no count badge — the strip is Dashboard alone', async () => {
    await openCrewmate()
    await waitFor(() => expect(strip()).toEqual(['Dashboard']))
    expect(screen.queryByTestId(`side-panel-leading-tab-${CREW_SCHEDULES_TAB_ID}`)).toBeNull()
    expect(screen.queryByTestId('member-schedules-count')).toBeNull()
    // Nor is the editor's pane mounted anywhere before the card asks for it.
    expect(screen.queryByTestId('crew-wake-section')).toBeNull()
  })

  it('lists this crewmate\'s schedules as readable rows — name, when, state — and no others', async () => {
    await openCrewmate()
    const list = await openSchedulesTab()
    await waitFor(() => expect(within(list).getAllByTestId('crew-schedule-row')).toHaveLength(2))
    const [first, second] = rows()
    expect(first).toHaveTextContent('triage new issues')
    expect(first).toHaveTextContent('0 9 * * *')
    expect(first).toHaveAttribute('data-state', 'on')
    expect(second).toHaveTextContent('weekly digest')
    expect(second).toHaveAttribute('data-state', 'paused')
    // The ownerless job is not this crewmate's: `oncall` is not the default crew.
    expect(list).not.toHaveTextContent('nightly backup')
    // Readable, not the editor: no wake rows, no section, and still no count.
    expect(within(list).queryByTestId('wake-row')).toBeNull()
    expect(screen.queryByTestId('crew-wake-section')).toBeNull()
    expect(screen.queryByTestId('member-schedules-count')).toBeNull()
  })

  it('matches a private schedule on the crewmate\'s IMMUTABLE id, not its display name', async () => {
    // The fixtures above all have name === slug, which cannot tell the two identities
    // apart. A private schedule's `member_id` is the slug, so for a crewmate whose
    // display name is not already its own slug, matching on the name showed nothing.
    mockRoster([row({ name: 'Radar One', slug: 'radar-one', slot_key: 'member-radar-one' })])
    vi.mocked(api.crons).mockResolvedValue({
      jobs: [
        { id: 'p1', name: 'triage', message: 'go', enabled: true, schedule: '0 9 * * *', last_status: '', agent: 'shared-template', member_id: 'radar-one' },
        { id: 'p2', name: 'sweep', message: 'go', enabled: false, schedule: 'every 6h', last_status: '', agent: 'shared-template', member_id: 'radar-one' },
      ],
    } as never)
    renderWithProviders(<NavigationLeaveGuardProvider><MembersPage /><LeaveProbe /></NavigationLeaveGuardProvider>)
    fireEvent.click(await screen.findByText('Radar One'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-radar-one'))
    await screen.findByTestId('member-identity-pill')
    const list = await openSchedulesTab()
    await waitFor(() => expect(within(list).getAllByTestId('crew-schedule-row')).toHaveLength(2))
  })

  it('does not hand the default crewmate a job that belongs to nobody', async () => {
    // `kirocrew` IS the default crew, and the crew editor's pane WOULD list the
    // ownerless job there (`wakesCrew`'s last fallback). The card does not: it answers
    // what wakes this crewmate, and a schedule with no crewmate is nobody's.
    await openCrewmate('kirocrew')
    const list = await openSchedulesTab()
    await within(list).findByTestId('crew-schedule-empty')
    expect(list).not.toHaveTextContent('nightly backup')
  })

  it('a crewmate nothing wakes gets the quiet line, in the panel\'s own words', async () => {
    vi.mocked(api.crons).mockResolvedValue({ jobs: [] } as never)
    await openCrewmate()
    const list = await openSchedulesTab()
    expect(await within(list).findByTestId('crew-schedule-empty')).toHaveTextContent(/Nothing wakes this crewmate on its own yet/i)
    // No `0` anywhere — not on the rail, not in the list.
    expect(screen.queryByTestId('member-schedules-count')).toBeNull()
    expect(within(screen.getByTestId('crew-profile-tabs')).queryByText('0')).toBeNull()
  })

  it('reads the list without asking which crew is the default', async () => {
    // The card lists only what is attributed to the open crewmate, so which crew is the
    // default changes nothing here. A failing read of it must cost the list nothing.
    vi.mocked(api.defaultAgent).mockRejectedValue(new Error('boom'))
    await openCrewmate()
    const list = await openSchedulesTab()
    await waitFor(() => expect(within(list).getAllByTestId('crew-schedule-row')).toHaveLength(2))
  })

  it('an unreadable cron list is not reported as "nothing wakes this crewmate"', async () => {
    // Absence of an answer is not an answer of none. The old chip dropped its badge on a
    // failed read for exactly this reason; the list must not state the affirmative
    // empty line on the strength of a request that failed.
    vi.mocked(api.crons).mockRejectedValue(new Error('boom'))
    await openCrewmate()
    const list = await openSchedulesTab()
    await waitFor(() => expect(vi.mocked(api.crons)).toHaveBeenCalled())
    await new Promise((r) => setTimeout(r, 50))
    expect(within(list).queryByTestId('crew-schedule-empty')).toBeNull()
    expect(list).not.toHaveTextContent(/Nothing wakes this crewmate/i)
  })

  it('a row opens that job on the Schedule page; the footer opens the page itself', async () => {
    await openCrewmate()
    const list = await openSchedulesTab()
    await waitFor(() => expect(within(list).getAllByTestId('crew-schedule-row')).toHaveLength(2))
    navigateSpy.mockClear()
    fireEvent.click(within(list).getByRole('button', { name: /triage new issues/ }))
    await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith('/schedule?job=j1'))
    navigateSpy.mockClear()
    fireEvent.click(screen.getByTestId('crew-schedule-open-all'))
    await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith('/schedule'))
  })

  it('reopens Sessions when its quiet-chat link is used again from another card tab', async () => {
    await openCrewmate()
    fireEvent.click(screen.getByRole('button', { name: 'Open quiet sessions' }))
    const firstCard = await screen.findByTestId('crew-profile-panel')
    expect(within(firstCard).getByRole('tab', { name: 'Sessions' })).toHaveAttribute('aria-selected', 'true')

    fireEvent.click(within(firstCard).getByRole('tab', { name: 'Profile' }))
    expect(within(firstCard).getByRole('tab', { name: 'Profile' })).toHaveAttribute('aria-selected', 'true')

    fireEvent.click(screen.getByRole('button', { name: 'Open quiet sessions' }))
    await waitFor(() => {
      const reopened = screen.getByTestId('crew-profile-panel')
      expect(reopened).not.toBe(firstCard)
      expect(within(reopened).getByRole('tab', { name: 'Sessions' })).toHaveAttribute('aria-selected', 'true')
    })
  })
})

describe('MembersPage Profile card — New schedule', () => {
  it('pushes the crew editor\'s own pane as a page over the card, with a back control naming the crewmate', async () => {
    await openCrewmate()
    await openSchedulesTab()
    fireEvent.click(screen.getByTestId('crew-schedule-create'))
    const page = await screen.findByTestId('crew-profile-page-new-schedule')
    // The SAME section the crew editor mounts, not a look-alike list — scoped to this
    // crewmate's own jobs.
    const body = within(page).getByTestId('member-schedules')
    const section = within(body).getByTestId('crew-wake-section')
    await waitFor(() => expect(within(section).getAllByTestId('wake-row')).toHaveLength(2))
    expect(section).not.toHaveTextContent('nightly backup')
    expect(screen.getByTestId('crew-profile-pushed-title')).toHaveTextContent('New schedule')
    expect(screen.getByTestId('crew-profile-back')).toHaveTextContent('oncall')

    // The pushed page is already the New schedule action, so its form opens in place.
    expect(within(section).getByTestId('crew-wake-create')).toBeInTheDocument()
    expect(await screen.findByLabelText('Name')).toBeInTheDocument()
    // The page's bar is the one title and the one exit: the section's own heading
    // and its New / Cancel toggle are withheld here (`chromeless`), so there is no
    // second "What wakes this crewmate" and no second close control under the bar.
    expect(section).toHaveAttribute('data-chromeless', 'true')
    expect(within(section).queryByTestId('crew-wake-add')).toBeNull()
    expect(within(page).queryByText('What wakes this crewmate')).toBeNull()
    expect(within(page).queryAllByRole('button', { name: /cancel new schedule/i })).toHaveLength(0)
    // The form's pinned-crew hint speaks this page's noun: the reader is on the
    // crewmate's Profile, not in the crew editor the editor's sentence names.
    expect(within(section).getByText("Created from this crewmate's profile, so the job runs as this crewmate.")).toBeInTheDocument()
    expect(within(section).queryByText(/this crew's editor/)).toBeNull()
    // A clean form: back pops to the list with no question asked.
    fireEvent.click(screen.getByTestId('crew-profile-back'))
    await waitFor(() => expect(screen.queryByTestId('crew-profile-page-new-schedule')).toBeNull())
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByTestId('crew-schedule-list')).toBeInTheDocument()
  })

  it('writes the provider TEMPLATE for a crewmate whose identity persists, filed under its NAME', async () => {
    // `agent` and `member_id` are different fields and the form has to pass both. For a
    // crewmate with a persisted identity the server keeps `member_id`, so `wakesCrew`
    // matches on that and `agent` is free to carry the template — which is what
    // `/schedule` labels the job by. `member_id` is the roster row's NAME, never a
    // derived id: slugification is lossy and a derived id can collide.
    mockRoster([row({
      name: 'radar', slug: 'radar', slot_key: 'member-radar', kiro_agent: 'kirocrew-worker',
      memory_version: 2, memory_owner: 'radar',
    })])
    renderWithProviders(<NavigationLeaveGuardProvider><MembersPage /><LeaveProbe /></NavigationLeaveGuardProvider>)
    fireEvent.click(await screen.findByText('radar'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-radar'))
    await screen.findByTestId('member-identity-pill')
    await openSchedulesTab()
    const section = await openCreateForm()
    await typeDraft()
    fireEvent.click(within(section).getByTestId('crew-wake-create-submit'))

    await waitFor(() => expect(api.createCron).toHaveBeenCalled())
    const submitted = vi.mocked(api.createCron).mock.calls[0][0] as { agent?: string; member_id?: string }
    expect(submitted.agent).toBe('kirocrew-worker')
    expect(submitted.member_id).toBe('radar')
    // And the record the server would keep is listed back under this crewmate.
    expect(wakesCrew(
      { id: 'j-new', name: 'Check the board', member_id: 'radar', agent: 'kirocrew-worker' } as never,
      'radar', false, 'radar',
    )).toBe(true)
  })

  it('writes the DISPLAY NAME for a crewmate whose identity does not persist, and lists it back', async () => {
    // A legacy crewmate has no persisted identity, so the server CLEARS `member_id` as
    // the job is created and `wakesCrew` falls through to comparing `agent` against the
    // display name. Writing the provider template there matched neither field.
    mockRoster([row({
      name: 'Radar One', slug: 'radar-one', slot_key: 'member-radar-one',
      kiro_agent: 'kirocrew-worker', memory_version: 1, memory_owner: '',
    })])
    renderWithProviders(<NavigationLeaveGuardProvider><MembersPage /><LeaveProbe /></NavigationLeaveGuardProvider>)
    fireEvent.click(await screen.findByText('Radar One'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-radar-one'))
    await screen.findByTestId('member-identity-pill')
    await openSchedulesTab()
    const section = await openCreateForm()
    await typeDraft()
    fireEvent.click(within(section).getByTestId('crew-wake-create-submit'))

    await waitFor(() => expect(api.createCron).toHaveBeenCalled())
    const submitted = vi.mocked(api.createCron).mock.calls[0][0] as { agent?: string; member_id?: string }
    expect(submitted.agent).toBe('Radar One')
    expect(submitted.member_id).toBe('Radar One')
    expect(wakesCrew(
      { id: 'j-new', name: 'Check the board', member_id: '', agent: 'Radar One' } as never,
      'Radar One', false, 'radar-one',
    )).toBe(true)
  })
})

describe('MembersPage Profile card — unsaved schedule draft', () => {
  it('asks before the card\'s close control discards a draft, and keeps the draft on Cancel', async () => {
    await openDraft()
    fireEvent.click(screen.getByTestId('crew-profile-close'))
    await answer('Cancel')
    expect(screen.getByTestId('crew-profile-panel')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()
    // Confirmed: the card goes, draft and all.
    fireEvent.click(screen.getByTestId('crew-profile-close'))
    await answer('Discard')
    await waitFor(() => expect(screen.queryByTestId('crew-profile-panel')).toBeNull())
    expect(screen.queryByDisplayValue('Check the board')).toBeNull()
  })

  it('asks before the card\'s back control or Escape pops the form away', async () => {
    await openDraft()
    fireEvent.click(screen.getByTestId('crew-profile-back'))
    await answer('Cancel')
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()
    expect(screen.getByTestId('crew-profile-page-new-schedule')).toBeInTheDocument()

    fireEvent.keyDown(screen.getByTestId('crew-profile-panel'), { key: 'Escape' })
    await answer('Cancel')
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()

    // Confirmed: back to the list, form gone.
    fireEvent.click(screen.getByTestId('crew-profile-back'))
    await answer('Discard')
    await waitFor(() => expect(screen.queryByTestId('crew-profile-page-new-schedule')).toBeNull())
    expect(screen.getByTestId('crew-schedule-list')).toBeInTheDocument()
  })

  it('the identity pill closes an open card through the same question instead of remounting it over a draft', async () => {
    // The pill is `aria-expanded` and a press on it while the card is open is a
    // CLOSE. Re-opening set the card's tab back to Profile, and the tab is part of
    // its React key, so a card opened on another tab (the quiet-chat Sessions link)
    // was remounted by that press and the draft inside it destroyed unasked. Pointer
    // users rarely reach the pill under the floating card's scrim; keyboard users do.
    await openCrewmate()
    fireEvent.click(screen.getByRole('button', { name: 'Open quiet sessions' }))
    const card = await screen.findByTestId('crew-profile-panel')
    expect(screen.getByRole('tab', { name: 'Sessions' })).toHaveAttribute('aria-selected', 'true')
    fireEvent.click(within(card).getByRole('tab', { name: 'Schedules' }))
    await screen.findByTestId('crew-schedule-list')
    await openCreateForm()
    await typeDraft()
    expect(screen.getByTestId('member-identity-pill')).toHaveAttribute('aria-expanded', 'true')

    fireEvent.click(screen.getByTestId('member-identity-pill'))
    await answer('Cancel')
    // Refused: the SAME card, still on its pushed page, with what was typed.
    expect(screen.getByTestId('crew-profile-page-new-schedule')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('member-identity-pill'))
    await answer('Discard')
    await waitFor(() => expect(screen.queryByTestId('crew-profile-panel')).toBeNull())
    expect(screen.getByTestId('member-identity-pill')).toHaveAttribute('aria-expanded', 'false')
  })

  it('asks before a click beside the floating card dismisses it', async () => {
    await openDraft()
    // The card floats over the thread (the side panel is open on a wide window), and a
    // click on the surface around it closes it — the same question as the close control.
    const scrim = screen.getByTestId('crew-profile-modal')
    fireEvent.click(scrim)
    await answer('Cancel')
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()
  })

  it('will not close the card while a create is in flight — no prompt, no discard', async () => {
    // A POST already sent cannot be cancelled by unmounting, so that window refuses
    // outright; it is typed-but-unsent work that asks.
    let release: (v: unknown) => void = () => {}
    vi.mocked(api.createCron).mockReturnValue(new Promise((r) => { release = r }) as never)
    const section = await openDraft()
    fireEvent.click(within(section).getByTestId('crew-wake-create-submit'))
    await waitFor(() => expect(api.createCron).toHaveBeenCalled())
    fireEvent.click(screen.getByTestId('crew-profile-close'))
    await new Promise((r) => setTimeout(r, 120))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByTestId('crew-profile-panel')).toBeInTheDocument()
    act(() => { release({ ok: true, id: 'j-new' }) })
  })

  it('asks before a crewmate switch from the switcher discards a draft, and remounts the card once it does', async () => {
    // Two rules meet here. The card is keyed on the crewmate, so a switch REMOUNTS it and
    // the form cannot survive with its `memberId` silently rebound. And because that
    // remount destroys typed work, the switch asks first.
    await openDraft('oncall', ['scribe'])
    const pickScribe = async () => {
      fireEvent.click(screen.getByTestId('crewmate-switcher'))
      const list = await screen.findByTestId('crewmate-switcher-list')
      fireEvent.click(within(list).getByRole('option', { name: /scribe/ }))
    }
    await pickScribe()
    await answer('Cancel')
    // Refused: still on the same crewmate, with what was typed.
    expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()

    await pickScribe()
    await answer('Discard')
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-scribe'))
    await waitFor(() => expect(screen.queryByDisplayValue('Check the board')).toBeNull())
  })

  it('asks before a team header row discards a draft', async () => {
    // Opening a team clears the open crewmate, unmounting the card with the form in it.
    ;(api.teams.list as ReturnType<typeof vi.fn>).mockResolvedValue({
      teams: [{ id: 't1', name: 'Ops', members: ['oncall'] }],
    })
    await openDraft()
    fireEvent.click(await screen.findByText('Ops'))
    await answer('Cancel')
    expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()
  })

  it('asks before the narrow-window Back button drops the member param over a draft', async () => {
    // Below md the header carries a Back button that clears `?member=`; clearing the
    // param unmounts the card and the draft with it, silently, on an ordinary tap.
    await openDraft()
    fireEvent.click(screen.getByTestId('member-back'))
    await answer('Cancel')
    expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall')
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()
  })

  it('asks before the quiet-chat Sessions link replaces a pushed schedule draft', async () => {
    await openDraft()
    fireEvent.click(screen.getByRole('button', { name: 'Open quiet sessions' }))
    await answer('Cancel')
    expect(screen.getByTestId('crew-profile-page-new-schedule')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Open quiet sessions' }))
    await answer('Discard')
    await waitFor(() => expect(screen.queryByDisplayValue('Check the board')).toBeNull())
    expect(screen.getByRole('tab', { name: 'Sessions' })).toHaveAttribute('aria-selected', 'true')
  })

  it('asks before a driving-session row leaves for the chat page over a draft', async () => {
    // A row opens the session on `/chat` — leaving `/members` by a raw `navigate`, which
    // the leave channel never sees.
    await openDraft()
    navigateSpy.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'Open driving session' }))
    await answer('Cancel')
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()
    expect(navigateSpy).not.toHaveBeenCalledWith(expect.stringContaining('/chat'))
  })

  it('holds a reload while a draft is open, and lets go once it is discarded', async () => {
    // A reload never reaches a leave guard; the browser's own prompt is the only thing
    // that can, and it is armed off the same flag as the published navigation stake.
    await openCrewmate()
    await openSchedulesTab()
    const section = await openCreateForm()
    expect(holdsDocument()).toBe(false)
    await typeDraft()
    await waitFor(() => expect(holdsDocument()).toBe(true))

    // The pushed page's Back is the one cancel (the section's own toggle is withheld
    // there), and it routes through the host's confirm.
    expect(within(section).queryByTestId('crew-wake-add')).toBeNull()
    fireEvent.click(screen.getByTestId('crew-profile-back'))
    await answer('Discard')
    await waitFor(() => expect(holdsDocument()).toBe(false))
  })

  it('vetoes leaving the route while a draft is open, and lets a clean card through', async () => {
    // The navigation-leave registry is synchronous, so this exit uses `window.confirm`.
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    try {
      await openCrewmate()
      await openSchedulesTab()
      await openCreateForm()
      // Clean: the shell is allowed to leave without a prompt at all.
      expect(askToLeave()).toBe('true')
      expect(confirmSpy).not.toHaveBeenCalled()

      await typeDraft()
      expect(askToLeave()).toBe('false')
      expect(confirmSpy).toHaveBeenCalledWith(expect.stringMatching(/lose the schedule/i))
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('panel-only actions keep a dirty Profile draft mounted without asking to discard it', async () => {
    await openDraft()
    expect(screen.getByTestId('side-panel-root')).toBeInTheDocument()

    // The desktop panel chord hides only the side panel.
    act(() => { window.dispatchEvent(new Event('toggle-activity-panel')) })
    await waitFor(() => expect(screen.queryByTestId('side-panel-root')).toBeNull())
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()

    // A transcript file link likewise stays inside this page. The optional IDE
    // bridge makes the callback observable without performing a file read.
    const opened = vi.fn()
    window.addEventListener('kirocrew-file-open', opened)
    ;(window as unknown as { __kirocrewPluginHandlesFiles?: boolean }).__kirocrewPluginHandlesFiles = true
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Open file' }))
      expect(opened).toHaveBeenCalledTimes(1)
      expect(screen.queryByRole('dialog')).toBeNull()
      expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()
    } finally {
      window.removeEventListener('kirocrew-file-open', opened)
      delete (window as unknown as { __kirocrewPluginHandlesFiles?: boolean }).__kirocrewPluginHandlesFiles
    }
  })

  it('Side Chat switches the side panel without asking or unmounting a dirty Profile draft', async () => {
    await openDraft()
    fireEvent.click(screen.getByRole('button', { name: 'Open side chat' }))

    await waitFor(() => expect(screen.getByRole('tab', { name: 'Side Chat' })).toHaveAttribute('aria-selected', 'true'))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()
  })

  it('the overlay scrim hides only the panel and keeps a dirty Profile draft', async () => {
    setWindowWidth(NARROW_WINDOW)
    await openCrewmate()
    fireEvent.click(screen.getByTestId('member-panel-toggle'))
    const overlay = await screen.findByTestId('member-side-panel')
    expect(overlay).toHaveAttribute('data-placement', 'overlay')
    await openSchedulesTab()
    await openCreateForm()
    await typeDraft()

    fireEvent.click(overlay)
    await waitFor(() => expect(screen.queryByTestId('member-side-panel')).toBeNull())
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()
  })

  it('accepted Workspace navigation closes a docked Profile before revealing Files', async () => {
    localStorage.setItem('mc-members-panel-open', '0')
    await openDraft()
    expect(screen.getByTestId('crew-profile-docked')).toBeInTheDocument()
    // Keep the pushed schedule page (and its draft) mounted while selecting the
    // covered Profile tab, which puts the Workspace tile behind that page. The
    // covered subtree is inert to users; firing it directly establishes the
    // stale state this regression is about without weakening that a11y contract.
    fireEvent.click(screen.getByRole('tab', { name: 'Profile', hidden: true }))
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('crew-profile-workspace'))
    await answer('Discard')

    await waitFor(() => expect(screen.queryByTestId('crew-profile-panel')).toBeNull())
    expect(screen.queryByDisplayValue('Check the board')).toBeNull()
    expect(await screen.findByTestId('side-panel-root')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Files' })).toHaveAttribute('aria-selected', 'true')
  })

  it('Workspace from a FLOATING Profile opens Files without asking — the card and its draft stay mounted', async () => {
    // With the panel already open the card floats, and Files opens beside it:
    // nothing unmounts, so there is no draft to ask about. The old code asked
    // anyway, and "keep my draft" then blocked Files for a draft never at risk.
    await openDraft()
    expect(screen.getByTestId('side-panel-root')).toBeInTheDocument()
    expect(screen.getByTestId('crew-profile-modal')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-profile-docked')).toBeNull()
    fireEvent.click(screen.getByRole('tab', { name: 'Profile', hidden: true }))
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('crew-profile-workspace'))

    await waitFor(() => expect(screen.getByRole('tab', { name: 'Files' })).toHaveAttribute('aria-selected', 'true'))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByTestId('crew-profile-modal')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()
  })

  it('keeps a dirty Profile mounted when another tab reveals the side panel', async () => {
    localStorage.setItem('mc-members-panel-open', '0')
    await openDraft()
    expect(screen.getByTestId('crew-profile-docked')).toBeInTheDocument()

    act(() => {
      window.dispatchEvent(new StorageEvent('storage', {
        key: 'mc-members-panel-open',
        newValue: '1',
      }))
    })
    expect(await screen.findByTestId('side-panel-root')).toBeInTheDocument()
    expect(screen.getByTestId('crew-profile-docked')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('keeps the draft through a resize across the docking boundary, which no guard can decline', async () => {
    // `beside` is recomputed from the live window width. Dragging the window narrow and
    // back cannot be asked about (declining cannot un-resize a window), so the card must
    // stay mounted with its form rather than fold away and take the draft with it.
    await openDraft()
    setWindowWidth(NARROW_WINDOW)
    fireEvent(window, new Event('resize'))
    await waitFor(() => expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument())
    expect(screen.queryByRole('dialog')).toBeNull()

    setWindowWidth(WIDE_WINDOW)
    fireEvent(window, new Event('resize'))
    await waitFor(() => expect(screen.getByTestId('crew-profile-panel')).toBeInTheDocument())
    expect(screen.getByDisplayValue('Check the board')).toBeInTheDocument()
  })
})

describe('MembersPage Profile card — Memory tile', () => {
  async function openProfile(member: Record<string, unknown>) {
    mockRoster([row(member)])
    renderWithProviders(<NavigationLeaveGuardProvider><MembersPage /><LeaveProbe /></NavigationLeaveGuardProvider>)
    fireEvent.click(await screen.findByText(member.name as string))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent(`member-${member.slug}`))
    fireEvent.click(await screen.findByTestId('member-identity-pill'))
    const card = await screen.findByTestId('crew-profile-panel')
    fireEvent.click(within(card).getByRole('tab', { name: 'Profile' }))
    return await screen.findByTestId('crew-profile-memory')
  }

  it("opens THAT crewmate's private memory page, the store preselected", async () => {
    const tile = await openProfile({
      name: 'radar', slug: 'radar', slot_key: 'member-radar',
      memory_store: 'member-radar', memory_version: 2, memory_owner: 'radar',
    })
    navigateSpy.mockClear()
    fireEvent.click(tile)
    await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith('/settings/overview?view=memory&store=member-radar'))
  })

  it('stays a readout for a crewmate on the shared store', async () => {
    const tile = await openProfile({
      name: 'scout', slug: 'scout', slot_key: 'member-scout',
      memory_store: 'default', memory_version: 1, memory_owner: '',
    })
    expect(tile.tagName).toBe('DIV')
  })
})
