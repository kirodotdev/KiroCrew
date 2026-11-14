import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { onTestFinished } from 'vitest'
import { KiroSignInCard } from '../pages/developer/KiroSignInCard'
import { sourcesNotice } from '../pages/developer/KiroSignInSources'
import { api, type KasLoginSources, type KasLoginStatus } from '../api/client'
import { KIRO_ACCOUNT_EMAIL_HIDDEN_KEY } from '../components/KiroAccountModal'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kasLoginStatus: vi.fn(),
      kasLoginSources: vi.fn(),
      kasLoginLogout: vi.fn().mockResolvedValue({ ok: true }),
      kasLoginBeginDevice: vi.fn(),
      kasLoginPoll: vi.fn().mockResolvedValue({ status: 'pending' }),
      kasLoginBeginLoopback: vi.fn(),
      kasLoginCancel: vi.fn().mockResolvedValue({ ok: true }),
    },
  }
})

const kasLoginStatus = vi.mocked(api.kasLoginStatus)
const kasLoginSources = vi.mocked(api.kasLoginSources)
const kasLoginLogout = vi.mocked(api.kasLoginLogout)

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
  provider: 'BuilderId',
  identity: 'builder_id',
  transport: 'device',
  expires_at: '2099-01-01T00:00:00Z',
  expired: false,
  has_refresh_token: true,
  refresh_rejected: false,
  usable: true,
}

const NONE: KasLoginSources = {
  active: 'none',
  kiro_crew: { stored: false },
  kiro_cli: { stored: false, usable: false },
  name_pending: false,
  name_unreadable: false,
  same_account: null,
  can_use_kiro_cli: false,
}

const ONLY_KIRO_CLI: KasLoginSources = {
  ...NONE,
  active: 'kiro_cli',
  kiro_cli: {
    stored: true,
    usable: true,
    account_type: 'IamIdentityCenter',
    portal_host: 'example.awsapps.com',
    email: 'person@example.com',
  },
}

const TWO_ACCOUNTS: KasLoginSources = {
  active: 'kiro_crew',
  kiro_crew: { stored: true, account_type: 'BuilderId' },
  kiro_cli: { stored: true, usable: true, account_type: 'IamIdentityCenter', portal_host: 'example.awsapps.com' },
  name_pending: false,
  name_unreadable: false,
  same_account: false,
  can_use_kiro_cli: true,
}

describe('KiroSignInCard: which account Kiro Agent runs as', () => {
  beforeEach(() => {
    kasLoginStatus.mockReset()
    kasLoginSources.mockReset()
    kasLoginLogout.mockReset().mockResolvedValue({ ok: true })
    vi.mocked(api.kasLoginBeginDevice).mockReset().mockResolvedValue({
      login_id: 'switch-account',
      user_code: 'ABCD-EFGH',
      verification_uri_complete: 'https://example.invalid/device',
      expires_at: '2099-01-01T00:00:00Z',
    })
    vi.mocked(api.kasLoginPoll).mockReset().mockResolvedValue({ status: 'pending' })
    vi.mocked(api.kasLoginCancel).mockReset().mockResolvedValue({ ok: true })
    // The account popup's hide-email toggle at its default (hidden).
    localStorage.removeItem(KIRO_ACCOUNT_EMAIL_HIDDEN_KEY)
  })

  // The popup's toggle set to show e-mails.
  const showEmails = () => localStorage.setItem(KIRO_ACCOUNT_EMAIL_HIDDEN_KEY, '0')

  it('says nothing extra when kiro-cli is not signed in', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    kasLoginSources.mockResolvedValue(NONE)
    renderWithProviders(<KiroSignInCard />)
    await screen.findByTestId('kas-login-embedded')
    await waitFor(() => expect(kasLoginSources).toHaveBeenCalled())
    expect(screen.queryByTestId('kiro-sign-in-sources')).toBeNull()
  })

  it('nothing stored here and kiro-cli signed in: says sessions use the Kiro CLI sign-in and names the account', async () => {
    showEmails()
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    kasLoginSources.mockResolvedValue(ONLY_KIRO_CLI)
    renderWithProviders(<KiroSignInCard />)
    const notice = await screen.findByTestId('kiro-sign-in-sources')
    expect(notice).toHaveAttribute('data-notice', 'using_kiro_cli')
    expect(notice).toHaveTextContent('Using your Kiro CLI sign-in')
    expect(notice).toHaveTextContent(
      'Kiro Agent runs as the account Kiro CLI is signed in to: person@example.com. You don\'t need to sign in here.',
    )
    // Nothing of this app's is stored, so there is nothing to remove.
    expect(screen.queryByTestId('kiro-sign-in-use-kiro-cli')).toBeNull()
    // The chooser is still there for a person who wants another account.
    expect(screen.getByTestId('kas-login-embedded')).toBeInTheDocument()
  })

  it('hide e-mail on (the default): the Kiro CLI-only notice names the account by its kind and portal', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    kasLoginSources.mockResolvedValue(ONLY_KIRO_CLI)
    renderWithProviders(<KiroSignInCard />)
    const notice = await screen.findByTestId('kiro-sign-in-sources')
    expect(notice).toHaveTextContent('signed in to: IAM Identity Center · example.awsapps.com.')
    expect(notice).not.toHaveTextContent('person@example.com')
  })

  it.each([
    ['two_accounts', false],
    ['maybe_two_accounts', null],
  ] as const)('%s notice: hides the Kiro CLI e-mail while hide e-mail is on, shows it when off', async (kind, same) => {
    const sources: KasLoginSources = {
      ...TWO_ACCOUNTS,
      kiro_cli: { ...TWO_ACCOUNTS.kiro_cli, email: 'person@example.com' },
      same_account: same,
    }
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    kasLoginSources.mockResolvedValue(sources)
    const hidden = renderWithProviders(<KiroSignInCard />)
    let notice = await screen.findByTestId('kiro-sign-in-sources')
    expect(notice).toHaveAttribute('data-notice', kind)
    expect(notice).toHaveTextContent('IAM Identity Center · example.awsapps.com')
    expect(notice).not.toHaveTextContent('person@example.com')
    hidden.unmount()

    showEmails()
    renderWithProviders(<KiroSignInCard />)
    notice = await screen.findByTestId('kiro-sign-in-sources')
    expect(notice).toHaveTextContent('person@example.com')
    expect(notice).not.toHaveTextContent('example.awsapps.com')
  })

  it('names the account by its kind and portal when no e-mail was read', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    kasLoginSources.mockResolvedValue({
      ...ONLY_KIRO_CLI,
      kiro_cli: { stored: true, usable: true, account_type: 'IamIdentityCenter', portal_host: 'example.awsapps.com' },
    })
    renderWithProviders(<KiroSignInCard />)
    const notice = await screen.findByTestId('kiro-sign-in-sources')
    expect(notice).toHaveTextContent('signed in to: IAM Identity Center · example.awsapps.com.')
  })

  it('shows the state before the name arrives, then the name, and an error notice when it cannot be read', async () => {
    showEmails()
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    kasLoginSources.mockResolvedValue({ ...ONLY_KIRO_CLI, kiro_cli: { stored: true, usable: true }, name_pending: true })
    const { queryClient } = renderWithProviders(<KiroSignInCard />)
    const notice = await screen.findByTestId('kiro-sign-in-sources')
    expect(notice).toHaveTextContent('signed in to: an account whose name is still being read.')
    act(() => queryClient.setQueryData(['kas-login-sources'], ONLY_KIRO_CLI))
    await waitFor(() => expect(notice).toHaveTextContent('signed in to: person@example.com.'))
    expect(screen.queryByTestId('kiro-sign-in-account-name-error')).toBeNull()
    act(() =>
      queryClient.setQueryData(['kas-login-sources'], {
        ...ONLY_KIRO_CLI,
        kiro_cli: { stored: true, usable: true },
        name_unreadable: true,
      }),
    )
    const error = await screen.findByTestId('kiro-sign-in-account-name-error')
    expect(error).toHaveAttribute('role', 'alert')
    expect(error).toHaveTextContent('Could not read the Kiro CLI account name')
    // A quiet inline line, not the boxed banner: Kiro CLI is still signed in.
    expect(error.tagName).toBe('SPAN')
    // Beside the sign-in chooser, whose Company SSO form can hold an unsaved draft.
    expect(within(error).queryByText('Ask the agent')).toBeNull()
    // Still "using the Kiro CLI sign-in", never "signed out", and no name in the sentence.
    const unnamed = screen.getByTestId('kiro-sign-in-sources')
    expect(unnamed).toHaveAttribute('data-notice', 'using_kiro_cli')
    expect(unnamed).toHaveTextContent(
      "Kiro Agent runs as the account Kiro CLI is signed in to. You don't need to sign in here.",
    )
    expect(unnamed).not.toHaveTextContent('could not read.')
    expect(screen.getByTestId('kas-login-embedded')).toBeInTheDocument()
  })

  it('an unreadable Kiro CLI name beside a sign-in here: names both by where they live, with the error notice', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    kasLoginSources.mockResolvedValue({
      ...TWO_ACCOUNTS,
      kiro_cli: { stored: true, usable: true },
      same_account: null,
      name_unreadable: true,
    })
    renderWithProviders(<KiroSignInCard />)
    const notice = await screen.findByTestId('kiro-sign-in-sources')
    expect(notice).toHaveTextContent(
      "If these are different accounts, Kiro Agent is not using your Kiro CLI account's quota. It runs as the sign-in stored in Kiro Crew.",
    )
    expect(screen.getByTestId('kiro-sign-in-account-name-error')).toHaveTextContent(
      'Could not read the Kiro CLI account name',
    )
    // Signed in, the chooser is not mounted: nothing to lose, so the error hands off.
    expect(within(screen.getByTestId('kiro-sign-in-account-name-error')).getByText('Ask the agent')).toBeInTheDocument()
    // Inline beside "Use the Kiro CLI account", so a missing name does not read as an alarm over it.
    expect(screen.getByTestId('kiro-sign-in-account-name-error').tagName).toBe('SPAN')
    expect(screen.getByTestId('kiro-sign-in-use-kiro-cli')).toBeInTheDocument()
  })

  it.each([
    ['signed in here', SIGNED_IN, { ...TWO_ACCOUNTS, kiro_cli: { stored: null, usable: false }, same_account: null }],
    ['nothing stored here', SIGNED_OUT, { ...NONE, active: 'unknown', kiro_cli: { stored: null, usable: false } }],
  ] as const)(
    'a failed read of the Kiro CLI sign-in (%s) is an error, never "signed out"',
    async (_label, status, sources) => {
      kasLoginStatus.mockResolvedValue(status)
      kasLoginSources.mockResolvedValue(sources as KasLoginSources)
      renderWithProviders(<KiroSignInCard />)
      const error = await screen.findByTestId('kiro-sign-in-kiro-cli-unreadable')
      expect(error).toHaveAttribute('role', 'alert')
      expect(error).toHaveTextContent('Could not check the Kiro CLI sign-in')
      // The card's own sign-in status and controls stay as they were.
      if (status.authenticated) expect(screen.getByTestId('kiro-sign-in-summary')).toBeInTheDocument()
      else expect(screen.getByTestId('kas-login-embedded')).toBeInTheDocument()
      expect(screen.queryByTestId('kiro-sign-in-sources')).toBeNull()
      expect(screen.getByTestId('kiro-sign-in-card')).not.toHaveTextContent(/signed out/i)
    },
  )

  it('a readable Kiro CLI store shows no "could not check" error', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    kasLoginSources.mockResolvedValue({ ...TWO_ACCOUNTS, kiro_cli: { stored: false, usable: false }, same_account: null })
    renderWithProviders(<KiroSignInCard />)
    await screen.findByTestId('kiro-sign-in-summary')
    await waitFor(() => expect(kasLoginSources).toHaveBeenCalled())
    expect(screen.queryByTestId('kiro-sign-in-kiro-cli-unreadable')).toBeNull()
  })

  // The route answers a corrupt Crew vault with 200, `kiro_crew.stored: null` and `active: 'unknown'`.
  it.each([
    ['signed in', SIGNED_IN],
    ['not signed in', SIGNED_OUT],
  ] as const)(
    'a failed read of the sign-in stored in Kiro Crew (%s) is an error naming that store, never "signed out"',
    async (_label, status) => {
      kasLoginStatus.mockResolvedValue(status)
      kasLoginSources.mockResolvedValue({ ...ONLY_KIRO_CLI, active: 'unknown', kiro_crew: { stored: null } })
      renderWithProviders(<KiroSignInCard />)
      const error = await screen.findByTestId('kiro-sign-in-kiro-crew-unreadable')
      expect(error).toHaveAttribute('role', 'alert')
      expect(error).toHaveTextContent('Could not check the sign-in stored in Kiro Crew')
      // No hand-off beside the embedded sign-in form.
      expect(within(error).queryByText('Ask the agent')).toBeNull()
      // The Kiro CLI store was read: no error for it.
      expect(screen.queryByTestId('kiro-sign-in-kiro-cli-unreadable')).toBeNull()
      if (status.authenticated) expect(screen.getByTestId('kiro-sign-in-summary')).toBeInTheDocument()
      else expect(screen.getByTestId('kas-login-embedded')).toBeInTheDocument()
      expect(screen.getByTestId('kiro-sign-in-card')).not.toHaveTextContent(/signed out/i)
    },
  )

  it('both stores unreadable: one error for each, each naming its store', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    kasLoginSources.mockResolvedValue({
      ...NONE,
      active: 'unknown',
      kiro_crew: { stored: null },
      kiro_cli: { stored: null, usable: false },
    })
    renderWithProviders(<KiroSignInCard />)
    expect(await screen.findByTestId('kiro-sign-in-kiro-crew-unreadable')).toHaveTextContent(
      'Could not check the sign-in stored in Kiro Crew',
    )
    expect(screen.getByTestId('kiro-sign-in-kiro-cli-unreadable')).toHaveTextContent('Could not check the Kiro CLI sign-in')
  })

  it('both stores readable: no "could not check" error for either', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    kasLoginSources.mockResolvedValue(TWO_ACCOUNTS)
    renderWithProviders(<KiroSignInCard />)
    await screen.findByTestId('kiro-sign-in-sources')
    expect(screen.queryByTestId('kiro-sign-in-kiro-crew-unreadable')).toBeNull()
    expect(screen.queryByTestId('kiro-sign-in-kiro-cli-unreadable')).toBeNull()
  })

  it('two different accounts with the same label: names them by where they live and leads with the quota', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    kasLoginSources.mockResolvedValue({
      ...TWO_ACCOUNTS,
      kiro_cli: { stored: true, usable: true, account_type: 'BuilderId' },
    })
    renderWithProviders(<KiroSignInCard />)
    const notice = await screen.findByTestId('kiro-sign-in-sources')
    expect(notice).toHaveAttribute('data-notice', 'two_accounts')
    expect(notice).toHaveTextContent(
      "Kiro Agent is not using your Kiro CLI account's quota. It runs as the sign-in stored in Kiro Crew, and the Kiro CLI sign-in is a different account.",
    )
    expect(notice).not.toHaveTextContent('Builder ID')
    expect(screen.queryByTestId('kiro-sign-in-account-name-error')).toBeNull()
  })

  it('keeps re-reading sources slowly after the name resolves, so a switch made outside Kiro Crew shows', async () => {
    vi.useFakeTimers()
    onTestFinished(() => { vi.useRealTimers() })
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    kasLoginSources.mockResolvedValue({ ...TWO_ACCOUNTS, same_account: true })
    renderWithProviders(<KiroSignInCard />, { queryDefaults: { staleTime: Infinity } })
    await act(async () => { await vi.advanceTimersByTimeAsync(500) })
    expect(kasLoginSources).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('kiro-sign-in-sources')).toBeNull()
    // Not at the fast cadence the name read uses: nothing is pending.
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(kasLoginSources).toHaveBeenCalledTimes(1)
    // `kiro-cli login` to another account, with this card's sign-in status unchanged.
    kasLoginSources.mockResolvedValue(TWO_ACCOUNTS)
    await act(async () => { await vi.advanceTimersByTimeAsync(50_000) })
    expect(kasLoginSources).toHaveBeenCalledTimes(2)
    expect(screen.getByTestId('kiro-sign-in-sources')).toHaveAttribute('data-notice', 'two_accounts')
  })

  it('two different accounts: names both and offers the Kiro CLI one, removing only after a confirm', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    kasLoginSources.mockResolvedValue(TWO_ACCOUNTS)
    renderWithProviders(<KiroSignInCard />)
    const notice = await screen.findByTestId('kiro-sign-in-sources')
    expect(notice).toHaveAttribute('data-notice', 'two_accounts')
    expect(notice).toHaveTextContent('Two different accounts are signed in')
    expect(notice).toHaveTextContent(
      'Kiro Agent runs as the account you signed in with here: Builder ID. Kiro CLI is signed in to another account: IAM Identity Center · example.awsapps.com. Kiro Agent is not using it.',
    )
    fireEvent.click(screen.getByTestId('kiro-sign-in-use-kiro-cli'))
    // One press removes nothing: it explains, and focus is on the choice that keeps things as they are.
    expect(kasLoginLogout).not.toHaveBeenCalled()
    expect(screen.getByTestId('kiro-sign-in-use-kiro-cli-explain')).toHaveTextContent(
      'This removes the sign-in stored here. Kiro Agent then runs as the Kiro CLI account. To undo it, sign in here again.',
    )
    expect(screen.getByTestId('kiro-sign-in-keep')).toHaveFocus()
    fireEvent.click(screen.getByTestId('kiro-sign-in-keep'))
    expect(screen.queryByTestId('kiro-sign-in-remove')).toBeNull()
    expect(kasLoginLogout).not.toHaveBeenCalled()

    fireEvent.click(screen.getByTestId('kiro-sign-in-use-kiro-cli'))
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    kasLoginSources.mockResolvedValue({ ...ONLY_KIRO_CLI, kiro_cli: TWO_ACCOUNTS.kiro_cli })
    fireEvent.click(screen.getByTestId('kiro-sign-in-remove'))
    await waitFor(() => expect(kasLoginLogout).toHaveBeenCalledWith('builder_id'))
    // The card then reads both again and says sessions run on the Kiro CLI sign-in.
    await waitFor(() =>
      expect(screen.getByTestId('kiro-sign-in-sources')).toHaveAttribute('data-notice', 'using_kiro_cli'),
    )
    expect(screen.getByTestId('kiro-sign-in-takes-effect')).toBeInTheDocument()
  })

  it('cannot tell whether they are one account: says so without calling them different', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    kasLoginSources.mockResolvedValue({
      ...TWO_ACCOUNTS,
      kiro_cli: { stored: true, usable: true, account_type: 'BuilderId' },
      same_account: null,
    })
    renderWithProviders(<KiroSignInCard />)
    const notice = await screen.findByTestId('kiro-sign-in-sources')
    expect(notice).toHaveAttribute('data-notice', 'maybe_two_accounts')
    expect(notice).not.toHaveTextContent('Two different accounts')
    // Both read "Builder ID", so the label names no one: each is named by where it lives.
    expect(notice).toHaveTextContent(
      'The Kiro CLI sign-in is also there, and Kiro Crew cannot tell whether they are the same account.',
    )
    expect(notice).not.toHaveTextContent('Builder ID')
    expect(screen.getByTestId('kiro-sign-in-use-kiro-cli')).toBeInTheDocument()
  })

  it('offers no button where removing would not put sessions on the Kiro CLI sign-in', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    kasLoginSources.mockResolvedValue({ ...TWO_ACCOUNTS, can_use_kiro_cli: false })
    renderWithProviders(<KiroSignInCard />)
    await screen.findByTestId('kiro-sign-in-sources')
    expect(screen.queryByTestId('kiro-sign-in-use-kiro-cli')).toBeNull()
  })

  it('compact: two different accounts open the fold a signed-in card would keep closed', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    kasLoginSources.mockResolvedValue(TWO_ACCOUNTS)
    renderWithProviders(<KiroSignInCard compact />)
    const card = screen.getByTestId('kiro-sign-in-card') as HTMLDetailsElement
    await waitFor(() => expect(card.open).toBe(true))
    expect(screen.getByTestId('kiro-sign-in-sources')).toBeInTheDocument()
  })

  it('switch account refetches sources even when authenticated and the status fields stay the same', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    kasLoginSources.mockResolvedValue({ ...TWO_ACCOUNTS, same_account: true })
    const { queryClient } = renderWithProviders(<KiroSignInCard />, { queryDefaults: { staleTime: Infinity } })
    await screen.findByTestId('kiro-sign-in-summary')
    await waitFor(() => expect(queryClient.getQueryState(['kas-login-sources'])?.status).toBe('success'))
    expect(screen.queryByTestId('kiro-sign-in-sources')).toBeNull()
    kasLoginSources.mockResolvedValue(TWO_ACCOUNTS)
    vi.mocked(api.kasLoginPoll).mockResolvedValue({ status: 'authorized' })
    fireEvent.click(screen.getByRole('button', { name: 'Switch account' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with GitHub' }))
    await waitFor(() => expect(kasLoginStatus).toHaveBeenCalledTimes(2))
    expect(queryClient.getQueryData(['kas-login'])).toEqual(SIGNED_IN)
    await waitFor(() => expect(kasLoginSources).toHaveBeenCalledTimes(2))
    expect(await screen.findByTestId('kiro-sign-in-sources')).toHaveAttribute('data-notice', 'two_accounts')
  })

  it('a switch completed during cancellation refetches sources while authenticated stays true', async () => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    kasLoginSources.mockResolvedValue({ ...TWO_ACCOUNTS, same_account: true })
    const { queryClient } = renderWithProviders(<KiroSignInCard />, { queryDefaults: { staleTime: Infinity } })
    await screen.findByTestId('kiro-sign-in-summary')
    await waitFor(() => expect(queryClient.getQueryState(['kas-login-sources'])?.status).toBe('success'))
    fireEvent.click(screen.getByRole('button', { name: 'Switch account' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with GitHub' }))
    const cancel = await screen.findByRole('button', { name: 'Use a different sign-in' })
    await waitFor(() => expect(api.kasLoginPoll).toHaveBeenCalledWith('switch-account'))
    kasLoginStatus.mockResolvedValueOnce(SIGNED_IN).mockResolvedValue({
      ...SIGNED_IN, expires_at: '2099-01-02T00:00:00Z',
    })
    kasLoginSources.mockResolvedValue(TWO_ACCOUNTS)
    fireEvent.click(cancel)
    await waitFor(() => expect(api.kasLoginCancel).toHaveBeenCalledWith('switch-account'))
    await waitFor(() => expect(queryClient.getQueryData(['kas-login'])).toEqual({
      ...SIGNED_IN, expires_at: '2099-01-02T00:00:00Z',
    }))
    await waitFor(() => expect(kasLoginSources).toHaveBeenCalledTimes(2))
    fireEvent.click(screen.getByTestId('kiro-sign-in-keep-current'))
    expect(await screen.findByTestId('kiro-sign-in-sources')).toHaveAttribute('data-notice', 'two_accounts')
  })

  it.each([false, true])('a failed sources read renders an error notice and keeps sign-in controls working (authenticated=%s)', async authenticated => {
    kasLoginStatus.mockResolvedValue(authenticated ? SIGNED_IN : SIGNED_OUT)
    kasLoginSources.mockRejectedValue(new Error('boom'))
    renderWithProviders(<KiroSignInCard />)
    const error = await screen.findByTestId('kiro-sign-in-sources-error')
    expect(error).toHaveAttribute('role', 'alert')
    expect(error).toHaveTextContent('Could not check which account Kiro Agent uses')
    expect(error).toHaveTextContent('boom')
    expect(screen.queryByTestId('kiro-sign-in-sources')).toBeNull()
    if (authenticated) fireEvent.click(screen.getByRole('button', { name: 'Switch account' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with GitHub' }))
    expect(await screen.findByTestId('kas-login-user-code')).toHaveTextContent('ABCD-EFGH')
  })

  it.each([
    ['title', 'These may be different accounts'],
    ['stakes', "If these are different accounts, Kiro Agent is not using your Kiro CLI account's quota."],
    ['removal', 'Removes the sign-in stored here.'],
  ])('the uncertain-account notice explains its %s before a click', async (part, copy) => {
    kasLoginStatus.mockResolvedValue(SIGNED_IN)
    kasLoginSources.mockResolvedValue({ ...TWO_ACCOUNTS, same_account: null })
    renderWithProviders(<KiroSignInCard />)
    const notice = await screen.findByTestId('kiro-sign-in-sources')
    expect(notice).toHaveTextContent(copy)
    if (part === 'title') expect(screen.getByText(copy)).toHaveClass('font-medium')
    expect(screen.getByTestId('kiro-sign-in-use-kiro-cli')).toBeInTheDocument()
    expect(kasLoginLogout).not.toHaveBeenCalled()
  })
})

describe('sourcesNotice', () => {
  it.each([
    [undefined, null],
    [NONE, null],
    [{ ...NONE, active: 'unknown' as const }, null],
    [ONLY_KIRO_CLI, 'using_kiro_cli'],
    [TWO_ACCOUNTS, 'two_accounts'],
    [{ ...TWO_ACCOUNTS, same_account: null }, 'maybe_two_accounts'],
    // One account in both places is not something to warn about.
    [{ ...TWO_ACCOUNTS, same_account: true }, null],
    // A kiro-cli sign-in no session could run on is not an account to warn about.
    [{ ...TWO_ACCOUNTS, kiro_cli: { stored: true, usable: false } }, null],
    // A store that could not be read is unknown, never "two accounts".
    [{ ...TWO_ACCOUNTS, kiro_cli: { stored: null, usable: false } }, null],
  ])('%#', (sources, expected) => {
    expect(sourcesNotice(sources as KasLoginSources | undefined)).toBe(expected)
  })
})
