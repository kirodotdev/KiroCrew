import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createTestStore, renderWithProviders } from './helpers'
import RemoteUpdateControl from '../components/RemoteUpdateControl'
import RemoteUpdateOffer, { REMOTE_OFFER_VERSION_KEY } from '../components/RemoteUpdateOffer'

vi.mock('../api/client', () => ({
  api: {
    instancesVersions: vi.fn(),
    remoteUpdateStatus: vi.fn(),
    updateRemoteInstance: vi.fn(),
  },
}))
import { api } from '../api/client'

const RUNNING = {
  state: 'running' as const, phase: 'updating' as const,
  started_at: 1, finished_at: null, elapsed_secs: 0, error: '',
  version_before: '0.9.0', version_after: '',
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  vi.mocked(api.remoteUpdateStatus).mockResolvedValue({ job: null })
  vi.mocked(api.updateRemoteInstance).mockResolvedValue({ ok: true, job: RUNNING })
})

describe('RemoteUpdateControl', () => {
  it('runs nothing on the first click: it asks for a confirmation naming the crew', async () => {
    const u = userEvent.setup()
    renderWithProviders(<RemoteUpdateControl instanceId="old" name="Old box" />)
    await u.click(await screen.findByRole('button', { name: 'Update Old box' }))
    expect(screen.getByText('Runs the update on “Old box”, then restarts it.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Update “Old box” now/ })).toBeInTheDocument()
    expect(api.updateRemoteInstance).not.toHaveBeenCalled()
  })

  it('keeps the confirmation inside a narrow parent for a long unbroken crew name', async () => {
    const long = 'x'.repeat(128)
    const u = userEvent.setup()
    renderWithProviders(<RemoteUpdateControl instanceId="old" name={long} />)
    await u.click(await screen.findByRole('button', { name: `Update ${long}` }))
    // jsdom does no layout, so this pins the structure that keeps Cancel on a
    // 320px screen: the control never grows past its parent, its text may
    // break inside a name, and the confirm and Cancel buttons wrap.
    const root = screen.getByTestId('remote-update-control')
    expect(root.className).toMatch(/\bmax-w-full\b/)
    expect(root.className).toMatch(/\bmin-w-0\b/)
    expect(root.className).toContain('[overflow-wrap:anywhere]')
    const row = screen.getByTestId('remote-update-confirm-row')
    expect(row.className).toMatch(/\bflex-wrap\b/)
    expect(row.className).toMatch(/\bmax-w-full\b/)
    const confirm = screen.getByRole('button', { name: new RegExp(`Update “${long}” now`) })
    expect(confirm.className).toMatch(/\bmax-w-full\b/)
    expect(row).toContainElement(screen.getByRole('button', { name: 'Cancel' }))
  })

  it('Cancel disarms without running anything', async () => {
    const u = userEvent.setup()
    renderWithProviders(<RemoteUpdateControl instanceId="old" name="Old box" />)
    await u.click(await screen.findByRole('button', { name: 'Update Old box' }))
    await u.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByRole('button', { name: 'Update Old box' })).toBeInTheDocument()
    expect(api.updateRemoteInstance).not.toHaveBeenCalled()
  })

  it('the confirming click starts exactly one job and shows its progress', async () => {
    const u = userEvent.setup()
    renderWithProviders(<RemoteUpdateControl instanceId="old" name="Old box" />)
    await u.click(await screen.findByRole('button', { name: 'Update Old box' }))
    await u.click(screen.getByRole('button', { name: /Update “Old box” now/ }))
    expect(await screen.findByText('Updating Old box…')).toBeInTheDocument()
    expect(api.updateRemoteInstance).toHaveBeenCalledTimes(1)
    expect(api.updateRemoteInstance).toHaveBeenCalledWith('old')
  })

  it('shows the restart phase of a running job', async () => {
    vi.mocked(api.remoteUpdateStatus).mockResolvedValue({ job: { ...RUNNING, phase: 'restarting' } })
    renderWithProviders(<RemoteUpdateControl instanceId="old" name="Old box" />)
    expect(await screen.findByText('Restarting Old box…')).toBeInTheDocument()
  })

  it('shows the version check after the restart', async () => {
    vi.mocked(api.remoteUpdateStatus).mockResolvedValue({ job: { ...RUNNING, phase: 'verifying' } })
    renderWithProviders(<RemoteUpdateControl instanceId="old" name="Old box" />)
    expect(await screen.findByText("Checking Old box's version…")).toBeInTheDocument()
  })

  it('shows the new version once the crew came back on it', async () => {
    vi.mocked(api.remoteUpdateStatus).mockResolvedValue({
      job: { ...RUNNING, state: 'succeeded', phase: 'done', finished_at: 2, version_after: '0.9.1' },
    })
    renderWithProviders(<RemoteUpdateControl instanceId="old" name="Old box" />)
    expect(await screen.findByText('Old box now runs 0.9.1.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Update Old box' })).toBeNull()
  })

  it('says plainly when the update installed nothing, and offers Update again', async () => {
    vi.mocked(api.remoteUpdateStatus).mockResolvedValue({
      job: { ...RUNNING, state: 'unchanged', phase: 'done', finished_at: 2, version_after: '0.9.0' },
    })
    renderWithProviders(<RemoteUpdateControl instanceId="old" name="Old box" />)
    expect(await screen.findByText(/Old box restarted but still runs 0\.9\.0: its updater installed nothing/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Update Old box' })).toBeInTheDocument()
  })

  it('says when the crew has not reconnected yet to confirm its version', async () => {
    vi.mocked(api.remoteUpdateStatus).mockResolvedValue({
      job: { ...RUNNING, state: 'unverified', phase: 'done', finished_at: 2 },
    })
    renderWithProviders(<RemoteUpdateControl instanceId="old" name="Old box" />)
    expect(await screen.findByText(/has not reconnected yet to confirm its version/)).toBeInTheDocument()
  })

  it('turns an unverified outcome into the new version once the crew answers with one', async () => {
    vi.mocked(api.remoteUpdateStatus).mockResolvedValue({
      job: { ...RUNNING, state: 'unverified', phase: 'done', finished_at: 2 },
    })
    vi.mocked(api.instancesVersions).mockResolvedValue({
      local_version: '0.9.1',
      instances: [{ id: 'old', name: 'Old box', version: '0.9.1', behind: false, updatable: true }],
    })
    renderWithProviders(<RemoteUpdateControl instanceId="old" name="Old box" />)
    expect(await screen.findByText('Old box now runs 0.9.1.')).toBeInTheDocument()
  })

  it('shows the remote error tail of a failed update and names the Try again it offers', async () => {
    vi.mocked(api.remoteUpdateStatus).mockResolvedValue({
      job: { ...RUNNING, state: 'failed', error: 'release feed unreachable', finished_at: 2 },
    })
    renderWithProviders(<RemoteUpdateControl instanceId="old" name="Old box" />)
    expect(await screen.findByText(/The update on Old box failed: release feed unreachable/)).toBeInTheDocument()
    // The next step names the button by the label it shows.
    expect(screen.getByText(/Fix the cause on Old box .*then select Try again\./)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Update Old box' })).toHaveTextContent('Try again')
  })

  it('says when the update landed but the restart failed', async () => {
    vi.mocked(api.remoteUpdateStatus).mockResolvedValue({
      job: { ...RUNNING, state: 'failed', phase: 'restarting', error: 'systemd refused', finished_at: 2 },
    })
    renderWithProviders(<RemoteUpdateControl instanceId="old" name="Old box" />)
    expect(await screen.findByText(/The new version is installed on Old box, but its restart failed: systemd refused/)).toBeInTheDocument()
    expect(screen.getByText(/Old box keeps running its old version until it restarts\. Run kirocrew restart on that crew/)).toBeInTheDocument()
    expect(screen.queryByText(/for example its network or sign-in/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Update Old box' })).toHaveTextContent('Try again')
  })

  it('keeps a failed status poll visible beside the progress of a running job', async () => {
    const u = userEvent.setup()
    vi.mocked(api.remoteUpdateStatus)
      .mockResolvedValueOnce({ job: null })
      .mockRejectedValue(new Error('tunnel dropped'))
    const { queryClient } = renderWithProviders(<RemoteUpdateControl instanceId="old" name="Old box" />)
    await u.click(await screen.findByRole('button', { name: 'Update Old box' }))
    await u.click(screen.getByRole('button', { name: /Update “Old box” now/ }))
    expect(await screen.findByText('Updating Old box…')).toBeInTheDocument()
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: ['instances', 'update-job', 'old'] })
    })
    expect(await screen.findByText(/Could not read Old box's update status: tunnel dropped/)).toBeInTheDocument()
    expect(screen.getByText('Updating Old box…')).toBeInTheDocument()
  })

  it('keeps a failed status read visible beside a settled outcome', async () => {
    const job = { ...RUNNING, state: 'succeeded' as const, phase: 'done' as const, finished_at: 2, version_after: '0.9.1' }
    vi.mocked(api.remoteUpdateStatus).mockResolvedValueOnce({ job }).mockRejectedValue(new Error('tunnel dropped'))
    const { queryClient } = renderWithProviders(<RemoteUpdateControl instanceId="old" name="Old box" />)
    expect(await screen.findByText('Old box now runs 0.9.1.')).toBeInTheDocument()
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: ['instances', 'update-job', 'old'] })
    })
    expect(await screen.findByText(/Could not read Old box's update status: tunnel dropped/)).toBeInTheDocument()
    expect(screen.getByText('Old box now runs 0.9.1.')).toBeInTheDocument()
  })

  it('reports a failed status read rather than looking idle', async () => {
    vi.mocked(api.remoteUpdateStatus).mockRejectedValue(new Error('tunnel dropped'))
    renderWithProviders(<RemoteUpdateControl instanceId="old" name="Old box" />)
    expect(await screen.findByText(/Could not read Old box's update status: tunnel dropped/)).toBeInTheDocument()
    expect(screen.queryByText(/Fix the cause on Old box/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Update Old box' })).toBeInTheDocument()
  })

  it('shows a refusal from the gateway', async () => {
    vi.mocked(api.updateRemoteInstance).mockRejectedValue(new Error('Old box is already updating'))
    const u = userEvent.setup()
    renderWithProviders(<RemoteUpdateControl instanceId="old" name="Old box" />)
    await u.click(await screen.findByRole('button', { name: 'Update Old box' }))
    await u.click(screen.getByRole('button', { name: /Update “Old box” now/ }))
    expect(await screen.findByText(/Old box is already updating/)).toBeInTheDocument()
    expect(screen.queryByText(/Fix the cause on Old box/)).toBeNull()
    await waitFor(() => expect(api.remoteUpdateStatus).toHaveBeenCalledTimes(2))
  })

  it('a refusal because a job already runs gives way to that job', async () => {
    vi.mocked(api.updateRemoteInstance).mockRejectedValue(new Error('Old box is already updating'))
    vi.mocked(api.remoteUpdateStatus)
      .mockResolvedValueOnce({ job: null })
      .mockResolvedValue({ job: { ...RUNNING, phase: 'restarting' } })
    const u = userEvent.setup()
    renderWithProviders(<RemoteUpdateControl instanceId="old" name="Old box" />)
    await u.click(await screen.findByRole('button', { name: 'Update Old box' }))
    await u.click(screen.getByRole('button', { name: /Update “Old box” now/ }))
    expect(await screen.findByText('Restarting Old box…')).toBeInTheDocument()
    expect(screen.queryByText(/Old box is already updating/)).toBeNull()
  })
})

function storeOn(version: string) {
  return createTestStore({ dashboard: { status: { version } } as never })
}

const BEHIND_ONE = {
  local_version: '0.9.1',
  instances: [
    { id: 'old', name: 'Old box', version: '0.9.0', behind: true, updatable: true },
    { id: 'same', name: 'Same box', version: '0.9.1', behind: false, updatable: true },
  ],
}

describe('RemoteUpdateOffer', () => {
  it('shows nothing and reads nothing on the first visit, and records the version', async () => {
    vi.mocked(api.instancesVersions).mockResolvedValue(BEHIND_ONE)
    renderWithProviders(<RemoteUpdateOffer />, { store: storeOn('0.9.1') })
    await waitFor(() => expect(localStorage.getItem(REMOTE_OFFER_VERSION_KEY)).toBe('0.9.1'))
    expect(api.instancesVersions).not.toHaveBeenCalled()
    expect(screen.queryByTestId('remote-update-offer')).toBeNull()
  })

  it('shows nothing when the version did not change', async () => {
    localStorage.setItem(REMOTE_OFFER_VERSION_KEY, '0.9.1')
    vi.mocked(api.instancesVersions).mockResolvedValue(BEHIND_ONE)
    renderWithProviders(<RemoteUpdateOffer />, { store: storeOn('0.9.1') })
    await waitFor(() => expect(localStorage.getItem(REMOTE_OFFER_VERSION_KEY)).toBe('0.9.1'))
    expect(api.instancesVersions).not.toHaveBeenCalled()
    expect(screen.queryByTestId('remote-update-offer')).toBeNull()
  })

  it('after a local update, lists only the crews that are behind, each with Update', async () => {
    localStorage.setItem(REMOTE_OFFER_VERSION_KEY, '0.9.0')
    vi.mocked(api.instancesVersions).mockResolvedValue(BEHIND_ONE)
    renderWithProviders(<RemoteUpdateOffer />, { store: storeOn('0.9.1') })
    const offer = await screen.findByTestId('remote-update-offer')
    expect(offer).toHaveTextContent('Some remote crews run an older version')
    expect(offer).toHaveTextContent('This machine now runs 0.9.1.')
    expect(offer).toHaveTextContent('Old box: 0.9.0')
    expect(offer).not.toHaveTextContent('Same box')
    expect(await screen.findByRole('button', { name: 'Update Old box' })).toBeInTheDocument()
    expect(api.updateRemoteInstance).not.toHaveBeenCalled()
    expect(localStorage.getItem(REMOTE_OFFER_VERSION_KEY)).toBe('0.9.1')
  })

  it('after a local update with no crew behind, renders nothing', async () => {
    localStorage.setItem(REMOTE_OFFER_VERSION_KEY, '0.9.0')
    vi.mocked(api.instancesVersions).mockResolvedValue({
      local_version: '0.9.1',
      instances: [{ id: 'same', name: 'Same box', version: '0.9.1', behind: false, updatable: true }],
    })
    renderWithProviders(<RemoteUpdateOffer />, { store: storeOn('0.9.1') })
    await waitFor(() => expect(api.instancesVersions).toHaveBeenCalled())
    expect(screen.queryByTestId('remote-update-offer')).toBeNull()
    expect(api.updateRemoteInstance).not.toHaveBeenCalled()
  })

  it('renders nothing when remote crews are turned off', async () => {
    localStorage.setItem(REMOTE_OFFER_VERSION_KEY, '0.9.0')
    vi.mocked(api.instancesVersions).mockRejectedValue(
      Object.assign(new Error('instances feature is disabled'), { status: 403 }),
    )
    renderWithProviders(<RemoteUpdateOffer />, { store: storeOn('0.9.1') })
    await waitFor(() => expect(api.instancesVersions).toHaveBeenCalled())
    expect(screen.queryByTestId('remote-update-offer')).toBeNull()
  })

  it('names the crews that did not answer, since they may be behind too', async () => {
    localStorage.setItem(REMOTE_OFFER_VERSION_KEY, '0.9.0')
    vi.mocked(api.instancesVersions).mockResolvedValue({
      local_version: '0.9.1',
      instances: [
        { id: 'old', name: 'Old box', version: '0.9.0', behind: true, updatable: true },
        { id: 'quiet', name: 'Quiet box', version: '', behind: false, updatable: true, error: 'capability_unreachable' },
      ],
    })
    renderWithProviders(<RemoteUpdateOffer />, { store: storeOn('0.9.1') })
    const offer = await screen.findByTestId('remote-update-offer')
    expect(offer).toHaveTextContent('Old box: 0.9.0')
    expect(offer).toHaveTextContent('No version from Quiet box yet, so they may be behind too.')
  })

  it('reports a failed versions read instead of looking like every crew is current', async () => {
    localStorage.setItem(REMOTE_OFFER_VERSION_KEY, '0.9.0')
    vi.mocked(api.instancesVersions).mockRejectedValue(
      Object.assign(new Error('gateway timed out'), { status: 504 }),
    )
    renderWithProviders(<RemoteUpdateOffer />, { store: storeOn('0.9.1') })
    const offer = await screen.findByTestId('remote-update-offer')
    expect(offer).toHaveTextContent("Could not check your remote crews' versions after this update")
    expect(offer).toHaveTextContent('gateway timed out')
  })

  it('lists a chained crew without an Update it could not run', async () => {
    localStorage.setItem(REMOTE_OFFER_VERSION_KEY, '0.9.0')
    vi.mocked(api.instancesVersions).mockResolvedValue({
      local_version: '0.9.1',
      instances: [{ id: 'hop', name: 'Hop box', version: '0.9.0', behind: true, updatable: false }],
    })
    renderWithProviders(<RemoteUpdateOffer />, { store: storeOn('0.9.1') })
    expect(await screen.findByTestId('remote-update-offer')).toHaveTextContent('Hop box: 0.9.0')
    expect(screen.queryByRole('button', { name: 'Update Hop box' })).toBeNull()
  })

  it('keeps an updated crew listed with its outcome after it stops being behind', async () => {
    localStorage.setItem(REMOTE_OFFER_VERSION_KEY, '0.9.0')
    vi.mocked(api.instancesVersions)
      .mockResolvedValueOnce(BEHIND_ONE)
      .mockResolvedValue({
        local_version: '0.9.1',
        instances: [{ id: 'old', name: 'Old box', version: '0.9.1', behind: false, updatable: true }],
      })
    vi.mocked(api.remoteUpdateStatus).mockResolvedValue({
      job: { ...RUNNING, state: 'succeeded', phase: 'done', finished_at: 2, version_after: '0.9.1' },
    })
    renderWithProviders(<RemoteUpdateOffer />, { store: storeOn('0.9.1') })
    expect(await screen.findByText('Old box now runs 0.9.1.')).toBeInTheDocument()
    await waitFor(() => expect(api.instancesVersions).toHaveBeenCalledTimes(2))
    expect(screen.getByTestId('remote-update-offer')).toHaveTextContent('Old box now runs 0.9.1.')
  })

  it('keeps Dismiss reachable on a short viewport: the card is bounded and only its body scrolls', async () => {
    localStorage.setItem(REMOTE_OFFER_VERSION_KEY, '0.9.0')
    vi.mocked(api.instancesVersions).mockResolvedValue({
      local_version: '0.9.1',
      instances: ['a', 'b', 'c'].map(id => ({ id, name: `Box ${id}`, version: '0.9.0', behind: true, updatable: true })),
    })
    renderWithProviders(<RemoteUpdateOffer />, { store: storeOn('0.9.1') })
    const offer = await screen.findByTestId('remote-update-offer')
    // jsdom does no layout, so this pins the structure that keeps the card on
    // screen: a column bounded to the viewport, with a scrolling body below a
    // header that holds Dismiss.
    expect(offer.className).toMatch(/\bflex-col\b/)
    expect(offer.className).toMatch(/\bmax-h-\[calc\(100dvh/)
    const body = screen.getByTestId('remote-update-offer-body')
    expect(body.className).toMatch(/\boverflow-y-auto\b/)
    expect(body.className).toMatch(/\bmin-h-0\b/)
    expect(body).toHaveTextContent('Box c: 0.9.0')
    expect(body).not.toContainElement(screen.getByRole('button', { name: 'Dismiss' }))
  })

  it('Dismiss closes the offer', async () => {
    localStorage.setItem(REMOTE_OFFER_VERSION_KEY, '0.9.0')
    vi.mocked(api.instancesVersions).mockResolvedValue(BEHIND_ONE)
    const u = userEvent.setup()
    renderWithProviders(<RemoteUpdateOffer />, { store: storeOn('0.9.1') })
    await screen.findByTestId('remote-update-offer')
    await u.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId('remote-update-offer')).toBeNull()
  })
})
