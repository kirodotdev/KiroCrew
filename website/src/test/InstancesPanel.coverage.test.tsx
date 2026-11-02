import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { InstancesPanel } from '../pages/settings/InstancesPanel'

vi.mock('../api/client', () => {
  class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.status = status
      this.body = body
    }
  }
  return {
    ApiError,
    api: {
      listInstances: vi.fn(),
      connectInstance: vi.fn(),
      disconnectInstance: vi.fn(),
      removeInstance: vi.fn(),
      instanceStatus: vi.fn(),
      patchConfig: vi.fn(),
    },
  }
})
import { api, ApiError } from '../api/client'

const row = (state: 'disconnected' | 'connected') => ({
  id: 'i1', name: 'box', ssh_host: 'box', remote_port: 7777, local_port: 7801,
  ttl: '20h', connection_method: 'ssh', ssm_target: '', aws_profile: '', aws_region: '',
  ssm_run_as: '', remote_bin: '', was_connected: state === 'connected',
  status: { state },
})
const listWith = (state: 'disconnected' | 'connected', active = true) =>
  vi.mocked(api.listInstances).mockResolvedValue({ active, instances: [row(state)], warm_set_cap: 5 } as never)

beforeEach(() => vi.clearAllMocks())

describe('InstancesPanel actions', () => {
  it('turns the feature on from the disabled state and shows the restart notice', async () => {
    vi.mocked(api.listInstances).mockRejectedValue(
      new ApiError(403, 'instances feature is disabled', JSON.stringify({ code: 'instances_disabled' })),
    )
    vi.mocked(api.patchConfig).mockResolvedValue({} as never)
    const u = userEvent.setup()
    renderWithProviders(<InstancesPanel />)

    await u.click(await screen.findByRole('button', { name: /Enable remote crew management/i }))
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('instances.enabled', true))
    expect(await screen.findByRole('status')).toBeInTheDocument()
  })

  it('turns the feature off from the enabled state', async () => {
    listWith('disconnected')
    vi.mocked(api.patchConfig).mockResolvedValue({} as never)
    const u = userEvent.setup()
    renderWithProviders(<InstancesPanel />)

    await u.click(await screen.findByRole('button', { name: /Disable remote crew management/i }))
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('instances.enabled', false))
  })

  it('confirms a connected tunnel in a status note that can be dismissed', async () => {
    listWith('disconnected')
    vi.mocked(api.connectInstance).mockResolvedValue({ state: 'connected' } as never)
    const u = userEvent.setup()
    renderWithProviders(<InstancesPanel />)

    await u.click(await screen.findByRole('button', { name: /^Connect$/i }))
    const note = await screen.findByText(/box/, { selector: 'span.break-words' })
    expect(note).toBeInTheDocument()
    await u.click(screen.getByRole('button', { name: /dismiss/i }))
    await waitFor(() => expect(screen.queryByText(/box/, { selector: 'span.break-words' })).not.toBeInTheDocument())
  })

  it('shows a rejected connect call with its error message', async () => {
    listWith('disconnected')
    vi.mocked(api.connectInstance).mockRejectedValue(new Error('gateway unreachable'))
    const u = userEvent.setup()
    renderWithProviders(<InstancesPanel />)

    await u.click(await screen.findByRole('button', { name: /^Connect$/i }))
    expect(await screen.findByText(/gateway unreachable/)).toBeInTheDocument()
  })

  it('disconnects a connected instance', async () => {
    listWith('connected')
    vi.mocked(api.disconnectInstance).mockResolvedValue({} as never)
    const u = userEvent.setup()
    renderWithProviders(<InstancesPanel />)

    await u.click(await screen.findByRole('button', { name: /Disconnect/i }))
    await waitFor(() => expect(api.disconnectInstance).toHaveBeenCalledWith('i1'))
  })

  it('removes an instance by disconnecting it first, then deleting it', async () => {
    listWith('disconnected')
    vi.mocked(api.removeInstance).mockResolvedValue({} as never)
    vi.mocked(api.disconnectInstance).mockRejectedValue(new Error('already down'))
    const u = userEvent.setup()
    renderWithProviders(<InstancesPanel />)

    await u.click(await screen.findByRole('button', { name: /remove box/i }))
    await waitFor(() => expect(api.removeInstance).toHaveBeenCalledWith('i1'))
    expect(api.disconnectInstance).toHaveBeenCalledWith('i1')
    expect(vi.mocked(api.disconnectInstance).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(api.removeInstance).mock.invocationCallOrder[0],
    )
  })

  it('reports a warning diagnosis as an error notice', async () => {
    listWith('disconnected')
    vi.mocked(api.instanceStatus).mockResolvedValue({
      state: 'disconnected', diagnosis: { code: 'remote_down', reason: 'remote dashboard down' },
    } as never)
    const u = userEvent.setup()
    renderWithProviders(<InstancesPanel />)

    await u.click(await screen.findByRole('button', { name: /Diagnose box/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/remote dashboard down/i)
    expect(api.instanceStatus).toHaveBeenCalledWith('i1', true)
  })
})
