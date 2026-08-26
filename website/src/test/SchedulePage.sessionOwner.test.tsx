import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SchedulePage from '../pages/SchedulePage'
import type { CronJob } from '../types'

/**
 * The Name cell renders the job's owning session as a second line, because
 * ownership decides chat-side reachability: a job with no owning session is
 * invisible to cron_list in chat, and this line is the only place the
 * dashboard explains that. Both states must render — the EMPTY one is the
 * point — and they must be visually distinguishable beyond the words alone
 * (owned = mono, ownerless = italic prose), so the assertions pin the class
 * treatment as well as the copy.
 */

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

describe('SchedulePage job owner line', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders the owning session key in mono under the name', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ id: 'owned-1', name: 'Owned job', session_key: 'web-abc123' })],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Owned job')).toBeInTheDocument())

    const owner = screen.getByText('web-abc123')
    expect(owner).toBeInTheDocument()
    // Identifier treatment: mono, NOT the italic empty-state prose.
    expect(owner.className).toContain('font-mono')
    expect(owner.className).not.toContain('italic')
    // No empty-state copy for an owned job.
    expect(screen.queryByText('No owning session')).not.toBeInTheDocument()
  })

  it('renders explicit italic copy for an ownerless job, never a blank line', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ id: 'ownerless-1', name: 'Ownerless job', session_key: null })],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Ownerless job')).toBeInTheDocument())

    const empty = screen.getByText('No owning session')
    expect(empty).toBeInTheDocument()
    // Prose treatment: italic, NOT the owned state's mono — the two states
    // must differ visually, not just in words.
    expect(empty.className).toContain('italic')
    expect(empty.className).not.toContain('font-mono')
  })

  it('renders both states distinguishably in the same table', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [
        mkJob({ id: 'owned-1', name: 'Owned job', session_key: 'web-abc123' }),
        mkJob({ id: 'ownerless-1', name: 'Ownerless job', session_key: null }),
      ],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Owned job')).toBeInTheDocument())

    expect(screen.getByText('web-abc123')).toBeInTheDocument()
    expect(screen.getByText('No owning session')).toBeInTheDocument()
  })

  it('keeps the name in its own block-truncate span so it still ellipsizes', async () => {
    // With the owner line as a display:block sibling, a bare name text node
    // would be wrapped in an anonymous block box where text-overflow falls
    // back to clip — the name must carry the same block+truncate treatment
    // as the owner span to keep its ellipsis.
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ id: 'owned-1', name: 'Owned job', session_key: 'web-abc123' })],
    })

    renderWithProviders(<SchedulePage />)
    const name = await screen.findByText('Owned job')
    expect(name.tagName).toBe('SPAN')
    expect(name.className).toContain('block')
    expect(name.className).toContain('truncate')
  })

  it('labels the owner key in the row tooltip instead of dumping the bare key', async () => {
    // The hover title is the only full-width reading of a truncated owner
    // line, and an unlabeled `name · key` dump gives a reader no way to know
    // WHAT the second token is. The owned state carries the explicit
    // "Owning session:" label; the ownerless state keeps the explicit copy.
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [
        mkJob({ id: 'owned-1', name: 'Owned job', session_key: 'web-abc123' }),
        mkJob({ id: 'ownerless-1', name: 'Ownerless job', session_key: null }),
      ],
    })

    renderWithProviders(<SchedulePage />)
    const owned = await screen.findByText('Owned job')
    expect(owned.closest('td')?.getAttribute('title')).toBe('Owned job · Owning session: web-abc123')
    const ownerless = screen.getByText('Ownerless job')
    expect(ownerless.closest('td')?.getAttribute('title')).toBe('Ownerless job · No owning session')
  })

  it('matches the owner key in the jobs filter', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [
        mkJob({ id: 'owned-1', name: 'Owned job', session_key: 'web-abc123' }),
        mkJob({ id: 'ownerless-1', name: 'Ownerless job', session_key: null }),
      ],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Owned job')).toBeInTheDocument())

    // A key visible in the table must be findable through the filter box —
    // an affirmative "no jobs match" for an on-screen value is a lie.
    const filter = screen.getByPlaceholderText(/filter/i)
    fireEvent.change(filter, { target: { value: 'web-abc123' } })
    await waitFor(() => expect(screen.queryByText('Ownerless job')).not.toBeInTheDocument())
    expect(screen.getByText('Owned job')).toBeInTheDocument()
  })

  it('shows the owner in full in the detail dialog', async () => {
    const { api } = await import('../api/client')
    const longKey = 'web-0123456789abcdef0123456789abcdef0123456789abcdef'
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ id: 'owned-1', name: 'Owned job', session_key: longKey })],
    })

    renderWithProviders(<SchedulePage />)
    const row = await screen.findByText('Owned job')
    row.click()

    await waitFor(() => expect(screen.getByText('Owning session')).toBeInTheDocument())
    // The dialog copy of the key (the row line truncates; this one is full).
    const copies = screen.getAllByText(longKey)
    expect(copies.length).toBeGreaterThanOrEqual(2)
    // The helper sentence is the OWNERLESS state's explainer; under a live
    // key it would read as a warning about the job in front of the reader.
    expect(screen.queryByText(
      'A job without an owning session is invisible to cron_list in chat and is managed from this page or the CLI.',
    )).not.toBeInTheDocument()
  })

  it('shows the ownerless copy in the detail dialog', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ id: 'ownerless-1', name: 'Ownerless job', session_key: null })],
    })

    renderWithProviders(<SchedulePage />)
    const row = await screen.findByText('Ownerless job')
    row.click()

    await waitFor(() => expect(screen.getByText('Owning session')).toBeInTheDocument())
    // Row line + dialog value both show the empty-state copy.
    expect(screen.getAllByText('No owning session').length).toBeGreaterThanOrEqual(2)
    // The empty state is exactly where the helper sentence earns its place:
    // it names the consequence (invisible to cron_list) and the remedy
    // (manage from this page or the CLI) — and the value node references it
    // so assistive tech hears it as a hint on the value, not a second label.
    const help = screen.getByText(
      'A job without an owning session is invisible to cron_list in chat and is managed from this page or the CLI.',
    )
    expect(help).toBeInTheDocument()
    expect(help.id).toBe('owning-session-help')
    const dialogEmpty = screen.getAllByText('No owning session').find(
      el => el.getAttribute('aria-describedby') === 'owning-session-help',
    )
    expect(dialogEmpty).toBeTruthy()
  })
})
