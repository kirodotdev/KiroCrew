import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { InstancesPanel } from '../pages/settings/InstancesPanel'

vi.mock('../api/client', () => {
  class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  }
  return {
    ApiError,
    api: {
      listInstances: vi.fn(),
      addInstance: vi.fn(),
      connectInstance: vi.fn(),
      disconnectInstance: vi.fn(),
      removeInstance: vi.fn(),
      instanceStatus: vi.fn(),
      restartInstance: vi.fn(),
      patchConfig: vi.fn(),
    },
  }
})
import { api, ApiError } from '../api/client'

beforeEach(() => vi.clearAllMocks())

describe('InstancesPanel', () => {
  it('shows an Enable toggle when the feature is disabled (403) and calls patchConfig', async () => {
    ;vi.mocked(api.listInstances).mockRejectedValue(
      new ApiError(403, 'instances feature is disabled (set instances.enabled=true)'),
    )
    ;vi.mocked(api.patchConfig).mockResolvedValue({})
    const u = userEvent.setup()
    renderWithProviders(<InstancesPanel />)
    expect(await screen.findByText(/Remote crew management is off/i)).toBeInTheDocument()
    await u.click(screen.getByRole('button', { name: /Enable remote crew management/i }))
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('instances.enabled', true))
  })

  it('shows a restart-required banner + Disable toggle when enabled but not active', async () => {
    ;vi.mocked(api.listInstances).mockResolvedValue({ active: false, instances: [], warm_set_cap: 5 })
    renderWithProviders(<InstancesPanel />)
    expect(await screen.findByText(/not active yet/i)).toBeInTheDocument()
    expect(screen.getByText(/kirocrew restart/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Disable remote crew management/i })).toBeInTheDocument()
  })

  it('renders the empty state + Add form when no instances configured', async () => {
    ;vi.mocked(api.listInstances).mockResolvedValue({ active: true, instances: [], warm_set_cap: 5 })
    renderWithProviders(<InstancesPanel />)
    expect(await screen.findByText(/No remote crews configured yet/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add remote crew' })).toBeInTheDocument()
  })

  it('passes the optional remote_bin path through the Add form', async () => {
    ;vi.mocked(api.listInstances).mockResolvedValue({ active: true, instances: [], warm_set_cap: 5 })
    ;vi.mocked(api.addInstance).mockResolvedValue({})
    const u = userEvent.setup()
    renderWithProviders(<InstancesPanel />)

    await screen.findByText(/No remote crews configured yet/i)
    await u.type(screen.getByPlaceholderText('Remote Host 1'), 'Nimbus')
    await u.type(screen.getByPlaceholderText('host-1-alias'), 'nimbus-alias')
    await u.type(
      screen.getByPlaceholderText(/leave blank for standard installs/i),
      '/home/nimbus/.local/bin/kirocrew',
    )
    await u.click(screen.getByRole('button', { name: 'Add remote crew' }))

    await waitFor(() =>
      expect(api.addInstance).toHaveBeenCalledWith(
        expect.objectContaining({
          name: 'Nimbus',
          ssh_host: 'nimbus-alias',
          remote_bin: '/home/nimbus/.local/bin/kirocrew',
        }),
      ),
    )
  })

  it('blocks adding an instance whose remote port duplicates another (SEC-016 mirror)', async () => {
    ;vi.mocked(api.listInstances).mockResolvedValue({
      active: true,
      warm_set_cap: 5,
      instances: [
        {
          id: 'cd-1',
          name: 'CD1',
          ssh_host: 'cd-1-alias',
          remote_port: 7777,
          local_port: 0,
          ttl: '20h',
          status: { state: 'disconnected' },
        },
      ],
    })
    const u = userEvent.setup()
    renderWithProviders(<InstancesPanel />)

    // Default remote port is 7777, which collides with the existing instance.
    expect(
      await screen.findByText(/already used by another remote crew/i),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add remote crew' })).toBeDisabled()

    // A distinct port clears the guard and re-enables Add.
    const portInput = screen.getByPlaceholderText('7777')
    await u.clear(portInput)
    await u.type(portInput, '7800')
    expect(screen.queryByText(/already used by another remote crew/i)).not.toBeInTheDocument()
  })

  it('switching the connection method to AWS SSM swaps in the SSM fields', async () => {
    // Regression guard for the native-<select> → SimpleSelect migration. The
    // picker is a Radix Select: a `change` event on the trigger does nothing —
    // open it, then click the option. Swapping the form's fields is the
    // observable consequence of the state move.
    ;vi.mocked(api.listInstances).mockResolvedValue({ active: true, instances: [], warm_set_cap: 5 })
    renderWithProviders(<InstancesPanel />)

    const trigger = await screen.findByRole('combobox', { name: 'Connection method' })
    expect(trigger).toHaveTextContent('SSH tunnel')
    expect(screen.getByLabelText('SSH host / alias')).toBeInTheDocument()

    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name: 'AWS SSM Session Manager' }))

    expect(trigger).toHaveTextContent('AWS SSM Session Manager')
    expect(screen.getByLabelText('SSM target (instance id)')).toBeInTheDocument()
    expect(screen.queryByLabelText('SSH host / alias')).not.toBeInTheDocument()
  })
})
