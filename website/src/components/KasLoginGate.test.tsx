import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { onTestFinished } from 'vitest'
import { renderWithProviders } from '../test/helpers'
import KasLoginGate, { KasLoginEmbedded } from './KasLoginGate'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { COMPANY_SIGN_IN_URL_KEY } from '../utils/companySignIn'
import { fmtTime } from '../i18n/format'

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
      screen.getByRole('heading', { name: 'Finish signing in with your browser' }),
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

  it('Start over after an expired code begins a fresh code for the same sign-in in one click', async () => {
    kasLoginPoll.mockResolvedValue({ status: 'expired' })
    renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with company SSO' }))
    fireEvent.change(await screen.findByLabelText('Company sign-in URL'), {
      target: { value: 'https://acme.awsapps.com/start' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByText('The code expired')).toBeInTheDocument()

    kasLoginPoll.mockResolvedValue({ status: 'pending' })
    kasLoginBeginDevice.mockResolvedValueOnce({
      login_id: 'login-2',
      user_code: 'WXYZ-2345',
      verification_uri_complete: 'https://app.kiro.dev/account/device?user_code=WXYZ-2345',
      expires_at: '2099-01-01T00:00:00Z',
    })
    fireEvent.click(screen.getByRole('button', { name: 'Start over' }))
    // One click to the new code, with the company URL sent again: no provider
    // to pick and no form to fill twice.
    expect(await screen.findByTestId('kas-login-user-code')).toHaveTextContent('WXYZ-2345')
    expect(kasLoginCancel).toHaveBeenCalledWith('login-1')
    expect(kasLoginBeginDevice).toHaveBeenCalledTimes(2)
    expect(kasLoginBeginDevice).toHaveBeenLastCalledWith('idc', {
      start_url: 'https://acme.awsapps.com/start',
    })
  })

  it('Start over during an account switch begins a fresh code although the old sign-in is still stored', async () => {
    // The account being replaced stays in the vault until a new one lands, so
    // the status read after the cancel still says signed in -- as the same account.
    kasLoginStatus.mockResolvedValue({
      authenticated: true,
      provider: 'Google',
      identity: 'social',
      transport: 'device',
      expires_at: '2099-02-01T00:00:00Z',
    })
    kasLoginPoll.mockResolvedValue({ status: 'expired' })
    renderWithProviders(
      <KasLoginEmbedded
        renderAuthenticated={(_status, { reauth }) => (
          <button type="button" onClick={reauth}>
            Switch account
          </button>
        )}
      />,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Switch account' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with GitHub' }))
    expect(await screen.findByText('The code expired')).toBeInTheDocument()

    kasLoginPoll.mockResolvedValue({ status: 'pending' })
    kasLoginBeginDevice.mockResolvedValueOnce({
      login_id: 'login-2',
      user_code: 'WXYZ-2345',
      verification_uri_complete: 'https://app.kiro.dev/account/device?user_code=WXYZ-2345',
      expires_at: '2099-01-01T00:00:00Z',
    })
    fireEvent.click(screen.getByRole('button', { name: 'Start over' }))
    expect(await screen.findByTestId('kas-login-user-code')).toHaveTextContent('WXYZ-2345')
    expect(kasLoginCancel).toHaveBeenCalledWith('login-1')
    expect(kasLoginBeginDevice).toHaveBeenCalledTimes(2)
    expect(kasLoginBeginDevice).toHaveBeenLastCalledWith('github', { replaces: 'social' })
  })

  it('Start over during an account switch begins a fresh code although the old token was refreshed meanwhile', async () => {
    const signedIn = {
      authenticated: true,
      provider: 'Google',
      identity: 'social',
      transport: 'device' as const,
      expires_at: '2099-02-01T00:00:00Z',
    }
    kasLoginStatus.mockResolvedValue(signedIn)
    kasLoginPoll.mockResolvedValue({ status: 'expired' })
    renderWithProviders(
      <KasLoginEmbedded
        renderAuthenticated={(_status, { reauth }) => (
          <button type="button" onClick={reauth}>
            Switch account
          </button>
        )}
      />,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Switch account' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with GitHub' }))
    expect(await screen.findByText('The code expired')).toBeInTheDocument()

    // The gateway refreshed the old account while the code was out: same
    // account, a later expiry than the cached status read.
    kasLoginStatus.mockResolvedValue({ ...signedIn, expires_at: '2099-02-01T01:00:00Z' })
    kasLoginPoll.mockResolvedValue({ status: 'pending' })
    kasLoginBeginDevice.mockResolvedValueOnce({
      login_id: 'login-2',
      user_code: 'WXYZ-2345',
      verification_uri_complete: 'https://app.kiro.dev/account/device?user_code=WXYZ-2345',
      expires_at: '2099-01-01T00:00:00Z',
    })
    fireEvent.click(screen.getByRole('button', { name: 'Start over' }))
    expect(await screen.findByTestId('kas-login-user-code')).toHaveTextContent('WXYZ-2345')
    expect(kasLoginBeginDevice).toHaveBeenCalledTimes(2)
    expect(kasLoginBeginDevice).toHaveBeenLastCalledWith('github', { replaces: 'social' })
  })

  it('an expired code can still switch to a different sign-in', async () => {
    kasLoginPoll.mockResolvedValue({ status: 'expired' })
    renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with Google' }))
    expect(await screen.findByText('The code expired')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Use a different sign-in' }))
    expect(
      await screen.findByRole('button', { name: 'Continue with GitHub' }),
    ).toBeInTheDocument()
    expect(kasLoginBeginDevice).toHaveBeenCalledTimes(1)
  })

  it('Start over on a failed sign-in whose cancel fails says so and starts nothing', async () => {
    kasLoginPoll.mockRejectedValue(new Error('No pending login with that id.'))
    kasLoginCancel.mockRejectedValueOnce(new Error('network down'))
    renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with Google' }))
    expect(await screen.findByText('Sign-in failed')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Start over' }))
    // On the failure screen itself: the poll's re-read after the settle can
    // briefly show the waiting screen, which has its own copy of the notice.
    await waitFor(() => {
      expect(screen.getByText('Sign-in failed')).toBeInTheDocument()
      expect(screen.getByTestId('kas-login-settle-error')).toHaveTextContent(
        'Couldn’t cancel the sign-in',
      )
    })
    expect(kasLoginBeginDevice).toHaveBeenCalledTimes(1)
  })

  it('the code screen says when its code expires', async () => {
    renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with Google' }))
    const note = await screen.findByTestId('kas-login-code-expiry')
    expect(note.textContent).toBe(`Code valid until ${fmtTime('2099-01-01T00:00:00Z')}`)
    // vitest pins TZ=UTC, so the begin answer's instant reads as midnight.
    expect(note.textContent).toContain('12:00')
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

  it('renders the verification URL as a link that opens in a new tab', async () => {
    renderWithProviders(<KasLoginGate />)

    fireEvent.click(await screen.findByRole('button', { name: 'Continue with Google' }))
    const url = 'https://app.kiro.dev/account/device?user_code=ABCD-EFGH'
    const link = await screen.findByRole('link', { name: url })
    expect(link).toHaveAttribute('href', url)
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    // Copying stays available for approving on another device.
    expect(screen.getByRole('button', { name: 'Copy link' })).toBeInTheDocument()
  })

  it('shows a verification URL with a non-http scheme as text, never as a link', async () => {
    kasLoginBeginDevice.mockResolvedValueOnce({
      login_id: 'login-1',
      user_code: 'ABCD-EFGH',
      verification_uri_complete: 'javascript:alert(1)',
      expires_at: '2099-01-01T00:00:00Z',
    })
    renderWithProviders(<KasLoginGate />)

    fireEvent.click(await screen.findByRole('button', { name: 'Continue with Google' }))
    expect(await screen.findByText('javascript:alert(1)')).toBeInTheDocument()
    expect(screen.queryByTestId('kas-login-verification-link')).not.toBeInTheDocument()
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

  it('cancels a pending sign-in and stops its poll once the account signs in elsewhere', async () => {
    // Installed before the render, so the poll's cadence can be stepped past below.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    onTestFinished(() => {
      vi.useRealTimers()
    })
    const { queryClient } = renderWithProviders(
      <KasLoginGate>
        <div data-testid="app-root" />
      </KasLoginGate>,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with Google' }))
    await screen.findByTestId('kas-login-user-code')
    await waitFor(() => expect(kasLoginPoll).toHaveBeenCalledWith('login-1'))

    // Another tab or the CLI finished a sign-in: the next status read says so.
    kasLoginStatus.mockResolvedValue({
      authenticated: true,
      provider: 'Google',
      identity: 'social',
      transport: 'device',
    })
    await act(() => queryClient.invalidateQueries({ queryKey: ['kas-login'] }))
    expect(await screen.findByTestId('app-root')).toBeInTheDocument()
    await waitFor(() => expect(kasLoginCancel).toHaveBeenCalledWith('login-1'))

    const polls = kasLoginPoll.mock.calls.length
    await act(() => vi.advanceTimersByTimeAsync(15_000))
    expect(kasLoginPoll).toHaveBeenCalledTimes(polls)
    expect(kasLoginCancel).toHaveBeenCalledTimes(1)
  })

  it('says so in the signed-in view when that cancel fails, and retries it from there', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    onTestFinished(() => {
      vi.useRealTimers()
    })
    const { queryClient } = renderWithProviders(
      <KasLoginGate>
        <div data-testid="app-root" />
      </KasLoginGate>,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with Google' }))
    await screen.findByTestId('kas-login-user-code')
    await waitFor(() => expect(kasLoginPoll).toHaveBeenCalledWith('login-1'))

    kasLoginCancel.mockRejectedValueOnce(new Error('network down'))
    kasLoginStatus.mockResolvedValue({
      authenticated: true,
      provider: 'Google',
      identity: 'social',
      transport: 'device',
    })
    await act(() => queryClient.invalidateQueries({ queryKey: ['kas-login'] }))
    const notice = await screen.findByTestId('kas-login-cancel-unsettled')
    expect(screen.getByTestId('app-root')).toBeInTheDocument()

    // The dropped session stays dropped: an unsettled cancel does not resume its poll.
    const polls = kasLoginPoll.mock.calls.length
    await act(() => vi.advanceTimersByTimeAsync(15_000))
    expect(kasLoginPoll).toHaveBeenCalledTimes(polls)

    fireEvent.click(within(notice).getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(kasLoginCancel).toHaveBeenCalledTimes(2))
    expect(kasLoginCancel).toHaveBeenNthCalledWith(2, 'login-1')
    // The retry settled: its status read came after the second cancel.
    await waitFor(() =>
      expect(kasLoginStatus.mock.invocationCallOrder.at(-1)).toBeGreaterThan(
        kasLoginCancel.mock.invocationCallOrder[1],
      ),
    )
    expect(screen.queryByTestId('kas-login-cancel-unsettled')).not.toBeInTheDocument()
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

  it('company SSO sends a start URL pasted without https:// for the gateway to complete', async () => {
    renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with company SSO' }))
    await screen.findByTestId('kas-login-sso-form')
    const field = screen.getByLabelText('Company sign-in URL') as HTMLInputElement
    fireEvent.change(field, { target: { value: 'acme.awsapps.com/start' } })
    // The browser's own URL check must not refuse the paste before the gateway
    // can add the scheme.
    expect(field.checkValidity()).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByTestId('kas-login-user-code')).toHaveTextContent('ABCD-EFGH')
    expect(kasLoginBeginDevice).toHaveBeenCalledWith('idc', {
      start_url: 'acme.awsapps.com/start',
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

  it('company SSO form remembers the submitted URL and region for the next visit', async () => {
    const first = renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with company SSO' }))
    await screen.findByTestId('kas-login-sso-form')
    fireEvent.change(screen.getByLabelText('Company sign-in URL'), {
      target: { value: ' https://acme.awsapps.com/start ' },
    })
    fireEvent.change(screen.getByLabelText('AWS Region (optional)'), {
      target: { value: 'eu-west-1' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByTestId('kas-login-user-code')).toHaveTextContent('ABCD-EFGH')
    first.unmount()

    // A reload or gateway restart mounts the chooser again; the fields are filled.
    renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with company SSO' }))
    await screen.findByTestId('kas-login-sso-form')
    expect(screen.getByLabelText('Company sign-in URL')).toHaveValue('https://acme.awsapps.com/start')
    expect(screen.getByLabelText('AWS Region (optional)')).toHaveValue('eu-west-1')
    expect(screen.getByRole('button', { name: 'Continue' })).toBeEnabled()
  })

  it('company SSO normalizes a remembered scheme-less URL before submitting', async () => {
    localStorage.setItem(COMPANY_SIGN_IN_URL_KEY, 'example.awsapps.com/start')
    renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with company SSO' }))
    await screen.findByTestId('kas-login-sso-form')
    const input = screen.getByLabelText('Company sign-in URL') as HTMLInputElement
    expect(input).toHaveValue('https://example.awsapps.com/start')
    expect(input.checkValidity()).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByTestId('kas-login-user-code')).toHaveTextContent('ABCD-EFGH')
    expect(kasLoginBeginDevice).toHaveBeenCalledWith('idc', {
      start_url: 'https://example.awsapps.com/start',
    })
  })

  it('company SSO form keeps nothing that was only typed, never submitted', async () => {
    const first = renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with company SSO' }))
    await screen.findByTestId('kas-login-sso-form')
    fireEvent.change(screen.getByLabelText('Company sign-in URL'), {
      target: { value: 'https://half.awsapps.com/start' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    first.unmount()

    renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with company SSO' }))
    await screen.findByTestId('kas-login-sso-form')
    expect(screen.getByLabelText('Company sign-in URL')).toHaveValue('')
  })

  it.each([
    [400, 'invalid_region', 'Check the AWS Region. A Region name looks like us-east-1.', ['AWS Region (optional)']],
    [400, 'invalid_start_url', 'Check the company sign-in URL.', ['Company sign-in URL']],
    [
      400,
      'start_url_or_region_rejected',
      'Check the company sign-in URL and the AWS Region.',
      ['Company sign-in URL', 'AWS Region (optional)'],
    ],
    [502, 'auth_service_unreachable', 'Check your connection', []],
  ])('company SSO: a %i %s begin error names the field to fix, not the connection', async (status, code, headline, fields) => {
    kasLoginBeginDevice.mockRejectedValueOnce(
      new ApiError(status, 'gateway detail', JSON.stringify({ error: 'gateway detail', code })),
    )
    renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with company SSO' }))
    await screen.findByTestId('kas-login-sso-form')
    fireEvent.change(screen.getByLabelText('Company sign-in URL'), {
      target: { value: 'https://acme.awsapps.com/strat' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(headline)
    expect(alert).toHaveTextContent('gateway detail')
    if (fields.length) expect(alert).not.toHaveTextContent('Check your connection')
    // Only the named fields are invalid, and they point at the error that says why.
    for (const label of ['Company sign-in URL', 'AWS Region (optional)']) {
      const input = screen.getByLabelText(label)
      if (fields.includes(label)) {
        expect(input).toHaveAttribute('aria-invalid', 'true')
        expect(input).toHaveAttribute('aria-describedby', alert.id)
      } else {
        expect(input).not.toHaveAttribute('aria-invalid')
        expect(input).not.toHaveAttribute('aria-describedby')
      }
    }
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

  it('after 30 s the browser wait says the sign-in is not finished, and Start over begins it again', async () => {
    loopbackShape()
    // Installed before the render: the 30 s timer is armed when the wait screen
    // mounts. shouldAdvanceTime keeps findBy/waitFor running on the fake clock.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    onTestFinished(() => {
      vi.useRealTimers()
    })
    renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with Google' }))
    await screen.findByRole('heading', { name: 'Waiting for your browser' })
    expect(screen.queryByTestId('kas-login-loopback-slow')).not.toBeInTheDocument()

    await act(() => vi.advanceTimersByTimeAsync(30_000))
    const notice = await screen.findByTestId('kas-login-loopback-slow')
    expect(notice).toHaveTextContent("The sign-in hasn't finished yet")
    kasLoginBeginLoopback.mockResolvedValueOnce({
      login_id: 'lb-2',
      user_code: '',
      verification_uri_complete: 'https://app.kiro.dev/signin?state=s2',
      expires_at: '2099-01-01T00:00:00Z',
      auth_url: 'https://app.kiro.dev/signin?state=s2',
      port: 3128,
    })
    fireEvent.click(within(notice).getByRole('button', { name: 'Start over' }))

    // The old listener is released before the new one starts, then the new
    // attempt's tab opens for the same provider and its wait starts afresh.
    await waitFor(() => expect(openSpy).toHaveBeenCalledWith(
      'https://app.kiro.dev/signin?state=s2',
      '_blank',
      'noopener,noreferrer',
    ))
    expect(kasLoginBeginLoopback).toHaveBeenCalledTimes(2)
    expect(kasLoginBeginLoopback).toHaveBeenLastCalledWith('google')
    expect(kasLoginCancel).toHaveBeenCalledWith('lb-1')
    expect(kasLoginCancel.mock.invocationCallOrder[0]).toBeLessThan(
      kasLoginBeginLoopback.mock.invocationCallOrder[1],
    )
    // Waited as one state: the old wait screen can still be committed when the
    // new tab opens, and the new one replaces it a render later.
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Waiting for your browser' })).toBeInTheDocument()
      expect(screen.queryByTestId('kas-login-loopback-slow')).not.toBeInTheDocument()
    })
    expect(kasLoginBeginDevice).not.toHaveBeenCalled()
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

  async function companySsoPollAnswers(code: string) {
    kasLoginPoll.mockResolvedValue({ status: 'error', code })
    renderWithProviders(<KasLoginGate />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with company SSO' }))
    fireEvent.change(await screen.findByLabelText('Company sign-in URL'), {
      target: { value: 'https://acme.awsapps.com/start' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  }

  // A coded failure: one "Sign-in failed" headline, and the sentence announced
  // once, inside the shared error notice.
  async function expectCodedProblem(sentence: string) {
    expect(await screen.findByRole('heading', { level: 1, name: 'Sign-in failed' })).toBeInTheDocument()
    const notice = screen.getByTestId('kas-login-problem-detail')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(within(notice).getByText(sentence)).toBeInTheDocument()
    expect(screen.getAllByText('Sign-in failed')).toHaveLength(1)
    expect(screen.getAllByText(sentence)).toHaveLength(1)
  }

  it('says plainly that an account with no Kiro profile cannot run chats', async () => {
    await companySsoPollAnswers('no_kiro_profile')
    await expectCodedProblem(
      "This account has no Kiro profile, so it can't run chats. Sign in with an account that has a Kiro subscription.",
    )
    // The provider list stays one click away for switching method.
    fireEvent.click(screen.getByRole('button', { name: 'Use a different sign-in' }))
    expect(
      await screen.findByRole('button', { name: 'Continue with company SSO' }),
    ).toBeInTheDocument()
  })

  it('a failed profile lookup offers Start over and never says there is no profile', async () => {
    await companySsoPollAnswers('profile_lookup_failed')
    await expectCodedProblem("We couldn't look up your Kiro profile. Start over to try again.")
    expect(screen.queryByText(/no Kiro profile/)).not.toBeInTheDocument()
    // Start over tries the same company sign-in again, start URL included.
    fireEvent.click(screen.getByRole('button', { name: 'Start over' }))
    await waitFor(() => expect(kasLoginBeginDevice).toHaveBeenCalledTimes(2))
    expect(kasLoginBeginDevice).toHaveBeenLastCalledWith('idc', {
      start_url: 'https://acme.awsapps.com/start',
    })
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
