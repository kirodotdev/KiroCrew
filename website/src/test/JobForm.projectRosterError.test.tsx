import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import JobForm from '../components/JobForm'
import type { CronJob } from '../types'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    updateCron: vi.fn(),
    createCron: vi.fn(),
    models: vi.fn().mockResolvedValue({ models: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
  },
}))

function messageJob(overrides: Partial<CronJob> = {}): CronJob {
  return {
    id: 'j1', name: 'nightly', message: 'do the thing', schedule: '', enabled: true,
    cron_expr: '0 3 * * *', ...overrides,
  } as CronJob
}

/**
 * The project-scoped roster fetch (`api.kirocrewAgents` keyed by working
 * directory) had its `.catch()` write into the SAME `error` state as
 * Save-time field validation. Two real bugs from that: (1) `setError` also
 * auto-scrolls the page to the bottom-of-form notice on every set, so a
 * background fetch failing mid-typing would yank the user's scroll position
 * for a failure unrelated to what they were doing; (2) the two failure kinds
 * share one string with no independent clear, so a stale roster error could
 * survive an otherwise-successful save (only the Save handler's own
 * `setError('')` clears it) or a validation error could be silently
 * clobbered by a late-resolving roster retry. Fixed by giving the roster
 * fetch its own state, rendered beside the working-directory field via the
 * same `ErrorNotice` component the sibling `AgentSelector` roster-failure UI
 * already uses, instead of the form-wide notice.
 */
describe('JobForm project roster fetch failure stays out of the form-wide error', () => {
  it('shows the roster failure beside the working-directory field, not in the form-wide notice', async () => {
    vi.mocked(api.kirocrewAgents).mockRejectedValueOnce(new Error('network unreachable'))
    renderWithProviders(
      <JobForm job={messageJob()} agents={[]} defaultAgent="" onSaved={() => {}} layout="vertical" />,
    )

    fireEvent.change(screen.getByLabelText('Working directory'), {
      target: { value: '/Users/you/projects/myrepo' },
    })

    await waitFor(() =>
      expect(screen.getByTestId('jobform-project-roster-error')).toHaveTextContent('network unreachable'),
    )
    // The form-wide notice (Save-time validation, no testId) must stay
    // absent -- a background roster failure is not a reason to show it or
    // scroll the page. Both notices share `role="alert"`, so a bare
    // `queryByRole` would match this test's own roster notice; assert on
    // the untagged one specifically by excluding the testId'd element.
    const alerts = screen.queryAllByRole('alert')
    expect(alerts.every(el => el.getAttribute('data-testid') === 'jobform-project-roster-error')).toBe(true)
  })

  it('keeps the roster failure visible after an unrelated successful save -- it describes the folder, not the save outcome', async () => {
    vi.mocked(api.kirocrewAgents).mockRejectedValueOnce(new Error('network unreachable'))
    vi.mocked(api.updateCron).mockResolvedValue({ ok: true })
    const onSaved = vi.fn()
    renderWithProviders(
      <JobForm job={messageJob()} agents={[]} defaultAgent="" onSaved={onSaved} layout="vertical" />,
    )

    fireEvent.change(screen.getByLabelText('Working directory'), {
      target: { value: '/Users/you/projects/myrepo' },
    })
    await waitFor(() => expect(screen.getByTestId('jobform-project-roster-error')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1))
    // A prior implementation shared one `error` string between the roster
    // fetch and Save-time validation, whose only clear point was the Save
    // handler's own `setError('')` -- so a successful save happened to also
    // wipe the (unrelated) roster message as a side effect, and the reverse
    // held too: a real validation error could be clobbered by a late roster
    // retry. The roster notice is now independent state describing the
    // CURRENT folder, so it is unaffected by an unrelated save outcome --
    // it still needs its own retry/folder-change to clear, checked below.
    expect(screen.getByTestId('jobform-project-roster-error')).toBeInTheDocument()
  })

  it('clears the roster failure when the folder is changed to one that resolves', async () => {
    vi.mocked(api.kirocrewAgents)
      .mockRejectedValueOnce(new Error('network unreachable'))
      .mockResolvedValueOnce({ agents: [], default_agent: '' })
    renderWithProviders(
      <JobForm job={messageJob()} agents={[]} defaultAgent="" onSaved={() => {}} layout="vertical" />,
    )

    const input = screen.getByLabelText('Working directory')
    fireEvent.change(input, { target: { value: '/Users/you/projects/myrepo' } })
    await waitFor(() => expect(screen.getByTestId('jobform-project-roster-error')).toBeInTheDocument())

    fireEvent.change(input, { target: { value: '/Users/you/projects/other' } })
    await waitFor(() =>
      expect(screen.queryByTestId('jobform-project-roster-error')).not.toBeInTheDocument(),
    )
  })
})
