import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { KiroSignInCard, KIRO_SIGN_IN_SETTING_ID, KIRO_SIGN_IN_SETTINGS_TAB } from './KiroSignInCard'
import { SETTINGS_MANUAL } from '../../components/commandPalette/settingsManual'
import { api, type KasLoginStatus } from '../../api/client'

vi.mock('../../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kasLoginStatus: vi.fn(),
      kasLoginLogout: vi.fn().mockResolvedValue({ ok: true }),
      kasLoginBeginDevice: vi.fn().mockResolvedValue({
        login_id: 'login-1',
        user_code: 'ABCD-EFGH',
        verification_uri_complete: 'https://example.invalid/device?user_code=ABCD-EFGH',
        expires_at: '2099-01-01T00:00:00Z',
      }),
      kasLoginPoll: vi.fn().mockResolvedValue({ status: 'pending' }),
      kasLoginBeginLoopback: vi.fn(),
      kasLoginCancel: vi.fn().mockResolvedValue({ ok: true }),
    },
  }
})

const kasLoginStatus = vi.mocked(api.kasLoginStatus)
const kasLoginLogout = vi.mocked(api.kasLoginLogout)
const kasLoginBeginDevice = vi.mocked(api.kasLoginBeginDevice)

const SIGNED_OUT: KasLoginStatus = {
  authenticated: false,
  provider: '',
  identity: '',
  transport: 'device',
  expires_at: null,
  expired: false,
  has_refresh_token: false,
  refresh_rejected: false,
  usable: false,
}

const SIGNED_IN: KasLoginStatus = {
  authenticated: true,
  provider: 'Google',
  identity: 'social',
  transport: 'device',
  expires_at: '2099-01-01T00:00:00Z',
  expired: false,
  has_refresh_token: true,
  refresh_rejected: false,
  usable: true,
}

describe('KiroSignInCard', () => {
  beforeEach(() => {
    kasLoginStatus.mockReset()
    kasLoginLogout.mockClear()
    kasLoginBeginDevice.mockClear()
  })

  it('is the deep-link target the registry and the chat error row agree on', () => {
    const entry = SETTINGS_MANUAL.find(e => e.id === KIRO_SIGN_IN_SETTING_ID)
    expect(entry).toBeTruthy()
    expect(entry?.tab).toBe(KIRO_SIGN_IN_SETTINGS_TAB)
    // The registry label key is the one the card renders as its title AND as
    // its `data-setting-label` anchor, so useSettingHighlight finds the card.
    expect(entry?.labelKey).toBe('pages.settings.kiroSignInCard.title')
  })

  it('shows a loader while the first status read is in flight', () => {
    kasLoginStatus.mockReturnValue(new Promise(() => {}))
    renderWithProviders(<KiroSignInCard />)
    expect(screen.getByTestId('kiro-sign-in-pending')).toBeInTheDocument()
    expect(screen.getByTestId('kiro-sign-in-card')).toHaveAttribute('data-setting-label', 'Kiro sign-in')
  })

  it('signed out: renders the embedded chooser with the intro sentence, not a full-screen gate', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    renderWithProviders(<KiroSignInCard />)
    expect(await screen.findByTestId('kas-login-card-intro')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Continue with Google' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Continue with GitHub' })).toBeInTheDocument()
    // Embedded chrome: a region, no scrim/main and no page headline.
    expect(screen.getByTestId('kas-login-embedded')).toBeInTheDocument()
    expect(screen.queryByRole('main')).toBeNull()
    expect(screen.queryByRole('heading', { level: 1 })).toBeNull()
    expect(screen.queryByTestId('kiro-sign-in-summary')).toBeNull()
  })

  it('signed out: picking a provider starts the device flow inside the card', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    renderWithProviders(<KiroSignInCard />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with GitHub' }))
    expect(await screen.findByTestId('kas-login-user-code')).toHaveTextContent('ABCD-EFGH')
    expect(kasLoginBeginDevice).toHaveBeenCalledWith('github', undefined)
    expect(screen.queryByRole('main')).toBeNull()
  })

  it('signed in: shows a token-free summary with sign-out, and sign-out calls the API with the slot', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    renderWithProviders(<KiroSignInCard />)
    const summary = await screen.findByTestId('kiro-sign-in-summary')
    expect(summary).toHaveAttribute('data-lapsed', 'false')
    expect(screen.getByTestId('kiro-sign-in-state')).toHaveTextContent('Signed in with Google')
    expect(screen.getByTestId('kiro-sign-in-expiry')).toHaveTextContent('Access token expires')
    // Never a credential, under any name.
    expect(summary.textContent).not.toMatch(/token:|at-|rt-/)
    expect(screen.queryByTestId('kas-login-card-intro')).toBeNull()

    // From here the gateway answers "signed out": set before the press so the
    // refetch the sign-out triggers cannot race the mock swap.
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    fireEvent.click(screen.getByTestId('kiro-sign-in-logout'))
    await waitFor(() => expect(kasLoginLogout).toHaveBeenCalledWith('social'))
    // The status re-read now says signed out, which unmounts the summary; the
    // "applies to processes started from now on" note lives on the card, so
    // it is still there beside the chooser the user now sees.
    expect(await screen.findByTestId('kas-login-card-intro')).toBeInTheDocument()
    expect(screen.getByTestId('kiro-sign-in-takes-effect')).toBeInTheDocument()
  })

  it('signed in without a refresh token: does not promise an automatic renewal', async () => {
    kasLoginStatus.mockResolvedValue({ ...SIGNED_IN, has_refresh_token: false })
    renderWithProviders(<KiroSignInCard />)
    const summary = await screen.findByTestId('kiro-sign-in-summary')
    expect(summary).toHaveAttribute('data-lapsed', 'false')
    expect(summary).toHaveTextContent('cannot renew itself')
    expect(summary).not.toHaveTextContent('renewed automatically')
  })

  it('lapsed (issuer refused the refresh): says the sign-in expired and offers sign in again, not a silent fallback', async () => {
    kasLoginStatus.mockResolvedValue({ ...SIGNED_IN, expired: true, refresh_rejected: true })
    renderWithProviders(<KiroSignInCard />)
    const summary = await screen.findByTestId('kiro-sign-in-summary')
    expect(summary).toHaveAttribute('data-lapsed', 'true')
    expect(screen.getByTestId('kiro-sign-in-state')).toHaveTextContent('Sign-in expired')
    expect(screen.getByTestId('kiro-sign-in-expiry')).toHaveTextContent('Kiro refused to renew this sign-in.')
    expect(summary).toHaveTextContent('nothing falls back to a kiro-cli login on its own')
    expect(screen.getByRole('button', { name: 'Sign in again' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Switch account' })).toBeNull()
  })

  it('lapsed (expired, nothing to renew it): the spawn predicate drives the state', async () => {
    kasLoginStatus.mockResolvedValue({
      ...SIGNED_IN,
      expired: true,
      has_refresh_token: false,
      usable: false,
    })
    renderWithProviders(<KiroSignInCard />)
    expect(await screen.findByTestId('kiro-sign-in-summary')).toHaveAttribute('data-lapsed', 'true')
    expect(screen.getByTestId('kiro-sign-in-expiry')).toHaveTextContent('nothing to renew it with')
  })

  it('sign in again opens the chooser over the stored identity and can be backed out of', async () => {
    kasLoginStatus.mockResolvedValue({ ...SIGNED_IN, refresh_rejected: true })
    renderWithProviders(<KiroSignInCard />)
    fireEvent.click(await screen.findByRole('button', { name: 'Sign in again' }))
    expect(await screen.findByRole('button', { name: 'Continue with Google' })).toBeInTheDocument()
    // The stored credential is untouched while the chooser is up: no logout call.
    expect(kasLoginLogout).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('kiro-sign-in-keep-current'))
    expect(await screen.findByTestId('kiro-sign-in-summary')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Continue with Google' })).toBeNull()
  })

  it('switching account names the slot being replaced, so the gateway can retire it once the new sign-in lands', async () => {
    kasLoginStatus.mockResolvedValue({ ...SIGNED_IN, identity: 'identity_center', provider: 'Enterprise' })
    renderWithProviders(<KiroSignInCard />)
    fireEvent.click(await screen.findByRole('button', { name: 'Switch account' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with GitHub' }))
    await waitFor(() =>
      expect(kasLoginBeginDevice).toHaveBeenCalledWith('github', { replaces: 'identity_center' }),
    )
    // Nothing is removed from the client side: the gateway does it after persist.
    expect(kasLoginLogout).not.toHaveBeenCalled()
  })

  it('keep-current is disabled while a begin is in flight, so a cancel cannot race the poll that persists the switch', async () => {
    kasLoginStatus.mockResolvedValue({ ...SIGNED_IN, identity: 'identity_center', provider: 'Enterprise' })
    // A begin that never settles: the window in which "keep current" must not act.
    kasLoginBeginDevice.mockReturnValueOnce(new Promise(() => {}))
    renderWithProviders(<KiroSignInCard />)
    fireEvent.click(await screen.findByRole('button', { name: 'Switch account' }))
    const keep = (await screen.findByTestId('kiro-sign-in-keep-current')) as HTMLButtonElement
    expect(keep.disabled).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Continue with GitHub' }))
    await waitFor(() => expect(keep.disabled).toBe(true))
    fireEvent.click(keep)
    // Still on the chooser over the stored identity: the cancel did not fire.
    expect(screen.queryByTestId('kiro-sign-in-summary')).toBeNull()
  })

  it('a switch whose previous slot could not be removed is reported, not swallowed', async () => {
    kasLoginStatus.mockResolvedValue({ ...SIGNED_IN, identity: 'identity_center', provider: 'Enterprise' })
    vi.mocked(api.kasLoginPoll).mockResolvedValue({
      status: 'authorized',
      code: 'previous_identity_not_removed',
    })
    renderWithProviders(<KiroSignInCard />)
    fireEvent.click(await screen.findByRole('button', { name: 'Switch account' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with GitHub' }))
    // The poll answers authorized-with-code; the flow returns to the signed-in
    // view (the store still resolves the old account) with the failure on top.
    const warning = await screen.findByTestId('kas-login-replace-warning')
    expect(warning).toHaveTextContent('previous sign-in could not be removed')
    expect(await screen.findByTestId('kiro-sign-in-summary')).toBeInTheDocument()
    vi.mocked(api.kasLoginPoll).mockResolvedValue({ status: 'pending' })
  })

  it('the embedded chrome hands failures to the agent where the gate cannot', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    kasLoginBeginDevice.mockRejectedValueOnce(new Error('auth service unreachable'))
    renderWithProviders(<KiroSignInCard />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with GitHub' }))
    const notice = await screen.findByTestId('kas-login-begin-error')
    expect(notice).toHaveTextContent('auth service unreachable')
    // ErrorNotice renders its hand-off control only when askAgent is on.
    expect(
      notice.querySelector('button[title="Open a chat with this error\'s full context attached"]'),
    ).not.toBeNull()
  })
})
