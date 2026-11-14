// Contract under test: the adapter reports the plan's used share exactly, and
// each display rounds it for itself. A share rounded here reads "100%" with
// credits left, and crosses the Billing badge's 70% and 90% lines early.
import { describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', () => ({ api: { kiroUsage: vi.fn() } }))
import { api } from '../api/client'
import { AcpAdapter } from '../providers/adapters/acp'

const period = { sessions: 0, messages: 0, tool_calls: 0 }
const payload = (used: number, plan: number) => ({
  billing: { plan: 'Pro', credits_used: used, credits_plan: plan },
  sessions: {
    total_sessions: 0, daily_history: [], today: period, this_week: period, this_month: period,
    avg_msgs_per_session: 0,
  },
})

describe('AcpAdapter billing percentUsed', () => {
  it('does not report a plan as 100% used while credits remain', async () => {
    vi.mocked(api.kiroUsage).mockResolvedValue(payload(996, 1000) as never)
    const usage = await new AcpAdapter().fetchUsage()
    expect(usage.billing?.percentUsed).toBeCloseTo(99.6, 5)
  })

  it('keeps 89.5% below the 90% danger line', async () => {
    vi.mocked(api.kiroUsage).mockResolvedValue(payload(895, 1000) as never)
    const usage = await new AcpAdapter().fetchUsage()
    expect(usage.billing?.percentUsed ?? 100).toBeLessThan(90)
  })
})
