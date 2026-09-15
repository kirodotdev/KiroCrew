import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SchedulePage from '../pages/SchedulePage'
import { SCHEDULE_PRESETS, presetCanonicalPrompt } from '../utils/schedulePresets'
import type { CronJob } from '../types'

// The hint fires only when the SOURCE TEMPLATE moved -- attributed via the
// saved canonical template-prompt snapshot, not the job's (possibly edited)
// message. It names the template and is dismissible.

const sample = SCHEDULE_PRESETS[0]
const CANON = presetCanonicalPrompt(sample.id)

const mkJob = (overrides: Partial<CronJob> = {}): CronJob => ({
  id: 'job-1',
  name: 'Seeded job',
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
    defaultAgent: vi.fn().mockResolvedValue({ default_agent: '' }),
  },
}))

const openDetail = async () => {
  await waitFor(() => expect(screen.getByText('Seeded job')).toBeInTheDocument())
  fireEvent.click(screen.getByText('Seeded job'))
}

const NOTICE = 'schedule-template-updated-notice'

describe('SchedulePage template-updated hint (attributable)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    try { localStorage.clear() } catch { /* ignore */ }
  })

  it('shows the notice, naming the template, when the template moved', async () => {
    const { api } = await import('../api/client')
    // snapshot != current template prompt -> the TEMPLATE moved.
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ source_preset: sample.id, source_template_prompt: CANON + ' (old wording)', message: 'my own edits' })],
    })

    renderWithProviders(<SchedulePage />)
    await openDetail()

    const notice = await screen.findByTestId(NOTICE)
    expect(notice).toBeInTheDocument()
    // Names its subject.
    expect(notice.textContent).toContain(sample.title)
  })

  it('does NOT show the notice when only the user edited their copy', async () => {
    const { api } = await import('../api/client')
    // snapshot == current template prompt -> template unchanged; the differing
    // `message` is the user's own edit, which must not fire the hint.
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ source_preset: sample.id, source_template_prompt: CANON, message: 'heavily edited by me' })],
    })

    renderWithProviders(<SchedulePage />)
    await openDetail()

    expect(await screen.findByText('Pause')).toBeInTheDocument()
    expect(screen.queryByTestId(NOTICE)).not.toBeInTheDocument()
  })

  it('does not show the notice for a job with no source_preset', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob({ message: 'hand written' })] })

    renderWithProviders(<SchedulePage />)
    await openDetail()

    expect(await screen.findByText('Pause')).toBeInTheDocument()
    expect(screen.queryByTestId(NOTICE)).not.toBeInTheDocument()
  })

  it('dismiss removes the notice', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ source_preset: sample.id, source_template_prompt: CANON + ' (old wording)' })],
    })

    renderWithProviders(<SchedulePage />)
    await openDetail()

    expect(await screen.findByTestId(NOTICE)).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('schedule-template-updated-dismiss'))
    await waitFor(() => expect(screen.queryByTestId(NOTICE)).not.toBeInTheDocument())
  })
})
