import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { api, type KiroCreditUsage } from '../api/client'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      sessionsUsageRefresh: vi.fn(),
      kiroUsage: vi.fn(async () => ({
        sessions: {
          total_sessions: 0,
          today: { sessions: 0, messages: 0, tool_calls: 0 },
          this_week: { sessions: 0, messages: 0, tool_calls: 0 },
          this_month: { sessions: 0, messages: 0, tool_calls: 0 },
          avg_msgs_per_session: 0,
          daily_history: [],
        },
        billing: {},
      })),
      kirocrewConfig: vi.fn(async () => ({ agent: { acp_backend: 'kiro' } })),
    },
  }
})

vi.mock('../pages/developer/useKiroSignInOffered', () => ({
  useKiroSignInOffered: () => false,
}))

vi.mock('../components/KiroAccountModal', async importOriginal => {
  const mod = await importOriginal<typeof import('../components/KiroAccountModal')>()
  return {
    ...mod,
    AccountIdentity: () => <div data-testid="account-identity" />,
    CreditUsage: (props: Parameters<typeof mod.CreditUsage>[0]) => (
      <div data-testid="credit-usage"><mod.CreditUsage {...props} /></div>
    ),
  }
})

import { KiroCreditsCard } from '../pages/developer/KiroCreditsCard'
import { renderWithProviders } from './helpers'

const ACCOUNT_A: KiroCreditUsage = {
  used: 10,
  limit: 100,
  overage: 0,
  bonusCredits: [],
  stale: false,
  email: 'account-a@example.com',
  accountType: 'SocialGoogle',
}

const ACCOUNT_B: KiroCreditUsage = {
  ...ACCOUNT_A,
  email: 'account-b@example.com',
}

describe('KiroCreditsCard', () => {
  it('links to Kiro account management under the usage block', () => {
    renderWithProviders(<KiroCreditsCard usage="none" handoff={false} />)

    const card = screen.getByTestId('kiro-credits-card')
    const usage = within(card).getByTestId('credit-usage')
    const manage = within(card).getByRole('link', { name: 'Manage account & subscription' })

    expect(usage.compareDocumentPosition(manage) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(manage).toHaveAttribute('href', 'https://app.kiro.dev/settings/account')
    expect(manage).toHaveAttribute('target', '_blank')
    expect(manage).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('does not offer an agent hand-off beside the sign-in card draft', () => {
    renderWithProviders(<KiroCreditsCard usage="failed" handoff={false} />)

    expect(screen.getByText('Could not read your balance.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Ask the agent/i })).not.toBeInTheDocument()
  })

  it('offers the agent hand-off on a failed reading when no sign-in card is beside it', () => {
    renderWithProviders(<KiroCreditsCard usage="failed" handoff />)

    expect(screen.getByText('Could not read your balance.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
  })

  it('clears refresh state when sign-in gains or changes the account', async () => {
    vi.mocked(api.sessionsUsageRefresh)
      .mockResolvedValueOnce({ usage: { available: false }, skipped: 'scrape_parked', retry_after: 3600 })
      .mockResolvedValueOnce({ usage: { available: false }, skipped: 'scrape_parked', retry_after: 3600 })
    const { rerender } = renderWithProviders(<KiroCreditsCard usage="none" handoff={false} />)

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await screen.findByText(/Balance checks are paused because recent ones failed/)
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeDisabled()

    rerender(<KiroCreditsCard usage={ACCOUNT_A} handoff={false} />)
    expect(screen.queryByText(/Balance checks are paused because recent ones failed/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled()

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await screen.findByText(/Balance checks are paused because recent ones failed/)
    rerender(<KiroCreditsCard usage={ACCOUNT_B} handoff={false} />)

    await waitFor(() => {
      expect(screen.queryByText(/Balance checks are paused because recent ones failed/)).not.toBeInTheDocument()
    })
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled()
  })
})
