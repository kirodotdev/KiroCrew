/** Tests for the create form's minimal-context control and its cheaper-mode hint.
 *
 *  The control changes what a job can SEE at run time, so the two properties that
 *  matter are that an existing job's setting round-trips instead of being
 *  silently cleared on the next save, and that the hint appears next to the
 *  control it names rather than somewhere the reader has to hunt for.
 *
 *  A script or command job takes no agent turn, so neither the control nor the
 *  hint belongs on it. That is asserted rather than assumed, because storing a
 *  flag that can never do anything is how a setting starts lying.
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders } from './helpers'
import JobForm, { buildBody, parseJobDefaults } from '../components/JobForm'
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

function makeJob(overrides: Partial<CronJob> = {}): CronJob {
  return {
    id: 'mc1', name: 'poller', message: 'Check the timestamp.', schedule: '', enabled: true,
    every_secs: 3600, ...overrides,
  } as CronJob
}

describe('minimal context in the job form', () => {
  it.each([false, true])('keeps private binding and the minimal-context toggle when editing=%s', async editing => {
    const onSaved = vi.fn()
    vi.mocked(api.createCron).mockResolvedValue({})
    vi.mocked(api.updateCron).mockResolvedValue({})
    const prompt = editing ? 'Summarize the new failures for me.' : 'Check whether disk usage is above 80.'
    renderWithProviders(<JobForm
      layout="vertical" agents={[]} onSaved={onSaved}
      memberId={editing ? 'different-member' : 'reviewer'} providerAgent="reviewer-provider"
      job={editing ? makeJob({ member_id: 'reviewer', message: prompt, minimal_context: true }) : undefined}
    />)
    if (!editing) {
      await userEvent.type(await screen.findByLabelText('Name'), 'Private check')
      await userEvent.type(screen.getByLabelText('Message'), prompt)
    }
    expect(screen.queryByTestId('jobform-mode-advice')).not.toBeInTheDocument()
    expect(screen.getByText("Skip optional context. This member's core guidance stays available.")).toBeVisible()
    const toggle = screen.getByLabelText('Minimal context')
    if (editing) expect(toggle).toBeChecked()
    else expect(toggle).not.toBeChecked()
    await userEvent.click(toggle)
    if (editing) expect(toggle).not.toBeChecked()
    else expect(toggle).toBeChecked()
    await userEvent.click(screen.getByRole('button', { name: editing ? 'Save' : 'Create' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    const body = expect.objectContaining({ member_id: 'reviewer', agent: 'reviewer-provider', minimal_context: !editing })
    if (editing) expect(api.updateCron).toHaveBeenCalledWith('mc1', body)
    else expect(api.createCron).toHaveBeenCalledWith(body)
  })

  it('keeps script advice for an explicitly Global-bound job', async () => {
    renderWithProviders(<JobForm layout="vertical" agents={[]} memberId="default" onSaved={() => {}} />)
    await userEvent.type(await screen.findByLabelText('Message'), 'Check whether disk usage is above 80.')
    expect(await screen.findByTestId('jobform-mode-advice')).toHaveTextContent(/script job/)
    expect(screen.queryByText("Skip optional context. This member's core guidance stays available.")).not.toBeInTheDocument()
    expect(screen.getByLabelText('Minimal context')).toBeEnabled()
  })

  it('defaults to off for a new job', () => {
    expect(parseJobDefaults(undefined).minimalContext).toBe(false)
  })

  it('reads the existing setting so an edit does not silently clear it', () => {
    expect(parseJobDefaults(makeJob({ minimal_context: true })).minimalContext).toBe(true)
    expect(parseJobDefaults(makeJob({ minimal_context: false })).minimalContext).toBe(false)
    // Absent on a job created before the field existed.
    expect(parseJobDefaults(makeJob()).minimalContext).toBe(false)
  })

  it('sends the flag for an agent job', () => {
    const f = { ...parseJobDefaults(makeJob()), minimalContext: true }
    const body = buildBody(f, 'UTC', () => {})
    expect(body!.minimal_context).toBe(true)
  })

  it('sends it as false rather than omitting it, so turning it back off persists', () => {
    const f = { ...parseJobDefaults(makeJob({ minimal_context: true })), minimalContext: false }
    const body = buildBody(f, 'UTC', () => {}, true)
    expect(body!.minimal_context).toBe(false)
  })

  it('omits the flag for a script job, which has no agent turn to trim', () => {
    const f = parseJobDefaults(makeJob({ script: '~/.kiro/crew/crons/f.py:run', message: '' }))
    const body = buildBody(f, 'UTC', () => {})
    expect(body!.minimal_context).toBeUndefined()
  })

  it('omits the flag for a command job', () => {
    const f = parseJobDefaults(makeJob({ command: 'df -h', message: '' }))
    const body = buildBody(f, 'UTC', () => {})
    expect(body!.minimal_context).toBeUndefined()
  })

  it('offers the control on the create form', async () => {
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    expect(await screen.findByLabelText('Minimal context')).toBeInTheDocument()
  })

  it('shows no hint before a prompt has been typed', () => {
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    expect(screen.queryByTestId('jobform-mode-advice')).not.toBeInTheDocument()
  })

  it('points a mechanical prompt at script mode', async () => {
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    await userEvent.type(
      await screen.findByLabelText('Message'),
      'Check whether disk usage is above 80.',
    )
    const note = await screen.findByTestId('jobform-mode-advice')
    expect(note).toHaveTextContent(/script job/)
    expect(note).toHaveAttribute('role', 'note')
  })

  it('points a reasoning prompt at minimal context instead of script mode', async () => {
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    await userEvent.type(
      await screen.findByLabelText('Message'),
      'Summarize the new failures for me.',
    )
    const note = await screen.findByTestId('jobform-mode-advice')
    expect(note).toHaveTextContent(/Minimal context keeps the agent/)
    expect(note).not.toHaveTextContent(/script job/)
  })

  it('stops nagging once the reader turns the control on', async () => {
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    await userEvent.type(
      await screen.findByLabelText('Message'),
      'Summarize the new failures for me.',
    )
    expect(await screen.findByTestId('jobform-mode-advice')).toBeInTheDocument()
    await userEvent.click(screen.getByLabelText('Minimal context'))
    expect(screen.queryByTestId('jobform-mode-advice')).not.toBeInTheDocument()
  })

  it('says nothing when the prompt drives a skill, which minimal context would break', async () => {
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    await userEvent.type(
      await screen.findByLabelText('Message'),
      'Load the prepare-pr skill and check the exit code.',
    )
    expect(screen.queryByTestId('jobform-mode-advice')).not.toBeInTheDocument()
  })

  it('offers neither the control nor the hint on a script job', async () => {
    renderWithProviders(
      <JobForm layout="vertical" agents={[]} job={makeJob({ script: '~/.kiro/crew/crons/f.py:run', message: '' })} onSaved={() => {}} />,
    )
    expect(await screen.findByLabelText('Name')).toBeInTheDocument()
    expect(screen.queryByLabelText('Minimal context')).not.toBeInTheDocument()
    expect(screen.queryByTestId('jobform-mode-advice')).not.toBeInTheDocument()
  })
})
