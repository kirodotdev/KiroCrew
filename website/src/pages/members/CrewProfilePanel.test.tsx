import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { unregisteredMarkers } from '../../test/guideTargets'
import { cleanup, render, screen, fireEvent, within, act, waitFor } from '@testing-library/react'
import type { MemberRosterRow } from '../../api/client'
import type { LegacyGoalLoop, StructuredMonitor } from '../../monitoring/automation'
import type { CronJob } from '../../types'

/* The crewmate's profile card (crewmate-panel IA), in isolation from the page
 * that feeds it. What is pinned here:
 *
 *   - The rail is the shared `Tablist` with `labels="active"`: every tab keeps
 *     its accessible name, but only the SELECTED one shows its word — the rest
 *     are their icon alone. A rail in a column a third of the row wide cannot
 *     carry five words.
 *   - Profile is a summary with doors, Schedules is the readable list, and
 *     "New schedule" PUSHES the host's create form as a page over the card with
 *     one back control pointing at the crewmate — the card never grows a second
 *     schedules editor of its own.
 *   - Escape pops a pushed page first and closes the card second.
 */

vi.mock('../../components/crew/CrewMonitoredAvatar', () => ({
  default: ({ seed, automation }: { seed: string; automation?: unknown }) => (
    <span data-testid="avatar-stub" data-monitor-active={automation ? 'true' : 'false'}>{seed}</span>
  ),
}))

import { useContext } from 'react'
import { createPortal } from 'react-dom'
import CrewProfilePanel, { PROFILE_TABS, ProfilePagePushContext } from './CrewProfilePanel'

const member = (overrides: Partial<MemberRosterRow> = {}): MemberRosterRow => ({
  name: 'oncall', slug: 'oncall', bound: true, slot_key: 'member-oncall', running: false,
  // `workspace` is the configured workspace's NAME, the way the roster carries it.
  kiro_agent: 'kirocrew', workspace: 'oncall-desk', memory_store: 'default', model: '',
  ...overrides,
} as MemberRosterRow)

const NOW = 1_800_000_000
const JOBS: CronJob[] = [
  { id: 'j1', name: 'triage new issues', message: 'go', enabled: true, schedule: '0 9 * * *', last_status: 'ok', next_run_ts: NOW + 600 } as CronJob,
  { id: 'j2', name: 'weekly digest', message: 'go', enabled: false, schedule: 'every 7d', last_status: '' } as CronJob,
]

const ACTIVE_MONITOR: StructuredMonitor = {
  kind: 'structured_monitor', id: 'monitor-1', slotKey: 'member-oncall', active: true,
  actionable: true, version: 1, monitorKind: 'github_pull_request', objective: 'review_ready',
  target: 'https://github.com/kirodotdev/KiroCrew/pull/42', createdAt: NOW - 3600,
  cadenceSecs: 300, nextProbeAt: NOW + 300, wakeInstructions: '',
  budgets: { maxRuntimeSecs: 14_400, maxAgentTurns: 8, maxTokens: 250_000, maxProviderErrors: 3 },
  latest: { classification: 'pending', reasonCode: 'checks_pending', observedAt: NOW - 30, decision: 'no_change' },
  usage: { probes: 5, wakes: 1, agentTurns: 1, inputTokens: 120, outputTokens: 20, providerErrors: 0, tokenUsageKnown: true },
  action: { wakeInFlight: false, wakeDelivery: '' }, terminal: null,
}

const WORK_LEDGER_MONITOR: LegacyGoalLoop = {
  kind: 'legacy_goal_loop', id: 'ledger-1', slotKey: 'member-oncall', message: '',
  createdAt: NOW - 1200, idleSecs: 600, maxCycles: 300, cycleCount: 9,
  active: true, lastFireAt: NOW - 600, nextDueAt: NOW + 600,
  maxRuntimeSecs: 86_400, stoppedReason: '', monitorOutcome: '', monitorKind: 'work-ledger',
}

type Handlers = {
  onClose: ReturnType<typeof vi.fn>
  onEdit: ReturnType<typeof vi.fn>
  onOpenFiles: ReturnType<typeof vi.fn>
  onOpenSchedule: ReturnType<typeof vi.fn>
  /** The host's draft guard for a pushed page's back action. The default here lets
   *  every back through; the guard case below replaces it with one that refuses. */
  onRequestBack: ReturnType<typeof vi.fn>
}

function setup(overrides: Partial<Parameters<typeof CrewProfilePanel>[0]> = {}): Handlers {
  const h: Handlers = {
    onClose: vi.fn(), onEdit: vi.fn(), onOpenFiles: vi.fn(), onOpenSchedule: vi.fn(),
    onRequestBack: vi.fn((proceed: () => void) => proceed()),
  }
  render(
    <CrewProfilePanel
      member={member()}
      running={false}
      automation={null}
      monitorReadState="ready"
      description="Watches the on-call queue and files what it finds. Escalates only when a page has gone unanswered."
      memoryLabel="Private to this crewmate"
      schedules={JOBS}
      schedulesLoading={false}
      nowTs={NOW}
      newScheduleBody={<div data-testid="host-create-form">host form</div>}
      sessionsBody={<div data-testid="host-sessions">host sessions</div>}
      notesBody={<div data-testid="host-notes">host notes</div>}
      goalsBody={<div data-testid="host-goals">host goals</div>}
      // Most cases read the Profile tab's body; the card's own default is the
      // strip's first tab (Sessions), which the rail cases assert explicitly.
      initialTab="profile"
      {...h}
      {...overrides}
    />,
  )
  return h
}

const tab = (name: string) => screen.getByRole('tab', { name })
/** The label text a sighted user can see on a tab: anything not screen-reader-only. */
const visibleLabel = (el: HTMLElement) =>
  Array.from(el.querySelectorAll('span'))
    .filter((s) => !s.classList.contains('sr-only') && s.getAttribute('aria-hidden') !== 'true' && s.textContent?.trim())
    .map((s) => s.textContent?.trim())
    .join('')

beforeEach(() => {
  localStorage.clear()
})

// The forwarding proof: every marker the render drew is registered by its ref.
afterEach(() => { expect(unregisteredMarkers()).toEqual([]) })

describe('CrewProfilePanel rail (Tablist labels="active")', () => {
  it('has the four tabs in strip order, every one named for assistive tech', () => {
    setup()
    const rail = screen.getByRole('tablist', { name: 'Profile' })
    expect(within(rail).getAllByRole('tab').map((t) => t.getAttribute('title')))
      .toEqual(['Sessions', 'Schedules', 'Goals', 'Profile'])
    expect(PROFILE_TABS).toEqual(['sessions', 'schedule', 'goals', 'profile'])
    for (const name of ['Profile', 'Schedules', 'Sessions', 'Goals']) {
      expect(tab(name)).toBeInTheDocument()
    }
  })

  it('shows the word on the SELECTED tab only; the others are icon-only with a hidden name', () => {
    setup({ initialTab: undefined })
    // Opens on the first tab of the strip: Sessions.
    expect(tab('Sessions')).toHaveAttribute('aria-selected', 'true')
    expect(visibleLabel(tab('Sessions'))).toBe('Sessions')
    for (const name of ['Schedules', 'Goals', 'Profile']) {
      expect(visibleLabel(tab(name))).toBe('')
      // Still named: the label rides an sr-only span, and the tooltip carries it too.
      expect(tab(name)).toHaveAccessibleName(name)
      expect(tab(name)).toHaveAttribute('title', name)
    }
    // The word travels with the selection.
    fireEvent.click(tab('Goals'))
    expect(visibleLabel(tab('Goals'))).toBe('Goals')
    expect(visibleLabel(tab('Sessions'))).toBe('')
    expect(screen.getByTestId('crew-profile-panel')).toHaveAttribute('data-tab', 'goals')
  })

  it('opens on the tab the host asked for', () => {
    setup({ initialTab: 'schedule' })
    expect(tab('Schedules')).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByTestId('crew-profile-pane-schedule')).toBeInTheDocument()
  })
})

describe('CrewProfilePanel Profile tab', () => {
  it('shows exact active monitor details and no controls', () => {
    setup({ automation: ACTIVE_MONITOR })
    const details = screen.getByTestId('crew-profile-monitor')
    expect(details).toHaveAttribute('data-monitor-active', 'true')
    expect(details).toHaveTextContent('Active')
    expect(details).toHaveTextContent('Pull request')
    expect(details).toHaveTextContent('https://github.com/kirodotdev/KiroCrew/pull/42')
    expect(details).toHaveTextContent('5m')
    expect(details).toHaveTextContent('5 / No limit')
    expect(details).toHaveTextContent('1h / 4h')
    expect(within(details).queryByRole('button')).toBeNull()
    expect(within(details).queryByRole('textbox')).toBeNull()
    expect(within(details).queryByRole('link')).toBeNull()
    expect(screen.getByTestId('avatar-stub')).toHaveAttribute('data-monitor-active', 'true')
  })

  it('shows exact work-ledger cycle and budget values', () => {
    setup({ automation: WORK_LEDGER_MONITOR })
    const details = screen.getByTestId('crew-profile-monitor')
    expect(details).toHaveAttribute('data-monitor-active', 'true')
    expect(details).toHaveTextContent('Task list')
    expect(details).toHaveTextContent('10m')
    expect(details).toHaveTextContent('9 / 300')
    expect(details).toHaveTextContent('20m / 24h')
  })

  it('keeps a stopped legacy loop readable without an active avatar', () => {
    setup({ automation: { ...WORK_LEDGER_MONITOR, active: false, cycleCount: 300, stoppedReason: 'cycle_cap' } })
    const details = screen.getByTestId('crew-profile-monitor')
    expect(details).toHaveAttribute('data-monitor-active', 'false')
    expect(details).toHaveTextContent('Stopped')
    expect(details).toHaveTextContent('300 / 300')
    expect(details).toHaveTextContent('Unknown / 24h')
    expect(details).toHaveTextContent('Check limit reached')
  })

  it('shows no monitor as one explicit state without inventing details', () => {
    setup({ automation: null, monitorReadState: 'ready' })
    const details = screen.getByTestId('crew-profile-monitor')
    expect(details).toHaveTextContent('Not watching anything')
    expect(within(details).queryByText('Type')).toBeNull()
    expect(details).not.toHaveTextContent('—')
    expect(within(details).queryByRole('alert')).toBeNull()
  })

  it('shows loading as a check, not as an error', () => {
    setup({ automation: null, monitorReadState: 'loading' })
    const details = screen.getByTestId('crew-profile-monitor')
    expect(details).toHaveTextContent('Checking monitor status…')
    expect(within(details).queryByText('Type')).toBeNull()
    expect(within(details).queryByRole('alert')).toBeNull()
  })

  it('shows a stopped runtime budget as fully used', () => {
    setup({ automation: { ...WORK_LEDGER_MONITOR, active: false, stoppedReason: 'runtime_budget' } })
    expect(screen.getByTestId('crew-profile-monitor')).toHaveTextContent('24h / 24h')
  })

  it('uses a generic label for an unknown stop reason', () => {
    setup({ automation: { ...WORK_LEDGER_MONITOR, active: false, stoppedReason: 'future_reason' } })
    const details = screen.getByTestId('crew-profile-monitor')
    expect(details).toHaveTextContent('Stopped')
    expect(details).not.toHaveTextContent('future_reason')
  })

  it('fails closed when monitor state cannot be read', () => {
    setup({ automation: ACTIVE_MONITOR, monitorReadState: 'error' })
    const details = screen.getByTestId('crew-profile-monitor')
    expect(details).toHaveAttribute('data-monitor-active', 'false')
    expect(within(details).getByRole('alert')).toHaveTextContent('Monitor status unavailable')
    expect(details).not.toHaveTextContent('Tries again')
    expect(within(details).queryByText('Type')).toBeNull()
  })

  it('does not promise reconnect recovery for an unsupported monitor', () => {
    setup({ automation: { ...ACTIVE_MONITOR, actionable: false }, monitorReadState: 'ready' })
    const details = screen.getByTestId('crew-profile-monitor')
    expect(details).toHaveTextContent('Monitor status unavailable')
    expect(details).not.toHaveTextContent('Tries again when reconnected')
  })

  it.each([
    ['session_start_failures', 'The session could not start'],
    ['session_unavailable', 'The session could not start'],
    ['structural_terminal', 'The last cycle failed'],
    ['model_refused', 'The last cycle failed'],
    ['interrupted_cycle', 'The last cycle failed'],
    ['sentinel_dropped', 'The monitor could not continue'],
    ['consecutive_failures', 'The last cycle failed'],
  ])('shows failure stop %s through the error notice', (stoppedReason, message) => {
    setup({ automation: { ...WORK_LEDGER_MONITOR, active: false, stoppedReason } })
    const details = screen.getByTestId('crew-profile-monitor')
    expect(within(details).getByRole('alert')).toHaveTextContent(message)
  })

  it.each([
    ['user_stop', 'user_stop', 'You stopped it', false],
    ['success', '', 'The watch finished', false],
    ['budget', 'agent_turn_budget', 'Budget reached', false],
    ['session_close', 'session_close', 'Session closed', false],
    ['target_unavailable', 'not_found', 'Target unavailable', true],
    ['blocked', 'future_block_reason', 'The monitor could not continue', true],
    ['blocked', 'completion_evidence_unavailable', 'The monitor could not continue', true],
  ])('keeps structured %s terminal evidence readable', (outcome, reason, message, failed) => {
    setup({
      automation: {
        ...ACTIVE_MONITOR,
        active: false,
        terminal: { outcome, reason, stoppedAt: NOW },
      },
    })
    const details = screen.getByTestId('crew-profile-monitor')
    const rendered = failed ? within(details).getByRole('alert') : details
    expect(rendered).toHaveTextContent(message)
  })

  it('never renders the prompt, wake text, banner, sentinel path or session key', () => {
    setup({
      automation: { ...ACTIVE_MONITOR, slotKey: 'member-SESSION-MARKER', wakeInstructions: 'WAKE-MARKER' },
    })
    const leaks = ['WAKE-MARKER', 'PROMPT-MARKER', 'BANNER-MARKER', 'SENTINEL-MARKER', 'SESSION-MARKER']
    const structured = screen.getByTestId('crew-profile-monitor').textContent ?? ''
    cleanup()
    setup({
      automation: {
        ...WORK_LEDGER_MONITOR,
        slotKey: 'member-SESSION-MARKER',
        message: 'PROMPT-MARKER',
        stopSentinelPath: '/tmp/SENTINEL-MARKER',
        judge: { wake_when: 'WAKE-MARKER BANNER-MARKER' },
      },
    })
    const legacy = screen.getByTestId('crew-profile-monitor').textContent ?? ''
    for (const marker of leaks) {
      expect(structured).not.toContain(marker)
      expect(legacy).not.toContain(marker)
    }
  })

  it('is a summary with doors: description, memory, workspace folder, notes', () => {
    const h = setup()
    expect(screen.getByTestId('crew-profile-name')).toHaveTextContent('oncall')
    expect(screen.getByTestId('crew-profile-description')).toHaveTextContent(/Watches the on-call queue/)
    expect(screen.getByTestId('crew-profile-memory')).toHaveTextContent('Private to this crewmate')
    // The workspace tile names the configured workspace as-is: the value is a
    // NAME, not a path, so nothing is taken off it.
    expect(screen.getByTestId('crew-profile-workspace')).toHaveTextContent('oncall-desk')
    expect(within(screen.getByTestId('crew-profile-workspace')).getByTitle('oncall-desk')).toBeInTheDocument()

    const memory = screen.getByTestId('crew-profile-memory')
    const workspace = screen.getByTestId('crew-profile-workspace')
    expect(memory.tagName).toBe('DIV')
    // A readout: solid hairline, no fill, no hover, no pointer. Never dashed —
    // dashed is this card's empty / coming-soon language.
    expect(memory).toHaveClass('border-border', 'bg-transparent')
    expect(memory).not.toHaveClass('border-dashed')
    expect(memory).not.toHaveClass('hover:bg-bg-hover')
    expect(memory).not.toHaveClass('cursor-pointer')
    expect(workspace).toHaveClass('bg-bg', 'hover:bg-bg-hover', 'cursor-pointer')
    expect(workspace).not.toHaveClass('border-dashed')
    fireEvent.click(workspace)
    expect(h.onOpenFiles).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Edit crewmate' }))
    expect(h.onEdit).toHaveBeenCalledTimes(1)
  })

  it('the Memory tile is a door, by click and by keyboard, once the host hands it one', () => {
    const onOpenMemory = vi.fn()
    setup({ onOpenMemory })
    const memory = screen.getByTestId('crew-profile-memory')
    expect(memory.tagName).toBe('BUTTON')
    expect(memory).toHaveAccessibleName(/Memory/)
    fireEvent.click(memory)
    expect(onOpenMemory).toHaveBeenCalledTimes(1)
    // A native button with no tabindex override: Tab reaches it, Enter/Space press it.
    expect(memory).toHaveAttribute('type', 'button')
    expect(memory).not.toHaveAttribute('tabindex')
    memory.focus()
    expect(memory).toHaveFocus()
  })

  it('draws the host settings (permission, model, effort) in place of editor doors', () => {
    setup({ settingsBody: <div data-testid="host-settings">host settings</div> })
    expect(within(screen.getByTestId('crew-profile-pane-profile')).getByTestId('host-settings')).toBeInTheDocument()
    // Changed right here now, so the card no longer sends them to the editor.
    expect(screen.queryByTestId('crew-profile-permissions')).toBeNull()
    expect(screen.queryByTestId('crew-profile-model')).toBeNull()
  })

  it('carries no guide marker: the permission picker it holds is answered in words, not guided', () => {
    setup()
    expect(document.querySelectorAll('[data-ui-location]')).toHaveLength(0)
  })

  it('says there is no description instead of rendering an empty card', () => {
    setup({ description: '' })
    expect(screen.getByTestId('crew-profile-about')).toHaveTextContent(/No description yet/)
    expect(screen.queryByTestId('crew-profile-read-more')).toBeNull()
  })

  it('shows the long-term goal under About when there is no description', () => {
    setup({ description: '', goal: 'Keep the release notes current' })
    const about = screen.getByTestId('crew-profile-about')
    expect(screen.getByTestId('crew-profile-goal-fallback')).toHaveTextContent('Keep the release notes current')
    expect(about).toHaveTextContent('From the goal')
    expect(about).not.toHaveTextContent(/No description yet/)
  })

  it('says the goal could not be loaded instead of claiming there is no description', () => {
    setup({ description: '', goal: '', goalError: true })
    const notice = screen.getByTestId('crew-profile-goal-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent("Couldn't load the goals.")
    expect(screen.queryByText('No description yet')).toBeNull()
  })

  it('prefers the description over the goal when both exist', () => {
    setup({ description: 'Watches the on-call queue', goal: 'Keep the release notes current' })
    expect(screen.getByTestId('crew-profile-description')).toHaveTextContent('Watches the on-call queue')
    expect(screen.queryByTestId('crew-profile-goal-fallback')).toBeNull()
  })

  it('shows the shared actionable error when the initial description read fails', () => {
    setup({ description: '', descriptionError: true })
    const notice = screen.getByTestId('crew-profile-description-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent(/could not load/i)
    expect(within(notice).getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
    expect(screen.queryByText('No description yet')).toBeNull()
  })

  it('shows the shared actionable error above a retained description after a refetch fails', () => {
    setup({
      description: 'Cached description remains readable.',
      descriptionError: true,
    })
    const notice = screen.getByTestId('crew-profile-description-error')
    const description = screen.getByTestId('crew-profile-description')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent(/could not load/i)
    expect(within(notice).getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
    expect(description).toHaveTextContent('Cached description remains readable.')
    expect(notice.compareDocumentPosition(description) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.queryByText('No description yet')).toBeNull()
  })

  it('Read more and Notes push a page over the card with one back control naming the crewmate', async () => {
    setup()
    fireEvent.click(screen.getByTestId('crew-profile-read-more'))
    expect(screen.getByTestId('crew-profile-page-about')).toBeInTheDocument()
    expect(screen.getByTestId('crew-profile-about-full')).toHaveTextContent(/Escalates only when/)
    expect(screen.getByTestId('crew-profile-pushed-title')).toHaveTextContent('About')
    expect(screen.getByTestId('crew-profile-back')).toHaveTextContent('oncall')
    fireEvent.click(screen.getByTestId('crew-profile-back'))
    // The page leaves after its exit animation (AnimatePresence), so wait for it.
    await waitFor(() => expect(screen.queryByTestId('crew-profile-page-about')).toBeNull())

    fireEvent.click(screen.getByTestId('crew-profile-notes'))
    expect(screen.getByTestId('crew-profile-page-notes')).toBeInTheDocument()
    expect(screen.getByTestId('host-notes')).toBeInTheDocument()
    expect(screen.getByTestId('crew-profile-pushed-title')).toHaveTextContent('Notes')
  })
})

describe('CrewProfilePanel Schedules tab', () => {
  it('lists the host\'s schedules as readable rows — no count badge, no editor section', () => {
    setup({ initialTab: 'schedule' })
    const pane = screen.getByTestId('crew-profile-pane-schedule')
    expect(within(pane).getAllByTestId('crew-schedule-row').map((r) => r.textContent))
      .toEqual([expect.stringContaining('triage new issues'), expect.stringContaining('weekly digest')])
    expect(within(pane).getByTestId('crew-schedule-next')).toHaveTextContent('triage new issues')
    expect(within(pane).queryByTestId('crew-wake-section')).toBeNull()
    expect(within(pane).queryByTestId('host-create-form')).toBeNull()
    // The rail carries no count: the Schedules tab is its icon (and its word when selected).
    expect(tab('Schedules')).toHaveTextContent(/^Schedules$/)
  })

  it('a row hands the job to the host; New schedule pushes the host\'s create form as a page', async () => {
    const h = setup({ initialTab: 'schedule' })
    expect(screen.queryByRole('button', { name: 'weekly digest' })).toBeNull()
    fireEvent.click(screen.getByTestId('crew-schedule-open-all'))
    expect(h.onOpenSchedule).toHaveBeenCalledOnce()

    fireEvent.click(screen.getByTestId('crew-schedule-create'))
    const page = screen.getByTestId('crew-profile-page-new-schedule')
    expect(within(page).getByTestId('host-create-form')).toBeInTheDocument()
    expect(screen.getByTestId('crew-profile-pushed-title')).toHaveTextContent('New schedule')
    expect(screen.getByTestId('crew-profile-back')).toHaveTextContent('oncall')
    // The card title is replaced by the back control while a page is up.
    expect(screen.queryByTestId('crew-profile-title')).toBeNull()
    // Back pops to the Schedules tab, not to Profile.
    fireEvent.click(screen.getByTestId('crew-profile-back'))
    await waitFor(() => expect(screen.queryByTestId('crew-profile-page-new-schedule')).toBeNull())
    expect(screen.getByTestId('crew-profile-pane-schedule')).toBeInTheDocument()
  })
})

describe('CrewProfilePanel other tabs', () => {
  it('Sessions shows the host\'s driving list; Goals shows the host\'s goals; there is no Computer tab', () => {
    setup()
    fireEvent.click(tab('Sessions'))
    expect(screen.getByTestId('host-sessions')).toBeInTheDocument()
    fireEvent.click(tab('Goals'))
    expect(screen.getByTestId('host-goals')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-profile-goals-soon')).toBeNull()
    expect(screen.queryByRole('tab', { name: 'Computer' })).toBeNull()
  })
})

describe('CrewProfilePanel closing', () => {
  it('makes covered tab content inert only while a pushed page is open', () => {
    setup()
    const covered = screen.getByTestId('crew-profile-pane-profile').parentElement as HTMLElement
    expect(covered).toHaveAttribute('aria-hidden', 'false')
    expect(covered).not.toHaveAttribute('inert')

    fireEvent.click(screen.getByTestId('crew-profile-read-more'))
    expect(covered).toHaveAttribute('aria-hidden', 'true')
    expect(covered).toHaveAttribute('inert', '')

    fireEvent.click(screen.getByTestId('crew-profile-back'))
    expect(covered).toHaveAttribute('aria-hidden', 'false')
    expect(covered).not.toHaveAttribute('inert')
  })

  it('the close control closes; Escape pops a pushed page first and closes second', async () => {
    const h = setup()
    fireEvent.click(screen.getByTestId('crew-profile-read-more'))
    fireEvent.keyDown(screen.getByTestId('crew-profile-panel'), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('crew-profile-page-about')).toBeNull())
    expect(h.onClose).not.toHaveBeenCalled()
    fireEvent.keyDown(screen.getByTestId('crew-profile-panel'), { key: 'Escape' })
    expect(h.onClose).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(h.onClose).toHaveBeenCalledTimes(2)
  })

  it('pushing Notes lands focus on the back control, so Escape still pops; popping returns focus to the opener', async () => {
    // The covered tabs go `inert`, which would otherwise drop the opener's focus
    // onto the body — outside the card, where its Escape handler cannot see it.
    const h = setup()
    const notes = screen.getByTestId('crew-profile-notes')
    notes.focus()
    fireEvent.click(notes)
    expect(screen.getByTestId('crew-profile-page-notes')).toBeInTheDocument()
    expect(document.activeElement).toBe(screen.getByTestId('crew-profile-back'))

    fireEvent.keyDown(document.activeElement as HTMLElement, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('crew-profile-page-notes')).toBeNull())
    expect(h.onClose).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(notes)
  })

  it('a pushed page pops only through the host\'s guard — back and Escape both ask, and a refusal keeps the page', async () => {
    // The pushed create form can hold a typed schedule; the card cannot see that, so
    // every pop is handed to the host with the pop as a continuation. A host that
    // never calls it leaves the page exactly where it is.
    const guard = vi.fn()
    const h = setup({ initialTab: 'schedule', onRequestBack: guard })
    fireEvent.click(screen.getByTestId('crew-schedule-create'))
    expect(screen.getByTestId('crew-profile-page-new-schedule')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('crew-profile-back'))
    expect(guard).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('crew-profile-page-new-schedule')).toBeInTheDocument()
    fireEvent.keyDown(screen.getByTestId('crew-profile-panel'), { key: 'Escape' })
    expect(guard).toHaveBeenCalledTimes(2)
    expect(screen.getByTestId('crew-profile-page-new-schedule')).toBeInTheDocument()
    // Escape over a pushed page never reaches the card's close.
    expect(h.onClose).not.toHaveBeenCalled()

    // The host decides later: running the continuation it was handed pops the page.
    const proceed = (guard.mock.calls[1] as [() => void])[0]
    act(() => { proceed() })
    await waitFor(() => expect(screen.queryByTestId('crew-profile-page-new-schedule')).toBeNull())
    expect(screen.getByTestId('crew-profile-pane-schedule')).toBeInTheDocument()
  })
})

describe('CrewProfilePanel tab pages', () => {
  function GoalsStub({ onPop }: { onPop: () => void }) {
    const push = useContext(ProfilePagePushContext)
    return (
      <>
        <button type="button" onClick={() => push?.open('Milestone A', onPop)}>open page</button>
        {push?.host ? createPortal(<div data-testid="stub-page">page body</div>, push.host) : null}
      </>
    )
  }

  it('pushes a tab page over the card with its title, and back hands it back to the tab', async () => {
    const onPop = vi.fn()
    const h = setup({ initialTab: 'goals', goalsBody: <GoalsStub onPop={onPop} /> })
    fireEvent.click(screen.getByRole('button', { name: 'open page' }))
    expect(await screen.findByTestId('stub-page')).toBeInTheDocument()
    expect(screen.getByTestId('crew-profile-pushed-title')).toHaveTextContent('Milestone A')
    expect(screen.getByTestId('crew-profile-back')).toHaveTextContent('oncall')
    fireEvent.click(screen.getByTestId('crew-profile-back'))
    expect(h.onRequestBack).toHaveBeenCalledTimes(1)
    expect(onPop).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.queryByTestId('crew-profile-pushed-title')).toBeNull())
  })
})
