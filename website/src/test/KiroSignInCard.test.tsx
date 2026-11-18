import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { KiroSignInCard } from '../pages/developer/KiroSignInCard'
import { KIRO_SIGN_IN_PATH } from '../pages/developer/kiroSignInLink'
import { KIRO_SIGN_IN_HIGHLIGHT_ANCHOR } from '../hooks/useSettingHighlight'
import { SETTINGS_MANUAL } from '../components/commandPalette/settingsManual'
import { KIRO_ACCOUNT_EMAIL_HIDDEN_KEY } from '../components/KiroAccountModal'
import { usePersistedBool } from '../hooks/usePersistedBool'
import { api, type KasLoginStatus } from '../api/client'
import { RELAY_VIEWER_CREWS_KEY } from '../hooks/useRelayViewerCrews'
import { KAS_LOGIN_STATUS_KEY } from '../hooks/useKasLogin'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kasLoginStatus: vi.fn(),
      // No kiro-cli sign-in beside the card's own: the card says nothing extra.
      kasLoginSources: vi.fn().mockResolvedValue({
        active: 'none',
        kiro_crew: { stored: false },
        kiro_cli: { stored: false, usable: false },
        name_pending: false,
        same_account: null,
        can_use_kiro_cli: false,
      }),
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
      // A landed sign-in asks for a fresh credit reading. Refused by default, so a
      // test that does not care keeps the plain re-read fallback and no network.
      sessionsUsageRefresh: vi.fn().mockRejectedValue(new Error('no refresh in this test')),
    },
  }
})

const kasLoginStatus = vi.mocked(api.kasLoginStatus)
// #36156: the saved config with KAS as the main harness, and a usage reading naming an account.
const ON_KAS = { agent: { acp_backend: 'kas' } }
const ADA = { used: 1, limit: 10, bonusCredits: [], email: 'ada@example.com' }
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

/** Opens the shared sign-in dialog from the signed-out card. */
async function openSignIn() {
  fireEvent.click(await screen.findByTestId('kiro-sign-in-open'))
}

describe('KiroSignInCard', () => {
  beforeEach(() => {
    kasLoginStatus.mockReset()
    kasLoginLogout.mockClear()
    kasLoginBeginDevice.mockClear()
  })

  it('deep-links to Settings > Kiro account, ringing the card', () => {
    // The chat error row navigates to KIRO_SIGN_IN_PATH; the route must open the
    // Settings tab that renders the card (pinned end to end in
    // SettingsPage.agentHarnessSignIn.test.tsx) and carry the card's own anchor.
    expect(KIRO_SIGN_IN_HIGHLIGHT_ANCHOR).toBe('kiro-sign-in')
    // The colon is percent-encoded so the route constant carries no prose-shaped
    // quasi; URLSearchParams decodes it back to `key:kiro-sign-in` on read.
    expect(KIRO_SIGN_IN_PATH).toBe('/settings/kiro-account?highlight=key%3Akiro-sign-in')
    expect(new URL(KIRO_SIGN_IN_PATH, 'http://x').searchParams.get('highlight')).toBe(`key:${KIRO_SIGN_IN_HIGHLIGHT_ANCHOR}`)
    // Search finds it by "sign out" / "switch account" (its own entry), never as
    // the card title, which would advertise signing in as a step.
    expect(SETTINGS_MANUAL.some(e => e.labelKey === 'pages.developer.kiroSignInCard.title')).toBe(false)
    expect(SETTINGS_MANUAL.find(e => e.id === 'kiro-account.kiro-sign-in')?.tab).toBe('kiro-account')
  })

  it('names the signed-in account next to the provider (#36156)', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    const { queryClient } = renderWithProviders(<KiroSignInCard />)
    // The top bar's whoami-backed reading is the only place a name exists; on KAS it names the vault account.
    act(() => {
      queryClient.setQueryData(['kirocrewConfig'], ON_KAS)
      queryClient.setQueryData(['kiro-usage'], ADA)
    })
    const account = () => screen.getByTestId('kiro-sign-in-account')
    try {
      await waitFor(() => expect(account()).toHaveTextContent('ada@example.com'))
      // Hidden by default, like the account popup: blurred, with the card's own Show toggle beside it.
      expect(account().querySelector('[data-account-hidden="true"]')).toHaveTextContent('ada@example.com')
      fireEvent.click(within(account()).getByRole('button', { name: 'Show email' }))
      expect(account().querySelector('[data-account-hidden]')).toBeNull()
    } finally {
      localStorage.removeItem('kirocrew:account-email-hidden')
    }
  })

  it('names an account name (not an e-mail) without blurring it (#36156)', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    const { queryClient } = renderWithProviders(<KiroSignInCard />)
    act(() => {
      queryClient.setQueryData(['kirocrewConfig'], ON_KAS)
      queryClient.setQueryData(['kiro-usage'], { used: 1, limit: 10, bonusCredits: [], account: 'Ada Lovelace' })
    })
    const account = () => screen.getByTestId('kiro-sign-in-account')
    await waitFor(() => expect(account()).toHaveTextContent('Ada Lovelace'))
    expect(account().querySelector('[data-account-hidden]')).toBeNull()
  })

  it('follows the account popup hide toggle on a card that is already showing (#36156)', async () => {
    // The popup's own toggle, mounted beside the card as the dashboard has them.
    function PopupToggle() {
      const [hidden, setHidden] = usePersistedBool(KIRO_ACCOUNT_EMAIL_HIDDEN_KEY, true)
      return <button onClick={() => setHidden(!hidden)}>{hidden ? 'popup: show' : 'popup: hide'}</button>
    }
    try {
      kasLoginStatus.mockResolvedValue(SIGNED_IN)
      const { queryClient } = renderWithProviders(<><KiroSignInCard /><PopupToggle /></>)
      act(() => {
        queryClient.setQueryData(['kirocrewConfig'], ON_KAS)
        queryClient.setQueryData(['kiro-usage'], ADA)
      })
      const account = () => screen.getByTestId('kiro-sign-in-account')
      await waitFor(() => expect(account().querySelector('[data-account-hidden="true"]')).not.toBeNull())
      fireEvent.click(screen.getByRole('button', { name: 'popup: show' }))
      await waitFor(() => expect(account().querySelector('[data-account-hidden]')).toBeNull())
      fireEvent.click(screen.getByRole('button', { name: 'popup: hide' }))
      await waitFor(() => expect(account().querySelector('[data-account-hidden="true"]')).not.toBeNull())
    } finally {
      localStorage.removeItem('kirocrew:account-email-hidden')
    }
  })

  it('names no account when the main harness is kiro-cli, whose login the usage reading then names (#36156)', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    const { queryClient } = renderWithProviders(<KiroSignInCard />)
    act(() => {
      queryClient.setQueryData(['kirocrewConfig'], { agent: { acp_backend: 'kiro' } })
      queryClient.setQueryData(['kiro-usage'], ADA)
    })
    await waitFor(() => expect(screen.getByTestId('kiro-sign-in-state')).toHaveTextContent('Signed in with Google'))
    expect(screen.queryByTestId('kiro-sign-in-account')).toBeNull()
    expect(screen.queryByText('ada@example.com')).toBeNull()
  })

  it('shows a loader while the first status read is in flight', () => {
    kasLoginStatus.mockReturnValue(new Promise(() => {}))
    renderWithProviders(<KiroSignInCard />)
    expect(screen.getByTestId('kiro-sign-in-pending')).toBeInTheDocument()
    const card = screen.getByTestId('kiro-sign-in-card')
    expect(card).toHaveTextContent('Kiro sign-in')
    // The anchor useSettingHighlight's direct lookup rings for KIRO_SIGN_IN_PATH.
    expect(card).toHaveAttribute('data-setting-key', KIRO_SIGN_IN_HIGHLIGHT_ANCHOR)
  })

  it('signed out: one Sign in button opens the shared sign-in dialog with the choices', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    renderWithProviders(<KiroSignInCard />)
    // The card itself holds no chooser: the choices live in the one dialog.
    await screen.findByTestId('kiro-sign-in-open')
    expect(screen.queryByRole('button', { name: 'Continue with Google' })).toBeNull()
    await openSignIn()
    expect(await screen.findByTestId('kiro-sign-in-dialog')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Continue with Google' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Continue with GitHub' })).toBeInTheDocument()
    // Card chrome inside the dialog: a region, no scrim/main and no page headline.
    expect(screen.getByTestId('kas-login-embedded')).toBeInTheDocument()
    expect(screen.queryByRole('main')).toBeNull()
    expect(screen.queryByRole('heading', { level: 1 })).toBeNull()
    expect(screen.queryByTestId('kiro-sign-in-summary')).toBeNull()
  })

  it('signed out: picking a provider starts the device flow inside the dialog', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    renderWithProviders(<KiroSignInCard />)
    await openSignIn()
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with GitHub' }))
    expect(await screen.findByTestId('kas-login-user-code')).toHaveTextContent('ABCD-EFGH')
    expect(kasLoginBeginDevice).toHaveBeenCalledWith('github', undefined)
    expect(screen.queryByRole('main')).toBeNull()
  })

  it('signed in: shows a token-free summary with sign-out, and sign-out calls the API with the slot', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    localStorage.setItem(
      'kc.acp.models.v1',
      JSON.stringify({ ts: 0, models: [{ name: 'auto', description: '' }] }),
    )
    const { queryClient } = renderWithProviders(<KiroSignInCard />)
    queryClient.setQueryData(['available-models', 'acp'], [{ name: 'auto' }, { name: 'account-a-model' }])
    queryClient.setQueryData(['kiro-usage'], ADA)
    queryClient.setQueryData(['relay-viewer', 'crews'], { crews: [{ endpointId: 'e1', name: 'Team Crew' }] })
    const reset = vi.spyOn(queryClient, 'resetQueries')
    const summary = await screen.findByTestId('kiro-sign-in-summary')
    expect(summary).toHaveAttribute('data-lapsed', 'false')
    expect(screen.getByTestId('kiro-sign-in-state')).toHaveTextContent('Signed in with Google')
    expect(screen.getByTestId('kiro-sign-in-expiry')).toHaveTextContent('Access token expires')
    // Never a credential, under any name.
    expect(summary.textContent).not.toMatch(/token:|at-|rt-/)
    expect(screen.queryByTestId('kiro-sign-in-open')).toBeNull()

    // From here the gateway answers "signed out": set before the press so the
    // refetch the sign-out triggers cannot race the mock swap.
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    fireEvent.click(screen.getByTestId('kiro-sign-in-logout'))
    await waitFor(() => expect(kasLoginLogout).toHaveBeenCalledWith('social'))
    // The model list was the signed-out account's: both frontend caches drop with it.
    await waitFor(() => expect(reset).toHaveBeenCalledWith({ queryKey: ['available-models'] }))
    expect(queryClient.getQueryData(['available-models', 'acp'])).toBeUndefined()
    expect(localStorage.getItem('kc.acp.models.v1')).toBeNull()
    // Its usage reading names it, so that drops too (#36156): no stale name on the next sign-in.
    expect(queryClient.getQueryData(['kiro-usage'])).toBeUndefined()
    // The Team Crews list was the signed-out account's: it is not shown as online any longer.
    expect(queryClient.getQueryData(['relay-viewer', 'crews'])).toBeUndefined()
    // The status re-read now says signed out, which unmounts the summary; the
    // "applies to processes started from now on" note lives on the card, so
    // it is still there beside the Sign in button the user now sees.
    expect(await screen.findByTestId('kiro-sign-in-open')).toBeInTheDocument()
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

  it('sign in again opens the dialog over the stored identity, and closing it backs out', async () => {
    kasLoginStatus.mockResolvedValue({ ...SIGNED_IN, refresh_rejected: true })
    renderWithProviders(<KiroSignInCard />)
    fireEvent.click(await screen.findByRole('button', { name: 'Sign in again' }))
    expect(await screen.findByRole('button', { name: 'Continue with Google' })).toBeInTheDocument()
    // The stored credential is untouched while the chooser is up: no logout call.
    expect(kasLoginLogout).not.toHaveBeenCalled()
    fireEvent.keyDown(screen.getByTestId('kiro-sign-in-dialog'), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('kiro-sign-in-dialog')).toBeNull())
    expect(screen.getByTestId('kiro-sign-in-summary')).toBeInTheDocument()
    expect(kasLoginLogout).not.toHaveBeenCalled()
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

  it('closing the dialog with a sign-in pending cancels it, so no unwatched login lands later', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    vi.mocked(api.kasLoginCancel).mockClear()
    renderWithProviders(<KiroSignInCard />)
    await openSignIn()
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with GitHub' }))
    await screen.findByTestId('kas-login-user-code')
    fireEvent.keyDown(screen.getByTestId('kiro-sign-in-dialog'), { key: 'Escape' })
    await waitFor(() => expect(api.kasLoginCancel).toHaveBeenCalledWith('login-1'))
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

  it('an authorized sign-in drops the model list the previous account was served', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    vi.mocked(api.kasLoginPoll).mockResolvedValue({ status: 'authorized' })
    localStorage.setItem(
      'kc.acp.models.v1',
      JSON.stringify({ ts: 0, models: [{ name: 'auto', description: '' }] }),
    )
    const { queryClient } = renderWithProviders(<KiroSignInCard />)
    queryClient.setQueryData(['available-models', 'acp'], [{ name: 'auto' }, { name: 'account-a-model' }])
    queryClient.setQueryData(['kiro-usage'], ADA)
    const reset = vi.spyOn(queryClient, 'resetQueries')
    await openSignIn()
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with GitHub' }))
    await waitFor(() => expect(reset).toHaveBeenCalledWith({ queryKey: ['available-models'] }))
    expect(queryClient.getQueryData(['available-models', 'acp'])).toBeUndefined()
    expect(localStorage.getItem('kc.acp.models.v1')).toBeNull()
    // The previous account's usage reading names it: dropped, not shown until the next poll (#36156).
    await waitFor(() => expect(queryClient.getQueryData(['kiro-usage'])).toBeNull())
    vi.mocked(api.kasLoginPoll).mockResolvedValue({ status: 'pending' })
  })

  it('an authorized sign-in puts a fresh reading in the top bar and re-reads the Team Crews list at once', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    vi.mocked(api.kasLoginPoll).mockResolvedValue({ status: 'authorized' })
    vi.mocked(api.sessionsUsageRefresh).mockClear()
    // The gateway's own refresh answers with the new account's plan; its cached
    // GET would still have said "sign in" until the bar's next 30 s poll.
    vi.mocked(api.sessionsUsageRefresh).mockResolvedValueOnce({
      usage: { available: true, credits_used: 3, credits_plan: 50, email: 'grace@example.com' },
    } as Awaited<ReturnType<typeof api.sessionsUsageRefresh>>)
    const { queryClient } = renderWithProviders(<KiroSignInCard />)
    queryClient.setQueryData(['kiro-usage'], 'signin-required')
    const reset = vi.spyOn(queryClient, 'resetQueries')
    await openSignIn()
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with GitHub' }))
    await waitFor(() => expect(api.sessionsUsageRefresh).toHaveBeenCalledTimes(1))
    await waitFor(() =>
      expect(queryClient.getQueryData(['kiro-usage'])).toMatchObject({ used: 3, limit: 50, email: 'grace@example.com' }),
    )
    expect(reset).toHaveBeenCalledWith({ queryKey: RELAY_VIEWER_CREWS_KEY })
    vi.mocked(api.kasLoginPoll).mockResolvedValue({ status: 'pending' })
  })

  it('a top-bar usage poll still in flight when the sign-in lands cannot put the previous reading back', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    vi.mocked(api.kasLoginPoll).mockResolvedValue({ status: 'authorized' })
    vi.mocked(api.sessionsUsageRefresh).mockClear()
    vi.mocked(api.sessionsUsageRefresh).mockResolvedValueOnce({
      usage: { available: true, credits_used: 3, credits_plan: 50, email: 'grace@example.com' },
    } as Awaited<ReturnType<typeof api.sessionsUsageRefresh>>)
    const { queryClient } = renderWithProviders(<KiroSignInCard />)
    // The bar's GET left before the sign-in and answers for the previous account
    // only after the refresh has published the new one.
    let answerStalePoll: (state: typeof ADA) => void = () => {}
    void queryClient
      .fetchQuery({ queryKey: ['kiro-usage'], queryFn: () => new Promise<typeof ADA>(r => { answerStalePoll = r }) })
      .catch(() => {})
    await openSignIn()
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with GitHub' }))
    await waitFor(() =>
      expect(queryClient.getQueryData(['kiro-usage'])).toMatchObject({ email: 'grace@example.com' }),
    )
    await act(async () => { answerStalePoll(ADA) })
    expect(queryClient.getQueryData(['kiro-usage'])).toMatchObject({ used: 3, limit: 50, email: 'grace@example.com' })
    vi.mocked(api.kasLoginPoll).mockResolvedValue({ status: 'pending' })
  })

  it('a status refetch in flight under a cancel cannot overwrite the fresh signed-in answer', async () => {
    // The poll never answers 'authorized': only the cancel's own read sees the sign-in.
    vi.mocked(api.kasLoginPoll).mockResolvedValue({ status: 'pending' })
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    const { queryClient } = renderWithProviders(<KiroSignInCard />)
    await openSignIn()
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with GitHub' }))
    const cancel = await screen.findByRole('button', { name: 'Use a different sign-in' })
    // A status refetch that left before the token landed, still unanswered.
    let answerStaleStatus: (s: KasLoginStatus) => void = () => {}
    void queryClient
      .fetchQuery({ queryKey: KAS_LOGIN_STATUS_KEY, queryFn: () => new Promise<KasLoginStatus>(r => { answerStaleStatus = r }) })
      .catch(() => {})
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    fireEvent.click(cancel)
    expect(await screen.findByTestId('kiro-sign-in-summary')).toBeInTheDocument()
    await act(async () => { answerStaleStatus(SIGNED_OUT) })
    expect(queryClient.getQueryData(KAS_LOGIN_STATUS_KEY)).toMatchObject({ authenticated: true })
    expect(screen.getByTestId('kiro-sign-in-summary')).toBeInTheDocument()
  })

  it('a cancel that finds the sign-in already landed drops the previous account\'s model list too', async () => {
    // The poll never answers 'authorized' here: the user backs out while the
    // credential is being stored, and only the cancel's fresh status read sees
    // it. The model caches must still drop, the same as on the poll path.
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    localStorage.setItem(
      'kc.acp.models.v1',
      JSON.stringify({ ts: 0, models: [{ name: 'auto', description: '' }] }),
    )
    const { queryClient } = renderWithProviders(<KiroSignInCard />)
    queryClient.setQueryData(['available-models', 'acp'], [{ name: 'auto' }, { name: 'account-a-model' }])
    const reset = vi.spyOn(queryClient, 'resetQueries')
    await openSignIn()
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with GitHub' }))
    const cancel = await screen.findByRole('button', { name: 'Use a different sign-in' })
    expect(reset).not.toHaveBeenCalledWith({ queryKey: ['available-models'] })
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    fireEvent.click(cancel)
    await waitFor(() => expect(api.kasLoginCancel).toHaveBeenCalled())
    await waitFor(() => expect(reset).toHaveBeenCalledWith({ queryKey: ['available-models'] }))
    expect(queryClient.getQueryData(['available-models', 'acp'])).toBeUndefined()
    expect(localStorage.getItem('kc.acp.models.v1')).toBeNull()
    expect(await screen.findByTestId('kiro-sign-in-summary')).toBeInTheDocument()
  })

  it('the embedded chrome hands failures to the agent where the gate cannot', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    kasLoginBeginDevice.mockRejectedValueOnce(new Error('auth service unreachable'))
    renderWithProviders(<KiroSignInCard />)
    await openSignIn()
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with GitHub' }))
    const notice = await screen.findByTestId('kas-login-begin-error')
    expect(notice).toHaveTextContent('auth service unreachable')
    // ErrorNotice renders its hand-off control only when askAgent is on.
    expect(
      notice.querySelector('button[title="Open a chat with this error\'s full context attached"]'),
    ).not.toBeNull()
  })
})

// The desktop shell's relay Crew windows ran as the signed-in account: Sign out
// and a landed Switch account close them through the bridge, at once.
describe('KiroSignInCard closes relay Crew windows', () => {
  const closeAll = vi.fn().mockResolvedValue(undefined)
  const setBridge = (bridge: unknown) => {
    ;(window as { relayCrewAPI?: unknown }).relayCrewAPI = bridge
  }

  beforeEach(() => {
    kasLoginStatus.mockReset()
    kasLoginLogout.mockClear()
    kasLoginBeginDevice.mockClear()
    closeAll.mockClear()
    vi.mocked(api.kasLoginCancel).mockClear()
    setBridge({ open: vi.fn(), closeAll })
  })
  afterEach(() => {
    delete (window as { relayCrewAPI?: unknown }).relayCrewAPI
    vi.mocked(api.kasLoginPoll).mockResolvedValue({ status: 'pending' })
  })

  it('a Sign out that lands closes them once', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    renderWithProviders(<KiroSignInCard />)
    await screen.findByTestId('kiro-sign-in-summary')
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    fireEvent.click(screen.getByTestId('kiro-sign-in-logout'))
    await waitFor(() => expect(closeAll).toHaveBeenCalledTimes(1))
    expect(closeAll).toHaveBeenCalledWith()
  })

  it('a Sign out that fails closes nothing', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    kasLoginLogout.mockRejectedValueOnce(new Error('Could not remove the stored sign-in.'))
    renderWithProviders(<KiroSignInCard />)
    await screen.findByTestId('kiro-sign-in-summary')
    fireEvent.click(screen.getByTestId('kiro-sign-in-logout'))
    expect(await screen.findByTestId('kiro-sign-in-logout-error')).toHaveTextContent('Could not remove')
    expect(closeAll).not.toHaveBeenCalled()
  })

  it('without the bridge, or with one that rejects, a Sign out still lands', async () => {
    for (const bridge of [undefined, { open: vi.fn(), closeAll: vi.fn().mockRejectedValue(new Error('gone')) }]) {
      setBridge(bridge)
      kasLoginStatus.mockResolvedValue(SIGNED_IN)
      const { unmount } = renderWithProviders(<KiroSignInCard />)
      await screen.findByTestId('kiro-sign-in-summary')
      kasLoginStatus.mockResolvedValue(SIGNED_OUT)
      fireEvent.click(screen.getByTestId('kiro-sign-in-logout'))
      expect(await screen.findByTestId('kiro-sign-in-open')).toBeInTheDocument()
      unmount()
    }
  })

  it('a Switch account that lands closes them once', async () => {
    kasLoginStatus.mockResolvedValue({ ...SIGNED_IN, identity: 'identity_center', provider: 'Enterprise' })
    vi.mocked(api.kasLoginPoll).mockResolvedValue({ status: 'authorized' })
    renderWithProviders(<KiroSignInCard />)
    fireEvent.click(await screen.findByRole('button', { name: 'Switch account' }))
    expect(closeAll).not.toHaveBeenCalled()
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with GitHub' }))
    await waitFor(() => expect(closeAll).toHaveBeenCalledTimes(1))
  })

  // A Switch account cancelled after its credential landed: settleCancel sees the
  // changed identity, and the detached poll never reports 'authorized'.
  const SWITCH_FROM: KasLoginStatus = { ...SIGNED_IN, identity: 'identity_center', provider: 'Enterprise' }
  const cancelSwitch = async (after: KasLoginStatus) => {
    kasLoginStatus.mockResolvedValue(SWITCH_FROM)
    renderWithProviders(<KiroSignInCard />)
    fireEvent.click(await screen.findByRole('button', { name: 'Switch account' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with GitHub' }))
    const cancel = await screen.findByRole('button', { name: 'Use a different sign-in' })
    // The read before the cancel still sees the old account; the read after it sees `after`.
    kasLoginStatus.mockResolvedValueOnce(SWITCH_FROM).mockResolvedValue(after)
    const reads = kasLoginStatus.mock.calls.length
    fireEvent.click(cancel)
    await waitFor(() => expect(api.kasLoginCancel).toHaveBeenCalled())
    // Both of settleCancel's reads (before and after the cancel) have answered.
    await waitFor(() => expect(kasLoginStatus.mock.calls.length).toBeGreaterThanOrEqual(reads + 2))
    await act(async () => { await Promise.resolve() })
  }

  it('a Switch account that lands under a cancel closes them once', async () => {
    await cancelSwitch({ ...SIGNED_IN, identity: 'social', provider: 'GitHub', expires_at: '2099-02-01T00:00:00Z' })
    await waitFor(() => expect(closeAll).toHaveBeenCalledTimes(1))
  })

  it('a Switch account cancelled before anything landed closes nothing', async () => {
    // Same identity and expiry after the cancel: the old account is still the one stored.
    await cancelSwitch(SWITCH_FROM)
    expect(closeAll).not.toHaveBeenCalled()
  })

  it('a first sign-in that lands under a cancel closes nothing', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    renderWithProviders(<KiroSignInCard />)
    await openSignIn()
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with GitHub' }))
    const cancel = await screen.findByRole('button', { name: 'Use a different sign-in' })
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    const reads = kasLoginStatus.mock.calls.length
    fireEvent.click(cancel)
    await waitFor(() => expect(api.kasLoginCancel).toHaveBeenCalled())
    // The cancel's one fresh read found the new sign-in, so settleCancel ran its landed branch.
    await waitFor(() => expect(kasLoginStatus.mock.calls.length).toBeGreaterThanOrEqual(reads + 1))
    expect(await screen.findByTestId('kiro-sign-in-summary')).toBeInTheDocument()
    expect(closeAll).not.toHaveBeenCalled()
  })

  it('a first sign-in closes nothing', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    vi.mocked(api.kasLoginPoll).mockResolvedValue({ status: 'authorized' })
    const { queryClient } = renderWithProviders(<KiroSignInCard />)
    const reset = vi.spyOn(queryClient, 'resetQueries')
    await openSignIn()
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with GitHub' }))
    // The authorized effect ran (it drops the model list), and closed nothing.
    await waitFor(() => expect(reset).toHaveBeenCalledWith({ queryKey: ['available-models'] }))
    expect(closeAll).not.toHaveBeenCalled()
  })
})
