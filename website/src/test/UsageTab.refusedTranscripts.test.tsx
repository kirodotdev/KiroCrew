//
// Contract under test: the usage page must not render a confident zero when
// the sessions scan refused transcripts (#6733). On a Windows roaming-profile
// (UNC) home EVERY transcript is refused by path validation, so the backend
// reports total=0 with a positive refusedTranscripts count. The page has to
// say the counts are incomplete instead of showing 0 as a fact.
//
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { NormalizedUsage } from '../providers'

// Build a NormalizedUsage with a controllable refusal count. Everything else is
// a zeroed baseline -- the silent-failure shape the fix targets.
function usage(refusedTranscripts: number): NormalizedUsage {
  const period = { sessions: 0, messages: 0, toolCalls: 0 }
  return {
    sessions: {
      total: 0,
      today: period,
      thisWeek: period,
      thisMonth: period,
      avgMsgsPerSession: 0,
      refusedTranscripts,
      dailyHistory: [],
    },
    billing: null,
  }
}

let current: NormalizedUsage = usage(0)

vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp',
    displayName: 'Kiro',
    capabilities: { usageBilling: true },
    fetchUsage: () => Promise.resolve(current),
  }),
}))

// UsageTab is a default export.
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

describe('UsageTab refused-transcript warning (#6733)', () => {
  it('shows the incomplete-counts warning when transcripts were refused', async () => {
    current = usage(7)
    mount()
    // The English catalog string names the actor and leads with "couldn't load".
    await waitFor(() => {
      expect(screen.getByText(/couldn't load/i)).toBeInTheDocument()
    })
    expect(screen.getByText(/incomplete/i)).toBeInTheDocument()
    // The count is interpolated into the message.
    expect(screen.getByText(/\b7\b/)).toBeInTheDocument()
  })

  it('shows no warning when nothing was refused', async () => {
    current = usage(0)
    mount()
    // The session activity card is the arrival signal for a settled render.
    await waitFor(() => {
      expect(screen.getByText(/Session Activity/i)).toBeInTheDocument()
    })
    expect(screen.queryByText(/couldn't load/i)).toBeNull()
  })
})
