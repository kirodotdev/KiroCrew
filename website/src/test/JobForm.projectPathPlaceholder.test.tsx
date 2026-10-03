import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import JobForm from '../components/JobForm'
import type { CronJob } from '../types'

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
