import { useEffect } from 'react'
import { Route, Routes, useLocation, useNavigate, type NavigateFunction } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { api, ApiError, type LaunchJob } from '../api/client'
import { registerRemoteProvisionerRenderer, type RemoteProvisionerFormProps } from '../components/remoteProvisionerRenderers'
import { Btn } from '../components/ui'
import { LocalOverlayRoot } from '../components/overlayRoot'
import RemoteCrewsWindow, { openRemoteCrewsWindow } from '../components/remoteCrewsWindow'
import { prefillCrewAddForm, relayPaneOpened, setActiveId } from '../store/instancesSlice'
import { useAppSelector } from '../store'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'
import SidePanelLayout from '../components/SidePanelLayout'
import { NavigationLeaveGuardProvider, useRegisterNavigationLeaveGuard } from '../components/NavigationLeaveGuard'
import { REMOTE_CREW_SETTINGS_PATH } from '../lib/remoteCrewAddLink'
import { consumeChatHandoff, installSoftNavigate, sendErrorToChat } from '../utils/errorReport'
import { createTestStore, renderWithProviders } from './helpers'

// RemoteCrewsWindow -> Suspense -> lazy RemoteCrewPanel must load before these waits settle.
const LAZY_PANEL = { timeout: 5000 }
const PROVISIONER = {
  id: 'window-test-provider', kind: 'window_test_provider', label: 'Test provider',
  posix_only: false, steps: [{ key: 'provision', label: 'Creating test crew' }],
}
const RUNNING_JOB: LaunchJob = {
  id: 'window-test-launch', tag: 'test-crew', provider_id: PROVISIONER.id,
  profile: '', region: 'us-east-1', size_key: 'small', status: 'running',
  signin: null, created_at: 0, updated_at: 0,
  steps: [{ key: 'provision', label: 'Creating test crew', state: 'active' }],
}
function ProvisionerForm({ launch, launching, disabled, activeJob }: RemoteProvisionerFormProps) {
  return <>
    <Btn disabled={disabled || launching} onClick={() => launch({ size_key: 'small' })}>Launch test crew</Btn>
    {activeJob && <span>Launch accepted</span>}
  </>
}
registerRemoteProvisionerRenderer({ kind: PROVISIONER.kind, component: ProvisionerForm })

let store: ReturnType<typeof createTestStore>
let navigate: NavigateFunction
const mayLeave = vi.fn(() => true)

function Routing() {
  useRegisterNavigationLeaveGuard(mayLeave)
  const navigateTo = useNavigate()
  navigate = navigateTo
  const { pathname } = useLocation()
  useEffect(() => {
    installSoftNavigate(navigateTo)
    return () => installSoftNavigate(null)
  }, [navigateTo])
  return <>
    <output aria-label="Current route">{pathname}</output>
    <Routes>
      <Route path="/settings/*" element={
        <SidePanelLayout title="Settings" basePath="/settings" rememberKey="settings"
          tabs={[{ key: 'overview', label: 'Overview' }, { key: 'instances', label: 'Remote Crew' }]}
        >
          {tab => tab === 'instances' ? <RemoteCrewPanel /> : <span>Overview settings</span>}
        </SidePanelLayout>
      } />
      <Route path="*" element={null} />
    </Routes>
  </>
}

beforeEach(() => {
  mayLeave.mockReset().mockReturnValue(true)
  localStorage.clear()
  sessionStorage.clear()
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(new Date('2026-11-24T00:00:00Z'))
  vi.spyOn(api, 'listInstances').mockResolvedValue({ active: true, instances: [] })
  vi.spyOn(api, 'cloudLaunches').mockResolvedValue({ jobs: [] })
  vi.spyOn(api, 'cloudIdentity').mockResolvedValue({
    identity: { account_type: 'BuilderId' },
    suggested_target: { license: '', start_url: '', region: '' },
    discovery: 'read',
  })
  vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({})
  vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(
    new ApiError(404, 'off', JSON.stringify({ code: 'RELAY_VIEWER_OFF' })),
  )
  store = createTestStore()
})

afterEach(() => {
  // Selecting another crew uses the window's real close path, resetting its store.
  act(() => { store.dispatch(setActiveId('cleanup-crew')) })
  cleanup()
  vi.restoreAllMocks()
  vi.clearAllTimers()
  vi.useRealTimers()
})

function LocalShell() {
  const hidden = useAppSelector(s => s.instances.activeId !== null || !!s.instances.relayActive)
  return <LocalOverlayRoot hidden={hidden}>
    <div data-testid="local-page" hidden={hidden}><Routing /></div>
    <RemoteCrewsWindow />
  </LocalOverlayRoot>
}

async function mountWindow(remoteActive = false, add = false, route = '/') {
  if (remoteActive) store.dispatch(setActiveId('remote-crew'))
  renderWithProviders(
    <NavigationLeaveGuardProvider><LocalShell /></NavigationLeaveGuardProvider>,
    { store, route },
  )
  await act(async () => { openRemoteCrewsWindow(add) })
}

async function visibleDialog(name: string) {
  const dialog = await screen.findByRole('dialog', { name }, LAZY_PANEL)
  await waitFor(() => expect(dialog).toBeVisible(), LAZY_PANEL)
  return dialog
}

describe('Remote Crews window', () => {
  describe.each(['ssh', 'relay'])('hidden Local Settings behind %s', crew => {
    it.each([false, true])('brings Local forward and uses its panel (add=%s)', async add => {
      if (crew === 'relay') store.dispatch(relayPaneOpened({ id: 'relay-crew', name: 'Relay crew' }))
      mayLeave.mockImplementation(() => {
        expect(store.getState().instances.activeId).toBeNull()
        expect(store.getState().instances.relayActive).toBeNull()
        return true
      })
      await mountWindow(crew === 'ssh', add, '/settings/overview')
      const addButton = await screen.findByRole('button', { name: 'Add Crew', hidden: true }, LAZY_PANEL)
      expect(document.querySelector('[data-local-overlay-root]')).not.toHaveStyle({ display: 'none' })
      expect(screen.getByLabelText('Current route')).toHaveTextContent(REMOTE_CREW_SETTINGS_PATH)
      expect(store.getState().instances.activeId).toBeNull()
      expect(store.getState().instances.relayActive).toBeNull()
      expect(mayLeave).toHaveBeenCalledTimes(1)
      if (add) await visibleDialog('Add a Crew')
      else expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
      expect(screen.queryByRole('dialog', { name: 'Remote Crews', hidden: true })).not.toBeInTheDocument()
      expect(screen.getAllByRole('button', { name: 'Add Crew', hidden: true })).toEqual([addButton])
      expect(screen.getByTestId('local-page')).toContainElement(addButton)
    })

    it.each([false, true])('keeps the mounted panel and its draft when requested again (add=%s)', async add => {
      await mountWindow(false, true, REMOTE_CREW_SETTINGS_PATH)
      const addButton = await screen.findByRole('button', { name: 'Add Crew', hidden: true }, LAZY_PANEL)
      const dialog = await visibleDialog('Add a Crew')
      fireEvent.click(within(dialog).getByRole('button', { name: /Connect a machine you have/ }))
      const connect = await visibleDialog('Connect a machine you have')
      const name = within(connect).getByRole('textbox', { name: 'Name' })
      fireEvent.change(name, { target: { value: 'settings draft' } })
      await act(async () => {
        store.dispatch(crew === 'ssh'
          ? setActiveId('remote-crew')
          : relayPaneOpened({ id: 'relay-crew', name: 'Relay crew' }))
      })
      expect(document.querySelector('[data-local-overlay-root]')).toHaveStyle({ display: 'none' })
      await act(async () => { openRemoteCrewsWindow(add) })
      expect(store.getState().instances.activeId).toBeNull()
      expect(store.getState().instances.relayActive).toBeNull()
      expect(connect).toBeVisible()
      expect(name).toHaveValue('settings draft')
      expect(screen.getAllByRole('button', { name: 'Add Crew', hidden: true })).toEqual([addButton])
      expect(screen.queryByRole('dialog', { name: 'Remote Crews', hidden: true })).not.toBeInTheDocument()
      expect(mayLeave).not.toHaveBeenCalled()
    })
  })

  it('closes when the already active crew is selected again', async () => {
    await mountWindow(true)
    const window = await visibleDialog('Remote Crews')
    const pickSeq = store.getState().instances.pickSeq ?? 0
    await act(async () => { store.dispatch(setActiveId('remote-crew')) })
    expect(store.getState().instances.activeId).toBe('remote-crew')
    expect(store.getState().instances.pickSeq).toBe(pickSeq + 1)
    await waitFor(() => expect(window).not.toBeInTheDocument(), LAZY_PANEL)
    await act(async () => { openRemoteCrewsWindow() })
    await visibleDialog('Remote Crews')
  })

  it.each([false, true])('uses the Remote Crew settings page from another tab (add=%s)', async add => {
    await mountWindow(false, add, '/settings/overview')
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Add Crew', hidden: true })).toHaveLength(1), LAZY_PANEL)
    expect(screen.getByLabelText('Current route')).toHaveTextContent(REMOTE_CREW_SETTINGS_PATH)
    expect(screen.queryByText('Overview settings')).not.toBeInTheDocument()
    if (add) await visibleDialog('Add a Crew')
    else expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: 'Remote Crews' })).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Add Crew', hidden: true })).toHaveLength(1)
    expect(mayLeave).toHaveBeenCalledTimes(1)
    // A request delivered after navigation must not replay on a later mount.
    await act(async () => { navigate('/settings/overview') })
    await act(async () => { navigate(REMOTE_CREW_SETTINGS_PATH) })
    await screen.findByRole('button', { name: 'Add Crew' }, LAZY_PANEL)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it.each([false, true])('retires a request vetoed by the Settings leave guard (add=%s)', async add => {
    mayLeave.mockReturnValue(false)
    await mountWindow(false, add, '/settings/overview')
    expect(screen.getByText('Overview settings')).toBeVisible()
    expect(screen.getByLabelText('Current route')).toHaveTextContent('/settings/overview')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add Crew' })).not.toBeInTheDocument()
    expect(mayLeave).toHaveBeenCalledTimes(1)
    await act(async () => { navigate('/chat') })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(mayLeave).toHaveBeenCalledTimes(1)
  })

  it('does not redirect a navigation to another Settings tab while the window is open', async () => {
    await mountWindow()
    await visibleDialog('Remote Crews')
    await act(async () => { navigate('/settings/overview') })
    expect(screen.getByText('Overview settings')).toBeVisible()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(mayLeave).not.toHaveBeenCalled()
  })

  it.each([false, true])('keeps the current Remote Crew page and does not ask to leave (add=%s)', async add => {
    mayLeave.mockReturnValue(false)
    await mountWindow(false, add, REMOTE_CREW_SETTINGS_PATH)
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Add Crew', hidden: true })).toHaveLength(1), LAZY_PANEL)
    expect(screen.getByLabelText('Current route')).toHaveTextContent(REMOTE_CREW_SETTINGS_PATH)
    if (add) await visibleDialog('Add a Crew')
    else expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: 'Remote Crews' })).not.toBeInTheDocument()
    expect(mayLeave).not.toHaveBeenCalled()
  })

  it.each([
    REMOTE_CREW_SETTINGS_PATH,
    `${REMOTE_CREW_SETTINGS_PATH}/?highlight=crew`,
    `${REMOTE_CREW_SETTINGS_PATH}/advanced`,
    '/settings/%69nstances',
    '/settings?tab=instances',
    '/settings',
  ])('opens Add on the existing settings panel without remounting at %s', async route => {
    sessionStorage.setItem('kirocrew:sidepanel-tab:settings', 'instances')
    await mountWindow(false, false, route)
    const add = await screen.findByRole('button', { name: 'Add Crew' }, LAZY_PANEL)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await act(async () => { openRemoteCrewsWindow(true) })
    const dialog = await visibleDialog('Add a Crew')
    expect(screen.queryByRole('dialog', { name: 'Remote Crews' })).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Add Crew', hidden: true })).toEqual([add])
    // A repeated Add request must leave an already-open form and its draft alone.
    fireEvent.click(within(dialog).getByRole('button', { name: /Connect a machine you have/ }))
    const connect = await visibleDialog('Connect a machine you have')
    const name = within(connect).getByRole('textbox', { name: 'Name' })
    fireEvent.change(name, { target: { value: 'settings draft' } })
    await act(async () => { openRemoteCrewsWindow(true) })
    expect(connect).toBeVisible()
    expect(name).toHaveValue('settings draft')
    expect(screen.getAllByRole('button', { name: 'Add Crew', hidden: true })).toEqual([add])
    await act(async () => { navigate('/chat') })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it.each([false, true])('retires an open window and its Add dialog on arrival at Settings (add-only=%s)', async addOnly => {
    await mountWindow(false, addOnly)
    if (!addOnly) {
      const window = await visibleDialog('Remote Crews')
      fireEvent.click(await within(window).findByRole('button', { name: 'Add Crew' }, LAZY_PANEL))
    }
    await visibleDialog('Add a Crew')
    await act(async () => { navigate(REMOTE_CREW_SETTINGS_PATH) })
    await screen.findByRole('button', { name: 'Add Crew' }, LAZY_PANEL)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Add Crew', hidden: true })).toHaveLength(1)
    await act(async () => { navigate('/chat') })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    // The retired Add state must not prevent a subsequent normal window closing.
    await act(async () => { openRemoteCrewsWindow() })
    const window = await visibleDialog('Remote Crews')
    fireEvent.click(within(window).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(window).not.toBeInTheDocument(), LAZY_PANEL)
  })

  it.each(['ssh', 'relay', 'local'])('closes nested Add and brings Local forward on a successful hand-off from %s', async crew => {
    if (crew === 'relay') store.dispatch(relayPaneOpened({ id: 'relay-crew', name: 'Relay crew' }))
    await mountWindow(crew === 'ssh', false, '/chat')
    const window = await visibleDialog('Remote Crews')
    fireEvent.click(await within(window).findByRole('button', { name: 'Add Crew' }, LAZY_PANEL))
    await visibleDialog('Add a Crew')
    await act(async () => { expect(sendErrorToChat('Window error')).toBe(true) })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(store.getState().instances.activeId).toBeNull()
    expect(store.getState().instances.relayActive).toBeNull()
    expect(screen.getByLabelText('Current route')).toHaveTextContent('/chat')
    expect(consumeChatHandoff()).toBe('Window error')
  })

  it('closes Add-only on hand-off and navigates to chat', async () => {
    await mountWindow(true, true)
    await visibleDialog('Add a Crew')
    await act(async () => { expect(sendErrorToChat('Add error')).toBe(true) })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(store.getState().instances.activeId).toBeNull()
    expect(screen.getByLabelText('Current route')).toHaveTextContent('/chat')
    expect(consumeChatHandoff()).toBe('Add error')
  })

  it.each(['storage', 'leave guard'])('keeps the window and remote selection when a hand-off fails at the %s', async failure => {
    await mountWindow(true)
    const window = await visibleDialog('Remote Crews')
    if (failure === 'storage') {
      vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('Storage full') })
    } else {
      installSoftNavigate(navigate, () => false)
    }
    await act(async () => { expect(sendErrorToChat('Unsent error')).toBe(false) })
    expect(window).toBeVisible()
    expect(store.getState().instances.activeId).toBe('remote-crew')
    expect(screen.getByLabelText('Current route')).toHaveTextContent(/^\/$/)
    expect(consumeChatHandoff()).toBeNull()
  })

  it('hands a panel error to local chat through its Ask the agent button', async () => {
    vi.mocked(api.listInstances).mockRejectedValue(
      new ApiError(403, 'off', JSON.stringify({ code: 'instances_disabled' })),
    )
    vi.spyOn(api, 'patchConfig').mockRejectedValue(new Error('Window enable failed'))
    await mountWindow(true)
    const window = await visibleDialog('Remote Crews')
    fireEvent.click(await within(window).findByRole('button', { name: 'Enable remote crew management' }, LAZY_PANEL))
    fireEvent.click(await within(window).findByRole('button', { name: /Ask the agent/i }, LAZY_PANEL))
    await waitFor(() => expect(window).not.toBeInTheDocument(), LAZY_PANEL)
    expect(store.getState().instances.activeId).toBeNull()
    expect(screen.getByLabelText('Current route')).toHaveTextContent('/chat')
    expect(consumeChatHandoff()).toContain('Window enable failed')
  })

  it('does not change the crew for another surface after the window closes', async () => {
    await mountWindow(true)
    const window = await visibleDialog('Remote Crews')
    fireEvent.click(within(window).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(window).not.toBeInTheDocument(), LAZY_PANEL)
    await act(async () => { expect(sendErrorToChat('Elsewhere')).toBe(true) })
    expect(store.getState().instances.activeId).toBe('remote-crew')
  })

  it.each([true, false])('shows progress after a registered launch only when accepted=%s', async accepted => {
    vi.spyOn(api, 'cloudProvisioners').mockResolvedValue({ provisioners: [PROVISIONER] })
    vi.spyOn(api, 'cloudLaunchStatus').mockResolvedValue(RUNNING_JOB)
    vi.spyOn(api, 'cloudLaunch').mockImplementation(async () => {
      if (!accepted) throw new Error('Test launch refused')
      vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [RUNNING_JOB] })
      return RUNNING_JOB
    })
    await mountWindow(false, true)
    const add = await visibleDialog('Add a Crew')
    fireEvent.click(within(add).getByRole('button', { name: /Set up a new Crew/ }))
    const setup = await visibleDialog('Set up a new Crew')
    fireEvent.click(await within(setup).findByRole('button', { name: 'Launch test crew' }, LAZY_PANEL))
    await within(setup).findByText(accepted ? 'Launch accepted' : 'Test launch refused', {}, LAZY_PANEL)
    fireEvent.click(within(setup).getByRole('button', { name: 'Close' }))
    if (accepted) {
      const window = await visibleDialog('Remote Crews')
      fireEvent.click(await within(window).findByRole('button', { name: /Setting up/ }, LAZY_PANEL))
      expect(await within(window).findByText('Creating test crew', {}, LAZY_PANEL)).toBeVisible()
    } else {
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument(), LAZY_PANEL)
    }
  })

  it('shows the management gate when Add cannot open', async () => {
    vi.mocked(api.listInstances).mockRejectedValue(
      new ApiError(403, 'off', JSON.stringify({ code: 'instances_disabled' })),
    )
    await mountWindow(false, true)
    const window = await visibleDialog('Remote Crews')
    expect(await within(window).findByRole('button', { name: 'Enable remote crew management' }, LAZY_PANEL)).toBeVisible()
    expect(screen.queryByRole('dialog', { name: 'Add a Crew' })).not.toBeInTheDocument()
    fireEvent.click(within(window).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(window).not.toBeInTheDocument(), LAZY_PANEL)
  })

  it('keeps a Settings add link waiting while management is off, then opens it with the link host', async () => {
    vi.mocked(api.listInstances).mockRejectedValue(
      new ApiError(403, 'off', JSON.stringify({ code: 'instances_disabled' })),
    )
    vi.spyOn(api, 'patchConfig').mockImplementation(async () => {
      vi.mocked(api.listInstances).mockResolvedValue({ active: true, instances: [] })
      return {} as never
    })
    store.dispatch(prefillCrewAddForm({ host: 'link-devbox', port: '', name: '' }))
    renderWithProviders(<Routing />, { store, route: REMOTE_CREW_SETTINGS_PATH })
    fireEvent.click(await screen.findByRole('button', { name: 'Enable remote crew management' }, LAZY_PANEL))
    const add = await visibleDialog('Connect a machine you have')
    expect(await within(add).findByDisplayValue('link-devbox', {}, LAZY_PANEL)).toBeVisible()
  })

  it.each([true, false])('keeps a pending launch mounted after Close until accepted=%s', async accepted => {
    vi.spyOn(api, 'cloudProvisioners').mockResolvedValue({ provisioners: [PROVISIONER] })
    vi.spyOn(api, 'cloudLaunchStatus').mockResolvedValue(RUNNING_JOB)
    let resolveLaunch!: (job: LaunchJob) => void
    let rejectLaunch!: (error: Error) => void
    vi.spyOn(api, 'cloudLaunch').mockReturnValue(new Promise<LaunchJob>((resolve, reject) => {
      resolveLaunch = resolve
      rejectLaunch = reject
    }))
    await mountWindow(false, true)
    const add = await visibleDialog('Add a Crew')
    fireEvent.click(within(add).getByRole('button', { name: /Set up a new Crew/ }))
    const setup = await visibleDialog('Set up a new Crew')
    const launch = await within(setup).findByRole('button', { name: 'Launch test crew' }, LAZY_PANEL)
    fireEvent.click(launch)
    await waitFor(() => expect(api.cloudLaunch).toHaveBeenCalledTimes(1), LAZY_PANEL)
    await waitFor(() => expect(launch).toBeDisabled(), LAZY_PANEL)
    fireEvent.click(within(setup).getByRole('button', { name: 'Close' }))
    const window = await visibleDialog('Remote Crews')
    const shortcut = vi.fn()
    document.addEventListener('keydown', shortcut)
    try {
      fireEvent.keyDown(await within(window).findByRole('button', { name: 'Add Crew' }, LAZY_PANEL), { key: ',', ctrlKey: true })
      expect(shortcut).not.toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', shortcut)
    }
    await waitFor(() => expect(setup).not.toBeInTheDocument(), LAZY_PANEL)
    await act(async () => {
      if (accepted) {
        vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [RUNNING_JOB] })
        resolveLaunch(RUNNING_JOB)
      } else {
        rejectLaunch(new Error('Delayed launch refused'))
      }
    })
    if (accepted) {
      fireEvent.click(await within(window).findByRole('button', { name: /Setting up/ }, LAZY_PANEL))
      expect(await within(window).findByText('Creating test crew', {}, LAZY_PANEL)).toBeVisible()
    } else {
      expect(await within(window).findByText('Delayed launch refused', {}, LAZY_PANEL)).toBeVisible()
    }
    expect(api.cloudLaunch).toHaveBeenCalledTimes(1)
  })

  it('closes Add-only without showing the window when cancelled', async () => {
    await mountWindow(false, true)
    const add = await visibleDialog('Add a Crew')
    fireEvent.click(within(add).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument(), LAZY_PANEL)
  })

  it('shows the window and nested Add dialog while the local overlay root is hidden', async () => {
    await mountWindow(true)
    expect(document.querySelector('[data-local-overlay-root]')).toHaveStyle({ display: 'none' })
    const window = await visibleDialog('Remote Crews')
    expect(window.closest('[data-local-overlay-root]')).toBeNull()
    const addButton = await within(window).findByRole('button', { name: 'Add Crew' }, LAZY_PANEL)
    await act(async () => { fireEvent.click(addButton) })
    const add = await visibleDialog('Add a Crew')
    expect(add.closest('[data-local-overlay-root]')).toBeNull()
  })

  it('shows Add-only outside both the hidden panel and the hidden local overlay root', async () => {
    await mountWindow(true, true)
    const add = await visibleDialog('Add a Crew')
    expect(add.closest('[hidden], [data-local-overlay-root]')).toBeNull()
    expect(screen.queryByRole('dialog', { name: 'Remote Crews' })).not.toBeInTheDocument()
    fireEvent.click(within(add).getByRole('button', { name: /Connect a machine you have/ }))
    await visibleDialog('Connect a machine you have')
  })

  it('keeps the window and connect draft when Escape reaches both modals', async () => {
    await mountWindow()
    const window = await visibleDialog('Remote Crews')
    const addButton = await within(window).findByRole('button', { name: 'Add Crew' }, LAZY_PANEL)
    await act(async () => { fireEvent.click(addButton) })
    const add = await visibleDialog('Add a Crew')
    fireEvent.click(within(add).getByRole('button', { name: /Connect a machine you have/ }))
    const connect = await visibleDialog('Connect a machine you have')
    const name = within(connect).getByRole('textbox', { name: 'Name' })
    fireEvent.change(name, { target: { value: 'unsaved crew' } })
    fireEvent.keyDown(name, { key: 'Escape' })
    expect(window).toBeInTheDocument()
    expect(connect).toBeVisible()
    expect(name).toHaveValue('unsaved crew')
    // Explicit dismissal of Add still works, and restores the outer window's dismissal.
    fireEvent.click(within(connect).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(connect).not.toBeInTheDocument(), LAZY_PANEL)
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(window).not.toBeInTheDocument(), LAZY_PANEL)
  })
})
