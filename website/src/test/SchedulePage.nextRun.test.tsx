import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SchedulePage from '../pages/SchedulePage'
import type { CronJob } from '../types'
import { i18next } from '../i18n/all'

/**
 * The Next run column is worded by the active locale, not built from English
 * templates: a German reader sees the German offset, and English keeps its
 * compact `in 2h` form.
 */

const NOW_MS = Date.UTC(2026, 10, 3, 12, 0, 0)

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
    agentCatalog: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    defaultAgent: vi.fn().mockResolvedValue({ default_agent: '' }),
  },
}))

const inTwoHours = (locale: string) =>
  new Intl.RelativeTimeFormat(locale, { numeric: 'auto', style: 'narrow' }).format(2, 'hour')

describe('SchedulePage next run', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(NOW_MS)
  })

  afterEach(async () => {
    vi.useRealTimers()
    if (i18next.language !== 'en') await i18next.changeLanguage('en')
  })

  it.each(['en', 'de'])('words the next run in %s', async (lang) => {
    await i18next.changeLanguage(lang)
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ next_run_ts: NOW_MS / 1000 + 2 * 3600 + 30 })],
    })

    renderWithProviders(<SchedulePage />)

    await waitFor(() => expect(screen.getByText(inTwoHours(lang))).toBeInTheDocument())
  })

  it('keeps the number for a run a day or more away, never "tomorrow"', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ next_run_ts: NOW_MS / 1000 + 47 * 3600 })],
    })

    renderWithProviders(<SchedulePage />)

    await waitFor(() => expect(screen.getByText('in 1d')).toBeInTheDocument())
    expect(screen.queryByText('tomorrow')).toBeNull()
  })

  it('reads a German next run in German, not English', async () => {
    await i18next.changeLanguage('de')
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ next_run_ts: NOW_MS / 1000 + 5 * 60 })],
    })

    renderWithProviders(<SchedulePage />)

    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())
    expect(screen.queryByText('in 5m')).toBeNull()
    expect(screen.getByText('in 5 m')).toBeInTheDocument()
  })
})
