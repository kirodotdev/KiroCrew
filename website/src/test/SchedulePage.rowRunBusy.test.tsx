import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, within, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import SchedulePage from '../pages/SchedulePage'
import type { CronJob } from '../types'

/**
 * A job row's Run, Cancel and Delete buttons while their request is out. They
 * took a native `disabled` and the label "...": the keyboard press that started
 * the request blurred focus to the page, so a failed run left the reader nowhere,
 * and a screen reader announced the busy button as dots. They stay focusable,
 * keep their name (the fixed Actions column has no room for a longer one),
 * say they are busy through `aria-busy`, and ignore a second press instead.
 * Mock conventions follow `SchedulePage.partlyBlocked.test.tsx`.
 */

const mkJob = (overrides: Partial<CronJob> = {}): CronJob => ({
  id: 'job-1',
  name: 'Nightly report',
  schedule: 'every 1d',
  message: 'send report',
  enabled: true,
  last_run_ts: 1_770_000_000,
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
    runCron: vi.fn(),
    cancelCron: vi.fn(),
    cronToChat: vi.fn().mockResolvedValue({}),
    cronHistoryAll: vi.fn().mockResolvedValue({ runs: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    agentCatalog: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    defaultAgent: vi.fn().mockResolvedValue({ default_agent: '' }),
  },
}))

const rowOf = (name: string) => screen.getByText(name).closest('tr') as HTMLElement

/** A request the test answers itself, so the busy state can be read. */
function held<T>() {
  let answer: (v: T) => void = () => {}
  const promise = new Promise<T>(resolve => { answer = resolve })
  return { promise, answer }
}

describe('SchedulePage row Run, Cancel and Delete while busy', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('keeps focus on Run, keeps its name, and ignores a second press', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob()] })
    const run = held<{ error?: string }>()
    vi.mocked(api).runCron.mockReturnValue(run.promise as never)

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())
    const button = within(rowOf('Nightly report')).getByRole('button', { name: 'Run' })

    button.focus()
    await userEvent.keyboard('{Enter}')
    expect(api.runCron).toHaveBeenCalledTimes(1)
    expect(button).toHaveAccessibleName('Run')
    expect(button).toHaveAttribute('aria-disabled', 'true')
    expect(button).toHaveAttribute('aria-busy', 'true')
    expect(button).not.toBeDisabled()
    expect(document.activeElement).toBe(button)
    // A sighted reader sees it working: a spinner over the hidden label, which
    // keeps the button's width so the row's other buttons do not move.
    expect(button.querySelector('svg.lucide-loader-circle, svg.lucide-loader2')).not.toBeNull()

    await userEvent.keyboard('{Enter}')
    expect(api.runCron).toHaveBeenCalledTimes(1)

    // A refused run leaves the row as it was, and the reader where they were.
    await act(async () => { run.answer({ error: 'job is paused upstream' }) })
    expect(await screen.findByText('job is paused upstream')).toBeInTheDocument()
    expect(button).toHaveAccessibleName('Run')
    expect(button).not.toHaveAttribute('aria-disabled')
    expect(button.querySelector('svg')).toBeNull()
    expect(document.activeElement).toBe(button)
  })

  it('keeps focus on Cancel, keeps its name, and ignores a second press', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob({ is_running: true })] })
    const cancel = held<{ error?: string }>()
    vi.mocked(api).cancelCron.mockReturnValue(cancel.promise as never)

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())
    const button = within(rowOf('Nightly report')).getByRole('button', { name: 'Cancel' })

    button.focus()
    await userEvent.keyboard('{Enter}')
    expect(api.cancelCron).toHaveBeenCalledTimes(1)
    expect(button).toHaveAccessibleName('Cancel')
    expect(button).toHaveAttribute('aria-busy', 'true')
    expect(button).toHaveAttribute('aria-disabled', 'true')
    expect(button.querySelector('svg')).not.toBeNull()
    expect(button).not.toBeDisabled()
    expect(document.activeElement).toBe(button)

    await userEvent.keyboard('{Enter}')
    expect(api.cancelCron).toHaveBeenCalledTimes(1)

    await act(async () => { cancel.answer({ error: 'nothing to cancel' }) })
    expect(await screen.findByText('nothing to cancel')).toBeInTheDocument()
    expect(button).toHaveAccessibleName('Cancel')
    expect(document.activeElement).toBe(button)
  })

  it('keeps focus on a confirmed Delete, keeps its name, and ignores a second press', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob()] })
    let refuse: (e: Error) => void = () => {}
    vi.mocked(api).deleteCron.mockReturnValue(new Promise((_, reject) => { refuse = reject }) as never)

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())
    const button = within(rowOf('Nightly report')).getByRole('button', { name: 'Delete' })

    button.focus()
    await userEvent.keyboard('{Enter}')
    expect(button).toHaveAccessibleName('Delete?')
    await userEvent.keyboard('{Enter}')
    expect(api.deleteCron).toHaveBeenCalledTimes(1)
    expect(button).toHaveAccessibleName('Delete')
    expect(button).toHaveAttribute('aria-disabled', 'true')
    expect(button).toHaveAttribute('aria-busy', 'true')
    expect(button).not.toBeDisabled()
    expect(document.activeElement).toBe(button)
    expect(button.querySelector('svg')).not.toBeNull()

    // A press while it is pending neither re-arms nor sends a second delete.
    await userEvent.keyboard('{Enter}')
    expect(api.deleteCron).toHaveBeenCalledTimes(1)
    expect(button).toHaveAccessibleName('Delete')

    // A refused delete keeps the row, and the reader where they were.
    await act(async () => { refuse(new Error('job is locked')) })
    expect(await screen.findByText('job is locked')).toBeInTheDocument()
    expect(button).toHaveAccessibleName('Delete')
    expect(button).not.toHaveAttribute('aria-busy')
    expect(document.activeElement).toBe(button)
  })

  it('keeps Run natively disabled on a paused job', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob({ enabled: false })] })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())
    expect(within(rowOf('Nightly report')).getByRole('button', { name: 'Run' })).toBeDisabled()
  })
})
