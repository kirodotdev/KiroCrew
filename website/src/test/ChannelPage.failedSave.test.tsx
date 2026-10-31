/**
 * A refused save keeps what the user typed (Channels page).
 *
 * The New Channel dialog and the Add Agent form used to close BEFORE their
 * request ran, so a rejected `channelCreate` / `channelAddAgent` unmounted the
 * form and threw away the topic, preset, role, task and agent. Each form now
 * stays open until the save lands: a failure shows inside the form and the same
 * button retries; a success still closes it (and opens the new channel).
 *
 * Mock conventions follow `ChannelPageCoverage.test.tsx`: the api client is the
 * single seam, auto-mocked.
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest'
import { screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ChannelPage from '../pages/ChannelPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

vi.mock('../api/client')

beforeAll(() => {
  // jsdom doesn't implement scrollIntoView
  Element.prototype.scrollIntoView = vi.fn()
})

type Raw = Record<string, unknown>

const member = (over: Raw = {}): Raw => ({
  id: 'a1', role: 'Researcher', agent_name: 'kirocrew',
  state: 'listening', listen_mode: 'mention', approval_policy: 'writes', ...over,
})

const channelOf = (over: Raw = {}): Raw => ({
  id: 'ch1', topic: 'Gamma rollout', members: { a1: member() }, messages: [], ...over,
})

const ROSTER = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'built-in', source: 'kirocrew' },
  { name: 'oncall', kiro_agent: 'oncall-agent', workspace: 'oncall', memory_store: 'oncall-kb', description: 'paging', source: 'package' },
]

/** The channel a successful create returns. */
const CREATED = channelOf({ id: 'ch9', topic: 'Investigate gamma', members: {} })

/** A promise the test settles by hand, to hold a save in flight. */
function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

beforeEach(() => {
  vi.clearAllMocks()
  const channels = [channelOf()]
  vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels })
  // By id, so opening the created channel reads that channel, not the first one.
  vi.mocked(api).channelGet = vi.fn().mockImplementation((id: string) =>
    Promise.resolve(id === CREATED.id ? CREATED : channels[0]))
  vi.mocked(api).channelPresets = vi.fn().mockResolvedValue({})
  vi.mocked(api).kirocrewAgents = vi.fn().mockResolvedValue({ agents: ROSTER, default_agent: 'kirocrew' })
  vi.mocked(api).agentCatalog = vi.fn().mockResolvedValue({ agents: ROSTER, default_agent: 'kirocrew' })
})

async function renderPage() {
  renderWithProviders(<ChannelPage />)
  await waitFor(() => expect(screen.queryByText('Loading channels...')).not.toBeInTheDocument())
}

/** Open the dialog, type a topic and pick the second preset, then press Create. */
async function submitNewChannel(topic: string) {
  await userEvent.click(screen.getByRole('button', { name: '+ New' }))
  const dialog = await screen.findByRole('dialog', { name: 'New channel' })
  fireEvent.change(within(dialog).getByLabelText('Topic'), { target: { value: topic } })
  await userEvent.click(within(dialog).getByText('Custom (empty)'))
  await userEvent.click(within(dialog).getByRole('button', { name: 'Create' }))
  return dialog
}

/** Open the agents panel and the Add Agent form, fill role + task + agent, press Add. */
async function submitAddAgent() {
  await userEvent.click(await screen.findByRole('button', { name: '1 agent' }))
  await userEvent.click(screen.getByRole('button', { name: '+ Add Agent' }))
  await userEvent.click(await screen.findByLabelText('Switch agent'))
  await userEvent.click(await screen.findByText('oncall'))
  fireEvent.change(screen.getByLabelText('Role'), { target: { value: 'Logs Agent' } })
  fireEvent.change(screen.getByLabelText('Task'), { target: { value: 'Grep the access log' } })
  await userEvent.click(screen.getByRole('button', { name: 'Add' }))
}

describe('ChannelPage — a refused create keeps the New Channel dialog', () => {
  it('keeps the dialog, the topic and the preset, and shows the error inside it', async () => {
    vi.mocked(api).channelCreate = vi.fn().mockRejectedValue(new Error(JSON.stringify({ error: 'channel cap reached' })))
    await renderPage()
    const dialog = await submitNewChannel('Investigate gamma')

    const notice = await within(dialog).findByTestId('new-channel-error')
    expect(notice).toHaveTextContent('Failed to create channel')
    expect(notice).toHaveTextContent('channel cap reached')
    expect(screen.getByRole('dialog', { name: 'New channel' })).toBeInTheDocument()
    expect(within(dialog).getByLabelText('Topic')).toHaveValue('Investigate gamma')
    // The preset choice survived too: a retry sends the same empty team.
    vi.mocked(api).channelCreate = vi.fn().mockResolvedValue({ channel: CREATED })
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(vi.mocked(api).channelCreate).toHaveBeenCalledWith('Investigate gamma', []))
  })

  it('closes the dialog and opens the new channel once the retry succeeds', async () => {
    vi.mocked(api).channelCreate = vi.fn().mockRejectedValueOnce(new Error('gateway timeout'))
      .mockResolvedValueOnce({ channel: CREATED })
    await renderPage()
    const dialog = await submitNewChannel('Investigate gamma')
    await within(dialog).findByTestId('new-channel-error')

    await userEvent.click(within(dialog).getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New channel' })).not.toBeInTheDocument())
    expect(await screen.findByRole('heading', { name: 'Investigate gamma' })).toBeInTheDocument()
    expect(screen.queryByTestId('channel-error')).not.toBeInTheDocument()
  })

  it('locks the topic and preset while creating, then unlocks them after failure', async () => {
    const pending = deferred<{ channel: Raw }>()
    vi.mocked(api).channelCreate = vi.fn().mockReturnValue(pending.promise)
    await renderPage()
    const dialog = await submitNewChannel('Investigate gamma')
    const topic = within(dialog).getByLabelText('Topic')
    const incident = within(dialog).getByRole('button', { name: /Incident Response/ })
    const custom = within(dialog).getByRole('button', { name: /Custom \(empty\)/ })

    expect(topic).toHaveAttribute('readonly')
    await userEvent.type(topic, ' changed')
    await userEvent.click(incident)
    expect(topic).toHaveValue('Investigate gamma')
    expect(custom).toHaveClass('!border-accent')
    expect(incident).not.toHaveClass('!border-accent')

    pending.reject(new Error('gateway timeout'))
    await within(dialog).findByTestId('new-channel-error')
    expect(topic).not.toHaveAttribute('readonly')
    await userEvent.clear(topic)
    await userEvent.type(topic, 'Second attempt')
    await userEvent.click(incident)
    expect(topic).toHaveValue('Second attempt')
    expect(incident).toHaveClass('!border-accent')
  })

  it('sends one create for a double press while the first is in flight', async () => {
    const pending = deferred<{ channel: Raw }>()
    vi.mocked(api).channelCreate = vi.fn().mockReturnValue(pending.promise)
    await renderPage()
    const dialog = await submitNewChannel('Investigate gamma')
    const create = within(dialog).getByRole('button', { name: 'Create' })
    expect(create).toHaveAttribute('aria-busy', 'true')
    await userEvent.click(create)
    expect(vi.mocked(api).channelCreate).toHaveBeenCalledTimes(1)
    pending.resolve({ channel: CREATED })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New channel' })).not.toBeInTheDocument())
  })

  it('leaves a reopened dialog and its new draft open when the old create lands', async () => {
    const pending = deferred<{ channel: Raw }>()
    vi.mocked(api).channelCreate = vi.fn().mockReturnValue(pending.promise)
    await renderPage()
    const first = await submitNewChannel('Investigate gamma')
    await userEvent.click(within(first).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New channel' })).not.toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: '+ New' }))
    const second = await screen.findByRole('dialog', { name: 'New channel' })
    fireEvent.change(within(second).getByLabelText('Topic'), { target: { value: 'Second draft' } })

    pending.resolve({ channel: CREATED })
    expect(await screen.findByRole('heading', { name: 'Investigate gamma' })).toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: 'New channel' })).toBeInTheDocument()
    expect(within(second).getByLabelText('Topic')).toHaveValue('Second draft')
  })

  it('still reports a failure that lands after the dialog was cancelled', async () => {
    const pending = deferred<never>()
    vi.mocked(api).channelCreate = vi.fn().mockReturnValue(pending.promise)
    await renderPage()
    const dialog = await submitNewChannel('Investigate gamma')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New channel' })).not.toBeInTheDocument())

    pending.reject(new Error('gateway timeout'))
    const notice = await screen.findByTestId('channel-error')
    expect(notice).toHaveTextContent('Failed to create channel')
    expect(notice).toHaveTextContent('gateway timeout')
  })
})

describe('ChannelPage — a refused add keeps the Add Agent form', () => {
  it('keeps the form with its role, task and agent, and shows the error inside it', async () => {
    vi.mocked(api).channelAddAgent = vi.fn().mockRejectedValue(new Error('gateway timeout'))
    await renderPage()
    await submitAddAgent()

    const notice = await screen.findByTestId('add-agent-error')
    expect(notice).toHaveTextContent('Failed to add agent')
    expect(notice).toHaveTextContent('gateway timeout')
    expect(screen.getByLabelText('Role')).toHaveValue('Logs Agent')
    expect(screen.getByLabelText('Task')).toHaveValue('Grep the access log')
    expect(screen.queryByTestId('channel-error')).not.toBeInTheDocument()
  })

  it('locks role and task while adding, then unlocks them after failure', async () => {
    const pending = deferred<{ ok: boolean }>()
    vi.mocked(api).channelAddAgent = vi.fn().mockReturnValue(pending.promise)
    await renderPage()
    await submitAddAgent()
    const role = screen.getByLabelText('Role')
    const task = screen.getByLabelText('Task')

    expect(role).toHaveAttribute('readonly')
    expect(task).toHaveAttribute('readonly')
    await userEvent.type(role, ' changed')
    await userEvent.type(task, ' changed')
    expect(role).toHaveValue('Logs Agent')
    expect(task).toHaveValue('Grep the access log')

    pending.reject(new Error('gateway timeout'))
    await screen.findByTestId('add-agent-error')
    expect(role).not.toHaveAttribute('readonly')
    expect(task).not.toHaveAttribute('readonly')
    await userEvent.clear(role)
    await userEvent.type(role, 'Metrics Agent')
    await userEvent.clear(task)
    await userEvent.type(task, 'Inspect metrics')
    expect(role).toHaveValue('Metrics Agent')
    expect(task).toHaveValue('Inspect metrics')
  })

  it('closes the form once the retry succeeds, sending the same values', async () => {
    vi.mocked(api).channelAddAgent = vi.fn().mockRejectedValueOnce(new Error('gateway timeout'))
      .mockResolvedValueOnce({ ok: true })
    await renderPage()
    await submitAddAgent()
    await screen.findByTestId('add-agent-error')

    await userEvent.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '+ Add Agent' })).toBeInTheDocument())
    expect(vi.mocked(api).channelAddAgent).toHaveBeenCalledTimes(2)
    expect(vi.mocked(api).channelAddAgent).toHaveBeenLastCalledWith('ch1', {
      role: 'Logs Agent', task: 'Grep the access log', agent: 'oncall',
    })
    expect(screen.queryByTestId('add-agent-error')).not.toBeInTheDocument()
  })

  it('leaves a reopened Add Agent form and its new draft open when the old add lands', async () => {
    const pending = deferred<{ ok: boolean }>()
    vi.mocked(api).channelAddAgent = vi.fn().mockReturnValue(pending.promise)
    await renderPage()
    await submitAddAgent()
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await userEvent.click(await screen.findByRole('button', { name: '+ Add Agent' }))
    fireEvent.change(await screen.findByLabelText('Role'), { target: { value: 'Second role' } })

    // `act` flushes the old save's continuation before the assertion.
    await act(async () => { pending.resolve({ ok: true }); await pending.promise })
    expect(vi.mocked(api).channelAddAgent).toHaveBeenCalledTimes(1)
    expect(screen.getByLabelText('Role')).toHaveValue('Second role')
  })
})
