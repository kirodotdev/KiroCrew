/**
 * A seed prompt that provably never ran must not leave a recorded, empty
 * session -- and a seed that MAY be running must never be torn down.
 *
 * Two app seeders guard this -- Issue Radar and Auto Improvement. Both used to
 * read the raw `api.sendChat` response through `readSendReceipt` themselves;
 * both now call the chat-core transport `sendTurn` and branch on its receipt
 * status. This file pins that mapping for BOTH seeders, so the contract cannot
 * drift between them again:
 *
 *   - `refused`         -> slot deleted, nothing recorded, the server's own
 *                          reason surfaces (a refusal inside a 200 is the case a
 *                          status-only check used to miss).
 *   - `unknown`         -> NOT deleted, recorded. Accepted, receipt unreadable:
 *                          deleting would cancel real work over a mangled reply.
 *   - `transport-error` / `response-late` -> no receipt, indeterminate (a reset
 *                          after the server took the POST rejects the fetch like
 *                          a request that never left; a stalled POST may still
 *                          land), so the seeder ASKS the slot: an empty slot
 *                          after bounded re-reads means the seed never landed ->
 *                          torn down + reported like a refusal; a message, or a
 *                          probe the server cannot answer, means it is or may be
 *                          running -> recorded and opened, never cancelled and
 *                          never left orphaned. The policy lives once, in
 *                          `utils/seedReceipt.ts`, and both seeders call it.
 *   - `dispatched` / `queued` -> recorded normally.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import type { SendReceipt } from '../chat-core/transport/sendTurn'
import { i18nT } from '../i18n/t'

const { dispatch, apiMock, sendTurn, saveInvestigation, getInvestigation } = vi.hoisted(() => ({
  dispatch: vi.fn(),
  apiMock: {
    chatFolders: vi.fn(),
    createChatFolder: vi.fn(),
    renameSlot: vi.fn(),
    chatSlotDetail: vi.fn(),
  },
  sendTurn: vi.fn(),
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
vi.mock('../chat-core/transport/sendTurn', () => ({ sendTurn }))
vi.mock('../apps/issue-radar/api', () => ({ issueRadarApi: { saveInvestigation, getInvestigation } }))

import { useAgentSession as useIssueRadarSession } from '../apps/issue-radar/lib/agentSession'
import { useAgentSession as useAutoImproveSession } from '../apps/auto-improvement/lib/agentSession'

const receipt = (status: SendReceipt['status'], reason?: string): SendReceipt =>
  ({ status, body: {}, ...(reason ? { reason } : {}) })

/** Did the SUT ask for the freshly created slot to be deleted? */
const deletedSlot = () =>
  dispatch.mock.calls.some((c) => (c[0] as { type: string }).type === 'deleteSlot')

beforeEach(() => {
  vi.resetAllMocks()
  dispatch.mockImplementation((action: { type: string }) => ({
    unwrap: () =>
      action.type === 'createSlot'
        ? Promise.resolve({ key: 'slot-1' })
        : Promise.resolve(undefined),
  }))
  apiMock.chatFolders.mockResolvedValue([
    { id: 'repo-1', name: 'Issue Radar - demo-repo' },
    { id: 'repo-2', name: 'Auto-Improve - acme/demo-repo' },
  ])
})

describe('Issue Radar seed — the sendTurn receipt decides', () => {
  /** Open a session and hand back the result plus the LIVE hook handle:
   *  `openSession` swallows the throw, returns null, and reports through `error`
   *  — which lands in a later render, so it is read via `waitFor`, never off the
   *  snapshot taken before the await. */
  async function open() {
    const { result } = renderHook(() => useIssueRadarSession())
    const opened = await result.current.openSession({
      repoRef: { host: 'github.com', owner: 'acme', repo: 'demo-repo' } as never,
      number: 4237,
      title: '#4237 · seed refused',
      prompt: 'seed',
      existing: null,
    })
    return { opened, result }
  }

  beforeEach(() => {
    getInvestigation.mockResolvedValue({ investigation: null })
    saveInvestigation.mockResolvedValue({ investigation: { slot_key: 'slot-1' } })
  })

  it('sends the seed through the transport, addressed to the new slot', async () => {
    sendTurn.mockResolvedValue(receipt('dispatched'))
    await open()
    expect(sendTurn).toHaveBeenCalledWith({ message: 'seed', slot: 'slot-1' })
  })

  it('tears down the slot on `refused` and keeps the server\'s reason', async () => {
    // The refusal-inside-a-200 case a status-only check missed entirely.
    sendTurn.mockResolvedValue(receipt('refused', 'slot is stopping'))

    const { opened, result } = await open()
    expect(opened).toBeNull()
    await waitFor(() => expect(result.current.error?.message).toBe(
      `${i18nT('pages.chatPage.could_not_start_a_new_session')} (slot is stopping)`,
    ))
    expect(deletedSlot()).toBe(true)
    // ...and nothing is recorded, so no row points at a session that never ran.
    expect(saveInvestigation).not.toHaveBeenCalled()
  })

  it('still tears down on a reasonless refusal (a plain non-2xx), in human words', async () => {
    sendTurn.mockResolvedValue(receipt('refused'))

    const { opened, result } = await open()
    expect(opened).toBeNull()
    // No raw status enum and no English wrapper around a localized fragment:
    // the whole message is the core's own "could not start" copy.
    await waitFor(() => expect(result.current.error?.message).toBe(String(i18nT('pages.chatPage.could_not_start_a_new_session'))))
    expect(deletedSlot()).toBe(true)
    expect(saveInvestigation).not.toHaveBeenCalled()
  })

  describe.each(['transport-error', 'response-late'] as const)('`%s` asks the slot instead of guessing', (status) => {
    beforeEach(() => sendTurn.mockResolvedValue(receipt(status)))

    it('tears down and reports when the slot stays empty — the seed never landed', async () => {
      // Bounded re-reads (a late arrival gets a second) then the verdict; the
      // polling itself is pinned in seedReceipt.test.ts.
      apiMock.chatSlotDetail.mockResolvedValue({ messages: [], total: 0 })

      const { opened, result } = await open()
      expect(opened).toBeNull()
      await waitFor(() => expect(result.current.error?.message).toBe(String(i18nT('pages.chatPage.could_not_start_a_new_session'))))
      expect(apiMock.chatSlotDetail).toHaveBeenCalledWith('slot-1', 1)
      expect(deletedSlot()).toBe(true)
      expect(saveInvestigation).not.toHaveBeenCalled()
    }, 10_000)

    it('records and opens when the slot already holds the seed — it is running', async () => {
      apiMock.chatSlotDetail.mockResolvedValue({ messages: [{ role: 'user', content: 'seed' }], total: 1 })

      const { result } = await open()
      expect(result.current.error).toBeNull()
      expect(deletedSlot()).toBe(false)
      expect(saveInvestigation).toHaveBeenCalled()
    })

    it('records and opens when the probe itself fails — never cancel or orphan what may be running', async () => {
      apiMock.chatSlotDetail.mockRejectedValue(new Error('HTTP 503'))

      const { result } = await open()
      expect(result.current.error).toBeNull()
      expect(deletedSlot()).toBe(false)
      expect(saveInvestigation).toHaveBeenCalled()
    })

    it('reports at once when the slot is already gone — nothing can be running in it', async () => {
      apiMock.chatSlotDetail.mockRejectedValue({ status: 404, message: 'not found' })

      const { opened, result } = await open()
      expect(opened).toBeNull()
      await waitFor(() => expect(result.current.error).toBeTruthy())
      expect(apiMock.chatSlotDetail).toHaveBeenCalledTimes(1)
      expect(saveInvestigation).not.toHaveBeenCalled()
    })
  })

  it.each([
    ['unknown', 'the seed may be running'],
  ] as const)('does NOT tear down on `%s` — %s', async (status) => {
    sendTurn.mockResolvedValue(receipt(status))

    const { result } = await open()
    expect(result.current.error).toBeNull()
    expect(deletedSlot()).toBe(false)
    expect(saveInvestigation).toHaveBeenCalled()
  })

  it.each(['dispatched', 'queued'] as const)('records normally on `%s`', async (status) => {
    sendTurn.mockResolvedValue(receipt(status))

    const { result } = await open()
    expect(result.current.error).toBeNull()
    expect(deletedSlot()).toBe(false)
    expect(saveInvestigation).toHaveBeenCalled()
  })
})

describe('Auto Improvement seed — the same receipt contract', () => {
  const saved = vi.fn()

  /** The record store is this app's own backend, reached with bare `fetch`:
   *  GET answers "no record yet", PUT is captured so the test can tell whether a
   *  session was recorded. */
  beforeEach(() => {
    saved.mockReset()
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'PUT') {
        saved(init.body)
        return { ok: true, json: async () => ({ session: { slot_key: 'slot-1', status: 'open' } }) }
      }
      return { ok: true, json: async () => ({ session: null }) }
    }))
  })

  async function open() {
    const { result } = renderHook(() => useAutoImproveSession())
    const opened = await result.current.openSession({
      kind: 'pr',
      id: 42,
      repo: 'acme/demo-repo',
      title: 'PR #42 · seed',
      prompt: 'seed',
    })
    return { opened, result }
  }

  it('sends the seed through the transport, addressed to the new slot', async () => {
    sendTurn.mockResolvedValue(receipt('dispatched'))
    await open()
    expect(sendTurn).toHaveBeenCalledWith({ message: 'seed', slot: 'slot-1' })
  })

  it.each([
    ['with the server\'s reason', 'slot agent mismatch', /slot agent mismatch/],
    ['with the core\'s own copy when the body carried none', undefined, String(i18nT('pages.chatPage.could_not_start_a_new_session'))],
  ] as const)('tears down and records nothing on `refused` %s', async (_label, reason, expected) => {
    sendTurn.mockResolvedValue(receipt('refused', reason))

    const { opened, result } = await open()
    expect(opened).toBeNull()
    await waitFor(() => expect(result.current.error?.message).toMatch(expected))
    expect(deletedSlot()).toBe(true)
    expect(saved).not.toHaveBeenCalled()
  })

  it.each([
    ['transport-error', 'empty', { messages: [], total: 0 }, true],
    ['transport-error', 'seeded', { messages: [{ role: 'user' }], total: 1 }, false],
    ['response-late', 'empty', { messages: [], total: 0 }, true],
    ['response-late', 'seeded', { messages: [{ role: 'user' }], total: 1 }, false],
  ] as const)('on `%s` asks the slot: %s -> tears down %s', async (status, _label, detail, tornDown) => {
    sendTurn.mockResolvedValue(receipt(status))
    apiMock.chatSlotDetail.mockResolvedValue(detail)

    const { opened, result } = await open()
    if (tornDown) {
      expect(opened).toBeNull()
      await waitFor(() => expect(result.current.error).toBeTruthy())
      expect(saved).not.toHaveBeenCalled()
    } else {
      expect(opened).not.toBeNull()
      expect(result.current.error).toBeNull()
      expect(saved).toHaveBeenCalled()
    }
    expect(deletedSlot()).toBe(tornDown)
  }, 10_000)

  it.each(['unknown', 'dispatched', 'queued'] as const)(
    'keeps the slot and records the session on `%s`',
    async (status) => {
      sendTurn.mockResolvedValue(receipt(status))

      const { opened, result } = await open()
      expect(opened).not.toBeNull()
      expect(result.current.error).toBeNull()
      expect(deletedSlot()).toBe(false)
      expect(saved).toHaveBeenCalled()
    },
  )
})
