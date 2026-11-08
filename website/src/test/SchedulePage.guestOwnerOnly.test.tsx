import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SchedulePage from '../pages/SchedulePage'
import type { CronJob } from '../types'

const guest = vi.hoisted(() => ({ value: true }))
vi.mock('../hooks/sharedCrew/useSharedCrew', async importOriginal => ({
  ...(await importOriginal<typeof import('../hooks/sharedCrew/useSharedCrew')>()),
  useIsGuest: () => guest.value,
}))

const job = (id: string, name: string, extra: Partial<CronJob> = {}): CronJob => ({
  id, name, message: 'msg', enabled: true, schedule: 'every 1h', last_status: 'ok', ...extra,
} as CronJob)

vi.mock('../api/client', () => ({
  api: {
    crons: vi.fn(),
    deleteCron: vi.fn().mockResolvedValue({}),
    batchDeleteCron: vi.fn().mockResolvedValue({}),
    createCron: vi.fn().mockResolvedValue({}),
    models: vi.fn().mockResolvedValue([]),
    updateCron: vi.fn().mockResolvedValue({}),
    toggleCron: vi.fn().mockResolvedValue({}),
    runCron: vi.fn().mockResolvedValue({}),
    cancelCron: vi.fn().mockResolvedValue({}),
    cronToChat: vi.fn().mockResolvedValue({}),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    agentCatalog: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    defaultAgent: vi.fn().mockResolvedValue({ default_agent: '' }),
    cronFolders: vi.fn().mockResolvedValue([]),
    cronHistoryAll: vi.fn().mockResolvedValue({ runs: [] }),
  },
}))

async function renderBoard() {
  const { api } = await import('../api/client')
  vi.mocked(api).crons.mockResolvedValue({
    jobs: [job('a1', 'Attended job'), job('b2', 'Auto job', { approval_mode: 'auto' }), job('c3', 'Script job', { script: 'x.py:f' })],
  })
  renderWithProviders(<SchedulePage />)
  await waitFor(() => expect(screen.getByText('Auto job')).toBeInTheDocument())
}

describe('SchedulePage for a guest on an owner-only schedule', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    guest.value = true
  })

  it('greys Run and Delete and the row checkbox, and says why', async () => {
    await renderBoard()
    const row = (name: string) => screen.getByText(name).closest('tr') as HTMLElement
    for (const name of ['Auto job', 'Script job']) {
      const tr = row(name)
      expect(tr.querySelector('[data-testid="schedule-owner-only"]')).not.toBeNull()
      expect((tr.querySelector('input[type="checkbox"]') as HTMLInputElement).disabled).toBe(true)
      const buttons = Array.from(tr.querySelectorAll('button')) as HTMLButtonElement[]
      const run = buttons.find(b => b.textContent === 'Run')
      const del = buttons.find(b => b.textContent === 'Delete')
      expect(run?.disabled).toBe(true)
      expect(del?.disabled).toBe(true)
      expect(run?.parentElement?.getAttribute('title')).toBe('Owner only')
      expect(del?.getAttribute('title')).toBe('Owner only')
    }
  })

  it('leaves an attended schedule alone', async () => {
    await renderBoard()
    const tr = screen.getByText('Attended job').closest('tr') as HTMLElement
    expect(tr.querySelector('[data-testid="schedule-owner-only"]')).toBeNull()
    expect((tr.querySelector('input[type="checkbox"]') as HTMLInputElement).disabled).toBe(false)
    const buttons = Array.from(tr.querySelectorAll('button')) as HTMLButtonElement[]
    expect(buttons.find(b => b.textContent === 'Run')?.disabled).toBe(false)
    expect(buttons.find(b => b.textContent === 'Delete')?.disabled).toBe(false)
  })

  it('locks nothing for the owner', async () => {
    guest.value = false
    await renderBoard()
    const tr = screen.getByText('Auto job').closest('tr') as HTMLElement
    expect(tr.querySelector('[data-testid="schedule-owner-only"]')).toBeNull()
    const buttons = Array.from(tr.querySelectorAll('button')) as HTMLButtonElement[]
    expect(buttons.find(b => b.textContent === 'Run')?.disabled).toBe(false)
    expect(buttons.find(b => b.textContent === 'Delete')?.disabled).toBe(false)
  })

  it('greys Run Now, Delete and Save in an owner-only schedule\'s detail dialog', async () => {
    await renderBoard()
    await act(async () => { screen.getByText('Auto job').click() })
    const dialog = await screen.findByRole('dialog')
    for (const name of ['Run Now', 'Delete', 'Save']) {
      const button = within(dialog).getByRole('button', { name })
      expect(button).toBeDisabled()
      expect(button.getAttribute('title')).toBe('Owner only')
    }
  })

  it('shows an owner-only schedule\'s fields read-only, and says so in the dialog itself', async () => {
    await renderBoard()
    await act(async () => { screen.getByText('Auto job').click() })
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByTestId('jobform-owner-only').textContent).toBe('Owner only')
    expect(within(dialog).getByLabelText('Name')).toBeDisabled()
  })

  it('greys Pause and Resume in an owner-only schedule\'s detail dialog', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [job('b2', 'Auto job', { approval_mode: 'auto' }), job('p4', 'Paused auto job', { approval_mode: 'auto', enabled: false })],
    })
    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Paused auto job')).toBeInTheDocument())
    await act(async () => { screen.getByText('Paused auto job').click() })
    const paused = await screen.findByRole('dialog')
    const resume = within(paused).getByRole('button', { name: 'Resume' })
    expect(resume).toBeDisabled()
    expect(resume.getAttribute('title')).toBe('Owner only')
    await act(async () => { within(paused).getByRole('button', { name: 'Close' }).click() })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await act(async () => { screen.getByText('Auto job').click() })
    const running = await screen.findByRole('dialog')
    const pause = within(running).getByRole('button', { name: 'Pause' })
    expect(pause).toBeDisabled()
    expect(pause.getAttribute('title')).toBe('Owner only')
  })

  it('leaves Run Now on for the owner in the detail dialog', async () => {
    guest.value = false
    await renderBoard()
    await act(async () => { screen.getByText('Auto job').click() })
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('button', { name: 'Run Now' })).toBeEnabled()
  })
})
