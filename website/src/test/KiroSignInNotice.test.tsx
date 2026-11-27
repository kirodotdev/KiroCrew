import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'

import { renderWithProviders, createTestStore } from './helpers'
import { sseConnected } from '../store/dashboardSlice'
import { namedCeiling } from './namedCeiling'
import KiroSignInNotice, { KIRO_SIGN_IN_NOTICE_POLL_MS } from '../components/KiroSignInNotice'
import KiroSignInNoticeFallback from '../components/KiroSignInNoticeFallback'
import ErrorBoundary from '../components/ErrorBoundary'
import { NavigationLeaveGuardProvider, useRegisterNavigationLeaveGuard } from '../components/NavigationLeaveGuard'
import { api, type KasLoginStatus } from '../api/client'
import { ApiError } from '../api/apiError'
import { __resetGatewayConnectedForTests, setGatewayConnected } from '../utils/errorReport'

const navigate = vi.fn()
vi.mock('react-router-dom', async importOriginal => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => navigate,
}))

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kasLoginStatus: vi.fn(),
      kirocrewConfig: vi.fn(),
    },
  }
})

// Whether the build offers Kiro sign-in (`useKiroSignInOffered`): unanswered by
// default, as before the schema read lands.
const signInOffered = vi.hoisted(() => ({ value: undefined as boolean | undefined, error: null as Error | null }))
vi.mock('../pages/developer/useKiroSignInOffered', () => ({
  useKiroSignInOffered: () => signInOffered.value,
  useKiroSignInOfferedQuery: () => ({ offered: signInOffered.value, error: signInOffered.error }),
}))

const kasLoginStatus = vi.mocked(api.kasLoginStatus)
const kirocrewConfig = vi.mocked(api.kirocrewConfig)

// Token-free status bodies, exactly the fields `/api/kas-login` returns.
const HEALTHY: KasLoginStatus = {
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
/** Access token past the margin and nothing to renew it with. */
const EXPIRED: KasLoginStatus = { ...HEALTHY, expires_at: '2000-01-01T00:00:00Z', expired: true, has_refresh_token: false, usable: false }
/** The issuer refused the last renewal; `usable` stays true (see auth/service.py status). */
const REFUSED: KasLoginStatus = { ...HEALTHY, refresh_rejected: true }
/**
 * The usual refusal: the renewal tick only asks inside the refresh margin, so
 * the access token is already `expired` when the refusal lands. `usable` still
 * reads true, because a refresh token is stored.
 */
const REFUSED_EXPIRED: KasLoginStatus = { ...REFUSED, expires_at: '2000-01-01T00:00:00Z', expired: true }
/** The one body every expired shape shows, whatever the cause or audience. */
const EXPIRED_BODY = 'Sign in again to resume sessions and agent work.'
const SIGNED_OUT: KasLoginStatus = { ...HEALTHY, authenticated: false, provider: '', identity: '', expires_at: null, has_refresh_token: false, usable: false }

const notice = () => screen.queryByTestId('kiro-sign-in-notice')
const refused = () => screen.queryByTestId('kiro-sign-in-refused')
const failure = () => screen.queryByTestId('kiro-sign-in-status-error')

// Every wait below sits behind a chained query: the config read lands, which
// enables the status read, whose answer decides the render.
const chain = () => namedCeiling('KIRO_SIGN_IN_NOTICE_CHAIN', 5000)
const findNotice = () => screen.findByTestId('kiro-sign-in-notice', {}, chain())
const findRefused = () => screen.findByTestId('kiro-sign-in-refused', {}, chain())
const findFailure = () => screen.findByTestId('kiro-sign-in-status-error', {}, chain())

function backend(id: string) {
  kirocrewConfig.mockResolvedValue({ agent: { acp_backend: id } } as Awaited<ReturnType<typeof api.kirocrewConfig>>)
}

/** A page below the notice that answers the leave guard (holds a draft, or not). */
function NavigationVeto({ guard }: { guard: () => boolean }) {
  useRegisterNavigationLeaveGuard(guard)
  return null
}

/** Let a landed answer's render commit before an absence check. */
const settle = () => act(async () => { await Promise.resolve() })

beforeEach(() => {
  signInOffered.value = undefined
  signInOffered.error = null
  navigate.mockReset()
  kasLoginStatus.mockReset()
  kirocrewConfig.mockReset()
})

// Unmount first so no query timer fires on the fake clock, then restore real
// timers even when a fake-clock test fails part-way.
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('KiroSignInNotice', () => {
  it('shows an expired sign-in with nothing to renew it at the same weight as a refusal', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(EXPIRED)
    renderWithProviders(<KiroSignInNotice />)
    const el = await findNotice()
    // The agents cannot run: not a quieter warning beside a red refusal.
    expect(el).toHaveAttribute('role', 'alert')
    expect(el).toHaveTextContent(/^Sign-in expired\./)
    expect(el).toHaveTextContent(EXPIRED_BODY)
    // The agent the hand-off would open runs as this same lapsed sign-in.
    expect(within(el).queryByRole('button', { name: /Ask the agent/i })).not.toBeInTheDocument()
    expect(within(el).getByTestId('kiro-sign-in-notice-open')).toBeInTheDocument()
    expect(refused()).not.toBeInTheDocument()
  })

  it('shows a refused renewal as an error under its own title, with the hand-off and the way to fix it', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(REFUSED)
    renderWithProviders(<KiroSignInNotice />)
    const el = await findRefused()
    expect(el).toHaveAttribute('role', 'alert')
    // Not "expired": the sign-in still works until its access token runs out.
    expect(el).toHaveTextContent(/^Kiro refused to renew your sign-in/)
    expect(el).not.toHaveTextContent('Sign-in expired.')
    expect(el).toHaveTextContent('Agents will stop when this sign-in expires')
    expect(within(el).getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
    fireEvent.click(within(el).getByTestId('kiro-sign-in-notice-open'))
    expect(await screen.findByTestId('kiro-sign-in-dialog')).toBeInTheDocument()
    expect(navigate).not.toHaveBeenCalled()
    expect(notice()).not.toBeInTheDocument()
  })

  it('says only "Sign-in expired." once a refused sign-in has also expired, not that agents will stop', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(REFUSED_EXPIRED)
    renderWithProviders(<KiroSignInNotice />)
    const el = await findRefused()
    expect(el).toHaveAttribute('role', 'alert')
    expect(el).toHaveTextContent(/^Sign-in expired\./)
    expect(el).toHaveTextContent(EXPIRED_BODY)
    expect(el).not.toHaveTextContent('refused')
    expect(el).not.toHaveTextContent('Agents will stop')
    expect(within(el).queryByRole('button', { name: /Ask the agent/i })).not.toBeInTheDocument()
    expect(within(el).getByTestId('kiro-sign-in-notice-open')).toBeInTheDocument()
  })

  it('shows a dismissed refusal again once it has expired, since the agents have now stopped', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(REFUSED)
    const { queryClient } = renderWithProviders(<KiroSignInNotice />)
    fireEvent.click(within(await findRefused()).getByRole('button', { name: 'Dismiss' }))
    expect(refused()).not.toBeInTheDocument()

    kasLoginStatus.mockResolvedValue(REFUSED_EXPIRED)
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['kas-login'] }) })
    expect(await findRefused()).toHaveTextContent(EXPIRED_BODY)
  })

  it('opens the shared sign-in dialog in place, with the choices, never starting a sign-in itself', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(EXPIRED)
    const guard = vi.fn().mockReturnValue(false)
    renderWithProviders(
      <NavigationLeaveGuardProvider>
        <NavigationVeto guard={guard} />
        <KiroSignInNotice />
      </NavigationLeaveGuardProvider>,
    )
    fireEvent.click(within(await findNotice()).getByTestId('kiro-sign-in-notice-open'))
    const dialog = await screen.findByTestId('kiro-sign-in-dialog')
    // The page below is not left, so its leave guard is not asked and nothing navigates.
    expect(guard).not.toHaveBeenCalled()
    expect(navigate).not.toHaveBeenCalled()
    expect(within(dialog).getByRole('button', { name: 'Continue with Google' })).toBeInTheDocument()
  })

  it('renders a failed status read as an error with the agent hand-off, not as nothing', async () => {
    backend('kas')
    kasLoginStatus.mockRejectedValue(new Error('token_store_failed: the sign-in store could not be read'))
    renderWithProviders(<KiroSignInNotice />)
    const el = await findFailure()
    expect(el).toHaveAttribute('role', 'alert')
    expect(el).toHaveTextContent(/^We could not check sign-in status/)
    // What to do, in words. The server's code means nothing to the reader;
    // the hand-off still gets it from the report pinned to the rejection.
    expect(el).toHaveTextContent('Sign in again if your Kiro sign-in stopped working, or ask the agent to look into it.')
    expect(el).not.toHaveTextContent('token_store_failed')
    expect(within(el).getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
    expect(notice()).not.toBeInTheDocument()

    // The way to fix it without the agent: the shared sign-in dialog, in place.
    fireEvent.click(within(el).getByTestId('kiro-sign-in-notice-open'))
    expect(await screen.findByTestId('kiro-sign-in-dialog')).toBeInTheDocument()
    fireEvent.keyDown(screen.getByTestId('kiro-sign-in-dialog'), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('kiro-sign-in-dialog')).toBeNull())

    fireEvent.click(within(el).getByRole('button', { name: 'Dismiss' }))
    expect(failure()).not.toBeInTheDocument()
  })

  it('leaves a status read that failed while the gateway was down to the offline state (#32966)', async () => {
    backend('kas')
    kasLoginStatus.mockRejectedValue(new Error('Failed to fetch'))
    setGatewayConnected(false)
    try {
      renderWithProviders(<KiroSignInNotice />)
      await waitFor(() => expect(kasLoginStatus).toHaveBeenCalled(), chain())
      await settle()
      expect(failure()).not.toBeInTheDocument()
      expect(notice()).not.toBeInTheDocument()
    } finally {
      __resetGatewayConnectedForTests()
    }
  })

  it('shows nothing to a caller the status read refuses as not the owner, and does not ask again', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    backend('kas')
    const message = 'Only the dashboard owner can manage Kiro sign-in.'
    kasLoginStatus.mockRejectedValue(new ApiError(403, message, JSON.stringify({ error: message, code: 'owner_only' })))
    const { queryClient } = renderWithProviders(<KiroSignInNotice />)
    // Settled as a refusal, not still in flight: absence below is the answer.
    await waitFor(() => expect(queryClient.getQueryState(['kas-login'])?.status).toBe('error'), chain())
    await settle()
    expect(failure()).not.toBeInTheDocument()
    expect(notice()).not.toBeInTheDocument()
    // The same caller gets the same refusal: the poll does not ask again.
    await vi.advanceTimersByTimeAsync(KIRO_SIGN_IN_NOTICE_POLL_MS * 2)
    expect(kasLoginStatus).toHaveBeenCalledTimes(1)
  })

  it('says a backend-setting read that failed before any good read could not be read, with no sign-in claim', async () => {
    kirocrewConfig.mockRejectedValue(new Error('config read failed'))
    kasLoginStatus.mockResolvedValue(EXPIRED)
    const store = createTestStore()
    store.dispatch(sseConnected())
    renderWithProviders(<KiroSignInNotice />, { store })
    const el = await screen.findByTestId('kiro-sign-in-config-error', {}, chain())
    expect(el).toHaveAttribute('role', 'alert')
    expect(el).toHaveTextContent(/^Could not read the dashboard settings/)
    expect(el).toHaveTextContent('Reload the page to try again, or ask the agent to look into it.')
    // kiro-cli and Claude have no Kiro sign-in: nothing here may claim one.
    expect(el).not.toHaveTextContent(/sign-in/i)
    expect(within(el).queryByTestId('kiro-sign-in-notice-open')).not.toBeInTheDocument()
    expect(within(el).getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
    expect(failure()).not.toBeInTheDocument()
    expect(notice()).not.toBeInTheDocument()
    expect(kasLoginStatus).not.toHaveBeenCalled()

    fireEvent.click(within(el).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId('kiro-sign-in-config-error')).not.toBeInTheDocument()
  })

  it('leaves a failed first backend-setting read to the offline and token surfaces while they show', async () => {
    kirocrewConfig.mockRejectedValue(new Error('config read failed'))
    const store = createTestStore()
    // Offline: the gateway's transport has not connected.
    renderWithProviders(<KiroSignInNotice />, { store })
    await waitFor(() => expect(kirocrewConfig).toHaveBeenCalled(), chain())
    await settle()
    expect(screen.queryByTestId('kiro-sign-in-config-error')).not.toBeInTheDocument()

    // Online, but the gateway asks for a token: the token banner says so.
    act(() => {
      store.dispatch(sseConnected())
      window.dispatchEvent(new Event('mc-auth-required'))
    })
    await settle()
    expect(screen.queryByTestId('kiro-sign-in-config-error')).not.toBeInTheDocument()

    // Neither holds any more: the failure is this notice's to report.
    act(() => { window.dispatchEvent(new Event('mc-auth-cleared')) })
    expect(await screen.findByTestId('kiro-sign-in-config-error', {}, chain())).toBeInTheDocument()
  })

  it.each([
    ['kiro-cli', 'kiro'],
    ['Claude', 'claude'],
  ])('shows nothing for a backend-setting read that fails after one named %s', async (_name, id) => {
    backend(id)
    kasLoginStatus.mockResolvedValue(EXPIRED)
    const { queryClient } = renderWithProviders(<KiroSignInNotice />)
    await waitFor(() => expect(kirocrewConfig).toHaveBeenCalled(), chain())
    kirocrewConfig.mockRejectedValue(new Error('config read failed'))
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['kirocrewConfig'] }) })
    expect(queryClient.getQueryState(['kirocrewConfig'])?.status).toBe('error')
    await settle()
    expect(screen.queryByTestId('kiro-sign-in-config-error')).not.toBeInTheDocument()
    expect(failure()).not.toBeInTheDocument()
    expect(notice()).not.toBeInTheDocument()
    expect(kasLoginStatus).not.toHaveBeenCalled()
  })

  it('says so, with the hand-off and no sign-in claim, when the notice itself fails to load', async () => {
    function Broken(): never { throw new Error('Failed to fetch dynamically imported module') }
    renderWithProviders(
      <ErrorBoundary scope="kiro-sign-in-notice" fallback={error => <KiroSignInNoticeFallback error={error} />}>
        <Broken />
      </ErrorBoundary>,
    )
    const el = await screen.findByTestId('kiro-sign-in-notice-load-error')
    expect(el).toHaveAttribute('role', 'alert')
    expect(el).toHaveTextContent(/^Part of the dashboard could not load/)
    expect(el).not.toHaveTextContent(/sign-in/i)
    expect(within(el).getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
    fireEvent.click(within(el).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId('kiro-sign-in-notice-load-error')).not.toBeInTheDocument()
  })

  it('keeps checking the sign-in when the backend-setting read fails after one named KAS', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(EXPIRED)
    const { queryClient } = renderWithProviders(<KiroSignInNotice />)
    await findNotice()
    kirocrewConfig.mockRejectedValue(new Error('config read failed'))
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['kirocrewConfig'] }) })
    expect(queryClient.getQueryState(['kirocrewConfig'])?.status).toBe('error')
    expect(notice()).toBeInTheDocument()
    expect(failure()).not.toBeInTheDocument()
  })

  it('keeps a dismissed failure hidden across failed polls, and shows a new one after a good read', async () => {
    backend('kas')
    kasLoginStatus.mockRejectedValue(new Error('token_store_failed'))
    const { queryClient } = renderWithProviders(<KiroSignInNotice />)
    fireEvent.click(within(await findFailure()).getByRole('button', { name: 'Dismiss' }))

    // The same failure on the next read stays dismissed.
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['kas-login'] }) })
    expect(kasLoginStatus).toHaveBeenCalledTimes(2)
    expect(failure()).not.toBeInTheDocument()

    // A good read, then a failure again: that is a new failure.
    kasLoginStatus.mockResolvedValue(HEALTHY)
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['kas-login'] }) })
    kasLoginStatus.mockRejectedValue(new Error('token_store_failed'))
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['kas-login'] }) })
    expect(await findFailure()).toBeInTheDocument()
  })

  it('gives the refused notice the same three controls: the hand-off, the sign-in button and dismiss', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(REFUSED)
    renderWithProviders(<KiroSignInNotice />)
    const el = await findRefused()
    // The access token still works, so the agent the hand-off opens can run.
    expect(within(el).getAllByRole('button')).toHaveLength(3)
    expect(within(el).getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
    expect(within(el).getByTestId('kiro-sign-in-notice-open')).toBeInTheDocument()
    expect(within(el).getByRole('button', { name: 'Dismiss' })).toBeInTheDocument()
  })

  it.each([
    ['expired', EXPIRED, findNotice],
    ['refused and expired', REFUSED_EXPIRED, findRefused],
  ])('gives the %s notice only the sign-in button and dismiss: the agent would run as the lapsed sign-in', async (_name, status, find) => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(status)
    renderWithProviders(<KiroSignInNotice />)
    const el = await find()
    expect(within(el).getAllByRole('button')).toHaveLength(2)
    expect(within(el).queryByRole('button', { name: /Ask the agent/i })).not.toBeInTheDocument()
    expect(within(el).getByTestId('kiro-sign-in-notice-open')).toBeInTheDocument()
    expect(within(el).getByRole('button', { name: 'Dismiss' })).toBeInTheDocument()
  })

  it('stays dismissed for the same cause, and shows again when the cause changes', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(REFUSED)
    const { queryClient } = renderWithProviders(<KiroSignInNotice />)
    fireEvent.click(within(await findRefused()).getByRole('button', { name: 'Dismiss' }))
    expect(refused()).not.toBeInTheDocument()

    // The same refusal read again stays dismissed.
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['kas-login'] }) })
    expect(kasLoginStatus).toHaveBeenCalledTimes(2)
    expect(refused()).not.toBeInTheDocument()

    // A different cause is new information: the token has now expired, nothing renews it.
    kasLoginStatus.mockResolvedValue(EXPIRED)
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['kas-login'] }) })
    expect(await findNotice()).toBeInTheDocument()
  })

  it('is absent on Settings > Kiro account, where the sign-in card itself shows the state', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(REFUSED)
    renderWithProviders(<KiroSignInNotice />, { route: '/settings/kiro-account?highlight=key%3Akiro-sign-in' })
    await waitFor(() => expect(kasLoginStatus).toHaveBeenCalled(), chain())
    await settle()
    expect(refused()).not.toBeInTheDocument()
    expect(notice()).not.toBeInTheDocument()
  })

  it('is absent while the sign-in is healthy', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(HEALTHY)
    renderWithProviders(<KiroSignInNotice />)
    await waitFor(() => expect(kasLoginStatus).toHaveBeenCalled(), chain())
    await settle()
    expect(notice()).not.toBeInTheDocument()
    expect(refused()).not.toBeInTheDocument()
  })

  it('is absent with no stored sign-in: not signed in is not lapsed', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    renderWithProviders(<KiroSignInNotice />)
    await waitFor(() => expect(kasLoginStatus).toHaveBeenCalled(), chain())
    await settle()
    expect(notice()).not.toBeInTheDocument()
  })

  it.each([
    ['kiro-cli', 'kiro'],
    ['Claude', 'claude'],
  ])('never reads the sign-in on the %s backend of a build that offers no Kiro sign-in', async (_name, id) => {
    signInOffered.value = false
    backend(id)
    kasLoginStatus.mockResolvedValue(EXPIRED)
    renderWithProviders(<KiroSignInNotice />)
    await waitFor(() => expect(kirocrewConfig).toHaveBeenCalled(), chain())
    // Let the config land and any status read that would follow it run.
    await settle()
    expect(kasLoginStatus).not.toHaveBeenCalled()
    expect(notice()).not.toBeInTheDocument()
    expect(failure()).not.toBeInTheDocument()
  })

  it.each(['kiro', 'claude'])('shows a lapsed sign-in on the %s backend when its Bots run on KAS, naming the Bots', async id => {
    kirocrewConfig.mockResolvedValue({ agent: { acp_backend: id }, kas_in_use: true } as Awaited<ReturnType<typeof api.kirocrewConfig>>)
    kasLoginStatus.mockResolvedValue(EXPIRED)
    renderWithProviders(<KiroSignInNotice />)
    const el = await findNotice()
    // Expired says the same two lines whoever runs as the sign-in.
    expect(el).toHaveTextContent(/^Sign-in expired\./)
    expect(el).toHaveTextContent(EXPIRED_BODY)
    // The main agent runs on another credential, so its hand-off still works.
    expect(within(el).getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
    expect(within(el).getByTestId('kiro-sign-in-notice-open')).toBeInTheDocument()
  })

  it.each([
    [REFUSED, 'Bots will stop when this sign-in expires'],
    [REFUSED_EXPIRED, EXPIRED_BODY],
  ])('a refused renewal where only Bots run on KAS: Bots while it works, the expired copy once it lapses', async (status, text) => {
    kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'kiro' }, kas_in_use: true } as Awaited<ReturnType<typeof api.kirocrewConfig>>)
    kasLoginStatus.mockResolvedValue(status)
    renderWithProviders(<KiroSignInNotice />)
    const el = await findRefused()
    expect(el).toHaveTextContent(text)
    expect(within(el).getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
  })

  it.each(['kiro', 'claude'])('shows a STORED lapsed sign-in on the %s backend of a build that offers Kiro sign-in, naming the account features', async id => {
    signInOffered.value = true
    backend(id)
    kasLoginStatus.mockResolvedValue(EXPIRED)
    renderWithProviders(<KiroSignInNotice />)
    const el = await findNotice()
    expect(el).toHaveTextContent(/^Sign-in expired\./)
    // The agents keep running on another credential: only account features stop.
    expect(el).toHaveTextContent('Sign in again to use features that need your Kiro account.')
    expect(el).not.toHaveTextContent(/sessions|agent work/i)
    // The main agent runs on another credential, so its hand-off still works.
    expect(within(el).getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
  })

  it('never asks someone who never signed in, off KAS: no stored sign-in, no notice', async () => {
    signInOffered.value = true
    backend('claude')
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    renderWithProviders(<KiroSignInNotice />)
    await waitFor(() => expect(kasLoginStatus).toHaveBeenCalled(), chain())
    await settle()
    expect(notice()).not.toBeInTheDocument()
    expect(refused()).not.toBeInTheDocument()
  })

  it('reports a failed status read off KAS too, as an error with its way to the card', async () => {
    signInOffered.value = true
    backend('claude')
    kasLoginStatus.mockRejectedValue(new Error('token_store_failed: the sign-in store could not be read'))
    renderWithProviders(<KiroSignInNotice />)
    const el = await findFailure()
    // Its way in is the shared sign-in dialog's button.
    expect(within(el).getByTestId('kiro-sign-in-notice-open')).toBeInTheDocument()
  })

  it('says so off KAS when the settings schema read fails, instead of leaving the sign-in unchecked', async () => {
    signInOffered.error = new Error('Config schema fetch failed: 500')
    backend('claude')
    const store = createTestStore()
    store.dispatch(sseConnected())
    renderWithProviders(<KiroSignInNotice />, { store })
    const el = await screen.findByTestId('kiro-sign-in-schema-error', {}, chain())
    expect(el).toHaveTextContent('Could not read the dashboard settings')
    expect(kasLoginStatus).not.toHaveBeenCalled()
    fireEvent.click(within(el).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId('kiro-sign-in-schema-error')).not.toBeInTheDocument()
  })

  it('leaves a failed schema read alone on KAS, where the status read runs anyway', async () => {
    signInOffered.error = new Error('Config schema fetch failed: 500')
    backend('kas')
    kasLoginStatus.mockResolvedValue(HEALTHY)
    const store = createTestStore()
    store.dispatch(sseConnected())
    renderWithProviders(<KiroSignInNotice />, { store })
    await waitFor(() => expect(kasLoginStatus).toHaveBeenCalled(), chain())
    await settle()
    expect(screen.queryByTestId('kiro-sign-in-schema-error')).not.toBeInTheDocument()
  })

  it('never reads the sign-in on a KAS-named backend the gateway says nothing runs on', async () => {
    signInOffered.value = false
    kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'kas' }, kas_in_use: false } as Awaited<ReturnType<typeof api.kirocrewConfig>>)
    kasLoginStatus.mockResolvedValue(EXPIRED)
    renderWithProviders(<KiroSignInNotice />)
    await waitFor(() => expect(kirocrewConfig).toHaveBeenCalled(), chain())
    await settle()
    expect(kasLoginStatus).not.toHaveBeenCalled()
    expect(notice()).not.toBeInTheDocument()
  })

  it('clears as soon as a new sign-in lands on the shared status entry', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(EXPIRED)
    const { queryClient } = renderWithProviders(<KiroSignInNotice />)
    await findNotice()
    // What the sign-in flow does when its credential lands (KasLoginGate
    // invalidates `['kas-login']`), so the notice does not wait for its poll.
    kasLoginStatus.mockResolvedValue(HEALTHY)
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['kas-login'] }) })
    await waitFor(() => expect(notice()).not.toBeInTheDocument(), chain())
  })

  it('re-reads the status on its own interval, so a refusal or a renewal shows without a reload', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    backend('kas')
    kasLoginStatus.mockResolvedValue(HEALTHY)
    renderWithProviders(<KiroSignInNotice />)
    await waitFor(() => expect(kasLoginStatus).toHaveBeenCalledTimes(1), chain())
    expect(refused()).not.toBeInTheDocument()

    // The gateway's renewal tick records a refusal between two reads.
    kasLoginStatus.mockResolvedValue(REFUSED)
    await vi.advanceTimersByTimeAsync(KIRO_SIGN_IN_NOTICE_POLL_MS)
    await waitFor(() => expect(refused()).toBeInTheDocument(), chain())

    // A successful renewal (or a new sign-in) clears it on the next read.
    kasLoginStatus.mockResolvedValue(HEALTHY)
    await vi.advanceTimersByTimeAsync(KIRO_SIGN_IN_NOTICE_POLL_MS)
    await waitFor(() => expect(refused()).not.toBeInTheDocument(), chain())
  })
})
