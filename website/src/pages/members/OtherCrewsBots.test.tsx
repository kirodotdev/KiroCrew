// The Bots page's section for the bots on the user's other crews (#39808): one
// group per remote crew the switcher has a tab for, under the crew's name; a
// notice for a crew that could not be read or whose tunnel failed, and its tab's
// state for one otherwise not connected; a count of the bots past the row cap;
// the page's search box narrows the rows; a click opens that crew's window; the
// local read failing is one notice for the section.
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { createTestStore, renderWithProviders } from '../../test/helpers'
import OtherCrewsBots from './OtherCrewsBots'
import { api, type InstanceView } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { namedCeiling } from '../../test/namedCeiling'

vi.mock('../../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../../api/client')>()
  return {
    ...mod,
    api: { ...mod.api, listInstances: vi.fn(), instancesBots: vi.fn() },
  }
})

const listInstances = vi.mocked(api.listInstances)
const instancesBots = vi.mocked(api.instancesBots)

// The section sits behind a real chain: the ['instances'] read resolves, the
// section finds a crew tab, and only then does the instancesBots read start and
// answer. On a loaded worker that is more than Testing Library's default second,
// so each wait on it carries this ceiling.
const BOTS_CHAIN = () => namedCeiling('OTHER_CREWS_BOTS_READY', 5000)

function crew(id: string, name: string, state: InstanceView['status']['state']): InstanceView {
  return {
    id, name, ssh_host: id, remote_port: 0, local_port: 0, ttl: '', remote_bin: '',
    connection_method: 'ssh', ssm_target: '', aws_profile: '', aws_region: '', ssm_run_as: '',
    was_connected: true, status: { instance_id: id, state },
  }
}

function setup(search = '', botsFail = false, omitted = 0) {
  listInstances.mockResolvedValue({
    active: true,
    instances: [
      crew('a', 'Lab box', 'connected'), crew('b', 'Cloud box', 'connected'),
      { ...crew('c', 'Old box', 'error'), status: { instance_id: 'c', state: 'error', error: 'ssh: connection refused' } },
      crew('d', 'Spare box', 'disconnected'),
    ],
    warm_set_cap: 0,
    sso: {} as Awaited<ReturnType<typeof api.listInstances>>['sso'],
  })
  if (botsFail) instancesBots.mockRejectedValue(new Error('gateway restarting'))
  else instancesBots.mockResolvedValue({
    crews: [
      { id: 'a', bots: [{ name: 'scout', display_name: 'Scout' }, { name: 'ledger', display_name: '' }], error: '', omitted },
      { id: 'b', bots: [], error: 'capability_unreachable', omitted: 0 },
    ],
  })
  // A warm pane for `a`, so the click switches to it without a reconnect.
  const store = createTestStore({
    instances: { ...createTestStore().getState().instances, warm: { a: { port: 1, token: 't' } } },
  })
  const view = renderWithProviders(<ul><OtherCrewsBots search={search} /></ul>, { store })
  return { store, ...view }
}

describe('OtherCrewsBots', () => {
  beforeEach(() => vi.clearAllMocks())

  it('lists each crew under its name, says which could not be read, and opens the crew on a click', async () => {
    const { store } = setup()

    await screen.findByText('Scout', undefined, BOTS_CHAIN())
    expect(screen.getByText('ledger')).toBeInTheDocument()
    expect(screen.getByText('Lab box')).toBeInTheDocument()
    expect(screen.getByText('Cloud box')).toBeInTheDocument()
    // `b`'s read failed and `c`'s tab is in the error state (it is not in the
    // reply): each gets the notice with its cause as the message; `d`,
    // disconnected, shows its tab's state as text.
    expect(screen.getByText('Old box')).toBeInTheDocument()
    const notices = screen.getAllByTestId('other-crew-error')
    expect(notices).toHaveLength(2)
    expect(notices[0]).toHaveTextContent(i18nT('pages.membersPage.other_crews_unreachable'))
    expect(notices[0]).toHaveTextContent('capability_unreachable')
    expect(notices[1]).toHaveTextContent(i18nT('pages.membersPage.other_crews_unreachable'))
    expect(notices[1]).toHaveTextContent('ssh: connection refused')
    expect(screen.queryByText(i18nT('pages.chatSidebar.crew_status_error'))).not.toBeInTheDocument()
    expect(screen.getByText('Spare box')).toBeInTheDocument()
    expect(screen.getByTestId('other-crew-status')).toHaveTextContent(i18nT('pages.chatSidebar.crew_status_offline'))
    expect(screen.queryByTestId('other-crew-omitted')).not.toBeInTheDocument()

    const row = screen.getByText('Scout').closest('button')!
    expect(within(row).getByTestId('other-crew-bot-open')).toHaveTextContent(i18nT('pages.membersPage.other_crews_open', { crew: 'Lab box' }))
    fireEvent.click(row)
    expect(store.getState().instances.activeId).toBe('a')
  })

  it('narrows the rows by the page search', async () => {
    setup('scou')

    await screen.findByText('Scout', undefined, BOTS_CHAIN())
    expect(screen.queryByText('ledger')).not.toBeInTheDocument()
  })

  it('counts the bots past the row cap, even under a search none of the listed rows match', async () => {
    setup('zzz', false, 3)

    expect(await screen.findByTestId('other-crew-omitted', undefined, BOTS_CHAIN())).toHaveTextContent(i18nT('pages.membersPage.hidden_more_count', { count: 3 }))
    expect(screen.getByText('Lab box')).toBeInTheDocument()
    expect(screen.queryByText('Scout')).not.toBeInTheDocument()
  })

  it('says once that the read failed, not that every crew is unreachable', async () => {
    setup('', true)

    expect(await screen.findByTestId('other-crews-error', undefined, BOTS_CHAIN())).toHaveTextContent(i18nT('pages.membersPage.other_crews_load_failed'))
    expect(screen.queryByTestId('other-crew-group')).not.toBeInTheDocument()
  })

  it('renders nothing and reads no bots without another crew', async () => {
    listInstances.mockResolvedValue({
      active: true, instances: [], warm_set_cap: 0,
      sso: {} as Awaited<ReturnType<typeof api.listInstances>>['sso'],
    })
    renderWithProviders(<ul><OtherCrewsBots search="" /></ul>)

    await waitFor(() => expect(listInstances).toHaveBeenCalled())
    expect(screen.queryByTestId('other-crews-bots')).not.toBeInTheDocument()
    expect(instancesBots).not.toHaveBeenCalled()
  })
})
