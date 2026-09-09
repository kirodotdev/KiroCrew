// `GET /api/usage/kiro` answers 200 even when the transcript directory itself
// could not be read, because billing is a separate half of the payload. The
// sessions half then carries the server's own `error` string, and the zeros
// beside it are a SHAPE rather than a measurement.
//
// Reading `s.today.sessions` straight off that response is what turned a
// server-side diagnostic into `Cannot read properties of undefined (reading
// 'sessions')` in the Usage tab — the tab renders `queryErr.message` verbatim,
// so whatever this method rejects with is what the user is told.

vi.mock('../api/client', () => ({
  api: {
    kiroUsage: vi.fn(),
  },
}))

import { api } from '../api/client'
import { AcpAdapter } from '../providers/adapters/acp'

const kiroUsage = api.kiroUsage as unknown as ReturnType<typeof vi.fn>

/** The route's payload when the directory read failed: complete zero statistics
 *  plus the reason. Mirrors `_parse_sessions`, whose shape
 *  `test_usage.py::test_an_unreadable_directory_still_answers_the_full_shape`
 *  pins on the backend side. */
function unreadableSessionsPayload() {
  return {
    username: 'someone',
    error: 'cannot read sessions directory',
    billing: { plan: 'Pro', credits_used: 10, credits_plan: 100 },
    sessions: {
      error: 'cannot read sessions directory',
      code: 'sessions_dir_unreadable',
      total_sessions: 0,
      total_messages: 0,
      total_tool_calls: 0,
      all_time_sessions: 0,
      daily_history: [],
      today: { sessions: 0, messages: 0, tool_calls: 0 },
      this_week: { sessions: 0, messages: 0, tool_calls: 0 },
      this_month: { sessions: 0, messages: 0, tool_calls: 0 },
      avg_msgs_per_session: 0,
      avg_tools_per_session: 0,
      refused_transcripts: 0,
    },
  }
}

/** The shape the route answered BEFORE the statistics contract was restored:
 *  the reason INSTEAD of the numbers. Kept as its own case because a client that
 *  survives only the complete shape still breaks on a gateway that has not been
 *  updated yet, and the wire is where the two meet. */
function errorOnlySessionsPayload() {
  return {
    username: 'someone',
    error: 'cannot read sessions directory',
    billing: { plan: 'Pro', credits_used: 10, credits_plan: 100 },
    sessions: {
      error: 'cannot read sessions directory',
      code: 'sessions_dir_unreadable',
    },
  }
}

function healthySessionsPayload() {
  const p = unreadableSessionsPayload()
  delete (p.sessions as Record<string, unknown>).error
  delete (p.sessions as Record<string, unknown>).code
  delete (p as Record<string, unknown>).error
  p.sessions.today = { sessions: 3, messages: 9, tool_calls: 4 }
  return p
}

describe('AcpAdapter.fetchUsage — an unreadable sessions directory', () => {
  beforeEach(() => vi.clearAllMocks())

  it('rejects with the message the server sent', async () => {
    kiroUsage.mockResolvedValue(unreadableSessionsPayload())
    await expect(new AcpAdapter().fetchUsage()).rejects.toThrow(
      'cannot read sessions directory',
    )
  })

  it('does not reject with a property-access TypeError', async () => {
    // The distinguishing assertion: reading `s.today.sessions` off this payload
    // also rejects, but with the client's own "Cannot read properties of
    // undefined (reading 'sessions')" instead of the server's diagnostic. The
    // message is what the Usage tab prints, so equality is the property that
    // matters, not merely that something was thrown.
    kiroUsage.mockResolvedValue(unreadableSessionsPayload())
    const err = await new AcpAdapter().fetchUsage().then(
      () => null,
      (e: unknown) => e,
    )
    expect(err).toBeInstanceOf(Error)
    expect((err as Error).message).toBe('cannot read sessions directory')
    expect((err as Error).message).not.toMatch(/undefined/)
  })

  it('reports the reason for an error-only payload too, not a TypeError', async () => {
    // A gateway that has not shipped the statistics contract yet answers the
    // reason INSTEAD of the numbers. Reaching for `s.today.sessions` there is
    // what produced "Cannot read properties of undefined (reading 'sessions')"
    // in the Usage tab, in place of the message the server had already written.
    kiroUsage.mockResolvedValue(errorOnlySessionsPayload())
    const err = await new AcpAdapter().fetchUsage().then(
      () => null,
      (e: unknown) => e,
    )
    expect(err).toBeInstanceOf(Error)
    expect((err as Error).message).toBe('cannot read sessions directory')
  })

  it('still normalizes a healthy response', async () => {
    // Guard the guard: a method that rejected unconditionally would pass both
    // assertions above.
    kiroUsage.mockResolvedValue(healthySessionsPayload())
    const usage = await new AcpAdapter().fetchUsage()
    expect(usage.sessions.today).toEqual({ sessions: 3, messages: 9, toolCalls: 4 })
    expect(usage.billing?.plan).toBe('Pro')
  })
})
