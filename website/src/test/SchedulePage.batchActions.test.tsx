import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, waitFor, within, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SchedulePage from '../pages/SchedulePage'
import { triggerCronRefresh } from '../store/dashboardSlice'
import { batchTargets } from '../components/CronBatchActionsMenu'
import type { CronJob } from '../types'

/**
 * The batch bar applies a row-menu action to every checked job, calling the
 * per-job endpoint once per job and skipping jobs the action would not change.
 * Set Telegram destination counts the checked jobs it changes, points them at one
 * Telegram destination, says how many now report there, and names every job it
 * did not update: a failure as an error, a Slack-pinned job as status.
 */

const mkJob = (overrides: Partial<CronJob> = {}): CronJob => ({
  id: 'job-1',
  name: 'Job',
  schedule: 'every 1d',
  message: 'm',
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
    channelTargets: vi.fn().mockResolvedValue([]),
    channelTopics: vi.fn().mockResolvedValue([]),
  },
}))

const JOBS = [
  mkJob({ id: 'a', name: 'Alpha job' }),
  mkJob({ id: 'b', name: 'Beta job', enabled: false, strict_schedule: true }),
  mkJob({ id: 'c', name: 'Gamma job', is_running: true }),
]

const check = (name: string) =>
  fireEvent.click(within(screen.getByText(name).closest('tr') as HTMLElement).getByRole('checkbox'))

async function renderChecked(names: string[]) {
  const { api } = await import('../api/client')
  vi.mocked(api).crons.mockResolvedValue({ jobs: JOBS })
  renderWithProviders(<SchedulePage />)
  await waitFor(() => expect(screen.getByText('Alpha job')).toBeInTheDocument())
  names.forEach(check)
  return api
}

function openMenu() {
  fireEvent.keyDown(screen.getByLabelText('Actions for the checked jobs'), { key: 'Enter' })
}

const menuItem = (label: string) =>
  screen.getAllByRole('menuitem').find(el => el.textContent?.startsWith(label)) as HTMLElement

const TG_CHAT = { channel_type: 'telegram', target_id: 'user:42', label: 'Telegram DM · 42', available: true, unavailable_reason: '' }
const TG_TOPIC = { channel_type: 'telegram', target_id: 'topic:42:7', label: 'Cron' }

/** Checks `names` among `jobs`, opens Actions and returns its Set Telegram destination row. */
async function findTelegramRow(jobs: CronJob[], names: string[]) {
  const { api } = await import('../api/client')
  vi.mocked(api).crons.mockResolvedValue({ jobs })
  renderWithProviders(<SchedulePage />)
  await waitFor(() => expect(screen.getByText(names[0])).toBeInTheDocument())
  names.forEach(check)
  openMenu()
  return (await screen.findByText('Set Telegram destination')).closest('[role="menuitem"]') as HTMLElement
}

/** The same, then opens that row's submenu. */
async function openTelegramSubmenu(jobs: CronJob[], names: string[]) {
  const { api } = await import('../api/client')
  // A Radix sub-trigger opens on ArrowRight.
  fireEvent.keyDown(await findTelegramRow(jobs, names), { key: 'ArrowRight' })
  return api
}

/** The same with Telegram connected, then picks the Cron topic; `beforePick` reads the open row first. */
async function sendToCronTopic(jobs: CronJob[], names: string[], beforePick?: (row: HTMLElement) => void) {
  const { api } = await import('../api/client')
  vi.mocked(api).channelTargets.mockResolvedValue([TG_CHAT])
  vi.mocked(api).channelTopics.mockResolvedValue([TG_TOPIC])
  await openTelegramSubmenu(jobs, names)
  // useTelegramDestinations starts the topics query only once the targets query has resolved.
  const topic = await screen.findByText('Topic · Cron', {}, { timeout: 5000 })
  beforePick?.(menuItem('Set Telegram destination'))
  fireEvent.click(topic.closest('[role="menuitem"]')!)
  return api
}

describe('batchTargets', () => {
  const running = new Set<string>()
  it('keeps only the jobs each action changes', () => {
    const ids = (a: Parameters<typeof batchTargets>[0]) => batchTargets(a, JOBS, running).map(j => j.id)
    expect(ids('pause')).toEqual(['a', 'c'])
    expect(ids('resume')).toEqual(['b'])
    expect(ids('strict_on')).toEqual(['a', 'c'])
    expect(ids('strict_off')).toEqual(['b'])
    expect(ids('run')).toEqual(['a'])
  })

  it('skips a job whose run was just started from this page', () => {
    expect(batchTargets('run', JOBS, new Set(['a']))).toEqual([])
  })
})

describe('SchedulePage batch actions', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    // Implementations outlive clearAllMocks, so a test's Telegram setup or update failure is undone here.
    const { api } = await import('../api/client')
    vi.mocked(api).channelTargets.mockResolvedValue([])
    vi.mocked(api).channelTopics.mockResolvedValue([])
    vi.mocked(api).updateCron.mockResolvedValue({})
  })

  it('pauses only the checked jobs that are not paused, and reloads once', async () => {
    const api = await renderChecked(['Alpha job', 'Beta job'])
    const loadsBefore = vi.mocked(api).crons.mock.calls.length
    openMenu()
    fireEvent.click(menuItem('Pause'))

    await waitFor(() => expect(api.toggleCron).toHaveBeenCalledTimes(1))
    expect(api.toggleCron).toHaveBeenCalledWith('a', false)
    await waitFor(() => expect(vi.mocked(api).crons.mock.calls.length).toBe(loadsBefore + 1))
    await waitFor(() => expect(screen.queryByText(/selected/)).not.toBeInTheDocument())
  })

  it('turns strict schedule off through the per-job update', async () => {
    const api = await renderChecked(['Alpha job', 'Beta job'])
    openMenu()
    fireEvent.click(menuItem('Allow small random delay'))
    await waitFor(() => expect(api.updateCron).toHaveBeenCalledWith('b', { strict_schedule: false }))
    expect(api.updateCron).toHaveBeenCalledTimes(1)
  })

  it('names the effect of each timing item and counts jobs, not bare numbers', async () => {
    await renderChecked(['Alpha job', 'Beta job'])
    openMenu()
    expect(menuItem('Run at exact time')).toHaveTextContent('Run at exact timeStart exactly on schedulechanges 1 job')
    expect(menuItem('Allow small random delay')).toHaveTextContent('Allow small random delayStart a little late to spread the loadchanges 1 job')
    expect(menuItem('Pause')).toHaveTextContent('Pausechanges 1 job')
    expect(screen.getByRole('menu').textContent).not.toMatch(/jitter|strict/i)
  })

  it('asks before Run now and runs only enabled, idle jobs', async () => {
    const api = await renderChecked(['Alpha job', 'Beta job', 'Gamma job'])
    openMenu()
    fireEvent.click(menuItem('Run Now'))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Run 1 job now?')).toBeInTheDocument()
    const skipped = within(dialog).getByTestId('schedule-run-skipped')
    expect(within(skipped).getAllByRole('listitem').map(li => li.textContent))
      .toEqual(['Beta job (paused)', 'Gamma job (already running)'])
    expect(api.runCron).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Run Now' }))

    await waitFor(() => expect(api.runCron).toHaveBeenCalledTimes(1))
    expect(api.runCron).toHaveBeenCalledWith('a')
  })

  it('returns focus to Actions after the delete confirm when a click alone opened the menu', async () => {
    await renderChecked(['Alpha job'])
    const trigger = screen.getByLabelText('Actions for the checked jobs')
    // No pointer or key event: `detail: 0` is the click a screen reader sends.
    fireEvent.click(trigger, { detail: 0 })
    fireEvent.click(await screen.findByRole('menuitem', { name: /^Delete 1 selected$/ }))

    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await waitFor(() => expect(document.activeElement).toBe(trigger))
  })

  it('lists at most five skipped jobs in the Run now confirm, then the rest as a count', async () => {
    const { api } = await import('../api/client')
    const paused = Array.from({ length: 7 }, (_, i) => mkJob({ id: `p${i}`, name: `Paused ${i}`, enabled: false }))
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob({ id: 'a', name: 'Alpha job' }), ...paused] })
    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Alpha job')).toBeInTheDocument())
    check('Alpha job'); paused.forEach(j => check(j.name))
    openMenu()
    fireEvent.click(menuItem('Run Now'))
    const skipped = within(await screen.findByRole('dialog')).getByTestId('schedule-run-skipped')
    expect(within(skipped).getAllByRole('listitem')).toHaveLength(5)
    expect(skipped).toHaveTextContent('+2 more')
  })

  it('skips a job paused while the Run now dialog was open', async () => {
    const { api } = await import('../api/client')
    const twoIdle = [mkJob({ id: 'a', name: 'Alpha job' }), mkJob({ id: 'd', name: 'Delta job' })]
    vi.mocked(api).crons.mockResolvedValue({ jobs: twoIdle })
    const { store } = renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Alpha job')).toBeInTheDocument())
    check('Alpha job'); check('Delta job')
    openMenu()
    fireEvent.click(menuItem('Run Now'))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Run 2 jobs now?')).toBeInTheDocument()

    vi.mocked(api).crons.mockResolvedValue({ jobs: [twoIdle[0], { ...twoIdle[1], enabled: false }] })
    const loads = vi.mocked(api).crons.mock.calls.length
    act(() => { store.dispatch(triggerCronRefresh()) })
    await waitFor(() => expect(vi.mocked(api).crons.mock.calls.length).toBeGreaterThan(loads))

    fireEvent.click(within(dialog).getByRole('button', { name: 'Run Now' }))
    await waitFor(() => expect(api.runCron).toHaveBeenCalledTimes(1))
    expect(api.runCron).toHaveBeenCalledWith('a')
  })

  it('keeps the batch bar to Clear and one Actions menu', async () => {
    await renderChecked(['Alpha job'])
    const bar = screen.getByText('1 selected').parentElement as HTMLElement
    expect(within(bar).getAllByRole('button').map(b => b.getAttribute('aria-label') ?? b.textContent))
      .toEqual(['Clear', 'Actions for the checked jobs'])
  })

  it('moves and deletes from the Actions menu', async () => {
    await renderChecked(['Alpha job'])
    openMenu()
    expect(screen.getByText('Move to folder')).toBeInTheDocument()
    fireEvent.click(await screen.findByRole('menuitem', { name: /^Delete 1 selected$/ }))
    expect(await screen.findByRole('dialog')).toHaveTextContent('Delete 1 scheduled job?')
  })

  it('keeps failed jobs checked and names the count and the reason', async () => {
    const api = await renderChecked(['Alpha job', 'Gamma job'])
    vi.mocked(api).toggleCron.mockImplementation(async (id: string) => {
      if (id === 'c') throw new Error('job is already running')
      return {}
    })
    openMenu()
    fireEvent.click(menuItem('Pause'))

    expect(await screen.findByText('Pause: 1 of 2 jobs failed (job is already running). Failed jobs stay selected.')).toBeInTheDocument()
    const rowCheckbox = (name: string) =>
      within(screen.getByText(name).closest('tr') as HTMLElement).getByRole('checkbox')
    expect(rowCheckbox('Gamma job')).toBeChecked()
    expect(rowCheckbox('Alpha job')).not.toBeChecked()
  })

  it('counts a refusal reported in the response body as a failure and names its reason', async () => {
    const api = await renderChecked(['Beta job'])
    vi.mocked(api).toggleCron.mockResolvedValue({ error: 'not your job' })
    openMenu()
    fireEvent.click(menuItem('Resume'))
    expect(await screen.findByText('Resume: 1 of 1 job failed (not your job). Failed jobs stay selected.')).toBeInTheDocument()
  })

  it('omits the reason when the failure carries none', async () => {
    const api = await renderChecked(['Alpha job', 'Gamma job'])
    vi.mocked(api).toggleCron.mockImplementation(async (id: string) => {
      if (id === 'c') throw 'bare rejection'  // not an Error: no message to show
      return {}
    })
    openMenu()
    fireEvent.click(menuItem('Pause'))
    expect(await screen.findByText('Pause: 1 of 2 jobs failed. Failed jobs stay selected.')).toBeInTheDocument()
  })

  it('points the unpinned checked jobs at a Telegram topic and lists a Slack-pinned one as unchanged', async () => {
    const jobs = [
      mkJob({ id: 'a', name: 'Alpha job' }),
      mkJob({ id: 'b', name: 'Beta job', enabled: false }),
      mkJob({ id: 's', name: 'Slack job', channel: 'C0123456' }),
    ]
    const api = await sendToCronTopic(jobs, ['Alpha job', 'Beta job', 'Slack job'])

    await waitFor(() => expect(api.updateCron).toHaveBeenCalledTimes(2))
    expect(api.updateCron).toHaveBeenCalledWith('a', { channel_type: 'telegram', target_id: 'topic:42:7' })
    expect(api.updateCron).toHaveBeenCalledWith('b', { channel_type: 'telegram', target_id: 'topic:42:7' })
    const kept = await screen.findByTestId('schedule-telegram-kept')
    expect(kept).toHaveTextContent('These checked jobs keep their Slack channel and were not updated. They stay selected.')
    expect(within(kept).getAllByRole('listitem').map(li => li.textContent)).toEqual(['Slack job'])
    expect(screen.getByTestId('schedule-telegram-updated')).toHaveTextContent('2 jobs now report to Topic · Cron.')
    // Nothing failed, so nothing is shown as an error.
    expect(screen.queryByTestId('schedule-telegram-failed')).not.toBeInTheDocument()
    // By the checkbox's own name: the kept list repeats the job's name as text.
    const rowCheckbox = (name: string) => screen.getByRole('checkbox', { name: `Select ${name}` })
    expect(rowCheckbox('Slack job')).toBeChecked()
    expect(rowCheckbox('Alpha job')).not.toBeChecked()
  })

  it('counts on Set Telegram destination only the checked jobs without a Slack channel', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).channelTargets.mockResolvedValue([TG_CHAT])
    const jobs = [
      mkJob({ id: 'a', name: 'Alpha job' }),
      mkJob({ id: 'b', name: 'Beta job', enabled: false }),
      mkJob({ id: 's', name: 'Slack job', channel: 'C0123456' }),
    ]
    const row = await findTelegramRow(jobs, ['Alpha job', 'Beta job', 'Slack job'])
    expect(row).toHaveTextContent('Set Telegram destinationchanges 2 jobs')
    expect(row).not.toHaveAttribute('data-disabled')
  })

  it('updates no job and lists it as kept when every checked job keeps a Slack channel', async () => {
    const api = await sendToCronTopic([mkJob({ id: 's', name: 'Slack job', channel: 'C0123456' })], ['Slack job'], row =>
      expect(row).toHaveTextContent('Set Telegram destinationchanges 0 jobs'))

    // The kept list proves the pick was handled, so the missing updates below are not just early.
    const kept = await screen.findByTestId('schedule-telegram-kept')
    expect(within(kept).getAllByRole('listitem').map(li => li.textContent)).toEqual(['Slack job'])
    expect(api.updateCron).not.toHaveBeenCalled()
    expect(screen.queryByTestId('schedule-telegram-updated')).not.toBeInTheDocument()
    expect(screen.queryByTestId('schedule-telegram-failed')).not.toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Select Slack job' })).toBeChecked()
  })

  it('says how many jobs now report to the picked destination when every update succeeds', async () => {
    await sendToCronTopic([mkJob({ id: 'a', name: 'Alpha job' }), mkJob({ id: 'b', name: 'Beta job' })], ['Alpha job', 'Beta job'])
    expect(await screen.findByTestId('schedule-telegram-updated')).toHaveTextContent('2 jobs now report to Topic · Cron.')
    expect(screen.queryByTestId('schedule-telegram-failed')).not.toBeInTheDocument()
    expect(screen.queryByTestId('schedule-telegram-kept')).not.toBeInTheDocument()
  })

  it('lists a job whose Telegram update failed, with the reason', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).updateCron.mockImplementation(async (id: string) => {
      if (id === 'b') throw new Error('target not allowed')
      return {}
    })
    await sendToCronTopic([mkJob({ id: 'a', name: 'Alpha job' }), mkJob({ id: 'b', name: 'Beta job' })], ['Alpha job', 'Beta job'])

    // The notice text itself names the job and the reason, so the agent hand-off carries them.
    expect(await screen.findByTestId('schedule-telegram-failed'))
      .toHaveTextContent('Some checked jobs could not be updated. They stay selected. Beta job (failed: target not allowed)')
  })

  it('says so in the Telegram submenu when the destinations cannot be loaded', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).channelTargets.mockRejectedValue(new Error('gateway unavailable'))
    await openTelegramSubmenu([mkJob({ id: 'a', name: 'Alpha job' })], ['Alpha job'])
    expect(await screen.findByText('Could not load your Telegram destinations.')).toBeInTheDocument()
  })

  it('disables an action no checked job would change', async () => {
    await renderChecked(['Beta job'])
    openMenu()
    expect(menuItem('Pause')).toHaveAttribute('data-disabled')
    expect(menuItem('Resume')).not.toHaveAttribute('data-disabled')
  })
})
