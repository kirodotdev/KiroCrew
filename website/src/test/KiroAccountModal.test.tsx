import { useQuery, useQueryClient } from '@tanstack/react-query'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import KiroAccountModal, { type KiroAccountUsage } from '../components/KiroAccountModal'
import { api } from '../api/client'
import type { KiroCreditUsage } from '../api/client'
import type { KiroUsageState } from '../api/kiroUsage'
import { installSoftNavigate, __resetNavSeamForTests } from '../utils/errorReport'
import { renderWithProviders } from './helpers'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      sessionsUsageRefresh: vi.fn(),
    },
  }
})

const refreshMock = vi.mocked(api.sessionsUsageRefresh)

const BASE_USAGE: KiroCreditUsage = {
  used: 10,
  limit: 100,
  overage: 0,
  bonusCredits: [],
  stale: false,
  email: 'owner@example.com',
  accountType: 'SocialGoogle',
}

describe('KiroAccountModal', () => {
  beforeEach(() => {
    localStorage.removeItem('kirocrew:account-email-hidden')
    __resetNavSeamForTests()
    sessionStorage.clear()
    installSoftNavigate(() => {})
  })

  afterEach(() => {
    __resetNavSeamForTests()
  })

  it('combines owner identity, plan, remaining credits, and billing details', async () => {
    renderWithProviders(
      <KiroAccountModal
        open
        onClose={vi.fn()}
        usage={{
          ...BASE_USAGE,
          used: 636,
          limit: 2_000,
          overage: 0,
          resets: '2026-08-01',
          plan: 'KIRO PRO+',
          overageRate: 0.04,
          costUsd: 0,
          bonusCredits: [
            { name: 'Welcome bonus', used: 500, total: 500, daysLeft: 13 },
            { name: 'Amb-Kiro-crew-test', used: 185.84, total: 2_000, daysLeft: 153 },
          ],
        }}
      />,
    )

    expect(await screen.findByText('owner@example.com')).toBeInTheDocument()
    expect(screen.getByText('OW')).toBeInTheDocument()
    expect(screen.getByText('Signed in with Google')).toBeInTheDocument()
    expect(screen.getByText('KIRO PRO+')).toBeInTheDocument()
    expect(screen.getByText(/Remaining credit balance: 1,364/)).toBeInTheDocument()
    expect(screen.getByText('$0.04 / credit')).toBeInTheDocument()
    expect(screen.getByText('$0.00')).toBeInTheDocument()
    expect(screen.getByText('Bonus credits')).toBeInTheDocument()
    expect(screen.getByText('Welcome bonus')).toBeInTheDocument()
    expect(screen.getByText('Amb-Kiro-crew-test')).toBeInTheDocument()
    expect(screen.getByText(/Remaining credit balance: 1,814.16/)).toBeInTheDocument()
    expect(screen.getByText(/Used: 185.84 \/ 2,000/)).toBeInTheDocument()
    expect(screen.getByText(/Days until expiration: 153/)).toBeInTheDocument()

    const progress = screen.getByRole('progressbar', { name: 'Kiro credit usage' })
    expect(progress).toHaveAttribute('aria-valuemin', '0')
    expect(progress).toHaveAttribute('aria-valuemax', '2000')
    expect(progress).toHaveAttribute('aria-valuenow', '636')

    const manage = screen.getByRole('link', { name: /Manage account/ })
    expect(manage).toHaveAttribute('href', 'https://app.kiro.dev/settings/account')
    expect(manage).toHaveAttribute('target', '_blank')
    expect(manage).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('caps the bar and remaining credits when usage exceeds the plan', async () => {
    renderWithProviders(
      <KiroAccountModal
        open
        onClose={vi.fn()}
        usage={{ ...BASE_USAGE, used: 2_500, limit: 2_000, overage: 500 }}
      />,
    )

    expect(await screen.findByText('owner@example.com')).toBeInTheDocument()
    expect(screen.getByText(/Remaining credit balance: 0/)).toBeInTheDocument()
    expect(screen.getByText('125%')).toBeInTheDocument()
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '2000')
  })

  it('keeps the panel useful when structured identity is unavailable', async () => {
    renderWithProviders(
      <KiroAccountModal
        open
        onClose={vi.fn()}
        usage={{ ...BASE_USAGE, email: undefined, account: undefined }}
      />,
    )

    expect(await screen.findByText('Account details unavailable')).toBeInTheDocument()
    expect(screen.getByText(/Remaining credit balance: 90/)).toBeInTheDocument()
  })

  it('hides email by default and persists an explicit visibility choice', async () => {
    const firstRender = renderWithProviders(
      <KiroAccountModal open onClose={vi.fn()} usage={BASE_USAGE} />,
    )

    const email = await screen.findByText('owner@example.com')
    expect(email).toHaveClass('blur-[5px]')
    expect(localStorage.getItem('kirocrew:account-email-hidden')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Show email' }))
    expect(email).not.toHaveClass('blur-[5px]')
    expect(localStorage.getItem('kirocrew:account-email-hidden')).toBe('0')

    firstRender.unmount()
    renderWithProviders(
      <KiroAccountModal open onClose={vi.fn()} usage={BASE_USAGE} />,
    )

    const persistedEmail = await screen.findByText('owner@example.com')
    expect(persistedEmail).not.toHaveClass('blur-[5px]')
    fireEvent.click(screen.getByRole('button', { name: 'Hide email' }))
    expect(persistedEmail).toHaveClass('blur-[5px]')
    expect(localStorage.getItem('kirocrew:account-email-hidden')).toBe('1')
  })

  it('keeps the generic label for an unspecified social provider', async () => {
    renderWithProviders(
      <KiroAccountModal
        open
        onClose={vi.fn()}
        usage={{ ...BASE_USAGE, accountType: 'Social' }}
      />,
    )

    expect(await screen.findByText('Signed in with Social login')).toBeInTheDocument()
  })

  it('renders independent identity and usage failure states', async () => {
    renderWithProviders(<KiroAccountModal open onClose={vi.fn()} usage="none" />)

    expect(await screen.findByText('Account details unavailable')).toBeInTheDocument()
    // `none` names the absence of a reading; only `failed` reports a read that failed.
    expect(screen.getByText('No balance reading is available for this account yet.')).toBeInTheDocument()
    expect(screen.queryByText('Could not read your balance.')).not.toBeInTheDocument()
  })

  it('states the failure instead of spinning when the fetch failed cold', async () => {
    // 'failed' and null are both "no reading", but only null still has a fetch
    // outstanding. Spinning on 'failed' would repeat, one level down, the defect
    // the top-bar pill was fixed for: the drill-in must not claim it is checking.
    renderWithProviders(<KiroAccountModal open onClose={vi.fn()} usage="failed" />)

    expect(await screen.findByText('Account details unavailable')).toBeInTheDocument()
    expect(screen.getByText('Could not read your balance.')).toBeInTheDocument()
    expect(screen.queryByText('Checking account…')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Checking credit usage')).not.toBeInTheDocument()
  })

  it('renders a cold failure through ErrorNotice with the agent hand-off, Refresh under it', async () => {
    // 'failed' is a failure to recover from, not a configuration the account is
    // in, so it takes the shared error surface. The hand-off opens the chat this
    // modal sits over, so the modal closes with it; Refresh stays as the retry.
    const onClose = vi.fn()
    renderWithProviders(<KiroAccountModal open onClose={onClose} usage="failed" />)

    const dialog = await screen.findByRole('dialog', { name: 'Kiro Account' })
    const alert = within(dialog).getByRole('alert')
    expect(alert).toHaveTextContent('Could not read your balance.')
    expect(within(dialog).getByRole('button', { name: /^Refresh$/ })).toBeEnabled()
    const handoff = within(dialog).getByRole('button', { name: /Ask the agent/i })
    fireEvent.click(handoff)
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })

  it('keeps spinning only while the reading is genuinely still in flight', async () => {
    renderWithProviders(<KiroAccountModal open onClose={vi.fn()} usage={null} />)

    expect(await screen.findByText('Checking account…')).toBeInTheDocument()
    expect(screen.queryByText('Account details unavailable')).not.toBeInTheDocument()
  })

  it('explains API-key auth instead of spinning or claiming a generic failure', async () => {
    // 'api-key' is terminal by construction: the usage API needs an SSO/OIDC
    // token that auth type never has (#5728). The panel must say so — not spin,
    // and not show the generic unavailable line that reads as a transient error.
    renderWithProviders(<KiroAccountModal open onClose={vi.fn()} usage="api-key" />)

    expect(
      await screen.findByText('Credit usage isn’t available for API key authentication'),
    ).toBeInTheDocument()
    expect(screen.queryByText('Could not read your balance.')).not.toBeInTheDocument()
    expect(screen.queryByText('Checking account…')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Checking credit usage')).not.toBeInTheDocument()
  })

  it('tells the user to sign in again and offers no Refresh for it', async () => {
    // 'signin-required' is terminal until the user re-authenticates. The /usage
    // read needs the same sign-in, so a Refresh here could only fail again.
    renderWithProviders(<KiroAccountModal open onClose={vi.fn()} usage="signin-required" />)

    expect(await screen.findByText(/Sign in to Kiro again/)).toBeInTheDocument()
    expect(screen.queryByText(/usage_text_scrape_enabled/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Refresh$/ })).not.toBeInTheDocument()
    expect(screen.queryByText('Could not read your balance.')).not.toBeInTheDocument()
    expect(screen.queryByText('Checking account…')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Checking credit usage')).not.toBeInTheDocument()
  })

  it('offers the agent hand-off on an expired sign-in and closes the modal with it', async () => {
    // An expired sign-in is the one unreadable state the user can act on, so it
    // renders through the shared ErrorNotice rather than a hand-written notice
    // (AUTOSDE errors-use-error-notice). The hand-off opens the chat this modal
    // sits over, so the modal must close with it: a hand-off the user cannot see
    // reads as a dead button.
    const onClose = vi.fn()
    renderWithProviders(
      <KiroAccountModal open onClose={onClose} usage="signin-required" />,
    )

    const handoff = await screen.findByRole('button', { name: /Ask the agent/i })
    expect(onClose).not.toHaveBeenCalled()

    fireEvent.click(handoff)

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })

  it('leaves the hand-off off the state that reports a configuration, not a failure', async () => {
    // 'api-key' describes how the account is set up; there is no error for the
    // agent to act on, so it keeps the passive notice. This pins the split — a
    // blanket migration would put a recovery button next to a line that needs
    // no recovery.
    for (const usage of ['api-key'] as const) {
      const { unmount } = renderWithProviders(
        <KiroAccountModal open onClose={vi.fn()} usage={usage} />,
      )

      expect(
        screen.queryByRole('button', { name: /Ask the agent/i }),
      ).not.toBeInTheDocument()

      unmount()
    }
  })

  it('calls onClose from the accessible close control', async () => {
    const onClose = vi.fn()
    renderWithProviders(
      <KiroAccountModal open onClose={onClose} usage={BASE_USAGE} />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce())
  })

  it('exposes a named modal dialog without an explicit ariaLabel', async () => {
    renderWithProviders(
      <KiroAccountModal open onClose={vi.fn()} usage={BASE_USAGE} />,
    )

    // The name comes from the rendered title (icon + text) via Modal's
    // aria-labelledby default, so it cannot fall out of sync with the header.
    const dialog = await screen.findByRole('dialog', { name: 'Kiro Account' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
  })
})

/**
 * Mirrors App.tsx: the modal's `usage` prop is derived from the `['kiro-usage']`
 * query, and the Refresh button writes its result into that same query. So a
 * successful click must re-render the meter through the prop, not through
 * component-local state — this harness is what makes that path testable.
 */
function QueryFedModal({ initial, onClose = vi.fn() }: { initial: KiroAccountUsage; onClose?: () => void }) {
  const qc = useQueryClient()
  const { data } = useQuery<KiroUsageState>({
    queryKey: ['kiro-usage'],
    // The refetch an invalidation triggers must not undo the written value.
    queryFn: () => (qc.getQueryData<KiroUsageState>(['kiro-usage']) ?? null),
    initialData: initial === 'failed' ? null : initial,
  })
  // A 'failed' start seeds the query with null (nothing cached) and shows the
  // failed notice until a refresh writes a reading into the query.
  return <KiroAccountModal open onClose={onClose} usage={data ?? (initial === 'failed' ? 'failed' : null)} />
}

const REFUSAL = (status: number, body: string) =>
  Object.assign(new Error(`HTTP ${status}`), { status, body })

const REFRESH = { name: /^Refresh$/ }
const REFRESHING = { name: 'Refreshing…' }

describe('KiroAccountModal refresh', () => {
  // The refresh-outcome tests start from the passive 'none' state: 'failed'
  // renders its own ErrorNotice, which would shadow the refresh's notice.
  beforeEach(() => {
    refreshMock.mockReset()
  })

  it('reports an unreadable gateway config through ErrorNotice and retries the config read', async () => {
    // The top bar could not learn which harness runs, so nothing about the
    // balance is established. The failure takes the shared error surface with
    // its hand-off, and Refresh under it re-asks for BOTH the config and the
    // balance; opening on this state already re-asks for the config once.
    refreshMock.mockResolvedValue({ usage: { available: false } })
    const onClose = vi.fn()
    // Mounted closed first so the spy is in place before the content (and its
    // opening re-ask) mounts: Modal renders its children only while open.
    const { queryClient, rerender } = renderWithProviders(
      <KiroAccountModal open={false} onClose={onClose} usage="config-unreadable" />,
    )
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    rerender(<KiroAccountModal open onClose={onClose} usage="config-unreadable" />)
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['kirocrewConfig'] }))
    const dialog = screen.getByRole('dialog', { name: 'Kiro Account' })
    const alert = within(dialog).getByRole('alert')
    expect(alert).toHaveTextContent('Could not load settings, so your balance can’t be shown.')
    expect(within(alert).getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
    expect(within(dialog).queryByRole('progressbar')).not.toBeInTheDocument()

    invalidate.mockClear()
    fireEvent.click(within(dialog).getByRole('button', REFRESH))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['kirocrewConfig'] })
    await waitFor(() => expect(refreshMock).toHaveBeenCalledTimes(1))

    // The hand-off opens the chat under this overlay, so it closes the modal.
    fireEvent.click(within(alert).getByRole('button', { name: /Ask the agent/i }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })

  it('offers Refresh under the no-reading notice and beside a live reading, nowhere else', () => {
    // The two no-reading states a refresh can fill: primary action under the
    // notice, each state with its own sentence.
    for (const [usage, notice] of [['failed', 'Could not read your balance.'], ['none', 'No balance reading is available for this account yet.']] as const) {
      const { unmount } = renderWithProviders(<KiroAccountModal open onClose={vi.fn()} usage={usage} />)
      expect(screen.getByText(notice)).toBeInTheDocument()
      expect(screen.getByRole('button', REFRESH)).toBeEnabled()
      unmount()
    }
    // A live reading keeps a compact Refresh beside the meter.
    {
      const { unmount } = renderWithProviders(<KiroAccountModal open onClose={vi.fn()} usage={BASE_USAGE} />)
      expect(screen.getByRole('progressbar')).toBeInTheDocument()
      expect(screen.getByRole('button', REFRESH)).toBeEnabled()
      unmount()
    }
    // API-key auth has no credit readout to refresh; an expired sign-in would
    // fail again; a loading cache has nothing to refresh yet.
    for (const usage of ['api-key', 'signin-required', null] as const) {
      const { unmount } = renderWithProviders(<KiroAccountModal open onClose={vi.fn()} usage={usage} />)
      expect(screen.queryByRole('button', REFRESH)).not.toBeInTheDocument()
      unmount()
    }
  })

  it('uses the one verb and no cost words anywhere in the modal', () => {
    for (const usage of ['failed', BASE_USAGE] as const) {
      const { unmount } = renderWithProviders(<KiroAccountModal open onClose={vi.fn()} usage={usage} />)
      const dialog = screen.getByRole('dialog', { name: 'Kiro Account' })
      expect(within(dialog).getAllByRole('button', REFRESH)).toHaveLength(1)
      expect(dialog.textContent).not.toMatch(/credits?\)|spend|spent|Check balance|only the person/i)
      unmount()
    }
  })

  it('disables the button and says it is refreshing while the POST is in flight', async () => {
    let settle: (v: { usage: Record<string, unknown> }) => void = () => {}
    refreshMock.mockImplementation(() => new Promise(resolve => { settle = resolve }))
    renderWithProviders(<QueryFedModal initial="failed" />)

    fireEvent.click(screen.getByRole('button', REFRESH))

    const pending = await screen.findByRole('button', REFRESHING)
    expect(pending).toBeDisabled()
    expect(pending).toHaveAttribute('aria-busy', 'true')
    expect(refreshMock).toHaveBeenCalledTimes(1)
    // A second click while pending cannot reach the mutation: the button is
    // disabled, so one click storm is one refresh.
    fireEvent.click(pending)
    expect(refreshMock).toHaveBeenCalledTimes(1)

    settle({ usage: { credits_plan: 1000, credits_used: 41 } })
    await screen.findByRole('progressbar', { name: 'Kiro credit usage' })
  })

  it('renders the meter from the refreshed payload and keeps Refresh beside it', async () => {
    refreshMock.mockResolvedValue({
      usage: { credits_plan: 2000, credits_used: 636, plan: 'KIRO PRO+', resets: '2026-08-01' },
    })
    const { queryClient } = renderWithProviders(<QueryFedModal initial="failed" />)

    fireEvent.click(screen.getByRole('button', REFRESH))

    const progress = await screen.findByRole('progressbar', { name: 'Kiro credit usage' })
    expect(progress).toHaveAttribute('aria-valuenow', '636')
    expect(progress).toHaveAttribute('aria-valuemax', '2000')
    expect(screen.getByText('KIRO PRO+')).toBeInTheDocument()
    // The notice is gone; the compact Refresh and the check time take its place.
    expect(screen.queryByText('Could not read your balance.')).not.toBeInTheDocument()
    expect(screen.getByText(/Checked at/)).toBeInTheDocument()
    expect(screen.getByRole('button', REFRESH)).toBeEnabled()
    // The pill's query holds the same reading, so the top bar updates too.
    const cached = queryClient.getQueryData<KiroUsageState>(['kiro-usage'])
    expect(cached).toMatchObject({ used: 636, limit: 2000 })
  })

  it('states a stale reading as a fact, with Refresh beside it', () => {
    renderWithProviders(<KiroAccountModal open onClose={vi.fn()} usage={{ ...BASE_USAGE, stale: true }} />)
    expect(
      screen.getByText('Showing an earlier reading; the latest refresh did not return one.'),
    ).toBeInTheDocument()
    expect(screen.queryByText(/may be/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', REFRESH)).toBeEnabled()
  })

  it('reports a 409 through the error surface with the hand-off, holds Refresh, and re-reads the query once it settles', async () => {
    // The gateway is already refreshing. The refusal is surfaced like every
    // other refresh outcome -- an ErrorNotice with the agent hand-off -- and
    // the modal re-reads the pill's query once after the other refresh has had
    // time to finish, so its result shows without another press.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      refreshMock.mockRejectedValue(REFUSAL(409, '{"code":"refresh_in_flight"}'))
      const onClose = vi.fn()
      const { queryClient } = renderWithProviders(<QueryFedModal initial="none" onClose={onClose} />)
      const invalidate = vi.spyOn(queryClient, 'invalidateQueries')

      fireEvent.click(screen.getByRole('button', REFRESH))

      const dialog = await screen.findByRole('dialog', { name: 'Kiro Account' })
      const alert = await within(dialog).findByRole('alert')
      expect(alert).toHaveTextContent('A refresh is already running.')
      expect(within(dialog).queryByRole('status')).not.toBeInTheDocument()
      expect(within(alert).getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
      // Refresh is held while the notice shows: pressing it would only get the
      // same 409 again. Held, not pending -- the other refresh is the one running.
      const refreshButton = screen.getByRole('button', REFRESH)
      expect(refreshButton).toBeDisabled()
      expect(refreshButton).not.toHaveAttribute('aria-busy')
      fireEvent.click(refreshButton)
      expect(refreshMock).toHaveBeenCalledTimes(1)

      expect(invalidate).not.toHaveBeenCalled()
      await vi.advanceTimersByTimeAsync(3100)
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['kiro-usage'] })
      // The re-read has landed: the notice has done its job and Refresh is offered again.
      await waitFor(() => expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument())
      expect(screen.getByRole('button', REFRESH)).toBeEnabled()
      expect(onClose).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('closes the modal from the 409 notice\'s hand-off', async () => {
    refreshMock.mockRejectedValue(REFUSAL(409, '{"code":"refresh_in_flight"}'))
    const onClose = vi.fn()
    renderWithProviders(<QueryFedModal initial="none" onClose={onClose} />)
    fireEvent.click(screen.getByRole('button', REFRESH))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('A refresh is already running.')
    // The hand-off opens the chat under this overlay, so it closes the modal.
    fireEvent.click(within(alert).getByRole('button', { name: /Ask the agent/i }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })

  it('does not stamp a refresh that returned the earlier reading as checked now', async () => {
    // The gateway answered with its dimmed prior reading: nothing new was
    // fetched. The meter renders it (dimmed), but "Checked at" would claim a
    // freshness the numbers do not have, so the outcome is reported instead.
    refreshMock.mockResolvedValue({
      usage: { credits_plan: 2000, credits_used: 636, stale: true },
    })
    const onClose = vi.fn()
    renderWithProviders(<QueryFedModal initial="none" onClose={onClose} />)

    fireEvent.click(screen.getByRole('button', REFRESH))

    const dialog = await screen.findByRole('dialog', { name: 'Kiro Account' })
    // Surfaced as an error: the refresh the user asked for fetched nothing new,
    // so it takes the shared error surface with its hand-off, not a status line.
    const alert = await within(dialog).findByRole('alert')
    expect(alert).toHaveTextContent('The refresh did not return a new reading. Showing the earlier one.')
    expect(within(dialog).queryByRole('status')).not.toBeInTheDocument()
    expect(within(dialog).getByRole('progressbar')).toHaveAttribute('aria-valuenow', '636')
    expect(within(dialog).queryByText(/Checked at/)).not.toBeInTheDocument()
    // One statement of the fact: the gray stale line yields to the notice that
    // says the same thing, instead of both rendering under the same meter.
    expect(within(dialog).queryByText(/Showing an earlier reading; the latest refresh/)).not.toBeInTheDocument()
    expect(within(dialog).getAllByText(/earlier/)).toHaveLength(1)
    // Not a hold: the user may press again at once.
    expect(within(dialog).getByRole('button', REFRESH)).toBeEnabled()
    // The hand-off opens the chat under this overlay, so it closes the modal.
    fireEvent.click(within(dialog).getByRole('button', { name: /Ask the agent/i }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })

  it('reports any other failure plainly, as nothing more than a failed refresh', async () => {
    refreshMock.mockRejectedValue(REFUSAL(500, 'boom'))
    renderWithProviders(<QueryFedModal initial="none" />)
    fireEvent.click(screen.getByRole('button', REFRESH))
    // The one failure sentence: a refresh that failed leaves the user where
    // "could not read your balance" already had them.
    const alert = (await screen.findByText('Could not read your balance.')).closest('[role="alert"]')
    expect(alert).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    // A plain failure is not a hold: the user can try again at once.
    expect(screen.getByRole('button', REFRESH)).toBeEnabled()
  })

  it('replaces the standing failure notice with the refresh outcome instead of stacking them', async () => {
    // The `failed` state already says "could not read"; a refresh that fails
    // too must not add a second banner under it. One alert, one hand-off --
    // the fact is stated once, in the one sentence both share.
    refreshMock.mockRejectedValue(REFUSAL(500, 'boom'))
    renderWithProviders(<QueryFedModal initial="failed" />)
    const dialog = await screen.findByRole('dialog', { name: 'Kiro Account' })
    expect(within(dialog).getByRole('alert')).toHaveTextContent('Could not read your balance.')

    fireEvent.click(within(dialog).getByRole('button', REFRESH))

    await waitFor(() => expect(refreshMock).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(within(dialog).queryByRole('button', REFRESHING)).not.toBeInTheDocument())
    const alerts = within(dialog).getAllByRole('alert')
    expect(alerts).toHaveLength(1)
    expect(alerts[0]).toHaveTextContent('Could not read your balance.')
    expect(within(dialog).getAllByText('Could not read your balance.')).toHaveLength(1)
    expect(within(dialog).getAllByRole('button', { name: /Ask the agent/i })).toHaveLength(1)
    // Refresh still sits under the (one) notice it can retry.
    expect(within(dialog).getByRole('button', REFRESH)).toBeEnabled()
  })

  it('replaces the standing failure notice with the 409 notice instead of stacking them', async () => {
    // The 409 is an outcome notice like the others: one alert at a time, so it
    // takes the standing "could not read" notice's place while it shows, and
    // Refresh under it is held.
    refreshMock.mockRejectedValue(REFUSAL(409, '{"code":"refresh_in_flight"}'))
    renderWithProviders(<QueryFedModal initial="failed" />)
    const dialog = await screen.findByRole('dialog', { name: 'Kiro Account' })
    expect(within(dialog).getByRole('alert')).toHaveTextContent('Could not read your balance.')

    fireEvent.click(within(dialog).getByRole('button', REFRESH))

    await within(dialog).findByText('A refresh is already running.')
    const alerts = within(dialog).getAllByRole('alert')
    expect(alerts).toHaveLength(1)
    expect(alerts[0]).toHaveTextContent('A refresh is already running.')
    expect(within(dialog).queryByText('Could not read your balance.')).not.toBeInTheDocument()
    expect(within(dialog).getAllByRole('button', { name: /Ask the agent/i })).toHaveLength(1)
    expect(within(dialog).getByRole('button', REFRESH)).toBeDisabled()
  })

  it('explains a parked scrape from the skipped marker and writes its payload into the query', async () => {
    refreshMock.mockResolvedValue({ usage: { available: false }, skipped: 'scrape_parked', retry_after: 3600 })
    const { queryClient } = renderWithProviders(<QueryFedModal initial="none" />)

    fireEvent.click(screen.getByRole('button', REFRESH))

    const paused = await screen.findByText(
      'Balance checks are paused because recent ones failed. Try again in about 60 min.',
    )
    expect(paused.closest('[role="alert"]')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
    expect(queryClient.getQueryData(['kiro-usage'])).toBe('none')
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    // Refresh is held while the notice says when to try again: pressing it
    // would only produce the same parked answer. Held, not pending.
    const refreshButton = screen.getByRole('button', REFRESH)
    expect(refreshButton).toBeDisabled()
    expect(refreshButton).not.toHaveAttribute('aria-busy')
    expect(screen.queryByRole('button', REFRESHING)).not.toBeInTheDocument()
  })

  it("drops the previous account's reading when a parked refresh comes back without one", async () => {
    // Account A's reading is on screen; the user switched accounts and the
    // scrape is parked, so the refresh answers the parked marker with an
    // unavailable payload. That payload must land in the pill's query NOW, not
    // after its next poll, so neither the modal nor the pill keeps showing A.
    refreshMock.mockResolvedValue({ usage: { available: false }, skipped: 'scrape_parked', retry_after: 3600 })
    const { queryClient } = renderWithProviders(<QueryFedModal initial={BASE_USAGE} />)
    expect(screen.getByText('owner@example.com')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', REFRESH))

    await waitFor(() => expect(queryClient.getQueryData(['kiro-usage'])).toBe('none'))
    await waitFor(() => expect(screen.queryByText('owner@example.com')).not.toBeInTheDocument())
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(screen.getByText(/Balance checks are paused because recent ones failed/)).toBeInTheDocument()
    expect(screen.getByRole('button', REFRESH)).toBeDisabled()
  })

  it('treats a refresh that produced no plan as a failure and writes that state into the query', async () => {
    refreshMock.mockResolvedValue({ usage: { available: false } })
    const { queryClient } = renderWithProviders(<QueryFedModal initial="none" />)

    fireEvent.click(screen.getByRole('button', REFRESH))

    const failed = await screen.findByText('Could not read your balance.')
    expect(failed.closest('[role="alert"]')).toBeInTheDocument()
    expect(queryClient.getQueryData(['kiro-usage'])).toBe('none')
  })

  it('puts the refresh outcome above Refresh in the no-reading state too, replacing the standing box', async () => {
    // Same shape as `failed`: the outcome notice takes the standing box's
    // place above the button, so the notice is in one position whichever
    // no-reading state the modal opened in.
    refreshMock.mockRejectedValue(REFUSAL(500, 'boom'))
    renderWithProviders(<QueryFedModal initial="none" />)
    const dialog = await screen.findByRole('dialog', { name: 'Kiro Account' })
    expect(within(dialog).getByText('No balance reading is available for this account yet.')).toBeInTheDocument()

    fireEvent.click(within(dialog).getByRole('button', REFRESH))

    const alert = await within(dialog).findByRole('alert')
    expect(alert).toHaveTextContent('Could not read your balance.')
    expect(within(dialog).queryByText('No balance reading is available for this account yet.')).not.toBeInTheDocument()
    const button = within(dialog).getByRole('button', REFRESH)
    expect(alert.compareDocumentPosition(button) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('drops the previous account\'s reading the moment a refresh comes back without one', async () => {
    // Account A's reading (email + balance) is on screen. The user switched
    // kiro-cli to an account with no live credential; the refresh answers
    // signin_required. The pill's query must hold that state NOW -- not after
    // its next poll -- so neither the modal nor the pill keeps showing A.
    refreshMock.mockResolvedValue({ usage: { available: false, reason: 'signin_required' } })
    const { queryClient } = renderWithProviders(<QueryFedModal initial={BASE_USAGE} />)
    expect(screen.getByText('owner@example.com')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', REFRESH))

    await waitFor(() => expect(queryClient.getQueryData(['kiro-usage'])).toBe('signin-required'))
    await waitFor(() => expect(screen.queryByText('owner@example.com')).not.toBeInTheDocument())
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(screen.getByText(/Sign in to Kiro again/)).toBeInTheDocument()
  })
})
