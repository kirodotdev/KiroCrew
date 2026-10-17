import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import CrewVersionStatus, { CrewVersionsReadError } from '../pages/settings/CrewVersionStatus'

vi.mock('../api/client', () => ({
  api: {
    instancesVersions: vi.fn(),
    remoteUpdateStatus: vi.fn(),
    updateRemoteInstance: vi.fn(),
  },
}))
import { api } from '../api/client'

const VERSIONS = {
  local_version: '0.9.1',
  instances: [
    { id: 'old', name: 'Old box', version: '0.9.0', behind: true, updatable: true },
    { id: 'hop', name: 'Hop box', version: '0.9.0', behind: true, updatable: false },
    { id: 'same', name: 'Same box', version: '0.9.1', behind: false, updatable: true },
    { id: 'quiet', name: 'Quiet box', version: '', behind: false, updatable: true },
  ],
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.instancesVersions).mockResolvedValue(VERSIONS)
  vi.mocked(api.remoteUpdateStatus).mockResolvedValue({ job: null })
})

describe('CrewVersionStatus', () => {
  it('shows the exact versions and an Update on a crew that runs an older build', async () => {
    renderWithProviders(<CrewVersionStatus instanceId="old" name="Old box" connected />)
    expect(await screen.findByText('Behind: remote 0.9.0, local 0.9.1')).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: 'Update Old box' })).toBeInTheDocument()
    expect(api.updateRemoteInstance).not.toHaveBeenCalled()
  })

  it('shows the badge but no Update on a crew this dashboard cannot run commands on', async () => {
    renderWithProviders(<CrewVersionStatus instanceId="hop" name="Hop box" connected />)
    expect(await screen.findByText('Behind: remote 0.9.0, local 0.9.1')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Update Hop box' })).toBeNull()
  })

  it('shows nothing on a crew that runs the same build', async () => {
    renderWithProviders(<CrewVersionStatus instanceId="same" name="Same box" connected />)
    await waitFor(() => expect(api.instancesVersions).toHaveBeenCalled())
    expect(screen.queryByTestId('remote-behind-badge')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Update Same box' })).toBeNull()
  })

  it('shows nothing when the crew did not report a version', async () => {
    renderWithProviders(<CrewVersionStatus instanceId="quiet" name="Quiet box" connected />)
    await waitFor(() => expect(api.instancesVersions).toHaveBeenCalled())
    expect(screen.queryByTestId('remote-behind-badge')).toBeNull()
  })

  it('reads nothing for a crew that is not connected', () => {
    renderWithProviders(<CrewVersionStatus instanceId="old" name="Old box" connected={false} />)
    expect(api.instancesVersions).not.toHaveBeenCalled()
    expect(screen.queryByTestId('remote-behind-badge')).toBeNull()
  })

  it('says so once for the list when the versions read fails, instead of looking current', async () => {
    vi.mocked(api.instancesVersions).mockRejectedValue(new Error('peer read failed'))
    renderWithProviders(
      <>
        <CrewVersionsReadError anyConnected />
        <CrewVersionStatus instanceId="old" name="Old box" connected />
        <CrewVersionStatus instanceId="same" name="Same box" connected />
      </>,
    )
    expect(await screen.findByText("Could not read your remote crews' versions")).toBeInTheDocument()
    expect(screen.getAllByText(/peer read failed/)).toHaveLength(1)
    expect(screen.queryByTestId('remote-behind-badge')).toBeNull()
  })

  it('reports nothing for the list when no crew is connected', () => {
    renderWithProviders(<CrewVersionsReadError anyConnected={false} />)
    expect(api.instancesVersions).not.toHaveBeenCalled()
  })

  it('keeps an update outcome on the row after the crew stops being behind', async () => {
    vi.mocked(api.instancesVersions).mockResolvedValue({
      local_version: '0.9.1',
      instances: [{ id: 'old', name: 'Old box', version: '0.9.1', behind: false, updatable: true }],
    })
    const job = {
      state: 'succeeded' as const, phase: 'done' as const, started_at: 1, finished_at: 2,
      elapsed_secs: 40, error: '', version_before: '0.9.0', version_after: '0.9.1',
    }
    vi.mocked(api.remoteUpdateStatus).mockResolvedValue({ job })
    const { queryClient } = renderWithProviders(<CrewVersionStatus instanceId="old" name="Old box" connected />)
    await waitFor(() => expect(api.instancesVersions).toHaveBeenCalled())
    expect(screen.queryByText('Old box now runs 0.9.1.')).toBeNull()
    act(() => { queryClient.setQueryData(['instances', 'update-job', 'old'], { job }) })
    expect(await screen.findByText('Old box now runs 0.9.1.')).toBeInTheDocument()
    expect(screen.queryByTestId('remote-behind-badge')).toBeNull()
  })

  it('hides the stale badge while an update waits for the crew to reconnect', async () => {
    const job = {
      state: 'unverified' as const, phase: 'done' as const, started_at: 1, finished_at: 2,
      elapsed_secs: 200, error: '', version_before: '0.9.0', version_after: '',
    }
    vi.mocked(api.remoteUpdateStatus).mockResolvedValue({ job })
    const { queryClient } = renderWithProviders(<CrewVersionStatus instanceId="old" name="Old box" connected />)
    await screen.findByText('Behind: remote 0.9.0, local 0.9.1')
    act(() => { queryClient.setQueryData(['instances', 'update-job', 'old'], { job }) })
    expect(await screen.findByText(/has not reconnected yet to confirm its version/)).toBeInTheDocument()
    expect(screen.queryByTestId('remote-behind-badge')).toBeNull()
  })

  it('reports a crew whose version read failed instead of hiding its row', async () => {
    vi.mocked(api.instancesVersions).mockResolvedValue({
      local_version: '0.9.1',
      instances: [{ id: 'old', name: 'Old box', version: '', behind: false, updatable: true, error: 'capability_unreachable' }],
    })
    renderWithProviders(<CrewVersionStatus instanceId="old" name="Old box" connected />)
    expect(await screen.findByText("Could not read Old box's version (capability_unreachable).")).toBeInTheDocument()
    expect(screen.queryByTestId('remote-behind-badge')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Update Old box' })).toBeNull()
  })

  it('shares one versions read between every row', async () => {
    renderWithProviders(
      <>
        <CrewVersionStatus instanceId="old" name="Old box" connected />
        <CrewVersionStatus instanceId="same" name="Same box" connected />
      </>,
    )
    await screen.findByText('Behind: remote 0.9.0, local 0.9.1')
    expect(api.instancesVersions).toHaveBeenCalledTimes(1)
  })
})
