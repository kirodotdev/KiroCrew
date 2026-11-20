//
// Contract under test: the Usage tab mounts the Plan Limits card with the
// report's `planLimits`, so a Codex reading shows its row and a report with no
// reading on a Kiro-only install shows no card.
//
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { NormalizedUsage } from '../providers'

const period = { sessions: 0, messages: 0, toolCalls: 0 }
const base: NormalizedUsage = {
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
  billing: null,
}

let current: NormalizedUsage = base

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

describe('UsageTab Plan Limits card', () => {
  it('shows the Codex row from the report', async () => {
    current = {
      ...base,
      planLimits: {
        codex: {
          fiveHour: { usedPercent: 10, resetsAt: null, windowMinutes: 300, stale: false },
          weekly: null,
          capturedAt: null,
        },
      },
    }
    mount()
    const row = await screen.findByTestId('plan-limits-row-codex')
    expect(within(row).getByText('5-hour 10%')).toBeInTheDocument()
  })

  it('shows no card for a Kiro-only install with no reading', async () => {
    current = { ...base, planLimits: { codex: null } }
    mount()
    expect(await screen.findByText(/Session Activity/i)).toBeInTheDocument()
    expect(screen.queryByTestId('plan-limits-row-codex')).toBeNull()
  })
})
