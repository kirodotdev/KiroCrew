import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import KiroAccountModal from '../components/KiroAccountModal'
import type { KiroCreditUsage } from '../api/client'
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
