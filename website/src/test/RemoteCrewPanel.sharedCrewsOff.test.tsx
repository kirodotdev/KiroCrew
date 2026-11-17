/**
 * Settings > Remote Crew with remote crew management off (#38256): the Shared
 * Crews card does not depend on that switch, because relay Crews open over the
 * relay rather than an SSH/SSM tunnel. It shows under the "off" card when the
 * relay viewer list is available, and keeps its own hidden rule otherwise.
 * A teammate gets an owner-only notice in place of the "off" card's switch.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, screen, waitFor, cleanup } from '@testing-library/react'
import { api, ApiError } from '../api/client'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'
import { renderWithProviders } from './helpers'

const DISABLED = new ApiError(
  403,
  'instances feature is disabled',
  JSON.stringify({ error: 'instances feature is disabled', code: 'instances_disabled' }),
)

function managementOff() {
  vi.spyOn(api, 'listInstances').mockRejectedValue(DISABLED)
  vi.spyOn(api, 'cloudLaunches').mockResolvedValue({ jobs: [] })
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  delete (window as { relayCrewAPI?: unknown }).relayCrewAPI
})

describe('RemoteCrewPanel with remote crew management off', () => {
  it('still shows the Shared Crews card when the relay viewer list is available', async () => {
    managementOff()
    ;(window as { relayCrewAPI?: unknown }).relayCrewAPI = { open: vi.fn(async () => ({ ok: true })) }
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({
      crews: [{
        endpointId: 'ep-1', name: 'Platform team', crewType: 'TEAM', presence: 'CONNECTED',
        relayConnectionStatus: 'CONNECTED', crewUrl: 'https://ep-1.relay.example',
      }],
    })
    renderWithProviders(<RemoteCrewPanel />)
    expect(await screen.findByText(/Remote crew management is off/i)).toBeInTheDocument()
    expect(await screen.findByTestId('shared-crews-card')).toBeInTheDocument()
    expect(screen.getByTestId('shared-crew-row')).toHaveTextContent('Platform team')
  })

  it('leaves the Shared Crews card hidden without the desktop bridge', async () => {
    managementOff()
    const crews = vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [] })
    await act(async () => { renderWithProviders(<RemoteCrewPanel />) })
    expect(await screen.findByText(/Remote crew management is off/i)).toBeInTheDocument()
    expect(crews).not.toHaveBeenCalled()
    expect(screen.queryByTestId('shared-crews-card')).not.toBeInTheDocument()
  })

  it('leaves the Shared Crews card hidden while the relay viewer is off (the route 404s)', async () => {
    managementOff()
    ;(window as { relayCrewAPI?: unknown }).relayCrewAPI = { open: vi.fn(async () => ({ ok: true })) }
    const crews = vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(new ApiError(404, 'Not Found'))
    renderWithProviders(<RemoteCrewPanel />)
    expect(await screen.findByText(/Remote crew management is off/i)).toBeInTheDocument()
    await waitFor(() => expect(crews).toHaveBeenCalled())
    expect(screen.queryByTestId('shared-crews-card')).not.toBeInTheDocument()
  })

  it('gives a teammate an owner-only notice instead of the switch, and keeps the Shared Crews card', async () => {
    managementOff()
    vi.spyOn(api, 'viewerOwnerOk').mockResolvedValue(false)
    ;(window as { relayCrewAPI?: unknown }).relayCrewAPI = { open: vi.fn(async () => ({ ok: true })) }
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({
      crews: [{
        endpointId: 'ep-1', name: 'Platform team', crewType: 'TEAM', presence: 'CONNECTED',
        relayConnectionStatus: 'CONNECTED', crewUrl: 'https://ep-1.relay.example',
      }],
    })
    renderWithProviders(<RemoteCrewPanel />)
    expect(await screen.findByTestId('remote-crews-owner-only')).toHaveTextContent("Only the Crew's owner can manage remote crews.")
    expect(screen.queryByRole('button', { name: /Enable remote crew management/i })).not.toBeInTheDocument()
    expect(await screen.findByTestId('shared-crews-card')).toBeInTheDocument()
  })
})
