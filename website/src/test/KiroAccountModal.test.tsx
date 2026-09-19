import { fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import KiroAccountModal from '../components/KiroAccountModal'
import type { KiroCreditUsage } from '../api/client'
import { installSoftNavigate, __resetNavSeamForTests } from '../utils/errorReport'
import { renderWithProviders } from './helpers'

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
    expect(screen.getByText('Credit usage unavailable')).toBeInTheDocument()
  })

  it('states the failure instead of spinning when the fetch failed cold', async () => {
    // 'failed' and null are both "no reading", but only null still has a fetch
    // outstanding. Spinning on 'failed' would repeat, one level down, the defect
    // the top-bar pill was fixed for: the drill-in must not claim it is checking.
    renderWithProviders(<KiroAccountModal open onClose={vi.fn()} usage="failed" />)

    expect(await screen.findByText('Account details unavailable')).toBeInTheDocument()
    expect(screen.getByText('Credit usage unavailable')).toBeInTheDocument()
    expect(screen.queryByText('Checking account…')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Checking credit usage')).not.toBeInTheDocument()
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
    expect(screen.queryByText('Credit usage unavailable')).not.toBeInTheDocument()
    expect(screen.queryByText('Checking account…')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Checking credit usage')).not.toBeInTheDocument()
  })

  it('explains the opted-out scrape and names the knob instead of a generic failure', async () => {
    // 'scrape-disabled' is terminal until the user flips
    // dashboard.usage_text_scrape_enabled (#7623): the free API returned no
    // plan and the billed fallback is off by default. The panel must name the
    // knob — a generic unavailable line reads as a transient error and gives
    // the user nothing to act on (the v0.1.3→v0.4.1 "pill silently vanished"
    // report was exactly this gap).
    renderWithProviders(<KiroAccountModal open onClose={vi.fn()} usage="scrape-disabled" />)

    expect(
      await screen.findByText(/dashboard\.usage_text_scrape_enabled/),
    ).toBeInTheDocument()
    expect(screen.queryByText('Credit usage unavailable')).not.toBeInTheDocument()
    expect(screen.queryByText('Checking account…')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Checking credit usage')).not.toBeInTheDocument()
  })

  it('tells the user to sign in again instead of blaming the opted-out scrape', async () => {
    // 'signin-required' is terminal until the user re-authenticates (#11602). It
    // must NOT render the scrape-disabled copy: that asserts the free API
    // returned no plan for this account (it was never called) and points at a
    // knob whose billed /usage turn needs the same lapsed sign-in, so acting on
    // it spends credits on attempts that cannot succeed.
    renderWithProviders(<KiroAccountModal open onClose={vi.fn()} usage="signin-required" />)

    expect(await screen.findByText(/Sign in to Kiro again/)).toBeInTheDocument()
    // The remedy the user must not be sent to.
    expect(
      screen.queryByText(/Set dashboard\.usage_text_scrape_enabled to true/),
    ).not.toBeInTheDocument()
    expect(screen.queryByText('Credit usage unavailable')).not.toBeInTheDocument()
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

  it('leaves the hand-off off the states that report a configuration, not a failure', async () => {
    // 'api-key' and 'scrape-disabled' describe how the account is set up; there
    // is no error for the agent to act on, so they keep the passive notice. This
    // pins the split — a blanket migration would put a recovery button next to a
    // line that needs no recovery.
    for (const usage of ['api-key', 'scrape-disabled'] as const) {
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
