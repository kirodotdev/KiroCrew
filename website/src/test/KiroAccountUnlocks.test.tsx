import { screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { KiroAccountUnlocksCard } from '../pages/developer/KiroAccountUnlocks'
import { KiroSignInCard } from '../pages/developer/KiroSignInCard'
import { api, type KasLoginStatus } from '../api/client'
import { KAS_LOGIN_STATUS_KEY } from '../hooks/useKasLogin'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kasLoginStatus: vi.fn(),
      kirocrewConfig: vi.fn(),
      kasLoginSources: vi.fn().mockResolvedValue({
        active: 'none',
        kiro_crew: { stored: false },
        kiro_cli: { stored: false, usable: false },
        name_pending: false,
        same_account: null,
        can_use_kiro_cli: false,
      }),
    },
  }
})

const kasLoginStatus = vi.mocked(api.kasLoginStatus)
const kirocrewConfig = vi.mocked(api.kirocrewConfig)
type Cfg = Awaited<ReturnType<typeof api.kirocrewConfig>>

const SIGNED_OUT: KasLoginStatus = {
  authenticated: false,
  provider: '',
  identity: '',
  transport: 'device',
  expires_at: null,
  expired: false,
  has_refresh_token: false,
  refresh_rejected: false,
  usable: false,
}

const SIGNED_IN: KasLoginStatus = {
  authenticated: true,
  provider: 'Google',
  identity: 'social',
  transport: 'device',
  expires_at: '2099-01-01T00:00:00Z',
  expired: false,
  has_refresh_token: true,
  refresh_rejected: false,
  usable: true,
}

const ROW_IDS = ['kiro-account-unlocks-kiro-agent', 'kiro-account-unlocks-kiro-models']

describe('KiroAccountUnlocks', () => {
  beforeEach(() => {
    kasLoginStatus.mockReset()
    kirocrewConfig.mockReset()
  })

  it('lists what the account unlocks under the signed-out choices', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'kas' } } as Cfg)
    renderWithProviders(<KiroSignInCard />)
    const list = await screen.findByTestId('kiro-account-unlocks-list')
    expect(screen.getByTestId('kiro-sign-in-inline')).toBeInTheDocument()
    expect(within(list).getByText('With a Kiro account')).toBeInTheDocument()
    expect(within(list).getByText('Kiro Agent')).toBeInTheDocument()
    expect(within(list).getByText('Kiro models')).toBeInTheDocument()
    expect(within(list).getAllByRole('listitem').length).toBeGreaterThan(0)
  })

  it('drops the signed-out list once signed in', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'kas' } } as Cfg)
    renderWithProviders(<KiroSignInCard />)
    await screen.findByTestId('kiro-sign-in-summary')
    expect(screen.queryByTestId('kiro-account-unlocks-list')).toBeNull()
  })

  it('shows every row Active when signed in on Kiro Agent', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'kas' } } as Cfg)
    renderWithProviders(<KiroAccountUnlocksCard />)
    await screen.findByTestId('kiro-account-unlocks-card')
    expect(screen.getByText('What your Kiro account unlocks')).toBeInTheDocument()
    for (const id of ROW_IDS) {
      const row = screen.getByTestId(id)
      expect(row).toHaveAttribute('data-state', 'on')
      expect(within(row).getByText('Active')).toBeInTheDocument()
      expect(within(row).queryByText('Not active')).toBeNull()
      expect(within(row).queryByRole('link')).toBeNull()
    }
  })

  it('names the harness in use, with one link to Agent Harness, on another harness', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'claude' } } as Cfg)
    renderWithProviders(<KiroAccountUnlocksCard />)
    await screen.findByTestId('kiro-account-unlocks-card')
    for (const id of ROW_IDS) {
      const row = screen.getByTestId(id)
      expect(row).toHaveAttribute('data-state', 'missing')
      expect(within(row).queryByText('Active')).toBeNull()
      expect(within(row).getByText('Not active')).toBeInTheDocument()
    }
    const agentRow = screen.getByTestId(ROW_IDS[0])
    expect(within(agentRow).getByText('Not in use: your agents run on Claude Code.')).toBeInTheDocument()
    expect(within(agentRow).getByRole('link', { name: 'Change in Agent Harness' })).toHaveAttribute('href', '/settings/agent')
    const modelsRow = screen.getByTestId(ROW_IDS[1])
    expect(within(modelsRow).getByText('Needs Kiro Agent as your main harness.')).toBeInTheDocument()
    expect(within(modelsRow).queryByRole('link')).toBeNull()
    expect(screen.getAllByRole('link', { name: 'Change in Agent Harness' })).toHaveLength(1)
  })

  it('counts Bots on Kiro Agent for the Kiro Agent row, not for Kiro models', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'claude' }, kas_in_use: true } as Cfg)
    renderWithProviders(<KiroAccountUnlocksCard />)
    await screen.findByTestId('kiro-account-unlocks-card')
    const agentRow = screen.getByTestId(ROW_IDS[0])
    expect(agentRow).toHaveAttribute('data-state', 'on')
    expect(within(agentRow).getByText('Active')).toBeInTheDocument()
    expect(within(agentRow).getByText('Your Bots run on Kiro Agent. Your main harness is Claude Code.')).toBeInTheDocument()
    expect(within(agentRow).getByRole('link', { name: 'Change in Agent Harness' })).toHaveAttribute('href', '/settings/agent')
    const modelsRow = screen.getByTestId(ROW_IDS[1])
    expect(modelsRow).toHaveAttribute('data-state', 'missing')
    expect(within(modelsRow).getByText('Not active')).toBeInTheDocument()
    expect(within(modelsRow).getByText('Needs Kiro Agent as your main harness.')).toBeInTheDocument()
  })

  it('names an unknown harness by its id', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'future-harness' } } as Cfg)
    renderWithProviders(<KiroAccountUnlocksCard />)
    await screen.findByTestId('kiro-account-unlocks-card')
    expect(within(screen.getByTestId(ROW_IDS[0])).getByText('Not in use: your agents run on future-harness.')).toBeInTheDocument()
  })

  it('asks for a fresh sign-in, not a harness switch, when the sign-in lapsed', async () => {
    kasLoginStatus.mockResolvedValue({ ...SIGNED_IN, usable: false })
    kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'kas' } } as Cfg)
    renderWithProviders(<KiroAccountUnlocksCard />)
    await screen.findByTestId('kiro-account-unlocks-card')
    for (const id of ROW_IDS) {
      const row = screen.getByTestId(id)
      expect(row).toHaveAttribute('data-state', 'missing')
      expect(within(row).getByText('Sign in again to use it.')).toBeInTheDocument()
      expect(within(row).getByText('Not active')).toBeInTheDocument()
      expect(within(row).queryByText('Active')).toBeNull()
      expect(within(row).queryByRole('link')).toBeNull()
    }
  })

  it('names the missing harness, not a fresh sign-in, when lapsed on another harness', async () => {
    kasLoginStatus.mockResolvedValue({ ...SIGNED_IN, usable: false })
    kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'claude' } } as Cfg)
    renderWithProviders(<KiroAccountUnlocksCard />)
    const agentRow = await screen.findByTestId(ROW_IDS[0])
    for (const id of ROW_IDS) {
      const row = screen.getByTestId(id)
      expect(row).toHaveAttribute('data-state', 'missing')
      expect(within(row).getByText('Not active')).toBeInTheDocument()
      expect(within(row).queryByText('Sign in again to use it.')).toBeNull()
    }
    expect(within(agentRow).getByText('Not in use: your agents run on Claude Code.')).toBeInTheDocument()
    expect(within(agentRow).getByRole('link', { name: 'Change in Agent Harness' })).toHaveAttribute('href', '/settings/agent')
    const modelsRow = screen.getByTestId(ROW_IDS[1])
    expect(within(modelsRow).getByText('Needs Kiro Agent as your main harness.')).toBeInTheDocument()
    expect(within(modelsRow).queryByRole('link')).toBeNull()
  })

  it('asks for a fresh sign-in only on the row it would turn on, when lapsed with Bots on Kiro Agent', async () => {
    kasLoginStatus.mockResolvedValue({ ...SIGNED_IN, refresh_rejected: true })
    kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'claude' }, kas_in_use: true } as Cfg)
    renderWithProviders(<KiroAccountUnlocksCard />)
    const agentRow = await screen.findByTestId(ROW_IDS[0])
    expect(agentRow).toHaveAttribute('data-state', 'missing')
    expect(within(agentRow).getByText('Sign in again to use it.')).toBeInTheDocument()
    expect(within(agentRow).getByText('Not active')).toBeInTheDocument()
    expect(within(agentRow).getByRole('link', { name: 'Change in Agent Harness' })).toHaveAttribute('href', '/settings/agent')
    const modelsRow = screen.getByTestId(ROW_IDS[1])
    expect(modelsRow).toHaveAttribute('data-state', 'missing')
    expect(within(modelsRow).getByText('Needs Kiro Agent as your main harness.')).toBeInTheDocument()
    expect(within(modelsRow).queryByText('Sign in again to use it.')).toBeNull()
  })

  it('renders nothing while signed out', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'kas' } } as Cfg)
    const { container, queryClient } = renderWithProviders(<KiroAccountUnlocksCard />)
    // Both reads have answered, so the empty card is the signed-out verdict.
    await waitFor(() => expect(queryClient.getQueryData(KAS_LOGIN_STATUS_KEY)).toEqual(SIGNED_OUT))
    await waitFor(() => expect(queryClient.getQueryData(['kirocrewConfig'])).toBeDefined())
    expect(container).toBeEmptyDOMElement()
  })

  it('says so when the config cannot be read, instead of vanishing', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    kirocrewConfig.mockRejectedValue(new Error('boom'))
    renderWithProviders(<KiroAccountUnlocksCard />)
    const notice = await screen.findByTestId('kiro-account-unlocks-config-error')
    expect(notice).toHaveTextContent('Could not read the dashboard settings')
    expect(screen.queryByTestId('kiro-account-unlocks-card')).toBeNull()
  })

  it('leaves the config notice to the sign-in card when the sign-in lapsed', async () => {
    const lapsed = { ...SIGNED_IN, usable: false }
    kasLoginStatus.mockResolvedValue(lapsed)
    kirocrewConfig.mockRejectedValue(new Error('boom'))
    const { container, queryClient } = renderWithProviders(<KiroAccountUnlocksCard />)
    // Both reads have answered: the lapsed status and the failed config read.
    await waitFor(() => expect(queryClient.getQueryData(KAS_LOGIN_STATUS_KEY)).toEqual(lapsed))
    await waitFor(() => expect(queryClient.getQueryState(['kirocrewConfig'])?.status).toBe('error'))
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing until the config answers, so no row is a guess', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    kirocrewConfig.mockReturnValue(new Promise<Cfg>(() => {}))
    const { container, queryClient } = renderWithProviders(<KiroAccountUnlocksCard />)
    await waitFor(() => expect(queryClient.getQueryData(KAS_LOGIN_STATUS_KEY)).toEqual(SIGNED_IN))
    expect(container).toBeEmptyDOMElement()
  })
})
