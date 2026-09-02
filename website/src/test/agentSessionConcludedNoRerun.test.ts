/**
 * A CONCLUDED investigation whose chat session was closed must not silently
 * start the work over.
 *
 * Closing the session is how a user marks an investigation done, and the record
 * says so: `issue_radar_record_investigation` defaults `status` to `resolved`, so
 * a record still reading `investigating` is one that never wrote a verdict.
 *
 * Before this, `openSession`'s fallback keyed only on a dead `slot_key`. That was
 * right for an investigation INTERRUPTED mid-flight -- the previous behaviour
 * there was a permanent dead end -- but on a finished one it re-seeded a whole
 * fresh investigation: re-doing completed work, spending an agent run, and
 * overwriting the verdict already on the record, all from a button that reads
 * "Resume".
 *
 * Both directions are pinned, because the wrong one is the expensive one:
 * `investigating` must still fall through to a replacement, and a concluded
 * record must decline until a second explicit click passes `force`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'

const { dispatch, apiMock, saveInvestigation, getInvestigation } = vi.hoisted(() => ({
  dispatch: vi.fn(),
  apiMock: {
    chatFolders: vi.fn(),
    createChatFolder: vi.fn(),
    sendChat: vi.fn(),
    chatSlotDetail: vi.fn(),
  },
  saveInvestigation: vi.fn(),
  getInvestigation: vi.fn(),
}))

vi.mock('../store', () => ({ useAppDispatch: () => dispatch }))
vi.mock('../store/chatSlice', () => ({
  createSlot: (arg: unknown) => ({ type: 'createSlot', arg }),
  switchSlot: (arg: unknown) => ({ type: 'switchSlot', arg }),
  deleteSlot: (arg: unknown) => ({ type: 'deleteSlot', arg }),
}))
vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn() }))
vi.mock('../api/client', () => ({ api: apiMock }))
vi.mock('../apps/issue-radar/api', () => ({ issueRadarApi: { saveInvestigation, getInvestigation } }))

import { useAgentSession, itemKey } from '../apps/issue-radar/lib/agentSession'

/** What `.unwrap()` throws for a slot the gateway no longer has: Redux Toolkit's
 *  serialized error, a plain object rather than an Error instance. */
const slotGone = { name: 'ApiError', message: 'not found', stack: 'ApiError: not found' }

/** The closed session's slot 404s on probe; creating a replacement succeeds. */
function harness() {
  dispatch.mockImplementation((action: { type: string; arg?: unknown }) => ({
    unwrap: () => {
      if (action.type === 'createSlot') return Promise.resolve({ key: 'slot-new' })
      return Promise.resolve(undefined)
    },
  }))
  // The closed session's slot 404s when probed; any other slot reads fine.
  apiMock.chatSlotDetail.mockImplementation((key: string) =>
    key === SLOT_KEY ? Promise.reject(slotGone) : Promise.resolve({ messages: [] }))
  apiMock.chatFolders.mockResolvedValue([{ id: 'f1', name: 'Issue Radar - demo-repo' }])
  apiMock.sendChat.mockResolvedValue({ status: 200, ok: true } as unknown as Response)
  saveInvestigation.mockResolvedValue({ investigation: { slot_key: 'slot-new' } })
  // Default: the server agrees with the record the case passed in, so the re-read
  // is a no-op and each case still exercises the status it names.
  getInvestigation.mockImplementation(() => Promise.resolve({ investigation: null }))
}

const REPO = { host: 'github.com', owner: 'acme', repo: 'demo-repo' } as never
/** The closed session's slot -- the one that 404s. */
const SLOT_KEY = 'slot-closed'

const open = async (status: string, force = false) => {
  const { result } = renderHook(() => useAgentSession())
  const opened = await result.current.openSession({
    repoRef: REPO,
    number: 6014,
    title: '#6014 · concluded',
    prompt: 'seed',
    existing: { slot_key: SLOT_KEY, status } as never,
    force,
  })
  return { opened, result }
}

const createdSlot = () =>
  dispatch.mock.calls.some((c) => (c[0] as { type: string }).type === 'createSlot')
const seeded = () => apiMock.sendChat.mock.calls.length > 0

describe('Issue Radar - a concluded investigation is not silently re-run', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    harness()
  })

  it('declines a resolved record whose session was closed', async () => {
    const { opened, result } = await open('resolved')
    expect(createdSlot()).toBe(false)
    expect(seeded()).toBe(false)
    // Not null: the decline hands back the record it just read, so the caller can
    // replace the stale copy that sent it here. `concludedFor` is the signal.
    expect(opened).not.toBeNull()
    // Declined, NOT failed: there is nothing to retry, so this must not surface
    // as the error state -- which reads "couldn't start" and implies a fault.
    // The DECLINED ITEM's identity, not a flag: the detail pane is reused across
    // items, so a bare boolean would follow the user to the next one.
    await waitFor(() => expect(result.current.concludedFor).toBe(itemKey(REPO, 6014)))
    expect(result.current.error).toBeNull()
  })

  it('also declines a status it has never seen, rather than assuming it may re-run', async () => {
    // Fails CLOSED on purpose: `archived` is today's third status, and a fourth
    // added later must not inherit a silent full re-investigation.
    for (const status of ['archived', 'something-new']) {
      vi.resetAllMocks()
      harness()
      const { opened } = await open(status)
      expect(createdSlot()).toBe(false)
      expect(opened).not.toBeNull()  // the record, not a decline signal; see concludedFor
    }
  })

  it('starts over when the user asks a second time', async () => {
    const { opened } = await open('resolved', true)
    expect(createdSlot()).toBe(true)
    expect(seeded()).toBe(true)
    expect(opened).toEqual({ slot_key: 'slot-new' })
  })

  /**
   * A Start over must NOT destroy the stored verdict at click time.
   *
   * The record is the only copy, so clearing it when the replacement session
   * opens loses the verdict permanently if that run is then abandoned or fails --
   * and it costs most on the path this change documents as unprotected, where a
   * user who did not read the relabel clicks again. The prior verdict therefore
   * survives the session swap.
   *
   * This leaves a known defect rather than hiding one: because the record merges
   * per key, the replacement's own recording can inherit a key the new verdict
   * omits. That has to be fixed where the transition is atomic (store-side, on
   * the first record of a new run), not here -- filed separately.
   */
  it('does not destroy the stored verdict when it starts a replacement run', async () => {
    await open('resolved', true)
    expect(createdSlot()).toBe(true)
    const patch = saveInvestigation.mock.calls.at(-1)?.[2]
    expect(patch).toMatchObject({ status: 'investigating' })
    // The absence is the assertion: no `findings` key means the stored verdict is
    // left alone, since every field merges last-writer-wins.
    expect(patch).not.toHaveProperty('findings')
  })

  it('still replaces an INTERRUPTED investigation without asking', async () => {
    // The case the dead-end fix exists for: no verdict was ever recorded, so
    // there is no finished work to protect.
    const { opened, result } = await open('investigating')
    expect(createdSlot()).toBe(true)
    expect(seeded()).toBe(true)
    expect(opened).toEqual({ slot_key: 'slot-new' })
    expect(result.current.concludedFor).toBeNull()
  })

  /**
   * The guard must read the CURRENT record, not the one the client cached.
   *
   * `InvestigateButton` reads the record cache-first with a 30s `staleTime`, and
   * the stale window is exactly the dangerous one: the agent records its verdict
   * through the MCP tool, the session closes, and the user clicks Resume without
   * the client necessarily having refetched. Trusting the cached `investigating`
   * there would re-seed a full investigation over finished work -- this PR's own
   * defect, arriving by another route.
   */
  it('declines on a verdict the cached record has not caught up with', async () => {
    // What the client holds: written before the agent recorded anything.
    // What the server holds: the verdict, plus the closed session.
    getInvestigation.mockResolvedValue({ investigation: { slot_key: SLOT_KEY, status: 'resolved' } })
    const { opened, result } = await open('investigating')
    expect(createdSlot()).toBe(false)
    expect(seeded()).toBe(false)
    // The record handed back is the SERVER's, carrying the verdict the client had
    // not caught up with -- which is what repairs the stale pill.
    expect(opened).toMatchObject({ status: 'resolved' })
    await waitFor(() => expect(result.current.concludedFor).toBe(itemKey(REPO, 6014)))
  })

  /**
   * And a re-read that fails must not be guessed past in either direction.
   *
   * Assuming `investigating` spends an agent run and overwrites a verdict;
   * assuming concluded shows a finished-work message that may be false. Both are
   * unrecoverable from the user's side, while surfacing the failure is retryable
   * -- the same rule the slot probe follows for a transient error.
   */
  it('surfaces a failed re-read instead of guessing the status', async () => {
    getInvestigation.mockRejectedValue(new Error('HTTP 503 upstream'))
    const { opened, result } = await open('investigating')
    expect(createdSlot()).toBe(false)
    expect(seeded()).toBe(false)
    expect(opened).toBeNull()
    await waitFor(() => expect(result.current.error).toBeTruthy())
    // Not the declined state: nothing was established about the work.
    expect(result.current.concludedFor).toBeNull()
  })

  /**
   * A record that has moved on to a LIVE replacement must be reattached to.
   *
   * Two tabs, or one stale tab: the investigation was started over elsewhere, so
   * the record already points at a running session. This client only knows the
   * old key, and the probe on that key says nothing about the new one. Reading
   * "gone" from the old probe and falling through would open a THIRD session and
   * overwrite the live one's link, orphaning a session that is running -- the
   * failure mode #6178's comment warns about, reached from the other side.
   */
  it('reattaches to a replacement session the record already names', async () => {
    // Started over elsewhere: new live slot, status back to investigating.
    getInvestigation.mockResolvedValue({
      investigation: { slot_key: 'slot-replacement', status: 'investigating' },
    })
    const { opened } = await open('investigating')
    // Reattached, not re-created: no third session, no re-seed.
    expect(createdSlot()).toBe(false)
    expect(seeded()).toBe(false)
    expect(dispatch.mock.calls.map((c) => c[0])).toEqual([
      { type: 'switchSlot', arg: 'slot-replacement' },
    ])
    expect(opened).toEqual({ slot_key: 'slot-new' })
  })

  /**
   * A decline must not touch the chat store at all.
   *
   * This is the invariant that made three earlier bugs impossible rather than
   * fixed. `switchSlot.pending` mutates a lot of state synchronously -- it
   * assigns `activeSlot`, caches the outgoing slot's activity, writes its message
   * page, and pushes it onto the MRU `slotHistory` -- and its rejected reducer
   * unwinds almost none of that. Three successive attempts to unwind it from the
   * hook each produced a new defect: a key left pointing at a deleted session, a
   * restore that raced the user switching chats, and a deleted key left in the
   * Alt+` history.
   *
   * Deciding the decline from a probe instead means there is nothing to unwind,
   * so this asserts the absence rather than the correctness of a cleanup.
   */
  it('dispatches nothing at all when it declines', async () => {
    const { opened } = await open('resolved')
    expect(opened).not.toBeNull()
    // The probe answered the question; no action was needed to ask it.
    expect(apiMock.chatSlotDetail).toHaveBeenCalledWith(SLOT_KEY, 1)
    expect(dispatch).not.toHaveBeenCalled()
  })
})
