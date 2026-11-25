/**
 * Settings > Remote Crew's Shared Crews cards: hidden until a list is loaded (a
 * 404, a refusal or a failed read shows nothing), the Crews listed with their
 * type, an empty state, rows opening
 * through the desktop bridge with the endpoint ID only, and the console link.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, screen, waitFor, cleanup } from '@testing-library/react'
import { RELAY_VIEWER_CREWS_KEY } from '../hooks/useRelayViewerCrews'
import userEvent from '@testing-library/user-event'
import { api, ApiError, type RelayCrewEntry } from '../api/client'
import SharedCrewsCards from './SharedCrewsCards'
import { renderWithProviders } from '../test/helpers'
import { KIRO_SIGN_IN_PATH } from '../pages/developer/kiroSignInLink'

const crew = (over: Partial<RelayCrewEntry>): RelayCrewEntry => ({
  endpointId: 'ep-1',
  name: 'Platform team',
  crewType: 'TEAM',
  presence: 'CONNECTED',
  relayConnectionStatus: 'CONNECTED',
  crewUrl: 'https://ep-1.relay.example',
  ...over,
})

function withBridge(open = vi.fn(async (_id: string) => ({ ok: true }))) {
  ;(window as { relayCrewAPI?: unknown }).relayCrewAPI = { open }
  return open
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  delete (window as { relayCrewAPI?: unknown }).relayCrewAPI
})

describe('SharedCrewsCards account line', () => {
  const PORTAL = 'https://example-portal.awsapps.example.test/start'

  async function accountLine(account: unknown) {
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({})], account } as never)
    renderWithProviders(<SharedCrewsCards />)
    await screen.findAllByTestId('shared-crew-row')
    return screen.queryByTestId('shared-crews-account')?.textContent ?? null
  }

  it.each([
    [{ accountType: 'BuilderId' }, 'Signed in with Builder ID'],
    [{ accountType: 'IamIdentityCenter', startUrl: PORTAL }, 'Signed in with IAM Identity Center · example-portal.awsapps.example.test'],
    [{ accountType: 'SocialGoogle' }, 'Signed in with Google'],
    [{ accountType: 'ExternalIdP' }, 'Signed in with Company SSO'],
  ])('names the account %j the list was read for', async (account, text) => {
    expect(await accountLine(account)).toBe(text)
  })

  it('leaves the line out when the gateway names no account, or one with no kind', async () => {
    expect(await accountLine(undefined)).toBeNull()
    cleanup()
    expect(await accountLine({ accountType: '' })).toBeNull()
  })

  it('shows no e-mail even if one were sent, and wraps instead of cutting', async () => {
    expect(await accountLine({ accountType: 'BuilderId', email: 'someone@example.test' })).toBe('Signed in with Builder ID')
    expect(screen.getByTestId('shared-crews-account').className).not.toMatch(/\btruncate\b/)
  })
})

describe('SharedCrewsCards', () => {
  it('lists the Crews in a browser too, with no desktop bridge', async () => {
    const spy = vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({})] })
    await act(async () => renderWithProviders(<SharedCrewsCards />))
    await waitFor(() => expect(spy).toHaveBeenCalled())
    expect(await screen.findAllByTestId('shared-crew-row')).toHaveLength(1)
  })

  it('renders nothing in a build that keeps the viewer off (the route 404s)', async () => {
    withBridge()
    const spy = vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(new ApiError(404, 'Not Found'))
    const { container, queryClient } = renderWithProviders(<SharedCrewsCards />)
    await waitFor(() => expect(spy).toHaveBeenCalled())
    // The 404 is in the query, so the hook has given its final answer.
    await waitFor(() => expect(queryClient.getQueryState(RELAY_VIEWER_CREWS_KEY)?.status).toBe('error'))
    expect(container).toBeEmptyDOMElement()
  })

  it('lists each Crew with its type and opens it through the bridge', async () => {
    const open = withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({
      crews: [
        crew({}),
        crew({ endpointId: 'ep-2', name: 'Quiet shift', crewType: 'PERSONAL', presence: 'GONE', relayConnectionStatus: 'DISCONNECTED' }),
      ],
    })
    renderWithProviders(<SharedCrewsCards />)
    expect(await screen.findByRole('region', { name: 'Shared with me' })).toBeInTheDocument()
    expect(screen.getByText('Shared Crews are managed by your admin in the AWS Management Console.')).toBeInTheDocument()
    const rows = screen.getAllByTestId('shared-crew-row')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveTextContent('Platform team')
    expect(rows[0]).toHaveTextContent('Shared')
    expect(rows[1]).toHaveTextContent('Private')
    expect(rows[1]).toHaveAttribute('aria-disabled', 'true')

    const u = userEvent.setup()
    await u.click(screen.getByRole('button', { name: 'Open Quiet shift' }))
    expect(open).not.toHaveBeenCalled()
    await u.click(screen.getByRole('button', { name: 'Open Platform team' }))
    expect(open).toHaveBeenCalledWith('ep-1')
  })

  it('records an opened Crew as seen and clears its New mark from the shared list', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({ unseen: true })] })
    const seen = vi.spyOn(api, 'relayViewerCrewsSeen').mockResolvedValue({ ok: true })
    const { queryClient } = renderWithProviders(<SharedCrewsCards />)
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Open Platform team' }))
    await waitFor(() => expect(seen).toHaveBeenCalledWith(['ep-1']))
    await waitFor(() => expect(
      queryClient.getQueryData<{ crews: RelayCrewEntry[] }>(RELAY_VIEWER_CREWS_KEY)?.crews[0].unseen).toBe(false))
  })

  it('shows a failed Open under its row', async () => {
    withBridge(vi.fn(async () => ({ ok: false, error: 'CREW_ACCESS_NOT_GRANTED' })))
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({})] })
    renderWithProviders(<SharedCrewsCards />)
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Open Platform team' }))
    expect(await screen.findByTestId('shared-crew-refusal')).toHaveTextContent('You no longer have access to this Crew.')
    expect(screen.getByRole('button', { name: /Ask the agent/ })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Go to Kiro account' })).toBeNull()
  })

  it('clears a row\'s refusal when the next Open succeeds', async () => {
    const open = withBridge(vi.fn(async (_id: string) => ({ ok: true }))
      .mockResolvedValueOnce({ ok: false, error: 'CREW_ACCESS_NOT_GRANTED' }))
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({})] })
    vi.spyOn(api, 'relayViewerCrewsSeen').mockResolvedValue({ ok: true })
    renderWithProviders(<SharedCrewsCards />)
    const u = userEvent.setup()
    await u.click(await screen.findByRole('button', { name: 'Open Platform team' }))
    expect(await screen.findByTestId('shared-crew-refusal')).toBeInTheDocument()
    await u.click(screen.getByRole('button', { name: 'Open Platform team' }))
    await waitFor(() => expect(open).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByTestId('shared-crew-refusal')).toBeNull())
  })

  it('shows an empty state and the setup card when no Crew is listed', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [] })
    renderWithProviders(<SharedCrewsCards />)
    expect(await screen.findByTestId('shared-crews-empty')).toHaveTextContent("You don't have any Shared Crews to open yet.")
    expect(screen.queryByTestId('shared-crew-row')).toBeNull()
    expect(screen.getByText('Set up a new Shared Crew')).toBeInTheDocument()
    const link = screen.getByRole('link', { name: 'Open the AWS Management Console' })
    expect(link).toHaveAttribute('href', 'https://console.aws.amazon.com/')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noreferrer')
  })

  it('says the relay domain setting hid the listed Crews instead of the empty state (row 65)', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [], offDomain: 1 })
    renderWithProviders(<SharedCrewsCards />)
    expect(await screen.findByTestId('shared-crews-off-domain')).toHaveTextContent('tunnel.relay_viewer_relay_domain')
    expect(screen.queryByTestId('shared-crews-empty')).toBeNull()
  })

  it.each([[409, 'SIGNED_OUT'], [403, 'REMOTE_ACCESS_DISABLED']])(
    'renders nothing, with no notice, when the read answers %i %s',
    async (status, code) => {
      withBridge()
      vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(new ApiError(status, 'refused', JSON.stringify({ code })))
      const { container, queryClient } = renderWithProviders(<SharedCrewsCards />)
      await waitFor(() => expect(queryClient.getQueryState(RELAY_VIEWER_CREWS_KEY)?.status).toBe('error'))
      expect(container).toBeEmptyDOMElement()
    },
  )

  it('says the list could not be read when the first read fails', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(new ApiError(502, 'Bad Gateway', JSON.stringify({ code: 'SERVICE_ERROR' })))
    renderWithProviders(<SharedCrewsCards />)
    expect(await screen.findByTestId('shared-crews-refresh-failed')).toHaveTextContent("Couldn't load your Shared Crews.")
    expect(screen.getByRole('button', { name: /Ask the agent/ })).toBeInTheDocument()
    expect(screen.queryByTestId('shared-crews-empty')).toBeNull()
  })

  it('keeps the rows through a later failed read and says the read failed', async () => {
    withBridge()
    const spy = vi.spyOn(api, 'relayViewerCrews').mockResolvedValueOnce({ crews: [crew({})] })
    const { queryClient } = renderWithProviders(<SharedCrewsCards />)
    expect(await screen.findByTestId('shared-crew-row')).toHaveTextContent('Platform team')
    expect(screen.queryByTestId('shared-crews-refresh-failed')).toBeNull()
    spy.mockRejectedValueOnce(new ApiError(502, 'Bad Gateway', JSON.stringify({ code: 'SERVICE_ERROR' })))
    await act(async () => { await queryClient.refetchQueries({ queryKey: RELAY_VIEWER_CREWS_KEY }) })
    expect(await screen.findByTestId('shared-crews-refresh-failed')).toHaveTextContent("Couldn't refresh your Shared Crews. This is the last list loaded.")
    expect(screen.getByTestId('shared-crew-row')).toHaveTextContent('Platform team')
  })

  it('links a signed-out Open refusal under its row, and no other refusal', async () => {
    withBridge(vi.fn(async () => ({ ok: false, error: 'SIGNED_OUT' })))
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({})] })
    renderWithProviders(<SharedCrewsCards />)
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Open Platform team' }))
    expect(await screen.findByTestId('shared-crew-refusal')).toHaveTextContent('Sign in to Kiro to open this Crew.')
    expect(screen.getByRole('link', { name: 'Go to Kiro account' })).toHaveAttribute('href', KIRO_SIGN_IN_PATH)
    // Signed out is a state, not a failure (Bug Bash row 62): a status, no alert, no hand-off.
    expect(screen.getByTestId('shared-crew-refusal')).toHaveAttribute('role', 'status')
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByRole('button', { name: /Ask the agent/ })).toBeNull()
  })
})
