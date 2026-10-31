import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'
import { copyToClipboard } from '../utils/clipboard'
import { consumeChatHandoff, __resetErrorJournalForTests } from '../utils/errorReport'
import { __resetInstanceFailuresForTests } from '../utils/instanceFailureReport'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn() }))

// PARTIAL: the panel also reads the chain-refusal store from this module through
// useSyncExternalStore, so replacing the whole module would break rendering. Only the
// announce is spied, which is the one decision under test.
vi.mock('../lib/chainAnnounce', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/chainAnnounce')>()),
  announceChainedCrew: vi.fn(),
}))

vi.mock('../api/client', () => {
  class ApiError extends Error {
    status: number
    // The real class carries the raw response body so a caller can read the
    // structured `code` the human message collapses away — the panel branches
    // on it to tell an unsupported-platform refusal from a load failure.
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.status = status
      this.body = body
    }
  }
  return {
    ApiError,
    // Mirrors the real predicate: the panel drops its refresh button only for an
    // auth denial, so the mock must distinguish one from any other ApiError.
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
    },
  }
})
import { api } from '../api/client'
import { registerCrewRowActions, type CrewRowActionProps } from '../components/remoteCrewRowActions'
import { DropdownMenuItem } from '../components/ui/dropdown-menu'

/** Open a crew row's overflow menu. */
async function openRowMenu(u: ReturnType<typeof userEvent.setup>, name: RegExp) {
  await u.click(await screen.findByRole('button', { name }))
}

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
  vi.mocked(copyToClipboard).mockResolvedValue(true)
  localStorage.clear()
  sessionStorage.clear()
  __resetErrorJournalForTests()
  __resetInstanceFailuresForTests()
  vi.mocked(api.cloudProvisioners).mockResolvedValue({ provisioners: [] })
})

describe('row actions a contributed provisioner draws on its crews', () => {
  // Registered once for the file: the registry is a module singleton.
  const startSpy = vi.fn()
  const DevSpaceItems = ({ inst, disabled }: CrewRowActionProps) => (
    <DropdownMenuItem disabled={disabled} onSelect={() => startSpy(inst.id)}>
      Start DevSpace
    </DropdownMenuItem>
  )
  const DevSpaceDetail = ({ inst }: CrewRowActionProps) => <p>DevSpace for {inst.name} is stopped</p>
  registerCrewRowActions({
    provisionerId: 'seam_panel_devspace',
    menuItems: DevSpaceItems,
    detail: DevSpaceDetail,
  })
  const Throws = () => {
    throw new Error('edition bug')
  }
  registerCrewRowActions({ provisionerId: 'seam_panel_broken', menuItems: Throws, detail: Throws })

  const DEVSPACE_CREW = {
    ...MANUAL_INSTANCE,
    id: 'ds1',
    name: 'research',
    ssh_host: 'ds-research-abcd1234',
    provisioner_id: 'seam_panel_devspace',
  }

  it('puts the contribution on the core row: detail under it, items in its menu', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [DEVSPACE_CREW] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    const detail = await screen.findByText('DevSpace for research is stopped')
    expect(detail.closest('[data-crew-id]')?.getAttribute('data-crew-id')).toBe('ds1')
    await openRowMenu(u, /More actions for research/i)
    await u.click(screen.getByRole('menuitem', { name: 'Start DevSpace' }))
    expect(startSpy).toHaveBeenCalledWith('ds1')
    // The core's own Remove still sits beside it.
    await openRowMenu(u, /More actions for research/i)
    expect(screen.getByRole('menuitem', { name: /Remove research/i })).toBeInTheDocument()
  })

  it('draws nothing for a chained row that carries the same stamp', async () => {
    const chained = { ...DEVSPACE_CREW, id: 'ds2', name: 'riding', via_instance_id: 'm1' }
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [MANUAL_INSTANCE, chained] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    await openRowMenu(u, /More actions for riding/i)
    expect(screen.queryByRole('menuitem', { name: 'Start DevSpace' })).not.toBeInTheDocument()
    expect(screen.queryByText(/DevSpace for riding/)).not.toBeInTheDocument()
  })

  it('a throwing contribution says so on the row and in the menu, and the row still works', async () => {
    const broken = { ...DEVSPACE_CREW, id: 'ds3', name: 'broken-crew', provisioner_id: 'seam_panel_broken' }
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [broken] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    const failed = /The extra controls for broken-crew failed to load: edition bug/
    const rowNotice = await screen.findByText(failed)
    expect(rowNotice.closest('[data-crew-id]')?.getAttribute('data-crew-id')).toBe('ds3')
    await openRowMenu(u, /More actions for broken-crew/i)
    const menu = screen.getByRole('menu')
    expect(within(menu).getByText(failed)).toBeInTheDocument()
    expect(within(menu).getByRole('menuitem', { name: /Remove broken-crew/i })).toBeInTheDocument()
    errors.mockRestore()
  })

  it('hands the crash to the agent with the boundary context, not just the sentence', async () => {
    const broken = { ...DEVSPACE_CREW, id: 'ds4', name: 'crash-crew', provisioner_id: 'seam_panel_broken' }
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [broken] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)

    const notice = (await screen.findByText(/The extra controls for crash-crew failed to load/)).closest('[role="alert"]') as HTMLElement
    await u.click(within(notice).getByRole('button', { name: /Ask the agent/i }))
    const prompt = consumeChatHandoff() || ''
    expect(prompt).toContain('crew-row-detail:seam_panel_broken')
    errors.mockRestore()
  })
})
