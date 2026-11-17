/**
 * Settings > Remote Crew's Shared Crews cards: hidden while the relay viewer is
 * off (no bridge, or the crews route 404s), the Crews listed with their type,
 * an empty state, a failed read shown inline with Try again, rows opening
 * through the desktop bridge with the endpoint ID only, and the console link.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, screen, waitFor, cleanup } from '@testing-library/react'
import { RELAY_VIEWER_CREWS_KEY } from '../hooks/useRelayViewerCrews'
import userEvent from '@testing-library/user-event'
import { api, ApiError, type RelayCrewEntry } from '../api/client'
import SharedCrewsCards from './SharedCrewsCards'
import { renderWithProviders } from '../test/helpers'

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

describe('SharedCrewsCards', () => {
  it('renders nothing and reads nothing without the desktop bridge', async () => {
    const spy = vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({})] })
    const { container } = await act(async () => renderWithProviders(<SharedCrewsCards />))
    expect(spy).not.toHaveBeenCalled()
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing while the relay viewer is off (the route 404s)', async () => {
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
        crew({ endpointId: 'ep-2', name: 'Night shift', crewType: 'PERSONAL', presence: 'GONE', relayConnectionStatus: 'DISCONNECTED' }),
      ],
    })
    renderWithProviders(<SharedCrewsCards />)
    expect(await screen.findByText('Shared Crews')).toBeInTheDocument()
    expect(screen.getByText('Shared Crews are managed by your admin in the AWS Management Console.')).toBeInTheDocument()
    const rows = screen.getAllByTestId('shared-crew-row')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveTextContent('Platform team')
    expect(rows[0]).toHaveTextContent('Team')
    expect(rows[1]).toHaveTextContent('Personal')
    expect(rows[1]).toHaveAttribute('aria-disabled', 'true')

    const u = userEvent.setup()
    await u.click(screen.getByRole('button', { name: 'Open Night shift' }))
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

  it('shows one inline error with Try again on a failed read, and lists the Crews after a retry', async () => {
    withBridge()
    const spy = vi.spyOn(api, 'relayViewerCrews')
      .mockRejectedValueOnce(new ApiError(502, 'Bad Gateway', JSON.stringify({ code: 'SERVICE_ERROR' })))
      .mockResolvedValue({ crews: [crew({})] })
    renderWithProviders(<SharedCrewsCards />)
    expect(await screen.findByTestId('shared-crews-refusal')).toHaveTextContent("Couldn't load your Shared Crews.")
    expect(screen.getAllByTestId('shared-crews-refusal')).toHaveLength(1)
    expect(screen.getByRole('button', { name: /Ask the agent/ })).toBeInTheDocument()
    await userEvent.setup().click(screen.getByTestId('shared-crews-retry'))
    expect(await screen.findByTestId('shared-crew-row')).toHaveTextContent('Platform team')
    expect(spy).toHaveBeenCalledTimes(2)
    expect(screen.queryByTestId('shared-crews-refusal')).toBeNull()
  })

  it('offers no Try again for a refusal a resend cannot fix', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(
      new ApiError(401, 'Unauthorized', JSON.stringify({ code: 'SIGNED_OUT' })),
    )
    renderWithProviders(<SharedCrewsCards />)
    expect(await screen.findByTestId('shared-crews-refusal')).toHaveTextContent('Sign in to Kiro to see your Shared Crews.')
    expect(screen.queryByTestId('shared-crews-retry')).toBeNull()
  })
})
