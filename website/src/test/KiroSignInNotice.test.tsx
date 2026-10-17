import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'

import { renderWithProviders, createTestStore } from './helpers'
import { sseConnected } from '../store/dashboardSlice'
import { namedCeiling } from './namedCeiling'
import KiroSignInNotice, { KIRO_SIGN_IN_NOTICE_POLL_MS } from '../components/KiroSignInNotice'
import KiroSignInNoticeFallback from '../components/KiroSignInNoticeFallback'
import ErrorBoundary from '../components/ErrorBoundary'
import { NavigationLeaveGuardProvider, useRegisterNavigationLeaveGuard } from '../components/NavigationLeaveGuard'
import { KIRO_SIGN_IN_PATH } from '../pages/developer/kiroSignInLink'
import { api, type KasLoginStatus } from '../api/client'

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
    expect(el).toHaveTextContent(/^Kiro sign-in expired/)
    expect(el).toHaveTextContent('Agents cannot run until you sign in again')
    // The agent the hand-off would open runs as this same lapsed sign-in.
    expect(within(el).queryByRole('button', { name: /Ask the agent/i })).not.toBeInTheDocument()
    expect(within(el).getByRole('button', { name: 'Open sign-in settings' })).toBeInTheDocument()
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
    expect(el).not.toHaveTextContent('Kiro sign-in expired')
    expect(el).toHaveTextContent('Agents will stop when this sign-in expires')
    expect(within(el).getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
    fireEvent.click(within(el).getByRole('button', { name: 'Open sign-in settings' }))
    expect(navigate).toHaveBeenCalledWith(KIRO_SIGN_IN_PATH)
    expect(notice()).not.toBeInTheDocument()
  })

  it('says the agents cannot run once a refused sign-in has also expired, not that they will stop', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(REFUSED_EXPIRED)
    renderWithProviders(<KiroSignInNotice />)
    const el = await findRefused()
    expect(el).toHaveAttribute('role', 'alert')
    expect(el).toHaveTextContent(/^Kiro refused to renew your sign-in/)
    expect(el).toHaveTextContent('Agents cannot run until you sign in again. This sign-in has already expired.')
    expect(el).not.toHaveTextContent('Agents will stop')
    expect(within(el).queryByRole('button', { name: /Ask the agent/i })).not.toBeInTheDocument()
    expect(within(el).getByRole('button', { name: 'Open sign-in settings' })).toBeInTheDocument()
  })

  it('shows a dismissed refusal again once it has expired, since the agents have now stopped', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(REFUSED)
    const { queryClient } = renderWithProviders(<KiroSignInNotice />)
    fireEvent.click(within(await findRefused()).getByRole('button', { name: 'Dismiss' }))
    expect(refused()).not.toBeInTheDocument()

    kasLoginStatus.mockResolvedValue(REFUSED_EXPIRED)
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['kas-login'] }) })
    expect(await findRefused()).toHaveTextContent('This sign-in has already expired.')
  })

  it('names where its button goes, and opens the sign-in card instead of starting a sign-in', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(EXPIRED)
    renderWithProviders(<KiroSignInNotice />)
    const el = await findNotice()
    fireEvent.click(within(el).getByRole('button', { name: 'Open sign-in settings' }))
    expect(navigate).toHaveBeenCalledWith(KIRO_SIGN_IN_PATH)
  })

  it('asks the page it sits above before leaving it, so an unsaved draft there is not discarded', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(EXPIRED)
    const guard = vi.fn().mockReturnValue(false)
    renderWithProviders(
      <NavigationLeaveGuardProvider>
        <NavigationVeto guard={guard} />
        <KiroSignInNotice />
      </NavigationLeaveGuardProvider>,
    )
    const open = within(await findNotice()).getByRole('button', { name: 'Open sign-in settings' })
    fireEvent.click(open)
    expect(guard).toHaveBeenCalledTimes(1)
    expect(navigate).not.toHaveBeenCalled()

    guard.mockReturnValue(true)
    fireEvent.click(open)
    expect(navigate).toHaveBeenCalledWith(KIRO_SIGN_IN_PATH)
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
    expect(el).toHaveTextContent('Open sign-in settings to check your Kiro sign-in, or ask the agent to look into it.')
    expect(el).not.toHaveTextContent('token_store_failed')
    expect(within(el).getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
    expect(notice()).not.toBeInTheDocument()

    // The way to fix it without the agent: the card, which this notice yields to.
    fireEvent.click(within(el).getByRole('button', { name: 'Open sign-in settings' }))
    expect(navigate).toHaveBeenCalledWith(KIRO_SIGN_IN_PATH)

    fireEvent.click(within(el).getByRole('button', { name: 'Dismiss' }))
    expect(failure()).not.toBeInTheDocument()
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
    expect(within(el).queryByRole('button', { name: 'Open sign-in settings' })).not.toBeInTheDocument()
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
    ['kiro-cli', ''],
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
    expect(within(el).getByRole('button', { name: 'Open sign-in settings' })).toBeInTheDocument()
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
    expect(within(el).getByRole('button', { name: 'Open sign-in settings' })).toBeInTheDocument()
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

  it('is absent on Settings > Agent Harness, where the sign-in card itself shows the state', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(REFUSED)
    renderWithProviders(<KiroSignInNotice />, { route: '/settings/agent?highlight=key%3Akiro-sign-in' })
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
    ['kiro-cli', ''],
    ['Claude', 'claude'],
  ])('never reads the sign-in on the %s backend, where it concerns no agent', async (_name, id) => {
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
