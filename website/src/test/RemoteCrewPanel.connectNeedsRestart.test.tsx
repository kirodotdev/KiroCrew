import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createTestStore, renderWithProviders } from './helpers'
import { setUpdateProgress } from '../store/dashboardSlice'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'
import { RemoteCrewRestartBanner } from '../pages/settings/RemoteCrewPanelRestartBanner'
import { EMPTY_INSTANCE_FORM } from '../pages/settings/InstanceFormFields'
import type { RootState } from '../store'
import { i18nT } from '../i18n/t'

// The panel reads its instance list from this client. When management is on but
// the gateway has NOT been restarted yet, the list reports `active: false`: the
// "not active yet" banner shows and connecting cannot work until the restart.
// The Connect button must say so, not look fully clickable (issue #23692).
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
    isAuthExpiredError: (e: unknown) =>
      e instanceof ApiError && (e as { authRequired?: boolean }).authRequired === true,
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
      restartGateway: vi.fn(),
    },
  }
})
import { api, ApiError } from '../api/client'

// One hand-added, disconnected crew — the cheapest row that shows a Connect
// button. Its connect state is the only thing under test here.
const MANUAL_INSTANCE = {
  id: 'm1',
  name: 'dev-box-1',
  connection_method: 'ssh' as const,
  ssm_target: '',
  ssh_host: 'dev-box-1',
  aws_profile: '',
  aws_region: '',
  ssm_run_as: '',
  remote_port: 5476,
  local_port: 0,
  ttl: '20h',
  remote_bin: '',
  was_connected: false,
  status: { instance_id: 'm1', state: 'disconnected' as const },
}

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
})

describe('RemoteCrewPanel — Connect while the gateway needs a restart', () => {
  it('disables Connect and says why when management is on but not active yet', async () => {
    // active:false is exactly the state that shows the "not active yet" banner.
    vi.mocked(api.listInstances).mockResolvedValue({ active: false, warm_set_cap: 5, instances: [MANUAL_INSTANCE] })
    renderWithProviders(<RemoteCrewPanel />)

    expect(await screen.findByText('dev-box-1')).toBeInTheDocument()
    const connect = screen.getByRole('button', { name: /^Connect$/i })
    // The two signals must agree: the banner says "not yet", so the button must
    // not look fully clickable. A reason is attached so the muting is explained.
    expect(connect).toBeDisabled()
    expect(connect).toHaveAttribute('title', expect.stringMatching(/restart/i))
    // A `title` on a disabled button is not keyboard- or screen-reader-reachable,
    // so the button also points at the restart banner via aria-describedby — and
    // that banner must actually be present on the page for the reference to
    // resolve to something a screen reader can read.
    const bannerId = connect.getAttribute('aria-describedby')
    expect(bannerId).toBeTruthy()
    expect(document.getElementById(bannerId as string)).toHaveTextContent(/restart/i)
  })

  it('leaves Connect enabled once the gateway is active', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [MANUAL_INSTANCE] })
    renderWithProviders(<RemoteCrewPanel />)

    expect(await screen.findByText('dev-box-1')).toBeInTheDocument()
    const connect = screen.getByRole('button', { name: /^Connect$/i })
    expect(connect).toBeEnabled()
    // No banner to describe once active, so the reference is dropped rather than
    // left dangling at an element that is no longer on the page.
    expect(connect).not.toHaveAttribute('aria-describedby')
  })
})

// Turning management on asks for a gateway restart (issue #32097). The banner
// offers it in place, and clears by itself once the restarted gateway is back.
describe('RemoteCrewPanel — the not-active-yet banner', () => {
  it('restarts the gateway from the banner, behind the shared two-step confirm', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: false, warm_set_cap: 5, instances: [MANUAL_INSTANCE] })
    vi.mocked(api.restartGateway).mockResolvedValue({ ok: true, status: 'restarting' })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    const restart = await screen.findByTestId('remote-crew-restart-gateway')
    await u.click(restart)
    expect(api.restartGateway).not.toHaveBeenCalled()
    await u.click(restart)
    await waitFor(() => expect(restart).toHaveTextContent(/Restarting/))
    expect(api.restartGateway).toHaveBeenCalledTimes(1)
    expect(restart).toBeDisabled()
  })

  it('shows a refused restart and leaves the button ready for another press', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: false, warm_set_cap: 5, instances: [MANUAL_INSTANCE] })
    vi.mocked(api.restartGateway).mockRejectedValue(new ApiError(409, 'The restart launcher cannot run here.'))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    const restart = await screen.findByTestId('remote-crew-restart-gateway')
    await u.click(restart)
    await u.click(restart)
    expect(await screen.findByTestId('remote-crew-restart-failed')).toHaveTextContent('The restart launcher cannot run here.')
    expect(restart).toBeEnabled()
    expect(restart).not.toHaveTextContent(/Restarting/)
  })

  it('shows a restart the gateway refused after its 200, and leaves the button ready (#35351)', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: false, warm_set_cap: 5, instances: [MANUAL_INSTANCE] })
    vi.mocked(api.restartGateway).mockResolvedValue({ ok: true, status: 'restarting' })
    const u = userEvent.setup()
    const { store } = renderWithProviders(<RemoteCrewPanel />)

    const restart = await screen.findByTestId('remote-crew-restart-gateway')
    await u.click(restart)
    await u.click(restart)
    await waitFor(() => expect(restart).toHaveTextContent(/Restarting/))
    act(() => { store.dispatch(setUpdateProgress({ step: 'restarting', detail: 'Restarting server…' })) })
    act(() => { store.dispatch(setUpdateProgress({ step: 'error', detail: 'The gateway entry point is gone.' })) })
    expect(await screen.findByTestId('remote-crew-restart-failed')).toHaveTextContent('The gateway entry point is gone.')
    expect(restart).toBeEnabled()
    expect(restart).not.toHaveTextContent(/Restarting/)
  })

  it('shows a restart request that never reached the gateway, and leaves the button ready', async () => {
    // The gateway answers before it restarts, so a fetch that rejects means the
    // restart was never accepted, not that it is under way.
    vi.mocked(api.listInstances).mockResolvedValue({ active: false, warm_set_cap: 5, instances: [MANUAL_INSTANCE] })
    vi.mocked(api.restartGateway).mockRejectedValue(new TypeError('Failed to fetch'))
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    const restart = await screen.findByTestId('remote-crew-restart-gateway')
    await u.click(restart)
    await u.click(restart)
    expect(await screen.findByTestId('remote-crew-restart-failed')).toHaveTextContent('Restart failed')
    expect(restart).toBeEnabled()
    expect(restart).not.toHaveTextContent(/Restarting/)
  })

  it('clears once the socket reconnects to a gateway that is active, with no Refresh', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: false, warm_set_cap: 5, instances: [MANUAL_INSTANCE] })
    const { queryClient } = renderWithProviders(<RemoteCrewPanel />)
    expect(await screen.findByTestId('remote-crew-restart-gateway')).toBeInTheDocument()

    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [MANUAL_INSTANCE] })
    // The reconnect's re-read of the crew list, which useWebSocket.frameTrace.test.ts
    // pins on both reconnect paths.
    void queryClient.invalidateQueries({ queryKey: ['instances'], exact: true })

    await waitFor(() => expect(screen.queryByTestId('remote-crew-restart-gateway')).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: /^Connect$/i })).toBeEnabled()
  })
})

// The banner's restart reloads the page, and the crew-form drafts live only in
// memory, so the armed step says the unsaved crew form will be cleared.
type CrewFormState = RootState['instances']['crewForms']

describe('RemoteCrewPanel — the banner Restart and an unsaved crew form', () => {
  const NOTE = () => i18nT('pages.settings.remoteCrewPanel.restart_clears_draft')

  function storeWith(crewForms: CrewFormState) {
    return createTestStore({
      instances: { warm: {}, activeId: null, mru: [], unread: {}, ready: {}, pendingPaneSlot: null, host: null, crewForms },
    })
  }

  async function armRestart(crewForms: CrewFormState) {
    vi.mocked(api.listInstances).mockResolvedValue({ active: false, warm_set_cap: 5, instances: [MANUAL_INSTANCE] })
    vi.mocked(api.restartGateway).mockResolvedValue({ ok: true, status: 'restarting' })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />, { store: storeWith(crewForms) })
    const restart = await screen.findByTestId('remote-crew-restart-gateway')
    expect(screen.queryByText(NOTE())).toBeNull()
    await u.click(restart)
    return { restart, u }
  }

  const DRAFTS: Array<[string, CrewFormState]> = [
    ['an add-form draft', { add: { ...EMPTY_INSTANCE_FORM, name: 'half-typed-crew' }, edit: null }],
    ['an edit-form draft', {
      add: null,
      edit: {
        id: 'm1',
        draft: { values: { ...EMPTY_INSTANCE_FORM, name: 'dev-box-renamed', sshHost: 'dev-box-1' }, baseline: MANUAL_INSTANCE },
        seq: 0,
      },
    }],
    ['a launch-form draft', { add: null, edit: null, launch: { subnet_id: 'subnet-0abc', identity: null } }],
  ]

  it.each(DRAFTS)('says the form will be cleared when the armed restart would drop %s', async (_label, crewForms) => {
    const { restart } = await armRestart(crewForms)

    expect(restart).toHaveTextContent(i18nT('pages.settings.aboutPanel.restart_confirm'))
    expect(api.restartGateway).not.toHaveBeenCalled()
    const noteId = restart.getAttribute('aria-describedby')
    expect(noteId).toBeTruthy()
    expect(document.getElementById(noteId as string)).toHaveTextContent(NOTE())
    // The visible text stays the accessible name; the note is only a description.
    expect(restart).not.toHaveAttribute('aria-label')
  })

  it('shows no note when no crew form holds a draft', async () => {
    const { restart } = await armRestart({ add: null, edit: null })
    expect(restart).toHaveTextContent(i18nT('pages.settings.aboutPanel.restart_confirm'))
    expect(restart).not.toHaveAttribute('aria-describedby')
    expect(screen.queryByText(NOTE())).toBeNull()
  })

  it('does not count an add link on its own as a draft', async () => {
    // Rendered alone: a mounted add form copies the link's values into `add`,
    // and this case is about the link with nothing typed from it.
    const u = userEvent.setup()
    const link = { host: 'link-host', port: '', name: 'from-link' }
    renderWithProviders(<RemoteCrewRestartBanner />, { store: storeWith({ add: null, edit: null, launch: null, addLink: link }) })
    const restart = screen.getByTestId('remote-crew-restart-gateway')
    await u.click(restart)
    expect(restart).toHaveTextContent(i18nT('pages.settings.aboutPanel.restart_confirm'))
    expect(restart).not.toHaveAttribute('aria-describedby')
    expect(screen.queryByText(NOTE())).toBeNull()
  })

  it('keeps the two-step confirm with a draft: the second click restarts and the note goes', async () => {
    const { restart, u } = await armRestart(DRAFTS[0][1])
    expect(screen.getByText(NOTE())).toBeInTheDocument()
    expect(api.restartGateway).not.toHaveBeenCalled()

    await u.click(restart)
    await waitFor(() => expect(restart).toHaveTextContent(/Restarting/))
    expect(api.restartGateway).toHaveBeenCalledTimes(1)
    expect(screen.queryByText(NOTE())).toBeNull()
    expect(restart).not.toHaveAttribute('aria-describedby')
  })
})
