import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders, createTestStore } from './helpers'
import { useAppSelector } from '../store'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'
import { prefillCrewAddForm, setCrewAddForm } from '../store/instancesSlice'
import { EMPTY_INSTANCE_FORM } from '../pages/settings/InstanceFormFields'

// A `kirocrew://remote-crew/add` link fills in the add form and nothing else:
// the crew is created only by the user's own Add click.
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

const NOTICE = 'Filled in from a link. Check that the SSH host is a machine you use, then add it.'
const link = { host: 'me@devbox.example.com', port: '7781', name: 'Dev box' }

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  vi.mocked(api.cloudProvisioners).mockResolvedValue({ provisioners: [] })
  vi.mocked(api.cloudIdentity).mockResolvedValue({
    identity: { account_type: 'BuilderId' },
    suggested_target: { license: '', start_url: '', region: '' },
  })
  vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
  vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
})

describe('Remote Crew add form opened by an add link', () => {
  it('starts from the link, says so, scrolls to it, and adds nothing on its own', async () => {
    const scrolled = vi.spyOn(HTMLElement.prototype, 'scrollIntoView').mockImplementation(() => {})
    const store = createTestStore()
    store.dispatch(prefillCrewAddForm(link))
    renderWithProviders(<RemoteCrewPanel />, { store })

    expect(await screen.findByText(NOTICE)).toBeInTheDocument()
    expect(screen.getByLabelText('Name')).toHaveValue('Dev box')
    expect(screen.getByLabelText('SSH host / alias')).toHaveValue('me@devbox.example.com')
    expect(screen.getByLabelText('Remote port')).toHaveValue('7781')
    expect(scrolled).toHaveBeenCalledTimes(1)
    expect(api.addInstance).not.toHaveBeenCalled()
    expect(api.connectInstance).not.toHaveBeenCalled()
    expect(api.patchConfig).not.toHaveBeenCalled()
    // The seeded values are held like typed ones, so they outlive a navigation.
    expect(store.getState().instances.crewForms.add).toMatchObject({ name: 'Dev box', sshHost: link.host, remotePort: '7781' })
    scrolled.mockRestore()
  })

  it('adds the crew only on Add, with the link values, and then drops the link', async () => {
    const u = userEvent.setup()
    vi.mocked(api.addInstance).mockResolvedValue({} as never)
    const store = createTestStore()
    store.dispatch(prefillCrewAddForm(link))
    renderWithProviders(<RemoteCrewPanel />, { store })

    await u.click(await screen.findByRole('button', { name: 'Add remote crew' }))
    await waitFor(() => expect(api.addInstance).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.addInstance).mock.calls[0][0]).toMatchObject({
      name: 'Dev box',
      connection_method: 'ssh',
      ssh_host: 'me@devbox.example.com',
      remote_port: 7781,
    })
    await waitFor(() => expect(screen.queryByText(NOTICE)).toBeNull())
    expect(store.getState().instances.crewForms.addLink).toBeNull()
    expect(screen.getByLabelText('Name')).toHaveValue('')
    expect(api.connectInstance).not.toHaveBeenCalled()
  })

  it('keeps a newer link and its line when an Add that was in flight finishes', async () => {
    // SettingsPage keys the panel by the link counter; this stands in for it.
    function KeyedPanel() {
      const seq = useAppSelector(s => s.instances.crewForms?.addLinkSeq ?? 0)
      return <RemoteCrewPanel key={seq} />
    }
    const u = userEvent.setup()
    let finishAdd: (value: unknown) => void = () => {}
    vi.mocked(api.addInstance).mockReturnValue(new Promise(resolve => { finishAdd = resolve }) as never)
    const store = createTestStore()
    store.dispatch(prefillCrewAddForm(link))
    renderWithProviders(<KeyedPanel />, { store })

    await u.click(await screen.findByRole('button', { name: 'Add remote crew' }))
    await waitFor(() => expect(api.addInstance).toHaveBeenCalledTimes(1))
    const newer = { host: 'other.example.com', port: '', name: 'Other' }
    act(() => { store.dispatch(prefillCrewAddForm(newer)) })
    expect(await screen.findByDisplayValue('other.example.com')).toBeInTheDocument()

    const listsBefore = vi.mocked(api.listInstances).mock.calls.length
    await act(async () => { finishAdd({}) })
    // The finished Add still reloads the crew list, which is how this test knows it ran.
    await waitFor(() => expect(vi.mocked(api.listInstances).mock.calls.length).toBeGreaterThan(listsBefore))
    expect(store.getState().instances.crewForms.addLink).toEqual(newer)
    expect(screen.getByText(NOTICE)).toBeInTheDocument()
    expect(screen.getByLabelText('SSH host / alias')).toHaveValue('other.example.com')
    expect(store.getState().instances.crewForms.add).toMatchObject({ sshHost: 'other.example.com', name: 'Other' })
  })

  it('keeps the form default for a port the link does not name', async () => {
    const store = createTestStore()
    store.dispatch(prefillCrewAddForm({ host: 'devbox', port: '', name: '' }))
    renderWithProviders(<RemoteCrewPanel />, { store })
    expect(await screen.findByText(NOTICE)).toBeInTheDocument()
    expect(screen.getByLabelText('SSH host / alias')).toHaveValue('devbox')
    expect(screen.getByLabelText('Remote port')).toHaveValue(EMPTY_INSTANCE_FORM.remotePort)
    expect(screen.getByLabelText('Name')).toHaveValue('')
  })

  it('restores what the user typed over the link on a later visit, without scrolling again', async () => {
    const scrolled = vi.spyOn(HTMLElement.prototype, 'scrollIntoView').mockImplementation(() => {})
    const store = createTestStore()
    store.dispatch(prefillCrewAddForm(link))
    store.dispatch(setCrewAddForm({ ...EMPTY_INSTANCE_FORM, name: 'Renamed', sshHost: link.host }))
    renderWithProviders(<RemoteCrewPanel />, { store })
    expect(await screen.findByText(NOTICE)).toBeInTheDocument()
    expect(screen.getByLabelText('Name')).toHaveValue('Renamed')
    expect(scrolled).not.toHaveBeenCalled()
    scrolled.mockRestore()
  })

  it('renders the add form unchanged when no link was opened', async () => {
    const scrolled = vi.spyOn(HTMLElement.prototype, 'scrollIntoView').mockImplementation(() => {})
    renderWithProviders(<RemoteCrewPanel />)
    expect(await screen.findByRole('button', { name: 'Add remote crew' })).toBeInTheDocument()
    expect(screen.queryByText(NOTICE)).toBeNull()
    expect(screen.getByLabelText('Name')).toHaveValue('')
    expect(screen.getByLabelText('SSH host / alias')).toHaveValue('')
    expect(screen.getByLabelText('Remote port')).toHaveValue(EMPTY_INSTANCE_FORM.remotePort)
    expect(scrolled).not.toHaveBeenCalled()
    scrolled.mockRestore()
  })
})
