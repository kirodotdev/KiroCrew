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
 * `createCron` had its own `.catch((e: Error) => ({ error: e.message }))`
 * from the start, but `updateCron` (the EDIT path) had none — so a rejected
 * PATCH (e.g. the backend's `project_path` validation) skipped past the
 * `if (res.error)` branch entirely and fell into the generic
 * `catch { setError('Failed to save') }`, discarding the real backend
 * message. Confirmed live: editing a job with an invalid working directory
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
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Working directory must be an absolute path (e.g. /Users/you/projects/myrepo).',
      )
    })
    expect(screen.queryByText('Failed to save')).not.toBeInTheDocument()
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
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Working directory must be an existing directory.',
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
      expect(screen.getByRole('alert')).toHaveTextContent(
        "Working directory refers to a protected system path and can't be used.",
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
      expect(screen.getByRole('alert')).toHaveTextContent("Agent 'ea-dev' not found")
    })
  })

  it('still saves successfully when updateCron resolves', async () => {
    vi.mocked(api.updateCron).mockResolvedValue({ ok: true })
    const onSaved = vi.fn()
    renderWithProviders(
      <JobForm job={messageJob()} agents={[]} defaultAgent="" onSaved={onSaved} layout="vertical" />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
