import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

vi.mock('../../api/client', () => ({
  api: { remoteControl: vi.fn(), setRemoteControl: vi.fn() },
}))

import { api, type RemoteControlState } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { RemoteControlCard } from './RemoteControlCard'

const state = (over: Partial<RemoteControlState>): RemoteControlState => ({
  requested: false,
  allowed: true,
  reason: '',
  notice: '',
  effective: false,
  running: false,
  live: true,
  default_on: false,
  ...over,
})

const get = api.remoteControl as ReturnType<typeof vi.fn>
const put = api.setRemoteControl as ReturnType<typeof vi.fn>

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

  it.each([
    ['api_key', 'signed in to Kiro with an API key'],
    ['external_idp', 'through an external identity provider'],
    ['not_signed_in', 'is not signed in to Kiro'],
    ['unknown_sign_in', 'does not recognise this kind of Kiro sign-in'],
    ['locked', 'managed by this build'],
  ] as const)('says a %s sign-in blocks a setting that is still on', async (reason, text) => {
    get.mockResolvedValue(state({ requested: true, allowed: false, reason }))
    renderWithProviders(<RemoteControlCard />)
    const notice = await screen.findByRole('status')
    expect(notice).toHaveTextContent('Remote Control is set to on, but it is blocked')
    expect(notice).toHaveTextContent(text)
    expect(notice).not.toHaveTextContent('Remote Control is off')
    // The button names what it does: keep the blocked setting off.
    expect(screen.getByRole('button', { name: 'Keep Remote Control off' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Turn off' })).toBeNull()
  })

  it('says on only while the relay is running, and restart-to-connect otherwise', async () => {
    get.mockResolvedValue(state({ requested: true, allowed: true, running: false, live: false }))
    renderWithProviders(<RemoteControlCard />)
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Remote Control is set to on but not connected. Restart',
    )
    expect(screen.queryByText(/Remote Control is on\./)).toBeNull()
    expect(screen.getByRole('button', { name: 'Turn off' })).toBeInTheDocument()
  })

  it('shows a later read failure even while an earlier read is cached', async () => {
    get.mockResolvedValueOnce(state({ requested: true, effective: true, running: true }))
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
    get.mockResolvedValueOnce(state({ requested: true, effective: true, running: true }))
    const { queryClient } = renderWithProviders(<RemoteControlCard />)
    expect(await screen.findByText(/Remote Control is on/)).toBeInTheDocument()
    get.mockRejectedValue(new ApiError(403, 'owner only'))
    await queryClient.refetchQueries({ queryKey: ['remote-control'] })
    expect(screen.queryByText(/Couldn't read the Remote Control setting/)).toBeNull()
  })

  it('turns it off in one click and says it stays off', async () => {
    get.mockResolvedValue(state({ requested: true, effective: true, running: true }))
    put.mockResolvedValue(state({}))
    renderWithProviders(<RemoteControlCard />)
    expect(await screen.findByText(/Remote Control is on/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Turn off' }))

    await waitFor(() => expect(put).toHaveBeenCalledWith(false))
    const done = await screen.findByRole('status')
    expect(done).toHaveTextContent('It stays off until you turn it back on.')
    expect(done).toHaveTextContent('kirocrew remote-control on')
    // The command is inline code, never translated text.
    expect(done.querySelector('code')).toHaveTextContent(/^kirocrew remote-control on$/)
    expect(screen.queryByRole('button', { name: 'Turn off' })).toBeNull()
  })

  it('says before the click that devices disconnect and turning it on needs the terminal', async () => {
    get.mockResolvedValue(state({ requested: true, effective: true, running: true }))
    renderWithProviders(<RemoteControlCard />)
    const effect = await screen.findByTestId('remote-control-off-effect')
    expect(effect).toHaveTextContent(
      'Turning it off disconnects your other devices within a few seconds.',
    )
    expect(effect).toHaveTextContent('Turning it back on needs the terminal command')
    expect(effect.querySelector('code')).toHaveTextContent(/^kirocrew remote-control on$/)
  })

  it('does not claim devices disconnect when the relay is not running', async () => {
    get.mockResolvedValue(state({ requested: true, allowed: false, reason: 'api_key' }))
    renderWithProviders(<RemoteControlCard />)
    const effect = await screen.findByTestId('remote-control-off-effect')
    expect(effect).not.toHaveTextContent('disconnects')
    expect(effect).toHaveTextContent('Turning it back on needs the terminal command')
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
    get.mockResolvedValue(state({ requested: true, effective: true, running: true }))
    put.mockRejectedValue(new Error('gateway unreachable'))
    renderWithProviders(<RemoteControlCard />)
    fireEvent.click(await screen.findByRole('button', { name: 'Turn off' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('gateway unreachable')
  })
})
