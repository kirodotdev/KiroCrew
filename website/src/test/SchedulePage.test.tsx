import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SchedulePage from '../pages/SchedulePage'
import { SCHEDULE_PRESETS, PRESET_CATEGORIES } from '../utils/schedulePresets'
import type { CronJob } from '../types'

// Covers the arm -> confirm -> delete -> revert state machine. This logic is on
// a destructive, irreversible action and had two real bugs (premature button
// re-enable before await load(), and confirmDeleteId not resetting on a failed
// delete) -- these tests lock in both fixes.

const mkJob = (overrides: Partial<CronJob> = {}): CronJob => ({
  id: 'job-1',
  name: 'Nightly report',
  schedule: 'every 1d',
  message: 'send report',
  enabled: true,
  ...overrides,
} as CronJob)

vi.mock('../api/client', () => ({
  api: {
    crons: vi.fn(),
    cronFolders: vi.fn().mockResolvedValue([]),
    deleteCron: vi.fn(),
    batchDeleteCron: vi.fn(),
    createCron: vi.fn().mockResolvedValue({}),
    models: vi.fn().mockResolvedValue([]),
    updateCron: vi.fn().mockResolvedValue({}),
    toggleCron: vi.fn().mockResolvedValue({}),
    runCron: vi.fn().mockResolvedValue({}),
    cronToChat: vi.fn().mockResolvedValue({}),
    cronHistoryAll: vi.fn().mockResolvedValue({ runs: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    syncKirocrewAgents: vi.fn().mockResolvedValue({}),
  },
}))

/**
 * Open the template gallery. It is no longer its own toolbar button: it is the
 * second half of the Add Job split button — one intent ("make a job") with two
 * starting points — so reaching it takes a menu open. Radix needs
 * keyboard-open in jsdom.
 */
const openGallery = async () => {
  fireEvent.keyDown(screen.getByLabelText('Browse schedule templates'), { key: 'Enter' })
  fireEvent.click(await screen.findByText('Browse all templates'))
}

describe('SchedulePage delete button state machine', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('arms on first click, deletes on second click', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob()] })
    vi.mocked(api).deleteCron.mockResolvedValue({})

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())

    const deleteBtn = screen.getByRole('button', { name: 'Delete' })
    expect(api.deleteCron).not.toHaveBeenCalled()

    // First click arms the row -- button swaps label, no API call yet.
    fireEvent.click(deleteBtn)
    expect(await screen.findByRole('button', { name: 'Confirm' })).toBeInTheDocument()
    expect(api.deleteCron).not.toHaveBeenCalled()

    // After delete, refresh the list to empty so the row disappears.
    vi.mocked(api).crons.mockResolvedValue({ jobs: [] })

    // Second click (now "Confirm") actually deletes.
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(api.deleteCron).toHaveBeenCalledWith('job-1'))
  })

  it('reverts confirm state back to Delete if the delete call fails', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob()] })
    vi.mocked(api).deleteCron.mockRejectedValue(new Error('boom'))

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    const confirmBtn = await screen.findByRole('button', { name: 'Confirm' })
    fireEvent.click(confirmBtn)

    await waitFor(() => expect(api.deleteCron).toHaveBeenCalled())
    // Even on failure, the button must revert out of "Confirm" -- otherwise
    // the row stays stuck with no way to re-arm.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument())
    expect(screen.getByText(/boom/)).toBeInTheDocument()
  })

  it('auto-reverts the armed state after the timeout if not confirmed', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob()] })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(await screen.findByRole('button', { name: 'Confirm' })).toBeInTheDocument()

    await vi.advanceTimersByTimeAsync(3100)

    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument()
    expect(api.deleteCron).not.toHaveBeenCalled()
    vi.useRealTimers()
  })

  it('arming a different row disarms the previously armed row', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ id: 'job-1', name: 'Nightly report' }), mkJob({ id: 'job-2', name: 'Weekly digest' })],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())

    const deleteButtons = screen.getAllByRole('button', { name: 'Delete' })
    fireEvent.click(deleteButtons[0])
    expect(await screen.findByRole('button', { name: 'Confirm' })).toBeInTheDocument()

    // Arming row 2 must disarm row 1 -- only one row confirmable at a time.
    const remainingDeleteButtons = screen.getAllByRole('button', { name: 'Delete' })
    fireEvent.click(remainingDeleteButtons[remainingDeleteButtons.length - 1])

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Confirm' })).toHaveLength(1)
    })
    expect(api.deleteCron).not.toHaveBeenCalled()
  })
})

describe('SchedulePage batch select + bulk delete', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  const twoJobs = [
    mkJob({ id: 'job-1', name: 'Nightly report' }),
    mkJob({ id: 'job-2', name: 'Weekly digest' }),
  ]

  it('selecting rows shows the batch bar and deleting requires typing delete', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: twoJobs })
    vi.mocked(api).batchDeleteCron.mockResolvedValue({ ok: true, deleted: ['job-1', 'job-2'], failed: [] })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())

    // Select both rows via their per-row checkboxes.
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select Nightly report' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select Weekly digest' }))
    expect(screen.getByText('2 selected')).toBeInTheDocument()

    // Open the confirm modal — the delete button is disarmed until "delete" is typed.
    fireEvent.click(screen.getByRole('button', { name: /Delete 2 selected/ }))
    const modal = await screen.findByRole('dialog')
    expect(modal).toBeInTheDocument()
    const armBtn = screen.getByRole('button', { name: 'Delete 2' })
    expect(armBtn).toBeDisabled()
    expect(api.batchDeleteCron).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText(/Type/, { selector: 'input' }), { target: { value: 'delete' } })
    expect(armBtn).not.toBeDisabled()

    vi.mocked(api).crons.mockResolvedValue({ jobs: [] })
    fireEvent.click(armBtn)
    await waitFor(() => expect(api.batchDeleteCron).toHaveBeenCalledWith(['job-1', 'job-2']))
    // Modal closes and selection clears on full success.
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('keeps failed ids selected and surfaces the failure count', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: twoJobs })
    vi.mocked(api).batchDeleteCron.mockResolvedValue({ ok: false, deleted: ['job-1'], failed: ['job-2'] })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('checkbox', { name: 'Select Nightly report' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select Weekly digest' }))
    fireEvent.click(screen.getByRole('button', { name: /Delete 2 selected/ }))
    await screen.findByRole('dialog')
    fireEvent.change(screen.getByLabelText(/Type/, { selector: 'input' }), { target: { value: 'delete' } })

    // job-1 deletes; job-2 fails and stays behind after reload.
    vi.mocked(api).crons.mockResolvedValue({ jobs: [twoJobs[1]] })
    fireEvent.click(screen.getByRole('button', { name: 'Delete 2' }))

    await waitFor(() => expect(api.batchDeleteCron).toHaveBeenCalledWith(['job-1', 'job-2']))
    // Modal stays open with the error; the failed job remains selected for retry.
    expect(await screen.findByText('1 of 2 jobs could not be deleted')).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    // The retry scope narrowed to the one failure — the confirm button counts
    // the still-selected ids. Asserted from INSIDE the dialog: it is modal, so
    // Radix marks the table behind it aria-hidden and the row checkbox is not
    // reachable by role while it is open.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Delete 1' })).toBeInTheDocument())

    // Dismiss the dialog: the failed row is still checked, ready for a retry.
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() =>
      expect(screen.getByRole('checkbox', { name: 'Select Weekly digest' })).toBeChecked())
  })
})

describe('SchedulePage empty-state preset cards', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders only the featured preset cards when there are no jobs', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [] })

    renderWithProviders(<SchedulePage />)

    const featured = SCHEDULE_PRESETS.filter(p => p.featured)
    const nonFeatured = SCHEDULE_PRESETS.filter(p => !p.featured)

    // Every featured preset surfaces on the empty state (derived — no counts).
    await waitFor(() => expect(screen.getByText(featured[0].title)).toBeInTheDocument())
    for (const p of featured) {
      expect(screen.getByText(p.title)).toBeInTheDocument()
    }
    // Non-featured presets stay in the gallery, not the empty state.
    for (const p of nonFeatured) {
      // A non-featured title must never collide with a featured one.
      if (!featured.some(f => f.title === p.title)) {
        expect(screen.queryByText(p.title)).not.toBeInTheDocument()
      }
    }
  })

  it('"Browse all templates" opens the gallery with its header', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [] })

    renderWithProviders(<SchedulePage />)
    const browse = await screen.findByRole('button', { name: /Browse all templates/ })
    fireEvent.click(browse)

    // The gallery modal renders its header + subtitle.
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText('Schedule templates')).toBeInTheDocument()
    expect(screen.getByText(/you review and save before anything runs/i)).toBeInTheDocument()
  })

  it('gallery renders a section label for every non-empty category', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [] })

    renderWithProviders(<SchedulePage />)
    fireEvent.click(await screen.findByRole('button', { name: /Browse all templates/ }))
    await screen.findByRole('dialog')

    // Derived from the data: only categories that actually have presets show.
    const nonEmpty = PRESET_CATEGORIES.filter(c => SCHEDULE_PRESETS.some(p => p.category === c.id))
    expect(nonEmpty.length).toBeGreaterThan(0)
    for (const cat of nonEmpty) {
      expect(screen.getByRole('region', { name: cat.label })).toBeInTheDocument()
    }
    // Empty categories must not render a section.
    const empty = PRESET_CATEGORIES.filter(c => !SCHEDULE_PRESETS.some(p => p.category === c.id))
    for (const cat of empty) {
      expect(screen.queryByRole('region', { name: cat.label })).not.toBeInTheDocument()
    }
  })

  it('clicking a gallery card closes the gallery and opens the create panel prefilled', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [] })

    renderWithProviders(<SchedulePage />)
    fireEvent.click(await screen.findByRole('button', { name: /Browse all templates/ }))
    await screen.findByRole('dialog')

    // Pick any preset; the gallery card carries a unique aria-label so the
    // click can't collide with an empty-state featured card of the same title.
    const preset = SCHEDULE_PRESETS[0]
    fireEvent.click(screen.getByRole('button', { name: `Use the ${preset.title} template` }))

    // Gallery closes and the seeded create dialog opens. Asserted by the
    // gallery's own card disappearing, NOT by "no dialog remains": the create
    // view is itself a dialog now, so a `queryByRole('dialog')` check here
    // would be satisfied only if the thing under test had failed to open.
    await waitFor(() => expect(screen.queryByRole('button', { name: `Use the ${preset.title} template` })).not.toBeInTheDocument())
    const nameInput = (await screen.findByLabelText('Name')) as HTMLInputElement
    expect(nameInput.value).toBe(preset.prefill.name)
    const msgInput = screen.getByLabelText('Message') as HTMLTextAreaElement
    expect(msgInput.value).toContain(preset.prefill.message)
  })
})

describe('SchedulePage template gallery (non-empty state)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('the Add Job split button opens the template gallery', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ id: 'job-1', name: 'Nightly report' })],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())

    // Blank-create stays a one-click primary; only the ▾ half is a menu.
    expect(screen.getByRole('button', { name: /Add Job/ })).toBeInTheDocument()
    await openGallery()

    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText('Schedule templates')).toBeInTheDocument()
  })

  it('clicking a preset card opens the create panel with the prompt + name prefilled', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [] })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Error Digest')).toBeInTheDocument())

    fireEvent.click(screen.getByText('Error Digest'))

    const nameInput = (await screen.findByLabelText('Name')) as HTMLInputElement
    expect(nameInput.value).toBe('Error Digest')
    const msgInput = screen.getByLabelText('Message') as HTMLTextAreaElement
    expect(msgInput.value).toContain('production errors')
  })

  it('a Monday weekly preset creates a Monday cron (pins the Mon=1 grid convention)', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [] })
    vi.mocked(api).createCron.mockResolvedValue({})

    // Derived from data, not a hardcoded title: any weekly preset scheduled on
    // Monday at a fixed time. Reached through the gallery, since write-capable
    // presets are deliberately absent from the featured empty-state row.
    const weekly = SCHEDULE_PRESETS.find(
      p => p.prefill.schedMode === 'weekly' &&
        p.prefill.weekDays?.length === 1 &&
        p.prefill.weekDays[0] === 1 &&
        !!p.prefill.weekTime,
    )!
    expect(weekly).toBeDefined()
    const [hh, mm] = weekly.prefill.weekTime!.split(':').map(Number)

    renderWithProviders(<SchedulePage />)
    fireEvent.click(await screen.findByRole('button', { name: /Browse all templates/ }))
    await screen.findByRole('dialog')
    fireEvent.click(screen.getByRole('button', { name: `Use the ${weekly.title} template` }))

    const createBtn = await screen.findByRole('button', { name: 'Create' })
    fireEvent.click(createBtn)

    await waitFor(() => expect(api.createCron).toHaveBeenCalled())
    const body = vi.mocked(api).createCron.mock.calls[0][0] as Record<string, unknown>
    // Pins JobForm's grid weekday convention (Mon=1): a weekDays:[1] preset must
    // map to a Monday cron. If JobForm's day-numbering changes, this fails
    // loudly instead of silently shifting every weekly preset by a day.
    expect(body.cron).toBe(`${mm} ${hh} * * 1`)
  })
})

describe('SchedulePage write-capable preset indicator', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('never features a write-capable preset (first-run surface stays read-only)', () => {
    // Invariant, not a snapshot: a one-click unattended automation that pushes
    // branches / opens PRs / edits issues must not sit in the empty-state row a
    // brand-new user sees first, while its guardrails are prompt text rather
    // than an enforced deny rule. Write-capable presets live in the gallery.
    const featuredWrites = SCHEDULE_PRESETS.filter(p => p.featured && p.writes)
    expect(featuredWrites.map(p => p.id)).toEqual([])
    // Guard: the row is not empty and write-capable presets do exist.
    expect(SCHEDULE_PRESETS.filter(p => p.featured).length).toBeGreaterThan(0)
    expect(SCHEDULE_PRESETS.filter(p => p.writes).length).toBeGreaterThan(0)
  })

  it('gallery shows the "Writes to your repos" badge on exactly the writes-tagged presets', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [] })

    renderWithProviders(<SchedulePage />)
    fireEvent.click(await screen.findByRole('button', { name: /Browse all templates/ }))
    await screen.findByRole('dialog')

    // Derived from data — no hardcoded counts: one badge per writes preset in
    // the gallery, plus any that also appear on the featured empty-state row
    // (currently none, by the invariant above).
    const writesCount = SCHEDULE_PRESETS.filter(p => p.writes).length
    const featuredWritesCount = SCHEDULE_PRESETS.filter(p => p.writes && p.featured).length
    const badges = screen.getAllByText('Writes to your repos')
    expect(badges.length).toBe(writesCount + featuredWritesCount)
    expect(writesCount).toBeGreaterThan(0)
  })

  it('picking a writes preset shows the advisory notice in the create panel', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [] })

    const writesPreset = SCHEDULE_PRESETS.find(p => p.writes)!

    renderWithProviders(<SchedulePage />)
    fireEvent.click(await screen.findByRole('button', { name: /Browse all templates/ }))
    await screen.findByRole('dialog')
    fireEvent.click(screen.getByRole('button', { name: `Use the ${writesPreset.title} template` }))

    expect(await screen.findByRole('note')).toHaveTextContent(/not enforced policy/i)
  })

  it('re-selecting the SAME preset resets the form (pins the selection-nonce remount)', async () => {
    // Without the nonce in JobDetailDialog's key, React keeps JobForm mounted
    // across a second pick of the same preset, so edits from the first pick
    // survive into the "fresh" form and can be saved unnoticed.
    // The create view is a modal, so the Templates button behind it is
    // aria-hidden while it is open — the re-pick path dismisses the create
    // dialog first, which is also what a user has to do.
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob()] })

    const preset = SCHEDULE_PRESETS[0]

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByLabelText('Browse schedule templates')).toBeInTheDocument())
    await openGallery()
    await screen.findByRole('dialog')
    fireEvent.click(screen.getByRole('button', { name: `Use the ${preset.title} template` }))

    const nameInput = await screen.findByDisplayValue(preset.prefill.name)
    fireEvent.change(nameInput, { target: { value: 'EDITED BY USER' } })
    expect(screen.getByDisplayValue('EDITED BY USER')).toBeInTheDocument()

    // Dismiss the create dialog, then re-open the gallery and pick the SAME
    // preset again.
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.getByLabelText('Browse schedule templates')).toBeInTheDocument())
    await openGallery()
    await screen.findByRole('dialog')
    fireEvent.click(screen.getByRole('button', { name: `Use the ${preset.title} template` }))

    // Must be a fresh mount seeded from the preset, not the edited state.
    await waitFor(() => expect(screen.getByDisplayValue(preset.prefill.name)).toBeInTheDocument())
    expect(screen.queryByDisplayValue('EDITED BY USER')).not.toBeInTheDocument()
  })

  it('saving a silence-promising preset sends silent=true in the create body', async () => {
    // Data-only assertions are not enough: an earlier revision seeded
    // prefill.silent into JobForm's defaults object while the state initializer
    // still read defaults.silent, so the flag never reached the API and every
    // quiet run still delivered "_No response._". This asserts the wire format,
    // which is what actually governs delivery.
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob()] })
    vi.mocked(api).createCron.mockResolvedValue({})

    const silentPreset = SCHEDULE_PRESETS.find(p => p.prefill.silent)!

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByLabelText('Browse schedule templates')).toBeInTheDocument())
    await openGallery()
    await screen.findByRole('dialog')
    fireEvent.click(screen.getByRole('button', { name: `Use the ${silentPreset.title} template` }))
    fireEvent.click(await screen.findByRole('button', { name: 'Create' }))

    await waitFor(() => expect(api.createCron).toHaveBeenCalled())
    const body = vi.mocked(api).createCron.mock.calls[0][0] as Record<string, unknown>
    expect(body.silent).toBe(true)
  })

  it('a report-only preset is never tagged writes', () => {
    // Guards the badge against drifting from the prompt: two presets were made
    // report-only because acting on untrusted comment/issue text would let a
    // commenter drive writes with the owner's credentials. The card must not
    // then claim the job pushes branches.
    for (const p of SCHEDULE_PRESETS) {
      if (/REPORT-ONLY/i.test(p.prefill.message)) {
        expect(p.writes, `${p.id} is report-only but tagged writes`).not.toBe(true)
      }
    }
  })

  it('presets that promise silence set prefill.silent', () => {
    // The "end silently" clause only holds if the saved job has silent=true;
    // otherwise every no-signal run delivers "_No response._".
    const silentPresets = SCHEDULE_PRESETS.filter(p => /end silently/i.test(p.prefill.message))
    expect(silentPresets.length).toBeGreaterThan(0)
    for (const p of silentPresets) {
      expect(p.prefill.silent, `${p.id} promises silence but omits silent`).toBe(true)
    }
  })

  it('picking a read-only preset shows no advisory notice', async () => {
    // Deliberately a SEPARATE render rather than closing and re-opening the
    // panel in one test: the close affordance is shared page chrome whose
    // accessible name is not this feature's contract (a second /Close/i button
    // on the page once made the combined test ambiguous). Two clean renders
    // assert the same behavioural pair without coupling to that chrome.
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [] })

    const readOnlyPreset = SCHEDULE_PRESETS.find(p => !p.writes)!

    renderWithProviders(<SchedulePage />)
    fireEvent.click(await screen.findByRole('button', { name: /Browse all templates/ }))
    await screen.findByRole('dialog')
    fireEvent.click(screen.getByRole('button', { name: `Use the ${readOnlyPreset.title} template` }))

    await waitFor(() => expect(screen.getByDisplayValue(readOnlyPreset.prefill.name)).toBeInTheDocument())
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
  })
})

describe('SchedulePage job detail dialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('opens the detail dialog on a row click, titled with the job name', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob()] })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

    fireEvent.click(screen.getByText('Nightly report'))

    const dialog = await screen.findByRole('dialog')
    expect(dialog).toBeInTheDocument()
    // The form is the detail view's payload, not just a titled shell.
    expect(await screen.findByDisplayValue('Nightly report')).toBeInTheDocument()
  })

  it('keeps the job selected after the dialog is dismissed, so the Executions filter survives', async () => {
    // The detail view was a side panel that could stay open beside the
    // Executions table; as a modal it cannot, so the job selection is held in
    // state SEPARATE from the dialog's open flag. Without that split, closing
    // the modal to look at the executions it was filtering would clear the
    // filter — this asserts the jobId still reaches the history query.
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob()] })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())

    fireEvent.click(screen.getByText('Nightly report'))
    await screen.findByRole('dialog')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    // The view switcher is a collapsing SegmentedControl: jsdom reports a 0px
    // container, so it renders in its dropdown mode showing only the active
    // segment. Open it, then pick Executions.
    fireEvent.click(screen.getByText('List'))
    fireEvent.click(await screen.findByText('Executions'))

    await waitFor(() => expect(api.cronHistoryAll).toHaveBeenCalledWith(
      expect.objectContaining({ jobId: 'job-1' }),
    ))
  })
})
