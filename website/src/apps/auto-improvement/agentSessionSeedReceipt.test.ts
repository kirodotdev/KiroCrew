/**
 * A seed prompt that provably never ran must not leave a recorded, empty
 * session -- and a seed that MAY be running must never be torn down.
 *
 * Auto Improvement's seeder calls the chat-core transport `sendTurn` and branches
 * on its receipt status, the same contract Issue Radar's seeder pins:
 *
 *   - `refused`         -> slot deleted, nothing recorded, the server's own
 *                          reason surfaces.
 *   - `unknown`         -> NOT deleted, recorded.
 *   - `transport-error` / `response-late` -> no receipt, indeterminate, so the
 *                          seeder ASKS the slot: an empty slot means the seed
 *                          never landed -> torn down + reported like a refusal; a
 *                          message means it is running -> recorded and opened.
 *                          The policy lives once, in `utils/seedReceipt.ts`.
 *   - `dispatched` / `queued` -> recorded normally.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { SendReceipt } from '../../chat-core/transport/sendTurn'
import { i18nT } from '../../i18n/t'

const { dispatch, apiMock, sendTurn } = vi.hoisted(() => ({
  dispatch: vi.fn(),
  apiMock: {
    chatFolders: vi.fn(),
    createChatFolder: vi.fn(),
    renameSlot: vi.fn(),
    chatSlotDetail: vi.fn(),
  },
  sendTurn: vi.fn(),
}))

vi.mock('../../store', () => ({ useAppDispatch: () => dispatch }))
vi.mock('../../store/chatSlice', () => ({
  createSlot: (arg: unknown) => ({ type: 'createSlot', arg }),
  switchSlot: (arg: unknown) => ({ type: 'switchSlot', arg }),
  deleteSlot: (arg: unknown) => ({ type: 'deleteSlot', arg }),
  requestSlotFolderOpen: (arg: unknown) => ({ type: 'requestSlotFolderOpen', arg }),
}))
vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn() }))
vi.mock('../../api/client', () => ({ api: apiMock }))
vi.mock('../../chat-core/transport/sendTurn', () => ({ sendTurn }))

import { useAgentSession as useAutoImproveSession } from './lib/agentSession'

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
    { id: 'repo-2', name: 'Auto-Improve - acme/demo-repo' },
  ])
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
    let opened: unknown = null
    await act(async () => {
      opened = await result.current.openSession({
        kind: 'pr',
        id: 42,
        repo: 'acme/demo-repo',
        title: 'PR #42 · seed',
        prompt: 'seed',
      })
    })
    return { opened: opened as Awaited<ReturnType<typeof result.current.openSession>>, result }
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
