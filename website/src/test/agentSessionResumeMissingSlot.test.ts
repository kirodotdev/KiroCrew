/**
 * Resuming an investigation whose chat session the user has since CLOSED must
 * open a fresh session, not fail.
 *
 * The resume branch already has that fallback: it swallows a "the slot is gone"
 * rejection and falls through to the create path. What it could not do was
 * RECOGNISE one. The rejection now arrives from the pre-flight slot probe rather
 * than from `dispatch(switchSlot(k)).unwrap()` -- the resume asks whether the slot
 * exists BEFORE touching the chat store -- but the shape it must classify is
 * unchanged, and RTK's serialized form is still covered below because the same
 * helper reads both surfaces,
 * and Redux Toolkit does not rethrow the original error — `createAsyncThunk`
 * stores `miniSerializeError(err)` on the rejected action and `unwrap()` throws
 * THAT: a plain `{ name, message, stack }` object, not an `Error` instance. The
 * old test for `isMissingSlot` never saw that shape because every harness here
 * fakes `unwrap`, so a hand-rolled `new Error('404 …')` sailed through the
 * `e instanceof Error` branch that real RTK never takes.
 *
 * So the message was read as `String(plainObject)` === '[object Object]', which
 * matches neither `404` nor `not found`, the rejection was re-thrown as a
 * genuine failure, and the button showed "couldn't start" — permanently, since
 * the record's dead `slot_key` is what drives the Resume label in the first
 * place.
 *
 * Both halves are pinned: the serialized 404 must fall through to a fresh
 * session, and a serialized 500 must NOT (a transient gateway failure that
 * orphans the live session and overwrites its record is the worse bug the
 * narrow check was added to prevent).
 *
 * The rule shipped in TWO copies — Issue Radar's fallback and Auto Improvement's
 * identical one — and both carried the same bug, so both fallbacks were
 * unreachable. It now has one home (`utils/thunkError`), pinned directly in the
 * second describe block so an edit to the rule cannot pass by satisfying only
 * the Issue Radar flow.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook } from '@testing-library/react'

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

import { useAgentSession } from '../apps/issue-radar/lib/agentSession'
import { errMessage, isMissingSlotError } from '../utils/thunkError'

/** Exactly what `unwrap()` throws: RTK's `miniSerializeError` copies only the
 *  string-valued `name`/`message`/`stack`/`code`, so the class is gone and
 *  `ApiError.status` (a number) does not survive either. */
const serializedError = (message: string) => ({
  name: 'ApiError',
  message,
  stack: 'ApiError: ' + message,
})

/** Reject the PROBE for the ALREADY-EXISTING slot with *rejection*, while the
 *  create path (and the switch to the newly created slot) succeeds. */
function harness(rejection: unknown) {
  dispatch.mockImplementation((action: { type: string; arg?: unknown }) => ({
    unwrap: () => {
      if (action.type === 'createSlot') return Promise.resolve({ key: 'slot-new' })
      return Promise.resolve(undefined)
    },
  }))
  apiMock.chatSlotDetail.mockImplementation((key: string) =>
    key === 'slot-closed' ? Promise.reject(rejection) : Promise.resolve({ messages: [] }))
  getInvestigation.mockResolvedValue({ investigation: null })
  apiMock.chatFolders.mockResolvedValue([{ id: 'f1', name: 'Issue Radar - demo-repo' }])
  apiMock.sendChat.mockResolvedValue({ status: 200, ok: true } as unknown as Response)
  saveInvestigation.mockResolvedValue({ investigation: { slot_key: 'slot-new' } })
}

const open = async () => {
  const { result } = renderHook(() => useAgentSession())
  return result.current.openSession({
    repoRef: { host: 'github.com', owner: 'acme', repo: 'demo-repo' } as never,
    number: 6014,
    title: '#6014 · closed session',
    prompt: 'seed',
    existing: { slot_key: 'slot-closed' } as never,
  })
}

const createdSlot = () =>
  dispatch.mock.calls.some((c) => (c[0] as { type: string }).type === 'createSlot')

describe('Issue Radar resume — a serialized rejection still names a missing slot', () => {
  beforeEach(() => {
    vi.resetAllMocks()
  })

  it('falls through to a fresh session when the closed slot 404s', async () => {
    harness(serializedError('not found'))
    const record = await open()
    expect(createdSlot()).toBe(true)
    expect(record).toEqual({ slot_key: 'slot-new' })
  })

  it('also recognises the bare "HTTP 404" message form', async () => {
    harness(serializedError('HTTP 404'))
    await open()
    expect(createdSlot()).toBe(true)
  })

  it('does NOT replace the session when the failure is transient', async () => {
    harness(serializedError('HTTP 500'))
    const record = await open()
    expect(createdSlot()).toBe(false)
    expect(record).toBeNull()
  })

  it('still honours a real Error instance', async () => {
    harness(new Error('not found'))
    await open()
    expect(createdSlot()).toBe(true)
  })

  // `switchSlot` now rejects via `rejectWithValue`, and `unwrap()` throws that
  // payload VERBATIM — `{ status, message }`, numbers intact — so the flow no
  // longer depends on prose once a status is present. Both directions:

  it('falls through to a fresh session on a structured 404 payload', async () => {
    harness({ status: 404, message: 'HTTP 404: no such slot' })
    const record = await open()
    expect(createdSlot()).toBe(true)
    expect(record).toEqual({ slot_key: 'slot-new' })
  })

  it('does NOT replace the session on a non-404 whose prose says "not found"', async () => {
    // The regression #6199 fixes: a 500 quoting "not found" used to read as a
    // dead slot and overwrite a session that was still alive. The structured
    // status must outrank the message.
    harness({ status: 500, message: 'agent "foo" not found' })
    const record = await open()
    expect(createdSlot()).toBe(false)
    expect(record).toBeNull()
  })
})

/**
 * The rule itself, at the boundary both apps' fallbacks sit on.
 *
 * It lived twice — once in each app's `agentSession` — and both copies carried
 * the same `instanceof Error` bug, so Auto Improvement's resume of a closed
 * session failed permanently in exactly the way Issue Radar's did. Sharing the
 * definition is what stops the twins diverging again; these cases pin the rule
 * directly so a future edit to it cannot pass by only satisfying the Issue Radar
 * flow above.
 */
describe('isMissingSlotError — the shared thunk-boundary rule', () => {
  it('reads a serialized rejection, which is what unwrap() actually throws', () => {
    expect(isMissingSlotError(serializedError('not found'))).toBe(true)
    expect(isMissingSlotError(serializedError('HTTP 404'))).toBe(true)
    expect(errMessage(serializedError('not found'))).toBe('not found')
  })

  it('reads a real Error, unchanged from before', () => {
    expect(isMissingSlotError(new Error('HTTP 404'))).toBe(true)
    expect(isMissingSlotError(new Error('boom'))).toBe(false)
  })

  it('classifies on a structured status, message not consulted', () => {
    // Status alone decides: no "404"/"not found" hint in the prose is needed…
    expect(isMissingSlotError({ status: 404, message: 'gone' })).toBe(true)
    expect(isMissingSlotError({ status: 404, message: '' })).toBe(true)
    // …and the payload stays readable by every message-based caller.
    expect(errMessage({ status: 404, message: 'gone' })).toBe('gone')
  })

  it('a structured non-404 status VETOES the prose match (the #6199 regression)', () => {
    // Before the status survived the boundary, both of these misread as "the
    // slot is gone" and replaced a live session. The regex may only speak for
    // rejections that carry no status at all.
    expect(isMissingSlotError({ status: 500, message: 'agent "foo" not found' })).toBe(false)
    expect(isMissingSlotError({ status: 502, message: 'HTTP 404 while proxying upstream' })).toBe(false)
    // A raw ApiError-shaped object that never crossed a thunk behaves the same.
    expect(isMissingSlotError(Object.assign(new Error('model not found'), { status: 500 }))).toBe(false)
    expect(isMissingSlotError(Object.assign(new Error('nope'), { status: 404 }))).toBe(true)
  })

  it('ignores a non-numeric status field and falls back to prose', () => {
    // Only a NUMERIC status is the wire contract; a string 'status' from some
    // unrelated shape must not suppress the fallback.
    expect(isMissingSlotError({ status: 'rejected', message: 'not found' })).toBe(true)
    expect(isMissingSlotError({ status: '500', message: 'not found' })).toBe(true)
  })

  it('refuses anything that is not a missing slot, so a live session is safe', () => {
    expect(isMissingSlotError(serializedError('HTTP 500'))).toBe(false)
    expect(isMissingSlotError(serializedError('Failed to fetch'))).toBe(false)
    // A 404 embedded in a longer number must not match on a digit run.
    expect(isMissingSlotError(serializedError('HTTP 4041'))).toBe(false)
  })

  it('degrades rather than throwing on shapes with no message at all', () => {
    expect(isMissingSlotError(undefined)).toBe(false)
    expect(isMissingSlotError(null)).toBe(false)
    expect(isMissingSlotError({})).toBe(false)
    expect(errMessage(undefined)).toBe('')
    expect(errMessage('not found')).toBe('not found')
  })

  /**
   * A message-less rejection must read as EMPTY, not as `'[object Object]'`.
   *
   * Two consumers render `errMessage(e) || <localized fallback>`, so the answer
   * for "this rejection said nothing" has to be falsy or the fallback is dead
   * and the class tag is shown to the user instead. `String({})` is truthy, so
   * routing an object through it would have swapped a localized string for
   * `'[object Object]'` at the agents page's create-crew failure — which is
   * exactly what an earlier revision of this change did.
   */
  it('answers empty for an object with no usable message, never the class tag', () => {
    expect(errMessage({})).toBe('')
    expect(errMessage({ message: 5 })).toBe('')
    expect(errMessage({ name: 'ApiError' })).toBe('')
    expect(errMessage(null)).toBe('')
    for (const e of [{}, { message: 5 }, null, undefined]) {
      expect(errMessage(e)).toBeFalsy()
    }
  })

  it('reads a thrown primitive as its own message', () => {
    expect(errMessage('boom')).toBe('boom')
    expect(errMessage(404)).toBe('404')
    expect(isMissingSlotError('slot not found')).toBe(true)
  })
})
