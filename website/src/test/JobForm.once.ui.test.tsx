import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import JobForm from '../components/JobForm'
import { defaultFireLocal, fireTimeLocal, toFireTime } from '../components/ScheduleLaterPopover'
import { api } from '../api/client'
import type { CronJob } from '../types'

vi.mock('../api/client', () => ({
  api: {
    models: vi.fn().mockResolvedValue([]),
    createCron: vi.fn().mockResolvedValue({ ok: true }),
    updateCron: vi.fn().mockResolvedValue({ ok: true }),
    chatFolders: vi.fn().mockResolvedValue([]),
    kirocrewConfig: vi.fn().mockResolvedValue({ dashboard: { folder_sort: 'custom' } }),
  },
}))

afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('JobForm create-mode dirty tracking under a moving clock', () => {
  it('does not report dirty when a quarter-hour passes under an untouched form', async () => {
    // The create form's one-shot default is "next quarter hour from now". At
    // 10:07:30 that is 10:15; ten minutes later it is 10:30. The `onceLocal`
    // STATE is seeded once at mount, so if the comparison baseline were
    // re-derived per render (the pre-fix shape), the first re-render after
    // the boundary would read the two as different and the untouched form
    // would announce itself dirty -- a spurious discard confirm.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const mountedAt = Date.parse('2026-09-17T10:07:30Z')
    vi.setSystemTime(mountedAt)
    const onDirtyChange = vi.fn()

    const props = { agents: [], defaultAgent: '', onSaved: vi.fn(), layout: 'vertical' as const, onDirtyChange }
    const { queryClient, rerender } = renderWithProviders(<JobForm {...props} />)

    // Let the mount-time queries (models, chat folders, folder sort) settle so
    // the later refetch is a genuine second round trip, not the first.
    await waitFor(() => expect(api.chatFolders).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(onDirtyChange).toHaveBeenCalledWith(false))
    expect(onDirtyChange).not.toHaveBeenCalledWith(true)
    const callsAfterMount = onDirtyChange.mock.calls.length

    // Cross the 10:15 boundary without touching any field.
    await act(async () => { await vi.advanceTimersByTimeAsync(10 * 60 * 1000) })
    expect(defaultFireLocal()).not.toBe(defaultFireLocal(mountedAt))

    // Two realistic re-render sources: the sidebar invalidating the shared
    // `['chat-folders']` key (a folder created or renamed while the form is
    // open), and the host re-rendering the form with identical props.
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ['chat-folders'] })
    })
    await waitFor(() => expect(api.chatFolders).toHaveBeenCalledTimes(2))
    rerender(<JobForm {...props} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })

    // Dirty edges are reported through an effect; none fired, and the only
    // report the host ever heard was the mount-time `false`.
    expect(onDirtyChange).not.toHaveBeenCalledWith(true)
    expect(onDirtyChange.mock.calls.length).toBe(callsAfterMount)

    // And the one-shot picker still carries the value the form OPENED with,
    // not the clock's current answer.
    fireEvent.click(screen.getByRole('combobox', { name: 'Schedule' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Run once' }))
    expect(screen.getByLabelText('Run once')).toHaveValue(defaultFireLocal(mountedAt))
  })

  it('leaves a reused Run-once create form clean and saveable after a success', async () => {
    // A successful create resets the fields for the next schedule. The one-shot
    // picker used to reset to '' while `dirty` kept comparing against the
    // frozen mount-time seed, so the untouched reused form announced itself
    // dirty and -- Run once still selected -- the next Save refused the EMPTY
    // picker with "Pick a time in the future". The reset must land back on the
    // seed the form opened with: not dirty, a valid picker, saveable again.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const mountedAt = Date.parse('2026-09-17T10:07:30Z')
    vi.setSystemTime(mountedAt)
    const onDirtyChange = vi.fn()
    const onSaved = vi.fn()
    renderWithProviders(
      <JobForm agents={[]} defaultAgent="" onSaved={onSaved} layout="vertical" onDirtyChange={onDirtyChange} />,
    )
    await waitFor(() => expect(api.chatFolders).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(onDirtyChange).toHaveBeenCalledWith(false))

    fireEvent.click(screen.getByRole('combobox', { name: 'Schedule' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Run once' }))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'first' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'remind me' } })
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(true))

    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(api.createCron).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1))
    const [first] = vi.mocked(api.createCron).mock.calls[0] as [Record<string, unknown>]
    expect(first.at).toBe(toFireTime(defaultFireLocal(mountedAt), mountedAt))

    // The host kept the form mounted. Nothing has been typed since, so the only
    // remaining divergence from what it opened with is the Run once selection
    // itself (`schedMode`), which the form cannot undo on the user's behalf.
    // The picker is back on the seed, not blank, and no error is showing.
    const picker = screen.getByLabelText('Run once')
    expect(picker).toHaveValue(defaultFireLocal(mountedAt))
    expect(picker).not.toHaveValue('')
    expect(screen.queryByText('Pick a time in the future')).toBeNull()

    // Prove no OTHER term is dirty: flipping the schedule back to what the form
    // opened with reads as untouched (the pre-fix '' picker would have kept
    // the form dirty through the Run-once term while Run once was selected,
    // and would surface again the moment it is re-selected).
    fireEvent.click(screen.getByRole('combobox', { name: 'Schedule' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Every interval' }))
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(false))
    fireEvent.click(screen.getByRole('combobox', { name: 'Schedule' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Run once' }))
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(true))
    expect(screen.getByLabelText('Run once')).toHaveValue(defaultFireLocal(mountedAt))

    // An immediate second save goes through on the same seed.
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'second' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'remind me again' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(api.createCron).toHaveBeenCalledTimes(2))
    const [second] = vi.mocked(api.createCron).mock.calls[1] as [Record<string, unknown>]
    expect(second.name).toBe('second')
    expect(second.at).toBe(toFireTime(defaultFireLocal(mountedAt), mountedAt))
    expect(screen.queryByText('Pick a time in the future')).toBeNull()
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(2))

    // The reset landed on the FROZEN seed, not a fresh clock read: crossing a
    // quarter-hour boundary afterwards neither moves nor blanks the picker.
    await act(async () => { await vi.advanceTimersByTimeAsync(10 * 60 * 1000) })
    expect(defaultFireLocal()).not.toBe(defaultFireLocal(mountedAt))
    expect(screen.getByLabelText('Run once')).toHaveValue(defaultFireLocal(mountedAt))
  })
})

describe('JobForm Run once strict setting', () => {
  it('does not enable Strict schedule when a new job selects Run once', async () => {
    const now = Date.parse('2026-09-17T10:07:30Z')
    vi.spyOn(Date, 'now').mockReturnValue(now)
    renderWithProviders(
      <JobForm agents={[]} defaultAgent="" onSaved={vi.fn()} layout="vertical" />,
    )

    fireEvent.click(screen.getByRole('combobox', { name: 'Schedule' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Run once' }))

    expect(screen.getByRole('switch', { name: 'Strict schedule' })).toHaveAttribute(
      'aria-checked',
      'false',
    )

    const scheduleFields = screen.getByTestId('jobform-schedule-fields')
    const onceRow = screen.getByTestId('jobform-run-once-row')
    const picker = screen.getByLabelText('Run once')
    expect(getComputedStyle(scheduleFields).gridTemplateColumns).toBe('minmax(0, 1fr)')
    expect(getComputedStyle(onceRow).gridColumn).toBe('1 / -1')
    expect(getComputedStyle(onceRow).width).toBe('100%')
    expect(picker).toHaveClass('w-full', 'min-w-0')
    expect(picker).toHaveValue(defaultFireLocal(now))
  })
})

describe('JobForm editing a stored recurring job under a fake clock', () => {
  const recurring = (over: Partial<CronJob> = {}): CronJob => ({
    id: 'recurring',
    name: 'standup',
    message: 'post the standup',
    schedule: 'every 1h',
    every_secs: 3600,
    enabled: true,
    ...over,
  } as CronJob)

  it('seeds Run once with the next quarter hour and saves the conversion', async () => {
    // Before the fix the picker opened EMPTY for a job stored with another
    // schedule and Save failed on the spot with "Pick a time in the future".
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const now = Date.parse('2026-09-17T10:07:30Z')
    vi.setSystemTime(now)
    const onSaved = vi.fn()
    renderWithProviders(
      <JobForm job={recurring()} agents={[]} defaultAgent="" onSaved={onSaved} layout="vertical" />,
    )
    await waitFor(() => expect(api.chatFolders).toHaveBeenCalledTimes(1))

    fireEvent.click(screen.getByRole('combobox', { name: 'Schedule' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Run once' }))

    const picker = screen.getByLabelText('Run once')
    expect(picker).toHaveValue(defaultFireLocal(now))
    expect(picker).not.toHaveValue('')

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(api.updateCron).toHaveBeenCalledTimes(1))
    const [id, body] = vi.mocked(api.updateCron).mock.calls[0] as [string, Record<string, unknown>]
    expect(id).toBe('recurring')
    expect(body.at).toBe(toFireTime(defaultFireLocal(now), now))
    expect(body).not.toHaveProperty('every')
    expect(body).not.toHaveProperty('cron')
    expect(screen.queryByText('Pick a time in the future')).toBeNull()
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1))
  })

  it('keeps an ordinary edit untouched when the record is refetched across a quarter hour', async () => {
    // The Run-once seed for a recurring job reads the clock, and a refetched
    // record is a NEW `job` identity that recomputes it. Neither may make an
    // untouched edit form announce itself dirty.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const mountedAt = Date.parse('2026-09-17T10:07:30Z')
    vi.setSystemTime(mountedAt)
    const onDirtyChange = vi.fn()
    const base = { agents: [], defaultAgent: '', onSaved: vi.fn(), layout: 'vertical' as const, onDirtyChange }
    const { rerender } = renderWithProviders(<JobForm {...base} job={recurring()} />)

    await waitFor(() => expect(api.chatFolders).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(onDirtyChange).toHaveBeenCalledWith(false))
    expect(onDirtyChange).not.toHaveBeenCalledWith(true)
    const callsAfterMount = onDirtyChange.mock.calls.length

    // Cross the 10:15 boundary, then hand the form a refetched record whose
    // only difference is a run having completed in the meantime.
    await act(async () => { await vi.advanceTimersByTimeAsync(10 * 60 * 1000) })
    expect(defaultFireLocal()).not.toBe(defaultFireLocal(mountedAt))
    rerender(<JobForm {...base} job={recurring({ last_run_ts: Math.floor(Date.now() / 1000) })} />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })

    expect(onDirtyChange).not.toHaveBeenCalledWith(true)
    expect(onDirtyChange.mock.calls.length).toBe(callsAfterMount)
    // The stored schedule is still what the form shows and would save.
    expect(screen.getByRole('combobox', { name: 'Schedule' })).toHaveTextContent('Every')

    // Switching to Run once now is user work (dirty), and the picker carries the
    // value the form OPENED with rather than the refetched seed.
    fireEvent.click(screen.getByRole('combobox', { name: 'Schedule' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Run once' }))
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(true))
    expect(screen.getByLabelText('Run once')).toHaveValue(defaultFireLocal(mountedAt))

    // And switching back reads as untouched again: the seed was never work.
    fireEvent.click(screen.getByRole('combobox', { name: 'Schedule' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Every interval' }))
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(false))
  })

  it('still renders a stored one-shot with its own instant, not the seed', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const now = Date.parse('2026-09-17T10:07:30Z')
    vi.setSystemTime(now)
    const at_ts = Math.floor(now / 1000) + 86_400 + 37
    renderWithProviders(
      <JobForm
        job={recurring({ schedule: '', every_secs: undefined, at_ts })}
        agents={[]}
        defaultAgent=""
        onSaved={vi.fn()}
        layout="vertical"
      />,
    )
    await waitFor(() => expect(api.chatFolders).toHaveBeenCalledTimes(1))

    const picker = screen.getByLabelText('Run once')
    expect(picker).toHaveValue(fireTimeLocal(at_ts))
    expect(picker).not.toHaveValue(defaultFireLocal(now))
  })
})
