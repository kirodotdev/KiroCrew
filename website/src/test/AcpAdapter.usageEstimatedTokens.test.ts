// The acp adapter maps `/api/usage/kiro`'s `sessions.estimated_tokens` block and
// each Daily History row's `est_tokens` into the normalized usage shape. Every
// number crosses the wire from a backend that may be older or newer than this
// client, so a missing block stays absent (no confident zero), a non-finite or
// non-numeric field reads as 0 inside a present block, and a row without a
// figure carries none.

vi.mock('../api/client', () => ({
  api: {
    kiroUsage: vi.fn(),
  },
}))

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { api } from '../api/client'
import { AcpAdapter } from '../providers/adapters/acp'

const kiroUsage = api.kiroUsage as unknown as ReturnType<typeof vi.fn>

function payload(sessionsExtra: Record<string, unknown>, history: Record<string, unknown>[] = []) {
  return {
    username: 'someone',
    billing: {},
    sessions: {
      total_sessions: 0,
      total_messages: 0,
      total_tool_calls: 0,
      all_time_sessions: 0,
      daily_history: history,
      today: { sessions: 0, messages: 0, tool_calls: 0 },
      this_week: { sessions: 0, messages: 0, tool_calls: 0 },
      this_month: { sessions: 0, messages: 0, tool_calls: 0 },
      avg_msgs_per_session: 0,
      avg_tools_per_session: 0,
      refused_transcripts: 0,
      ...sessionsExtra,
    },
  }
}

const day = { date: '2026-09-22', sessions: 1, messages: 2, tool_calls: 0, credits: 1 }

beforeEach(() => kiroUsage.mockReset())

describe('AcpAdapter.fetchUsage estimated tokens', () => {
  it('maps both periods and the per-day figure', async () => {
    kiroUsage.mockResolvedValue(
      payload(
        {
          estimated_tokens: {
            this_month: { input: 1200, output: 34, requests: 5 },
            last_month: { input: 9_300_000_000, output: 4_100_000, requests: 30825 },
            unreadable_sessions: 0,
          },
        },
        [{ ...day, est_tokens: 31_000_000 }],
      ),
    )
    const u = await new AcpAdapter().fetchUsage()
    expect(u.sessions.estimatedTokens).toEqual({
      thisMonth: { input: 1200, output: 34, requests: 5 },
      lastMonth: { input: 9_300_000_000, output: 4_100_000, requests: 30825 },
      incomplete: false,
    })
    expect(u.sessions.dailyHistory[0].estTokens).toBe(31_000_000)
  })

  it('marks the estimate incomplete when any session document could not be read', async () => {
    kiroUsage.mockResolvedValue(payload({ estimated_tokens: { this_month: {}, last_month: {}, unreadable_sessions: 2 } }))
    const u = await new AcpAdapter().fetchUsage()
    expect(u.sessions.estimatedTokens?.incomplete).toBe(true)
  })

  it.each([
    ['absent', undefined],
    ['null', null],
    ['an array', []],
    ['a string', 'x'],
  ])('leaves the estimate absent when the block is %s', async (_label, block) => {
    kiroUsage.mockResolvedValue(payload(block === undefined ? {} : { estimated_tokens: block }))
    const u = await new AcpAdapter().fetchUsage()
    expect(u.sessions.estimatedTokens).toBeUndefined()
  })

  it('reads a non-finite or non-numeric field as 0 inside a present block', async () => {
    kiroUsage.mockResolvedValue(
      payload({
        estimated_tokens: {
          this_month: { input: 'lots', output: null, requests: Number.NaN },
          unreadable_sessions: 'some',
        },
      }),
    )
    const u = await new AcpAdapter().fetchUsage()
    expect(u.sessions.estimatedTokens).toEqual({
      thisMonth: { input: 0, output: 0, requests: 0 },
      lastMonth: { input: 0, output: 0, requests: 0 },
      incomplete: false,
    })
  })

  it.each([
    ['absent', undefined],
    ['a string', '31M'],
    ['NaN', Number.NaN],
  ])('gives a day no figure when est_tokens is %s', async (_label, value) => {
    const row = value === undefined ? { ...day } : { ...day, est_tokens: value }
    kiroUsage.mockResolvedValue(payload({}, [row]))
    const u = await new AcpAdapter().fetchUsage()
    expect(u.sessions.dailyHistory[0].estTokens).toBeUndefined()
  })
})
