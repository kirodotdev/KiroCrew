/** Tests for the job form's Telegram destination picker.
 *
 *  A job can report to the Telegram private chat or one of its topics instead of
 *  the conversation that scheduled it. What matters: the picker exists only while
 *  Telegram is connected or the job is already pinned, a failed load says so, it offers the chat and the topics the bot already talks
 *  in, a pick reaches the backend as `send_message`'s `channel_type`/`target_id`
 *  pair, a pick and a Channel ID never coexist, a created job's pick does not carry
 *  over to the next one, and an unrelated edit never re-sends (and so never
 *  re-vets) the pin.
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
    channelTargets: vi.fn(),
    channelTopics: vi.fn(),
  },
}))

const CHAT = { channel_type: 'telegram', target_id: 'user:42', label: 'Telegram DM · 42', available: true, unavailable_reason: '' }
const TOPIC = { channel_type: 'telegram', target_id: 'topic:42:7', label: 'Cron' }

function makeJob(overrides: Partial<CronJob> = {}): CronJob {
  return {
    id: 'tg1', name: 'digest', message: 'Summarize the inbox.', schedule: '', enabled: true,
    every_secs: 3600, ...overrides,
  } as CronJob
}

beforeEach(() => {
  // Call counts carry over between tests otherwise, and one test asserts a call never happened.
  vi.clearAllMocks()
  vi.mocked(api.channelTargets).mockResolvedValue([CHAT])
  vi.mocked(api.channelTopics).mockResolvedValue([TOPIC])
})

describe('Telegram destination in the job form', () => {
  it('is not offered while Telegram is not connected', async () => {
    vi.mocked(api.channelTargets).mockResolvedValue([])
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    await waitFor(() => expect(api.channelTargets).toHaveBeenCalled())
    expect(screen.queryByLabelText('Telegram destination')).not.toBeInTheDocument()
    expect(api.channelTopics).not.toHaveBeenCalled()
  })

  it('keeps a saved pin on screen while Telegram is not connected, so it can be cleared', async () => {
    vi.mocked(api.channelTargets).mockResolvedValue([])
    const job = makeJob({ channel_type: 'telegram', target_id: 'topic:42:7' })
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} job={job} />)
    expect(await screen.findByLabelText('Telegram destination')).toHaveTextContent('topic:42:7')
  })

  it('says so when the destinations cannot be loaded', async () => {
    vi.mocked(api.channelTargets).mockRejectedValue(new Error('gateway unavailable'))
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    expect(await screen.findByText('Could not load your Telegram destinations.')).toBeInTheDocument()
  })

  it('points an existing job at a known topic, keeping its id', async () => {
    const onSaved = vi.fn()
    vi.mocked(api.updateCron).mockResolvedValue({})
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={onSaved} job={makeJob()} />)
    await userEvent.click(await screen.findByLabelText('Telegram destination'))
    expect(await screen.findByText('Private chat · 42')).toBeInTheDocument()
    await userEvent.click(await screen.findByText('Cron'))
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(api.updateCron).toHaveBeenCalledWith('tg1', expect.objectContaining({
      channel_type: 'telegram', target_id: 'topic:42:7',
    }))
  })

  it('keeps one destination: a pick empties the Channel ID, and a Channel ID turns the pick off', async () => {
    const onSaved = vi.fn()
    vi.mocked(api.updateCron).mockResolvedValue({})
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={onSaved} job={makeJob({ channel: 'C0AP77JJSN6' })} />)
    const picker = await screen.findByLabelText('Telegram destination')
    await userEvent.click(picker)
    await userEvent.click(await screen.findByText('Cron'))
    expect(screen.getByLabelText('Channel ID')).toHaveValue('')
    await userEvent.type(screen.getByLabelText('Channel ID'), 'C1')
    expect(picker).toHaveTextContent('Telegram: off')
    await userEvent.click(picker)
    await userEvent.click(await screen.findByText('Cron'))
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(api.updateCron).toHaveBeenCalledWith('tg1', expect.objectContaining({
      channel: '', channel_type: 'telegram', target_id: 'topic:42:7',
    }))
  })

  it('starts the next new job with no destination after one is created', async () => {
    vi.mocked(api.createCron).mockResolvedValue({})
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    await userEvent.type(screen.getByLabelText('Name'), 'digest')
    await userEvent.type(screen.getByLabelText('Message'), 'Summarize the inbox.')
    const picker = await screen.findByLabelText('Telegram destination')
    await userEvent.click(picker)
    await userEvent.click(await screen.findByText('Cron'))
    await userEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(api.createCron).toHaveBeenCalledWith(expect.objectContaining({ target_id: 'topic:42:7' })))
    await waitFor(() => expect(picker).toHaveTextContent('Telegram: off'))
  })

  it('leaves a saved pin out of an unrelated edit, and clears it when unpicked', () => {
    const pinned = parseJobDefaults(makeJob({ channel_type: 'telegram', target_id: 'topic:42:7' }))
    expect(buildBody({ ...pinned, name: 'renamed' }, 'UTC', () => {}, true)).not.toHaveProperty('target_id')
    expect(buildBody({ ...pinned, telegramTarget: '' }, 'UTC', () => {}, true)).toMatchObject({
      channel_type: '', target_id: '',
    })
  })
})
