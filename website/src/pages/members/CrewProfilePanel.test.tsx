import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within, act, waitFor } from '@testing-library/react'
import type { MemberRosterRow } from '../../api/client'
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

vi.mock('../../components/CrewStateAvatar', () => ({
  default: ({ seed }: { seed: string }) => <span data-testid="avatar-stub">{seed}</span>,
}))

import CrewProfilePanel, { PROFILE_TABS } from './CrewProfilePanel'

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
      description="Watches the on-call queue and files what it finds. Escalates only when a page has gone unanswered."
      memoryLabel="Private to this crewmate"
      schedules={JOBS}
      schedulesLoading={false}
      nowTs={NOW}
      newScheduleBody={<div data-testid="host-create-form">host form</div>}
      sessionsBody={<div data-testid="host-sessions">host sessions</div>}
      notesBody={<div data-testid="host-notes">host notes</div>}
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

  it('says there is no description instead of rendering an empty card', () => {
    setup({ description: '' })
    expect(screen.getByTestId('crew-profile-about')).toHaveTextContent(/No description yet/)
    expect(screen.queryByTestId('crew-profile-read-more')).toBeNull()
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
  it('Sessions shows the host\'s driving list; Goals is coming soon; there is no Computer tab', () => {
    setup()
    fireEvent.click(tab('Sessions'))
    expect(screen.getByTestId('host-sessions')).toBeInTheDocument()
    fireEvent.click(tab('Goals'))
    expect(screen.getByTestId('crew-profile-goals-soon')).toHaveTextContent(/coming soon/i)
    expect(screen.queryByRole('textbox')).toBeNull()
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
