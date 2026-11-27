import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders, createTestStore } from './helpers'
import { AddInstanceForm } from '../pages/settings/InstancesPanel'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'
import { prefillCrewAddForm } from '../store/instancesSlice'

// The add form's starting connection method follows the gateway's platform:
// SSH tunnel is not supported on a Windows gateway, so a new form starts on SSM.

const platformEnv = vi.hoisted(() => ({ value: 'other' as 'darwin' | 'windows' | 'other' }))

vi.mock('../hooks/useGatewayPlatform', async importOriginal => ({
  ...(await importOriginal<typeof import('../hooks/useGatewayPlatform')>()),
  useGatewayPlatform: () => platformEnv.value,
}))

vi.mock('../api/client', () => {
  class ApiError extends Error {
    status = 0
  }
  return {
    ApiError,
    isAuthExpiredError: () => false,
    api: {
      listInstances: vi.fn(),
      addInstance: vi.fn(),
      connectInstance: vi.fn(),
      disconnectInstance: vi.fn(),
      removeInstance: vi.fn(),
      instanceStatus: vi.fn(),
      updateInstance: vi.fn(),
      patchConfig: vi.fn(),
      cloudLaunches: vi.fn(),
      cloudPreflight: vi.fn(),
      cloudProvisioners: vi.fn(),
      cloudIamPolicy: vi.fn(),
      cloudLaunch: vi.fn(),
      cloudIdentity: vi.fn(),
      cloudLaunchStatus: vi.fn(),
      cloudLaunchCancel: vi.fn(),
      cloudLaunchSignin: vi.fn(),
      cloudStop: vi.fn(),
      cloudStart: vi.fn(),
      cloudDestroy: vi.fn(),
    },
  }
})
import { api } from '../api/client'

const SSH = 'SSH tunnel'
const SSM = 'AWS SSM Session Manager'
const method = () => screen.getByRole('combobox', { name: 'Connection method' }).textContent ?? ''

beforeEach(() => {
  vi.clearAllMocks()
  platformEnv.value = 'other'
  vi.mocked(api.cloudProvisioners).mockResolvedValue({ provisioners: [] })
  vi.mocked(api.cloudIdentity).mockResolvedValue({
    identity: { account_type: 'BuilderId' },
    suggested_target: { license: '', start_url: '', region: '' },
  })
  vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
  vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
})

describe('add form starting connection method', () => {
  it('starts a new form on AWS SSM Session Manager on a Windows gateway', () => {
    platformEnv.value = 'windows'
    renderWithProviders(<AddInstanceForm onAdded={() => {}} />)
    expect(method()).toContain(SSM)
    expect(screen.getByLabelText('SSM target (instance id)')).toHaveValue('')
    expect(screen.queryByLabelText('SSH host / alias')).toBeNull()
  })

  it.each(['other', 'darwin'] as const)('starts a new form on SSH tunnel when the gateway platform is %s', platform => {
    platformEnv.value = platform
    renderWithProviders(<AddInstanceForm onAdded={() => {}} />)
    expect(method()).toBe(SSH)
    expect(screen.getByLabelText('SSH host / alias')).toHaveValue('')
  })

  it('holds no draft for an untouched Windows form, and holds one once SSH tunnel is picked', async () => {
    platformEnv.value = 'windows'
    const u = userEvent.setup()
    const { store } = renderWithProviders(<AddInstanceForm onAdded={() => {}} />)
    expect(store.getState().instances.crewForms.add).toBeNull()
    await u.click(screen.getByRole('combobox', { name: 'Connection method' }))
    await u.click(screen.getByRole('option', { name: new RegExp(SSH) }))
    expect(method()).toContain(SSH)
    expect(store.getState().instances.crewForms.add).toMatchObject({ method: 'ssh', name: '' })
  })

  it('starts on AWS SSM Session Manager again when the Windows add modal is closed and reopened', async () => {
    platformEnv.value = 'windows'
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: 'Add Crew' }))
    await u.click(screen.getByRole('button', { name: /Connect a machine you have/ }))
    expect(method()).toContain(SSM)
    await u.click(screen.getByRole('button', { name: 'Close' }))
    await u.click(await screen.findByRole('button', { name: 'Add Crew' }))
    await u.click(screen.getByRole('button', { name: /Connect a machine you have/ }))
    expect(method()).toContain(SSM)
  })

  it('returns to AWS SSM Session Manager after a successful add on a Windows gateway', async () => {
    platformEnv.value = 'windows'
    vi.mocked(api.addInstance).mockResolvedValue({} as never)
    const u = userEvent.setup()
    const { store } = renderWithProviders(<AddInstanceForm onAdded={() => {}} />)
    await u.click(screen.getByRole('combobox', { name: 'Connection method' }))
    await u.click(screen.getByRole('option', { name: new RegExp(SSH) }))
    await u.type(screen.getByLabelText('Name'), 'box')
    await u.type(screen.getByLabelText('SSH host / alias'), 'box.example')
    await u.click(screen.getByRole('button', { name: 'Add remote crew' }))
    await waitFor(() => expect(api.addInstance).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.addInstance).mock.calls[0][0]).toMatchObject({ connection_method: 'ssh', ssh_host: 'box.example' })
    await waitFor(() => expect(screen.getByLabelText('Name')).toHaveValue(''))
    expect(method()).toContain(SSM)
    expect(store.getState().instances.crewForms.add).toBeNull()
  })

  it('keeps a form from an add link on SSH tunnel on a Windows gateway', () => {
    platformEnv.value = 'windows'
    const store = createTestStore()
    store.dispatch(prefillCrewAddForm({ host: 'me@devbox.example.com', port: '', name: 'Dev box' }))
    renderWithProviders(<AddInstanceForm onAdded={() => {}} />, { store })
    expect(method()).toContain(SSH)
    expect(screen.getByLabelText('SSH host / alias')).toHaveValue('me@devbox.example.com')
  })

  it('drops the draft and the link when a Windows link form is cleared by hand', async () => {
    platformEnv.value = 'windows'
    const u = userEvent.setup()
    const store = createTestStore()
    store.dispatch(prefillCrewAddForm({ host: 'devbox.example.com', port: '', name: 'Dev box' }))
    renderWithProviders(<AddInstanceForm onAdded={() => {}} />, { store })
    expect(store.getState().instances.crewForms.addLink).not.toBeNull()
    await u.clear(screen.getByLabelText('Name'))
    await u.clear(screen.getByLabelText('SSH host / alias'))
    expect(store.getState().instances.crewForms.add).toBeNull()
    expect(store.getState().instances.crewForms.addLink).toBeNull()
  })

  it('keeps the method and the blank check of a form already open when the platform reads Windows later', async () => {
    platformEnv.value = 'other'
    const u = userEvent.setup()
    const { store, rerender } = renderWithProviders(<AddInstanceForm onAdded={() => {}} />)
    expect(method()).toBe(SSH)
    platformEnv.value = 'windows'
    rerender(<AddInstanceForm onAdded={() => {}} />)
    expect(method()).toContain(SSH)
    await u.type(screen.getByLabelText('Name'), 'x')
    await u.clear(screen.getByLabelText('Name'))
    expect(store.getState().instances.crewForms.add).toBeNull()
  })
})
