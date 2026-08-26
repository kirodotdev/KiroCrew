/**
 * A seed prompt the server REFUSED must not leave a recorded, empty session.
 *
 * Two app seeders guard this -- Issue Radar and Auto Improvement -- and both
 * used to guard it with a status-only check (`!(seeded as Response).ok`).
 * `/api/chat` also declines inside a 200 by answering `{ok:false}`, so that
 * check passed a refusal as a success: the slot survived, the record was
 * written, and the user was navigated to exactly the empty session the guard
 * exists to prevent. Both now read the receipt through the shared
 * `readSendReceipt`; the Issue Radar seeder is exercised here, and the Auto
 * Improvement one is the same three lines against its own record store.
 *
 * The other half matters as much: an UNREADABLE 2xx receipt must NOT take the
 * failure path. The request was accepted, so the seed may be running, and
 * deleting the slot there would cancel real work over a mangled reply.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'

const { dispatch, apiMock, saveInvestigation } = vi.hoisted(() => ({
  dispatch: vi.fn(),
  apiMock: {
    chatFolders: vi.fn(),
    createChatFolder: vi.fn(),
    renameSlot: vi.fn(),
    sendChat: vi.fn(),
  },
  saveInvestigation: vi.fn(),
}))

vi.mock('../store', () => ({ useAppDispatch: () => dispatch }))
vi.mock('../store/chatSlice', () => ({
  createSlot: (arg: unknown) => ({ type: 'createSlot', arg }),
  switchSlot: (arg: unknown) => ({ type: 'switchSlot', arg }),
  deleteSlot: (arg: unknown) => ({ type: 'deleteSlot', arg }),
}))
vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn() }))
vi.mock('../api/client', () => ({ api: apiMock }))
vi.mock('../apps/issue-radar/api', () => ({ issueRadarApi: { saveInvestigation } }))

import { useAgentSession } from '../apps/issue-radar/lib/agentSession'

/** Did the SUT ask for the freshly created slot to be deleted? */
const deletedSlot = () =>
  dispatch.mock.calls.some((c) => (c[0] as { type: string }).type === 'deleteSlot')

/** Open a session and hand back the result plus the LIVE hook handle:
 *  `openSession` swallows the throw, returns null, and reports through `error`
 *  — which lands in a later render, so it is read via `waitFor`, never off the
 *  snapshot taken before the await. */
async function open() {
  const { result } = renderHook(() => useAgentSession())
  const opened = await result.current.openSession({
    repoRef: { host: 'github.com', owner: 'acme', repo: 'demo-repo' } as never,
    number: 4237,
    title: '#4237 · seed refused',
    prompt: 'seed',
    existing: null,
  })
  return { opened, result }
}

describe('Issue Radar seed — the receipt decides, not the status alone', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    dispatch.mockImplementation((action: { type: string }) => ({
      unwrap: () =>
        action.type === 'createSlot'
          ? Promise.resolve({ key: 'slot-1' })
          : Promise.resolve(undefined),
    }))
    apiMock.chatFolders.mockResolvedValue([{ id: 'repo-1', name: 'Issue Radar - demo-repo' }])
    saveInvestigation.mockResolvedValue({ investigation: { slot_key: 'slot-1' } })
  })

  it('tears down the slot when a 200 answers {ok:false}', async () => {
    // The case a status-only check missed entirely.
    apiMock.sendChat.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ ok: false, error: 'slot is stopping' }),
    })

    const { opened, result } = await open()
    expect(opened).toBeNull()
    // The server's own reason survives — "HTTP 200" would be actively misleading
    // for a refusal that arrived inside a success status.
    await waitFor(() => expect(result.current.error?.message).toMatch(/slot is stopping/))
    expect(deletedSlot()).toBe(true)
    // ...and nothing is recorded, so no row points at a session that never ran.
    expect(saveInvestigation).not.toHaveBeenCalled()
  })

  it('still tears down on a plain non-2xx', async () => {
    // The case it did catch, kept green.
    apiMock.sendChat.mockResolvedValue({
      ok: false,
      status: 503,
      json: () => Promise.reject(new Error('not json')),
    })

    const { opened, result } = await open()
    expect(opened).toBeNull()
    await waitFor(() => expect(result.current.error?.message).toMatch(/503/))
    expect(deletedSlot()).toBe(true)
    expect(saveInvestigation).not.toHaveBeenCalled()
  })

  it('does NOT tear down on an unreadable 2xx — the seed may be running', async () => {
    // Deleting here would cancel real work over a mangled reply.
    apiMock.sendChat.mockResolvedValue({
      ok: true,
      json: () => Promise.reject(new Error('unexpected end of JSON input')),
    })

    const { result } = await open()
    expect(result.current.error).toBeNull()
    expect(deletedSlot()).toBe(false)
    expect(saveInvestigation).toHaveBeenCalled()
  })

  it('records normally on an accepted seed', async () => {
    apiMock.sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) })

    const { result } = await open()
    expect(result.current.error).toBeNull()
    expect(deletedSlot()).toBe(false)
    expect(saveInvestigation).toHaveBeenCalled()
  })
})
