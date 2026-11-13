//
// Contract under test: the Usage tab's Billing card writes its amounts and its
// percentage in the reader's locale, in the card's own unit, like the Daily
// History table under it. The account API passes `currentUsageWithPrecision`
// through unrounded, so a raw number reads "1234.5678 / 10000".
//
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { NormalizedUsage } from '../providers'

function usage(billing: NormalizedUsage['billing']): NormalizedUsage {
  const period = { sessions: 0, messages: 0, toolCalls: 0 }
  return {
    sessions: {
      total: 0,
      today: period,
      thisWeek: period,
      thisMonth: period,
      avgMsgsPerSession: 0,
      refusedTranscripts: 0,
      unreadableSessionDirs: 0,
      dailyHistory: [],
    },
    billing,
  }
}

let current: NormalizedUsage = usage(null)

vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp',
    displayName: 'Kiro',
    capabilities: { usageBilling: true },
    fetchUsage: () => Promise.resolve(current),
  }),
}))
vi.mock('../api/client', () => ({
  api: {
    kirocrewConfig: vi.fn().mockResolvedValue({ agent: { acp_backend: 'kiro' } }),
    usageSeries: vi.fn().mockResolvedValue({ dates: [], series: [], total: 0, truncated: false, dropped_rows: 0, complete_from: null }),
  },
}))
vi.mock('../hooks/useSessionPalette', () => ({
  useSessionPalette: () => ({ paletteColors: ['#ff0000', '#00ff00', '#0000ff'] }),
}))

import UsageTab from '../pages/overview/UsageTab'

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <UsageTab />
    </QueryClientProvider>,
  )
}

afterEach(() => cleanup())

describe('UsageTab Billing card number format', () => {
  it('writes credits with grouping and two decimals, and the share as a percent', async () => {
    current = usage({ plan: 'Pro', used: 1234.5678, limit: 10000, unit: 'credits', percentUsed: 12.3456 })
    mount()
    expect(await screen.findByText('1,234.57 / 10,000.00')).toBeInTheDocument()
    expect(screen.getByText('12.3%')).toBeInTheDocument()
  })

  it('writes a dollar plan as money and a token plan as a whole count', async () => {
    current = usage({ plan: 'Team', used: 3.456, limit: 50, unit: 'usd' })
    mount()
    expect(await screen.findByText('$3.46 / $50.00')).toBeInTheDocument()
    cleanup()
    current = usage({ plan: 'Max', used: 1234567, unit: 'tokens' })
    mount()
    expect(await screen.findByText('1,234,567')).toBeInTheDocument()
  })
})
