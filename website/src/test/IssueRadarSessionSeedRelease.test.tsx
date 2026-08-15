import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook } from '@testing-library/react'

// The session-open path claims the record's link BEFORE seeding the first turn, so
// that losing a two-tab race is recoverable. This file pins the cost of that
// ordering: a seed that never starts must RELEASE the link it claimed. Otherwise the
// record points at a slot with no turn, and every later click resumes that empty
// session instead of starting the work -- a permanent dead end, and one that only
// exists because of the claim.
//
// The three outcomes are deliberately handled differently, because the wrong choice
// destroys work in one direction and strands it in the other:
//   * seed REJECTED (4xx/5xx)  -> nothing started: drop the slot, release the link.
//   * seed THREW, slot empty   -> did not land: drop the slot, release the link.
//   * seed THREW, slot has a turn (or the probe fails) -> may be running: touch
//     NOTHING. Losing a live session is worse than leaving an empty one behind.
//
// Release is complete only because the claim is a PURE RESERVATION -- it writes link
// fields and no user-visible state. The cases below pin that directly: a claim that
// also stamped the lifecycle would strand a finished item reading as `investigating`,
// since clearing the link cannot restore a status the record may never have had.
const dispatched: Array<{ type: string; arg?: unknown }> = []
const deleted: string[] = []
const switched: string[] = []

vi.mock('react-redux', () => ({ useDispatch: () => (thunk: unknown) => thunk }))
vi.mock('react-router-dom', () => ({ useNavigate: () => () => {} }))

vi.mock('../store', () => ({
  useAppDispatch: () => (action: { __kind: string; arg?: unknown }) => {
    dispatched.push({ type: action?.__kind ?? 'unknown', arg: action?.arg })
    if (action?.__kind === 'deleteSlot') deleted.push(String(action.arg))
    if (action?.__kind === 'switchSlot') switched.push(String(action.arg))
    return { unwrap: async () => ({ key: 'chat-new' }) }
  },
}))

vi.mock('../store/chatSlice', () => ({
  createSlot: (arg: unknown) => ({ __kind: 'createSlot', arg }),
  switchSlot: (arg: unknown) => ({ __kind: 'switchSlot', arg }),
  deleteSlot: (arg: unknown) => ({ __kind: 'deleteSlot', arg }),
}))

const api = {
  chatFolders: vi.fn(),
  createChatFolder: vi.fn(),
  renameSlot: vi.fn(),
  sendChat: vi.fn(),
  chatSlotDetail: vi.fn(),
}
vi.mock('../api/client', () => ({ api }))

const issueRadarApi = { saveInvestigation: vi.fn(), getInvestigation: vi.fn() }
vi.mock('../apps/issue-radar/api', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  issueRadarApi,
}))

const { useAgentSession } = await import('../apps/issue-radar/lib/agentSession')
const { InvestigationSlotConflictError } = await import('../apps/issue-radar/api')

const REF = { owner: 'o', repo: 'r' }
const ARGS = {
  repoRef: REF,
  number: 7,
  kind: 'pull' as const,
  verb: 'respond' as const,
  title: 'PR#7',
  prompt: 'do the thing',
  existing: null,
}

/** The release write: clears the link, and expects OUR slot key so it can only ever
 * clear our own claim rather than stealing one another tab has since made. */
const releaseCalls = () =>
  issueRadarApi.saveInvestigation.mock.calls.filter((c) => c[2]?.slot_key === '')

describe('a seed that never starts releases the link it claimed', () => {
  beforeEach(() => {
    dispatched.length = 0
    deleted.length = 0
    switched.length = 0
    for (const fn of Object.values(api)) fn.mockReset()
    issueRadarApi.saveInvestigation.mockReset()
    api.chatFolders.mockResolvedValue([{ id: 'f1', name: 'Issue Radar - r' }])
    api.renameSlot.mockResolvedValue({})
    issueRadarApi.saveInvestigation.mockResolvedValue({ investigation: { slot_key: 'chat-new' } })
  })

  it('releases the claim when the seed is rejected outright', async () => {
    api.sendChat.mockResolvedValue({ ok: false, status: 503 } as Response)
    const { result } = renderHook(() => useAgentSession())
    const got = await result.current.openSession(ARGS)
    expect(got).toBeNull()
    expect(deleted).toContain('chat-new')
    expect(releaseCalls()).toHaveLength(1)
    expect(releaseCalls()[0][5]).toBe('chat-new')
  })

  // A linked slot that still EXISTS is always resumed, never repaired. An empty slot is
  // indistinguishable from one whose seed is in flight in another tab, so repairing on
  // that guess deletes a session that is starting -- and this verb pushes commits and
  // posts replies, so two agents on one change request is far worse than one empty
  // session the user clears by hand. Distinguishing the two needs the reservation to
  // record WHEN it was made (a server-side lease), which this PR does not add.
  describe('an existing session is never replaced', () => {
    const linked = { ...ARGS, existing: { slot_key: 'chat-old' } }

    it('resumes a linked slot even when it has no turn yet', async () => {
      api.chatSlotDetail.mockResolvedValue({ messages: [], running: false })
      const { result } = renderHook(() => useAgentSession())
      await result.current.openSession(linked)
      expect(switched).toContain('chat-old')
      // No replacement session, and above all no delete: another tab may be seeding it.
      expect(dispatched.filter((d) => d.type === 'createSlot')).toHaveLength(0)
      expect(deleted).not.toContain('chat-old')
      expect(api.sendChat).not.toHaveBeenCalled()
    })

    it('resumes a linked slot that already has a turn', async () => {
      api.chatSlotDetail.mockResolvedValue({ messages: [{ id: 1 }], running: false })
      const { result } = renderHook(() => useAgentSession())
      await result.current.openSession(linked)
      expect(switched).toContain('chat-old')
      expect(api.sendChat).not.toHaveBeenCalled()
    })
  })

  it('claims the link WITHOUT stamping the lifecycle', async () => {
    // The claim happens before anything runs, so writing `investigating` here would be
    // a claim about the item that is not yet true -- and one a release cannot take back.
    api.sendChat.mockResolvedValue({ ok: true } as Response)
    const { result } = renderHook(() => useAgentSession())
    await result.current.openSession(ARGS)
    const claim = issueRadarApi.saveInvestigation.mock.calls.find(
      (c) => c[2]?.slot_key === 'chat-new',
    )
    expect(claim).toBeDefined()
    expect(claim?.[2]).not.toHaveProperty('status')
  })

  it('stamps `investigating` only once the session is really running', async () => {
    api.sendChat.mockResolvedValue({ ok: true } as Response)
    const { result } = renderHook(() => useAgentSession())
    await result.current.openSession(ARGS)
    const stamp = issueRadarApi.saveInvestigation.mock.calls.filter(
      (c) => c[2]?.status === 'investigating',
    )
    expect(stamp).toHaveLength(1)
    // Guarded by our own slot key, so a record another tab took over is not restamped.
    expect(stamp[0][5]).toBe('chat-new')
  })

  it('leaves a REJECTED seed with no lifecycle write at all', async () => {
    // The regression this pins: a resolved item whose slot was deleted takes the
    // fresh-session path; if the claim stamped `investigating`, a rejected seed would
    // leave that item permanently reading as under investigation with nothing running.
    api.sendChat.mockResolvedValue({ ok: false, status: 503 } as Response)
    const { result } = renderHook(() => useAgentSession())
    await result.current.openSession(ARGS)
    expect(
      issueRadarApi.saveInvestigation.mock.calls.filter((c) => c[2]?.status !== undefined),
    ).toHaveLength(0)
  })

  it('releases the claim when the seed throws and the slot has no turn', async () => {
    api.sendChat.mockRejectedValue(new Error('network down'))
    api.chatSlotDetail.mockResolvedValue({ messages: [], running: false })
    const { result } = renderHook(() => useAgentSession())
    await result.current.openSession(ARGS)
    expect(deleted).toContain('chat-new')
    expect(releaseCalls()).toHaveLength(1)
  })

  it('keeps a session that may be running, rather than destroying it', async () => {
    api.sendChat.mockRejectedValue(new Error('connection reset'))
    api.chatSlotDetail.mockResolvedValue({ messages: [{ role: 'user' }], running: true })
    const { result } = renderHook(() => useAgentSession())
    await result.current.openSession(ARGS)
    expect(deleted).not.toContain('chat-new')
    expect(releaseCalls()).toHaveLength(0)
  })

  it('treats an unanswerable probe as running, so work is never destroyed', async () => {
    api.sendChat.mockRejectedValue(new Error('connection reset'))
    api.chatSlotDetail.mockRejectedValue(new Error('gateway unreachable'))
    const { result } = renderHook(() => useAgentSession())
    await result.current.openSession(ARGS)
    expect(deleted).not.toContain('chat-new')
    expect(releaseCalls()).toHaveLength(0)
  })

  it('releases nothing on the happy path', async () => {
    api.sendChat.mockResolvedValue({ ok: true, status: 200 } as Response)
    const { result } = renderHook(() => useAgentSession())
    await result.current.openSession(ARGS)
    expect(deleted).not.toContain('chat-new')
    expect(releaseCalls()).toHaveLength(0)
    expect(switched).toContain('chat-new')
  })
})
