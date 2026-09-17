import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
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
    agentCatalog: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    chatFolders: vi.fn().mockResolvedValue([]),
  },
}))

function messageJob(overrides: Partial<CronJob> = {}): CronJob {
  return {
    id: 'j1', name: 'nightly', message: 'do the thing', schedule: '', enabled: true,
    cron_expr: '0 3 * * *', ...overrides,
  } as CronJob
}

/**
 * The project-directory placeholder in a mono input read as a FILLED value and
 * was macOS-specific. Prefixing with "e.g. " marks it as an example (UX Review
 * span=435d37ba26ce). Pinned so the "e.g. " lead cannot be dropped silently.
 */
describe('JobForm project-directory placeholder is marked as an example', () => {
  it('prefixes the example path with "e.g. "', () => {
    renderWithProviders(
      <JobForm job={messageJob()} agents={[]} defaultAgent="" onSaved={() => {}} layout="vertical" />,
    )
    const input = screen.getByLabelText('Project directory')
    expect(input.getAttribute('placeholder')).toMatch(/^e\.g\. /)
  })
})

/** The project binding is cleared for a job kind whose dispatch never reads it.
 *
 *  `project_path` is rendered only under `!isLlmless`, and a script/command
 *  job's subprocess dispatch derives no cwd from it. Submitting it anyway on a
 *  conversion re-persists a folder the form no longer shows and the run never
 *  uses -- the same reason `chat_folder_id` is cleared there (Opus Review
 *  span=be4da06858f3), and the same way a setting starts lying.
 */
describe('project binding on a job kind that cannot use it', () => {
  const makeBindingJob = (over: Partial<CronJob> = {}): CronJob =>
    ({
      id: 'pb1', name: 'bound', message: 'Write the brief.', schedule: '', enabled: true,
      every_secs: 3600, ...over,
    }) as CronJob

  it('clears project_path when a bound job is converted to a script job', () => {
    const body = buildBody(
      { ...parseJobDefaults(makeBindingJob({ script: 'a.py:run' })), projectPath: '/tmp/proj' },
      'UTC',
      () => {},
      true,
    )
    expect(body?.project_path).toBe('')
  })

  it('still sends the folder for a message job, including the empty clear', () => {
    const bound = buildBody(
      { ...parseJobDefaults(undefined), name: 'n', message: 'm', projectPath: '/tmp/proj' },
      'UTC',
      () => {},
    )
    expect(bound?.project_path).toBe('/tmp/proj')

    // "" is the real value for "unbind", so it must still reach the backend
    // rather than being dropped as falsy.
    const cleared = buildBody(
      { ...parseJobDefaults(makeBindingJob({ project_path: '/tmp/proj' })), projectPath: '' },
      'UTC',
      () => {},
    )
    expect(cleared?.project_path).toBe('')
  })
})

/**
 * The form-wide Save notice, identified by carrying no `data-testid`.
 *
 * Several notices in this form share `role="alert"` -- the project-directory
 * field error, the folder-order notice -- so a bare `getByRole('alert')` is
 * ambiguous and a total alert count couples these tests to how many tagged
 * siblings happen to render. Every tagged notice belongs to a specific field;
 * the form-wide one is the only untagged alert, so selecting on that is what
 * actually names the thing these tests are about.
 */
function formWideAlerts(): HTMLElement[] {
  return screen.queryAllByRole('alert').filter(el => !el.getAttribute('data-testid'))
}

/**
 * `createCron` had its own `.catch((e: Error) => ({ error: e.message }))`
 * from the start, but `updateCron` (the EDIT path) had none — so a rejected
 * PATCH (e.g. the backend's `project_path` validation) skipped past the
 * `if (res.error)` branch entirely and fell into the generic
 * `catch { setError('Failed to save') }`, discarding the real backend
 * message. Confirmed live: editing a job with an invalid project directory
 * always showed "Failed to save" with no way to tell why. These pin BOTH
 * halves of the fix — updateCron's own error surfaces at all, AND the
 * project_path-specific messages get remapped to the UI's own field label
 * rather than the raw backend field name.
 */
describe('cron JobForm surfaces the real update error (not "Failed to save")', () => {
  it('shows updateCron\'s rejection message instead of the generic failed-to-save text', async () => {
    vi.mocked(api.updateCron).mockRejectedValue(new Error('project_path must be an absolute path'))
    renderWithProviders(
      <JobForm job={messageJob()} agents={[]} defaultAgent="" onSaved={() => {}} layout="vertical" />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(screen.getByTestId('jobform-project-path-save-error')).toHaveTextContent(
        'Project directory must be an absolute path (e.g. /Users/you/projects/myrepo).',
      )
    })
    expect(screen.queryByText('Failed to save')).not.toBeInTheDocument()
    // Beside the project-directory field it names, NOT the form-wide notice at
    // the foot of the form the user must scroll to (UX Review span=d88c9e1f411b).
    // The form-wide notice is the alert carrying no `data-testid`, so its
    // absence is what this pins -- a total alert count would also fail for a
    // tagged sibling notice that says nothing about where this error rendered.
    expect(formWideAlerts()).toHaveLength(0)
    expect(screen.getByTestId('jobform-project-path-save-error')).toHaveAttribute('role', 'alert')
  })

  it('remaps the "existing directory" rejection to the field label, with no backend field name in it', async () => {
    vi.mocked(api.updateCron).mockRejectedValue(
      new Error('project_path must be an existing directory'),
    )
    renderWithProviders(
      <JobForm job={messageJob()} agents={[]} defaultAgent="" onSaved={() => {}} layout="vertical" />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(screen.getByTestId('jobform-project-path-save-error')).toHaveTextContent(
        'Project directory must be an existing directory.',
      )
    })
    expect(screen.queryByText(/project_path/)).not.toBeInTheDocument()
  })

  it('remaps the sensitive-path rejection to the field label', async () => {
    vi.mocked(api.updateCron).mockRejectedValue(
      new Error('project_path refers to a sensitive path'),
    )
    renderWithProviders(
      <JobForm job={messageJob()} agents={[]} defaultAgent="" onSaved={() => {}} layout="vertical" />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(screen.getByTestId('jobform-project-path-save-error')).toHaveTextContent(
        "Project directory refers to a protected system path and can't be used.",
      )
    })
  })

  it('falls through to the raw message for a non-project_path rejection', async () => {
    vi.mocked(api.updateCron).mockRejectedValue(new Error('Agent \'ea-dev\' not found'))
    renderWithProviders(
      <JobForm job={messageJob()} agents={[]} defaultAgent="" onSaved={() => {}} layout="vertical" />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(formWideAlerts()[0]).toHaveTextContent("Agent 'ea-dev' not found")
    })
    // A rejection that is NOT about the project directory stays form-wide; it
    // is not pinned to that one field.
    expect(screen.queryByTestId('jobform-project-path-save-error')).not.toBeInTheDocument()
  })

  it('still saves successfully when updateCron resolves', async () => {
    vi.mocked(api.updateCron).mockResolvedValue({ ok: true })
    const onSaved = vi.fn()
    renderWithProviders(
      <JobForm job={messageJob()} agents={[]} defaultAgent="" onSaved={onSaved} layout="vertical" />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1))
    expect(formWideAlerts()).toHaveLength(0)
  })
})
