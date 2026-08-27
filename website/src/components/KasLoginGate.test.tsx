import { fireEvent, screen } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import KasLoginGate from './KasLoginGate'
import { api } from '../api/client'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kasLoginStatus: vi.fn().mockResolvedValue({
        authenticated: false,
        provider: null,
        identity: null,
        transport: 'device',
      }),
      kasLoginBeginDevice: vi.fn().mockResolvedValue({
        login_id: 'login-1',
        user_code: 'ABCD-EFGH',
        verification_uri_complete: 'https://app.kiro.dev/account/device?user_code=ABCD-EFGH',
        expires_at: '2099-01-01T00:00:00Z',
      }),
      kasLoginPoll: vi.fn().mockResolvedValue({ status: 'pending' }),
    },
  }
})

const kasLoginStatus = vi.mocked(api.kasLoginStatus)
const kasLoginBeginDevice = vi.mocked(api.kasLoginBeginDevice)

describe('KasLoginGate', () => {
  beforeEach(() => {
    kasLoginStatus.mockResolvedValue({
      authenticated: false,
      provider: null,
      identity: null,
      transport: 'device',
    })
    kasLoginBeginDevice.mockClear()
  })

  it('renders the chooser with the two wired sign-in options', async () => {
    renderWithProviders(<KasLoginGate />)

    expect(
      await screen.findByRole('button', { name: 'Continue with Google' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Continue with GitHub' })).toBeInTheDocument()
    // Unwired providers (backend accepts only google/github) must not render:
    // a visible button whose click can only 400 is a guaranteed dead end.
    expect(
      screen.queryByRole('button', { name: 'Continue with AWS Builder ID' }),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Continue with your work account' }),
    ).not.toBeInTheDocument()
    expect(
      screen.getByRole('heading', { name: 'Sign in to Kiro' }),
    ).toBeInTheDocument()
  })

  it('starts the device flow and shows the user code to approve', async () => {
    renderWithProviders(<KasLoginGate />)

    fireEvent.click(await screen.findByRole('button', { name: 'Continue with Google' }))

    expect(await screen.findByTestId('kas-login-user-code')).toHaveTextContent('ABCD-EFGH')
    expect(kasLoginBeginDevice).toHaveBeenCalledWith('google')
    expect(
      screen.getByRole('heading', { name: 'Finish signing in on your phone or another computer' }),
    ).toBeInTheDocument()
    // Step 1's link is rendered as a copyable block, verbatim.
    expect(
      screen.getByText('https://app.kiro.dev/account/device?user_code=ABCD-EFGH'),
    ).toBeInTheDocument()
  })

  it('renders its children once the gateway reports an active sign-in', async () => {
    kasLoginStatus.mockResolvedValue({
      authenticated: true,
      provider: 'google',
      identity: 'user@example.com',
      transport: 'device',
    })
    renderWithProviders(
      <KasLoginGate>
        <div data-testid="app-root" />
      </KasLoginGate>,
    )

    expect(await screen.findByTestId('app-root')).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Continue with Google' }),
    ).not.toBeInTheDocument()
  })

  it('shows the action-guidance error with backend detail on its own line when begin fails', async () => {
    kasLoginBeginDevice.mockRejectedValueOnce(new Error('HTTP 502 upstream unavailable'))
    renderWithProviders(<KasLoginGate />)

    fireEvent.click(await screen.findByRole('button', { name: 'Continue with Google' }))
    const alert = await screen.findByRole('alert')
    // Guidance line and raw detail are separate elements — the backend detail
    // must never be suffixed onto the connection advice.
    expect(alert).toHaveTextContent('Could not start the sign-in')
    expect(alert).toHaveTextContent('HTTP 502 upstream unavailable')
  })

  it('offers a retry screen when the sign-in status cannot be read', async () => {
    kasLoginStatus.mockRejectedValue(new Error('boom'))
    renderWithProviders(<KasLoginGate />)

    expect(await screen.findByRole('button', { name: 'Check again' })).toBeInTheDocument()
  })

  it('recovers from an expired code back to the chooser via Start over', async () => {
    const kasLoginPoll = vi.mocked(api.kasLoginPoll)
    kasLoginPoll.mockResolvedValue({ status: 'expired' })
    renderWithProviders(<KasLoginGate />)

    fireEvent.click(await screen.findByRole('button', { name: 'Continue with Google' }))
    expect(await screen.findByText('The code expired')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Start over' }))
    expect(
      await screen.findByRole('button', { name: 'Continue with Google' }),
    ).toBeInTheDocument()
    kasLoginPoll.mockResolvedValue({ status: 'pending' })
  })

  it('cancels the device wait back to the chooser', async () => {
    renderWithProviders(<KasLoginGate />)

    fireEvent.click(await screen.findByRole('button', { name: 'Continue with GitHub' }))
    expect(await screen.findByText('ABCD-EFGH')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Use a different sign-in' }))
    expect(
      await screen.findByRole('button', { name: 'Continue with Google' }),
    ).toBeInTheDocument()
  })

  it('copies the verification link and confirms it', async () => {
    renderWithProviders(<KasLoginGate />)

    fireEvent.click(await screen.findByRole('button', { name: 'Continue with Google' }))
    const copyButton = await screen.findByRole('button', { name: 'Copy link' })
    fireEvent.click(copyButton)
    expect(await screen.findByRole('button', { name: 'Copied' })).toBeInTheDocument()
  })

  it('renders its children when a token lands mid-wait', async () => {
    kasLoginStatus.mockResolvedValue({
      authenticated: true,
      provider: 'Google',
      identity: 'social',
      transport: 'device',
    })
    renderWithProviders(
      <KasLoginGate>
        <div>app-content</div>
      </KasLoginGate>,
    )
    expect(await screen.findByText('app-content')).toBeInTheDocument()
  })
})
