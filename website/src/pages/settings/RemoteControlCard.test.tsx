import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { copyCode } from '../../utils/clipboard'

vi.mock('../../api/client', () => ({
  api: { remoteControl: vi.fn(), turnOffRemoteControl: vi.fn() },
}))
vi.mock('../../utils/clipboard', () => ({ copyCode: vi.fn(async () => true) }))

import { api, type RemoteControlState } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { RemoteControlCard } from './RemoteControlCard'

const state = (over: Partial<RemoteControlState>): RemoteControlState => ({
  requested: false,
  locked: false,
  running: false,
  ...over,
})

const get = api.remoteControl as ReturnType<typeof vi.fn>
const put = api.turnOffRemoteControl as ReturnType<typeof vi.fn>

describe('RemoteControlCard', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.resetAllMocks()
  })

  it('renders nothing when Remote Control was never turned on', async () => {
    get.mockResolvedValue(state({}))
    renderWithProviders(<RemoteControlCard />)
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(screen.queryByTestId('remote-control-card')).toBeNull()
  })

  it('says the build manages it, not restart-to-connect, on a locked build', async () => {
    get.mockResolvedValue(state({ requested: true, locked: true }))
    renderWithProviders(<RemoteControlCard />)
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Remote Control is managed by this build and cannot be turned on here.',
    )
    expect(screen.queryByText(/Restart/)).toBeNull()
    // Off still clears the stored setting; no turn-on hint, since `on` is refused here.
    expect(screen.getByRole('button', { name: 'Turn off' })).toBeInTheDocument()
    expect(screen.queryByTestId('remote-control-off-effect')).toBeNull()
  })

  it('gives no turn-on hint after turning it off on a locked build', async () => {
    get.mockResolvedValue(state({ requested: true, locked: true }))
    put.mockResolvedValue(state({ locked: true }))
    renderWithProviders(<RemoteControlCard />)
    fireEvent.click(await screen.findByRole('button', { name: 'Turn off' }))
    const done = await screen.findByText(/It stays off until you turn it back on/)
    expect(done).toHaveTextContent('managed by this build and cannot be turned on here')
    expect(done).not.toHaveTextContent('kirocrew remote-control on')
    expect(done.querySelector('code')).toBeNull()
  })

  it('says on only while the relay is running, and restart-to-connect otherwise', async () => {
    get.mockResolvedValue(state({ requested: true, running: false }))
    renderWithProviders(<RemoteControlCard />)
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Remote Control is set to on but not connected. Restart',
    )
    expect(screen.queryByText(/Remote Control is on\./)).toBeNull()
    expect(screen.getByRole('button', { name: 'Turn off' })).toBeInTheDocument()
    // The card reports only the setting and the connection, never the sign-in.
    expect(screen.getByTestId('remote-control-card')).not.toHaveTextContent(/sign/i)
  })

  it('shows a later read failure even while an earlier read is cached', async () => {
    get.mockResolvedValueOnce(state({ requested: true, running: true }))
    const { queryClient } = renderWithProviders(<RemoteControlCard />)
    expect(await screen.findByText(/Remote Control is on/)).toBeInTheDocument()

    get.mockRejectedValue(new ApiError(502, 'bad gateway'))
    await queryClient.refetchQueries({ queryKey: ['remote-control'] })

    expect(await screen.findByText(/Couldn't read the Remote Control setting/)).toBeInTheDocument()
    // The cached card stays, with its one-click off, beside the error.
    expect(screen.getByRole('button', { name: 'Turn off' })).toBeInTheDocument()
  })

  it('shows a failing refetch with cached off data without claiming an off action was saved', async () => {
    vi.useFakeTimers()
    const cached = state({})
    get.mockResolvedValueOnce(cached)
    const { queryClient } = renderWithProviders(<RemoteControlCard />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(queryClient.getQueryState(['remote-control'])?.status).toBe('success')
    expect(queryClient.getQueryData(['remote-control'])).toEqual(cached)
    expect(screen.queryByTestId('remote-control-card')).toBeNull()

    const error = new ApiError(502, 'bad gateway')
    get.mockRejectedValue(error)
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: ['remote-control'] })
      await vi.advanceTimersByTimeAsync(0)
    })

    expect(queryClient.getQueryState(['remote-control'])?.error).toBe(error)
    expect(queryClient.getQueryData(['remote-control'])).toEqual(cached)
    expect(screen.getByRole('alert')).toHaveTextContent("Couldn't read the Remote Control setting")
    expect(screen.getByRole('alert')).toHaveTextContent('bad gateway')
    expect(screen.queryByRole('status')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Turn off' })).toBeNull()
  })

  it('keeps a non-owner refusal quiet on a refresh too', async () => {
    get.mockResolvedValueOnce(state({ requested: true, running: true }))
    const { queryClient } = renderWithProviders(<RemoteControlCard />)
    expect(await screen.findByText(/Remote Control is on/)).toBeInTheDocument()
    get.mockRejectedValue(new ApiError(403, 'owner only'))
    await queryClient.refetchQueries({ queryKey: ['remote-control'] })
    expect(screen.queryByText(/Couldn't read the Remote Control setting/)).toBeNull()
  })

  it('turns it off in one click and says it stays off', async () => {
    get.mockResolvedValue(state({ requested: true, running: true }))
    put.mockResolvedValue(state({}))
    renderWithProviders(<RemoteControlCard />)
    expect(await screen.findByText(/Remote Control is on/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Turn off' }))

    await waitFor(() => expect(put).toHaveBeenCalledWith())
    const done = await screen.findByRole('status')
    expect(done).toHaveTextContent('It stays off until you turn it back on.')
    expect(done).toHaveTextContent('kirocrew remote-control on')
    // The command is inline code, never translated text.
    expect(done.querySelector('code')).toHaveTextContent(/^kirocrew remote-control on$/)
    expect(screen.queryByRole('button', { name: 'Turn off' })).toBeNull()
  })

  it('keeps the off confirmation when a poll started before the off answers late', async () => {
    get.mockResolvedValue(state({ requested: true, running: true }))
    let answerPoll: (s: RemoteControlState) => void = () => {}
    put.mockResolvedValue(state({}))
    const { queryClient } = renderWithProviders(<RemoteControlCard />)
    expect(await screen.findByText(/Remote Control is on/)).toBeInTheDocument()

    // A poll is in flight, holding the pre-off read.
    get.mockImplementationOnce(() => new Promise((resolve) => { answerPoll = resolve }))
    void queryClient.refetchQueries({ queryKey: ['remote-control'] })
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2))

    fireEvent.click(screen.getByRole('button', { name: 'Turn off' }))
    await screen.findByText(/It stays off until you turn it back on/)
    await act(async () => {
      answerPoll(state({ requested: true, running: true }))
    })
    // The stale poll was cancelled, so the PUT's answer stays in the cache.
    expect(queryClient.getQueryData(['remote-control'])).toMatchObject({ requested: false })
    expect(screen.getByText(/It stays off until you turn it back on/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Turn off' })).toBeNull()
  })

  it('says before the click that devices disconnect and turning it on needs the terminal', async () => {
    get.mockResolvedValue(state({ requested: true, running: true }))
    renderWithProviders(<RemoteControlCard />)
    const effect = await screen.findByTestId('remote-control-off-effect')
    expect(effect).toHaveTextContent(
      'Turning it off disconnects your other devices within a few seconds.',
    )
    expect(effect).toHaveTextContent('To turn it back on later, run')
    expect(effect).toHaveTextContent('in a terminal on the machine running Kiro Crew, then restart Kiro Crew.')
    expect(effect.querySelector('code')).toHaveTextContent(/^kirocrew remote-control on$/)
  })

  it('offers a copy button for the turn-on command, before and after the click', async () => {
    get.mockResolvedValue(state({ requested: true, running: true }))
    put.mockResolvedValue(state({}))
    renderWithProviders(<RemoteControlCard />)
    const effect = await screen.findByTestId('remote-control-off-effect')
    fireEvent.click(within(effect).getByRole('button', { name: 'Copy command' }))
    await waitFor(() => expect(copyCode).toHaveBeenCalledWith('kirocrew remote-control on'))

    fireEvent.click(screen.getByRole('button', { name: 'Turn off' }))
    const done = await screen.findByRole('status')
    expect(within(done).getByRole('button', { name: 'Copy command' })).toBeInTheDocument()
  })

  it('does not claim devices disconnect when the relay is not running', async () => {
    get.mockResolvedValue(state({ requested: true, running: false }))
    renderWithProviders(<RemoteControlCard />)
    const effect = await screen.findByTestId('remote-control-off-effect')
    expect(effect).not.toHaveTextContent('disconnects')
    expect(effect).toHaveTextContent('in a terminal on the machine running Kiro Crew, then restart Kiro Crew.')
  })

  it('shows a read failure instead of hiding the card', async () => {
    get.mockRejectedValue(new Error('gateway unreachable'))
    renderWithProviders(<RemoteControlCard />)
    expect(await screen.findByText(/Couldn't read the Remote Control setting/)).toBeInTheDocument()
  })

  it('stays hidden for a non-owner, whom the route refuses by design', async () => {
    get.mockRejectedValue(new ApiError(403, 'owner only'))
    renderWithProviders(<RemoteControlCard />)
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(screen.queryByText(/Couldn't read the Remote Control setting/)).toBeNull()
  })

  it('shows the error when turning it off fails', async () => {
    get.mockResolvedValue(state({ requested: true, running: true }))
    put.mockRejectedValue(new Error('gateway unreachable'))
    renderWithProviders(<RemoteControlCard />)
    fireEvent.click(await screen.findByRole('button', { name: 'Turn off' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('gateway unreachable')
  })
})
