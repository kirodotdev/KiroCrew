// `GET /api/usage/kiro` carries `tokens`: the usage shards' 30-day token and
// dollar-cost history, which a harness that bills in dollars (Claude Code) has
// instead of a credit plan (#35249). The adapter maps it onto the token card's
// and the daily token chart's fields, and leaves them unset when it is absent.

vi.mock('../api/client', () => ({
  api: {
    kiroUsage: vi.fn(),
  },
}))

import { api } from '../api/client'
import { AcpAdapter } from '../providers/adapters/acp'

const kiroUsage = api.kiroUsage as unknown as ReturnType<typeof vi.fn>

function payload(extra: Record<string, unknown> = {}) {
  const period = { sessions: 0, messages: 0, tool_calls: 0 }
  return {
    username: 'someone',
    billing: {},
    sessions: {
      total_sessions: 0, total_messages: 0, total_tool_calls: 0, all_time_sessions: 0,
      daily_history: [], today: period, this_week: period, this_month: period,
      avg_msgs_per_session: 0, avg_tools_per_session: 0, refused_transcripts: 0, unreadable_session_dirs: 0,
    },
    ...extra,
  }
}

const bucket = { input: 10, output: 20, cache_create: 3, cache_read: 4, cost_usd: 1.5 }

describe('AcpAdapter.fetchUsage — token history', () => {
  beforeEach(() => vi.clearAllMocks())

  it('maps the totals, the per-day buckets and the filter lists', async () => {
    kiroUsage.mockResolvedValue(payload({
      tokens: {
        total_input: 10, total_output: 20, cache_creation: 3, cache_read: 4, total: 37, cost_usd: 1.5,
        daily_history: [{
          date: '2026-11-10', ...bucket,
          models: { 'claude-x': bucket },
          providers: { claude_code: bucket },
          provider_models: { claude_code: { 'claude-x': bucket } },
        }],
        providers: ['claude_code'],
        models: ['claude-x'],
        provider_models: { claude_code: ['claude-x'] },
      },
    }))
    const usage = await new AcpAdapter().fetchUsage()
    const mapped = { input: 10, output: 20, cacheCreate: 3, cacheRead: 4, costUsd: 1.5 }
    expect(usage.tokens).toEqual({ input: 10, output: 20, cacheCreation: 3, cacheRead: 4, total: 37 })
    expect(usage.costUsd).toBe(1.5)
    expect(usage.tokenDailyHistory).toEqual([{
      date: '2026-11-10', ...mapped,
      models: { 'claude-x': mapped },
      providers: { claude_code: mapped },
      providerModels: { claude_code: { 'claude-x': mapped } },
    }])
    expect(usage.tokenProviders).toEqual(['claude_code'])
    expect(usage.tokenModels).toEqual(['claude-x'])
    expect(usage.tokenProviderModels).toEqual({ claude_code: ['claude-x'] })
  })

  it('carries the gateway local day the Today line picks its bucket by', async () => {
    kiroUsage.mockResolvedValue(payload({ local_day: '2026-11-10' }))
    expect((await new AcpAdapter().fetchUsage()).localDay).toBe('2026-11-10')
    kiroUsage.mockResolvedValue(payload())
    expect((await new AcpAdapter().fetchUsage()).localDay).toBeUndefined()
  })

  it.each([
    ['absent (an older gateway)', {}],
    ['empty (no shard in the window)', { tokens: {} }],
  ])('leaves the token fields unset when the history is %s', async (_label, extra) => {
    kiroUsage.mockResolvedValue(payload(extra))
    const usage = await new AcpAdapter().fetchUsage()
    expect(usage.tokens).toBeUndefined()
    expect(usage.tokenDailyHistory).toBeUndefined()
    expect(usage.costUsd).toBeUndefined()
  })
})
