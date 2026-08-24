import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { InstancesPanel, humanizeSecs } from '../pages/settings/InstancesPanel'

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

  it('allows adding an instance whose remote port matches another (#1972)', async () => {
    // Two stock installs necessarily report the SAME default remote port. The
    // local forward port is allocated independently, so this is a supported
    // configuration — the form must not treat it as a conflict.
    ;vi.mocked(api.listInstances).mockResolvedValue({
      active: true,
      warm_set_cap: 5,
      instances: [
        {
          id: 'cd-1',
          name: 'CD1',
          ssh_host: 'cd-1-alias',
          remote_port: 5476,
          local_port: 0,
          ttl: '20h',
          status: { state: 'disconnected' },
        },
      ],
    })
    const u = userEvent.setup()
    renderWithProviders(<InstancesPanel />)

    // The form pre-fills the port a stock gateway actually binds, which is the
    // same port the existing crew uses.
    const portInput = await screen.findByPlaceholderText('5476')
    expect(portInput).toHaveValue('5476')
    expect(screen.queryByText(/already used by another remote crew/i)).not.toBeInTheDocument()

    // Filling the remaining required fields enables Add despite the shared port.
    await u.type(screen.getByLabelText('Name'), 'CD2')
    await u.type(screen.getByLabelText('SSH host / alias'), 'cd-2-alias')
    expect(screen.getByRole('button', { name: 'Add remote crew' })).toBeEnabled()
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

  it('formats a token lifetime down to the unit that reads naturally', () => {
    // Drives the header's "expires in …" text, so a wrong unit here is a user
    // reading the wrong deadline for a credential.
    expect(humanizeSecs(3 * 3600 + 12 * 60)).toMatch(/3\s*h.*12\s*m/)
    expect(humanizeSecs(2 * 3600)).toMatch(/2\s*h/)
    expect(humanizeSecs(45 * 60)).toMatch(/45\s*m/)
    expect(humanizeSecs(30)).toMatch(/30\s*s/)
    // Non-positive is not an error state to hide: an expired token reads "0".
    expect(humanizeSecs(0)).toMatch(/0\s*s/)
    expect(humanizeSecs(-5)).toMatch(/0\s*s/)
  })

  it('reports a connect that came back not-connected instead of claiming success', async () => {
    // The mutation resolves either way; only `state` says whether the tunnel is
    // up, so treating a resolved promise as success would show a crew as
    // connected while its forward never opened.
    const inst = {
      id: 'i1', name: 'box', ssh_host: 'box', remote_port: 7777, local_port: 7801,
      ttl: '20h', connection_method: 'ssh', ssm_target: '', aws_profile: '', aws_region: '',
      ssm_run_as: '', remote_bin: '', was_connected: false,
      status: { state: 'disconnected' as const },
    }
    ;vi.mocked(api.listInstances).mockResolvedValue({ active: true, instances: [inst], warm_set_cap: 5 } as never)
    ;vi.mocked(api.connectInstance).mockResolvedValue({ state: 'error', error: 'ssh exited 255' } as never)
    const u = userEvent.setup()
    renderWithProviders(<InstancesPanel />)

    await u.click(await screen.findByRole('button', { name: /^Connect$/i }))
    expect(await screen.findByText(/ssh exited 255/i)).toBeInTheDocument()
  })

  it('surfaces a diagnosis reason on the row that asked for it', async () => {
    const inst = {
      id: 'i1', name: 'box', ssh_host: 'box', remote_port: 7777, local_port: 7801,
      ttl: '20h', connection_method: 'ssh', ssm_target: '', aws_profile: '', aws_region: '',
      ssm_run_as: '', remote_bin: '', was_connected: false,
      status: { state: 'disconnected' as const },
    }
    ;vi.mocked(api.listInstances).mockResolvedValue({ active: true, instances: [inst], warm_set_cap: 5 } as never)
    ;vi.mocked(api.instanceStatus).mockResolvedValue({
      state: 'disconnected', diagnosis: { code: 'remote_down', reason: 'remote dashboard down' },
    } as never)
    const u = userEvent.setup()
    renderWithProviders(<InstancesPanel />)

    await u.click(await screen.findByRole('button', { name: /Diagnose box/i }))
    expect(await screen.findByText(/remote dashboard down/i)).toBeInTheDocument()
  })
})
