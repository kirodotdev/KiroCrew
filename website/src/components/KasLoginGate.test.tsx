import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import KasLoginGate from './KasLoginGate'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'

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
      kasLoginBeginLoopback: vi.fn().mockResolvedValue({
        login_id: 'lb-1',
        user_code: '',
        verification_uri_complete: 'https://app.kiro.dev/signin?state=s1',
        expires_at: '2099-01-01T00:00:00Z',
        auth_url: 'https://app.kiro.dev/signin?state=s1',
        port: 3128,
      }),
      kasLoginCancel: vi.fn().mockResolvedValue({ ok: true }),
    },
  }
})

const kasLoginStatus = vi.mocked(api.kasLoginStatus)
const kasLoginBeginDevice = vi.mocked(api.kasLoginBeginDevice)
const kasLoginBeginLoopback = vi.mocked(api.kasLoginBeginLoopback)
const kasLoginPoll = vi.mocked(api.kasLoginPoll)
const kasLoginCancel = vi.mocked(api.kasLoginCancel)

function loopbackShape() {
  kasLoginStatus.mockResolvedValue({
    authenticated: false,
    provider: null,
    identity: null,
    transport: 'loopback',
  })
}

describe('KasLoginGate', () => {
  let openSpy: ReturnType<typeof vi.spyOn>
  beforeEach(() => {
    kasLoginStatus.mockResolvedValue({
      authenticated: false,
      provider: null,
      identity: null,
      transport: 'device',
    })
    kasLoginBeginDevice.mockClear()
    kasLoginBeginLoopback.mockClear()
    kasLoginCancel.mockClear()
    kasLoginPoll.mockReset()
    kasLoginPoll.mockResolvedValue({ status: 'pending' })
    openSpy = vi.spyOn(window, 'open').mockImplementation(() => null)
  })

  afterEach(() => {
    openSpy.mockRestore()
  })

  it('renders the chooser with all four sign-in options', async () => {
    renderWithProviders(<KasLoginGate />)

    expect(
      await screen.findByRole('button', { name: 'Continue with Google' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Continue with GitHub' })).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Continue with AWS Builder ID' }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Continue with company SSO' }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { name: 'Sign in to Kiro' }),
    ).toBeInTheDocument()
  })

  it('starts the device flow and shows the user code to approve', async () => {
    renderWithProviders(<KasLoginGate />)

    fireEvent.click(await screen.findByRole('button', { name: 'Continue with Google' }))

    expect(await screen.findByTestId('kas-login-user-code')).toHaveTextContent('ABCD-EFGH')
    expect(kasLoginBeginDevice).toHaveBeenCalledWith('google', undefined)
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

  it('starts a Builder ID device flow directly from its button', async () => {
    renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with AWS Builder ID' }))
    expect(await screen.findByTestId('kas-login-user-code')).toHaveTextContent('ABCD-EFGH')
    expect(kasLoginBeginDevice).toHaveBeenCalledWith('builder_id', undefined)
  })

  it('company SSO expands a form and only begins once a start URL is supplied', async () => {
    renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with company SSO' }))
    // The form replaces the button; nothing has begun yet.
    const form = await screen.findByTestId('kas-login-sso-form')
    expect(form).toBeInTheDocument()
    expect(kasLoginBeginDevice).not.toHaveBeenCalled()
    // Empty start URL keeps the submit disabled — no dead-end 400 round-trip.
    const submit = screen.getByRole('button', { name: 'Continue' })
    expect(submit).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Company sign-in URL'), {
      target: { value: '  https://acme.awsapps.com/start  ' },
    })
    expect(submit).toBeEnabled()
    fireEvent.click(submit)
    expect(await screen.findByTestId('kas-login-user-code')).toHaveTextContent('ABCD-EFGH')
    // Trimmed URL travels; the blank region field is omitted, not sent empty.
    expect(kasLoginBeginDevice).toHaveBeenCalledWith('idc', {
      start_url: 'https://acme.awsapps.com/start',
    })
  })

  it('company SSO form sends a supplied region and can be cancelled back to the chooser', async () => {
    renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with company SSO' }))
    await screen.findByTestId('kas-login-sso-form')
    fireEvent.change(screen.getByLabelText('Company sign-in URL'), {
      target: { value: 'https://acme.awsapps.com/start' },
    })
    fireEvent.change(screen.getByLabelText('AWS Region (optional)'), {
      target: { value: 'eu-west-1' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    // Cancel collapses the form without beginning anything and restores the button.
    expect(kasLoginBeginDevice).not.toHaveBeenCalled()
    expect(
      await screen.findByRole('button', { name: 'Continue with company SSO' }),
    ).toBeInTheDocument()
    // Re-open: the form starts fresh; fill both fields and submit.
    fireEvent.click(screen.getByRole('button', { name: 'Continue with company SSO' }))
    fireEvent.change(screen.getByLabelText('Company sign-in URL'), {
      target: { value: 'https://acme.awsapps.com/start' },
    })
    fireEvent.change(screen.getByLabelText('AWS Region (optional)'), {
      target: { value: 'eu-west-1' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByTestId('kas-login-user-code')).toHaveTextContent('ABCD-EFGH')
    expect(kasLoginBeginDevice).toHaveBeenCalledWith('idc', {
      start_url: 'https://acme.awsapps.com/start',
      region: 'eu-west-1',
    })
  })

  // ---- loopback transport ---------------------------------------------------

  it('loopback shape: Google opens the portal tab and shows the browser-wait screen', async () => {
    loopbackShape()
    renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with Google' }))
    expect(
      await screen.findByRole('heading', { name: 'Waiting for your browser' }),
    ).toBeInTheDocument()
    expect(kasLoginBeginLoopback).toHaveBeenCalledWith('google')
    expect(kasLoginBeginDevice).not.toHaveBeenCalled()
    expect(openSpy).toHaveBeenCalledWith(
      'https://app.kiro.dev/signin?state=s1',
      '_blank',
      'noopener,noreferrer',
    )
    // The explicit re-open button covers a blocked popup.
    fireEvent.click(screen.getByRole('button', { name: 'Open the sign-in page again' }))
    expect(openSpy).toHaveBeenCalledTimes(2)
  })

  it('loopback shape: Builder ID still uses the device flow', async () => {
    loopbackShape()
    renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with AWS Builder ID' }))
    expect(await screen.findByTestId('kas-login-user-code')).toHaveTextContent('ABCD-EFGH')
    expect(kasLoginBeginLoopback).not.toHaveBeenCalled()
    expect(kasLoginBeginDevice).toHaveBeenCalledWith('builder_id', undefined)
  })

  it('loopback begin answering loopback_unavailable falls straight back to the device flow', async () => {
    loopbackShape()
    kasLoginBeginLoopback.mockRejectedValueOnce(
      new ApiError(409, 'Loopback sign-in is not available here.', JSON.stringify({ error: 'x', code: 'loopback_unavailable' })),
    )
    renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with GitHub' }))
    expect(await screen.findByTestId('kas-login-user-code')).toHaveTextContent('ABCD-EFGH')
    expect(kasLoginBeginDevice).toHaveBeenCalledWith('github', undefined)
    // The switch is explained, and the 409 is never shown as an error.
    expect(screen.getByTestId('kas-login-fell-back')).toBeInTheDocument()
    expect(screen.queryByText(/loopback_unavailable/)).not.toBeInTheDocument()
  })

  it('loopback timeout degrades to the device flow for the same provider', async () => {
    loopbackShape()
    kasLoginPoll.mockResolvedValue({ status: 'expired', code: 'loopback_timeout' })
    renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with Google' }))
    // Not the expired-code problem screen: the code screen, with the reason.
    expect(await screen.findByTestId('kas-login-user-code')).toHaveTextContent('ABCD-EFGH')
    expect(screen.getByTestId('kas-login-fell-back')).toBeInTheDocument()
    expect(kasLoginBeginDevice).toHaveBeenCalledWith('google', undefined)
    expect(screen.queryByText('The code expired')).not.toBeInTheDocument()
  })

  it('"Use a code instead" cancels the listener and starts the device flow', async () => {
    loopbackShape()
    renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with Google' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Use a code instead' }))
    expect(await screen.findByTestId('kas-login-user-code')).toHaveTextContent('ABCD-EFGH')
    expect(kasLoginCancel).toHaveBeenCalledWith('lb-1')
    expect(kasLoginBeginDevice).toHaveBeenCalledWith('google', undefined)
    // Cancel settles (and status is re-read) BEFORE another login starts, so a
    // credential that landed under the click can never be shadowed by a second one.
    expect(kasLoginCancel.mock.invocationCallOrder[0]).toBeLessThan(
      kasLoginBeginDevice.mock.invocationCallOrder[0],
    )
  })

  it('"Use a code instead" after the redirect already landed signs in instead of restarting', async () => {
    loopbackShape()
    // The production QueryClient never lets queries go stale on their own
    // (freshness comes from server push), so the status re-read after cancel
    // must force a network fetch or it would see the cached signed-out state.
    renderWithProviders(
      <KasLoginGate>
        <div data-testid="app-root" />
      </KasLoginGate>,
      { queryDefaults: { staleTime: Infinity } },
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with Google' }))
    await screen.findByRole('button', { name: 'Use a code instead' })
    // The portal redirect completes and the gateway persists the token while the
    // user is reaching for the code button: the status re-read after cancel sees it.
    kasLoginStatus.mockResolvedValue({
      authenticated: true,
      provider: 'Google',
      identity: 'social',
      transport: 'loopback',
    })
    fireEvent.click(screen.getByRole('button', { name: 'Use a code instead' }))
    expect(await screen.findByTestId('app-root')).toBeInTheDocument()
    expect(kasLoginCancel).toHaveBeenCalledWith('lb-1')
    expect(kasLoginBeginDevice).not.toHaveBeenCalled()
  })

  it('keeps the waiting screen up with its buttons disabled until the cancel settles', async () => {
    loopbackShape()
    // Hold the cancel open so the settling window is observable.
    let releaseCancel: () => void = () => undefined
    kasLoginCancel.mockImplementationOnce(
      () => new Promise((resolve) => (releaseCancel = () => resolve({ ok: true }))),
    )
    renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with Google' }))
    const useCode = await screen.findByRole('button', { name: 'Use a code instead' })
    fireEvent.click(useCode)
    // Still on the waiting screen, every way off it disabled; a repeat click
    // is inert, so no second login can race the one being unwound.
    expect(useCode).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Use a different sign-in' })).toBeDisabled()
    fireEvent.click(useCode)
    expect(kasLoginCancel).toHaveBeenCalledTimes(1)
    expect(kasLoginBeginDevice).not.toHaveBeenCalled()
    releaseCancel()
    expect(await screen.findByTestId('kas-login-user-code')).toHaveTextContent('ABCD-EFGH')
    expect(kasLoginBeginDevice).toHaveBeenCalledTimes(1)
  })

  it('"Use a code instead" does not start a second login when the cancel itself fails', async () => {
    loopbackShape()
    // A cancel the gateway never acknowledged leaves the old listener able to
    // finish, so the user stays on its waiting screen (polling resumed) rather
    // than being offered a second login that would race it for the credential.
    kasLoginCancel.mockRejectedValueOnce(new Error('network down'))
    renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with Google' }))
    const useCode = await screen.findByRole('button', { name: 'Use a code instead' })
    fireEvent.click(useCode)
    await waitFor(() => expect(kasLoginCancel).toHaveBeenCalledWith('lb-1'))
    // Still the loopback waiting screen, re-enabled for another attempt.
    await waitFor(() => expect(useCode).toBeEnabled())
    expect(screen.queryByRole('button', { name: 'Continue with Google' })).not.toBeInTheDocument()
    expect(kasLoginBeginDevice).not.toHaveBeenCalled()
  })

  it('a loopback token-store failure is a real dead end, not a fallback', async () => {
    loopbackShape()
    kasLoginPoll.mockResolvedValue({ status: 'error', code: 'token_store_failed' })
    renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with Google' }))
    expect(
      await screen.findByRole('heading', { name: 'Something went wrong while waiting for approval' }),
    ).toBeInTheDocument()
    expect(screen.getByText('Sign-in failed')).toBeInTheDocument()
    expect(kasLoginBeginDevice).not.toHaveBeenCalled()
  })

  it('a dashboard not served from loopback uses the device flow even on a loopback shape', async () => {
    loopbackShape()
    const original = window.location
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...original, hostname: 'crew.tail1234.ts.net' },
    })
    try {
      renderWithProviders(<KasLoginGate />)
      fireEvent.click(await screen.findByRole('button', { name: 'Continue with Google' }))
      expect(await screen.findByTestId('kas-login-user-code')).toHaveTextContent('ABCD-EFGH')
      expect(kasLoginBeginLoopback).not.toHaveBeenCalled()
      // Not a fallback — the device flow was the intended transport here.
      expect(screen.queryByTestId('kas-login-fell-back')).not.toBeInTheDocument()
    } finally {
      Object.defineProperty(window, 'location', { configurable: true, value: original })
    }
  })
})
